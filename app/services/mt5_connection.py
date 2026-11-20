"""Centralized MetaTrader 5 connection lifecycle (Fase 3: estabilidad y reconexión).

Addresses audit findings H-05 (no automatic reconnection), H-09 (no ordered
shutdown) and H-13 (blocking MT5 calls executed directly inside coroutines).

Design constraints that drove this module:

- The ``MetaTrader5`` package is a thin wrapper over a native IPC client; it is
  not documented as safe for concurrent calls from multiple threads. Every call
  (``initialize``, ``shutdown``, ``terminal_info``, ``symbol_info``,
  ``symbol_info_tick``, ``copy_rates_from_pos``, ``copy_rates_range``, ...) is
  therefore funneled through a single dedicated worker thread, so two native
  calls can never execute at the same instant and ``initialize``/``shutdown``
  can never race an in-flight read.
- ``asyncio.wait_for`` (or cancelling the awaiting task) does NOT stop the
  native call running in the worker thread — Python cannot forcibly interrupt
  a blocked C extension call. A timeout here only stops *waiting* for the
  result; the thread stays occupied until the native call itself returns.
  Because there is only one worker thread, any further call would simply queue
  behind it. To avoid silently pretending the connection recovered, a timeout
  flips the manager into DEGRADED and refuses new calls until the stuck call
  actually finishes (observed from the background thread itself). Full
  recovery from a truly wedged native call (forced thread/process restart)
  requires the external watchdog planned for a later phase; this module does
  not claim to solve it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from functools import partial
from typing import Any, TypeVar

import MetaTrader5 as mt5

from app.core.config import Settings, settings
from app.core.logger import logger

T = TypeVar("T")

# MT5 error codes (see MetaTrader5.last_error()) that indicate a configuration
# problem requiring manual intervention (wrong credentials, unsupported call,
# invalid parameters) rather than a transient connectivity issue. Anything not
# in this set is treated as recoverable and retried with backoff — deliberately
# a short, explicit allowlist of *non-recoverable* codes instead of the other
# way around, so an unknown/new error code defaults to "keep retrying" rather
# than to "give up silently".
NON_RECOVERABLE_ERROR_CODES = frozenset({
    -2,  # RES_E_INVALID_PARAMS
    -5,  # RES_E_INVALID_VERSION
    -6,  # RES_E_AUTH_FAILED
    -7,  # RES_E_UNSUPPORTED
})


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


class MT5ConnectionError(RuntimeError):
    """Recoverable connection failure: safe to retry with backoff."""


class MT5ConfigurationError(MT5ConnectionError):
    """Non-recoverable failure (bad credentials/params): do not retry blindly."""


class MT5BusyError(RuntimeError):
    """A previous call is still occupying the single MT5 worker thread."""


class MT5ConnectionManager:
    """Owns the MT5 initialize/shutdown lifecycle and serializes every call.

    Only one instance should be used per process (see ``get_default_connection``)
    so that ``initialize``/``shutdown`` and every read genuinely share the same
    worker thread.
    """

    def __init__(
        self,
        mt5_module: Any = mt5,
        config: Settings = settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.mt5 = mt5_module
        self.config = config
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-io")
        self._state = ConnectionState.DISCONNECTED
        self._consecutive_failures = 0
        self._stuck = False  # a previous call exceeded its timeout and never returned
        self._connect_lock = asyncio.Lock()
        self._shutdown_done = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    def _set_state(self, new_state: ConnectionState) -> None:
        if new_state == self._state:
            return
        logger.info("mt5_connection | %s -> %s", self._state.value, new_state.value)
        self._state = new_state

    # -- serialized, timed execution of blocking MT5 calls -------------------

    async def run(self, func: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any) -> T:
        """Run a blocking MT5 call on the single dedicated worker thread.

        Raises ``MT5BusyError`` immediately (without submitting a new call) if
        a previous call is already known to be stuck past its timeout — queuing
        behind it would just wait forever on a single-worker executor.
        """
        if self._shutdown_done:
            raise MT5ConnectionError("Conexión apagada; no se ejecutan más llamadas MT5")
        if self._stuck:
            raise MT5BusyError("Operación MT5 anterior aún bloqueada; no se inician nuevas llamadas")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, partial(func, *args, **kwargs))
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._stuck = True
            self._set_state(ConnectionState.DEGRADED)
            logger.warning(
                "mt5_connection | timeout tras %.1fs esperando %s; "
                "el hilo nativo puede seguir bloqueado (degradado hasta liberarse)",
                timeout, getattr(func, "__name__", repr(func)),
            )

            def _mark_free(_: Any) -> None:
                self._stuck = False
                logger.info("mt5_connection | la llamada bloqueada finalmente retornó; hilo liberado")

            future.add_done_callback(lambda f: _mark_free(f))
            raise

    # -- connection lifecycle --------------------------------------------------

    def _last_error(self) -> tuple[int, str] | None:
        try:
            return self.mt5.last_error()
        except (AttributeError, RuntimeError, OSError):
            return None

    def _is_non_recoverable(self, error: tuple[int, str] | None) -> bool:
        return bool(error) and error[0] in NON_RECOVERABLE_ERROR_CODES

    async def ensure_connected(self) -> None:
        """Block (asynchronously, cancellable) until MT5 is usable.

        Retries ``initialize()`` with exponential backoff + jitter, bounded by
        ``MT5_RECONNECT_MAX_DELAY_SECONDS``. Resets the failure counter on
        success. Raises ``MT5ConfigurationError`` immediately — without
        entering the retry loop — for errors that look like a permanent
        configuration problem, so the bot never hammers a broken setup forever.
        """
        if self._state == ConnectionState.CONNECTED:
            return
        if self._state == ConnectionState.STOPPING:
            raise MT5ConnectionError("Apagado en curso; no se reconecta")
        async with self._connect_lock:
            if self._state == ConnectionState.CONNECTED:
                return
            delay = self.config.MT5_RECONNECT_INITIAL_DELAY_SECONDS
            while True:
                self._set_state(
                    ConnectionState.RECONNECTING if self._consecutive_failures else ConnectionState.CONNECTING
                )
                try:
                    initialized = await self.run(self.mt5.initialize, timeout=self.config.MT5_CONNECT_TIMEOUT_SECONDS)
                except MT5BusyError:
                    raise
                except TimeoutError:
                    initialized = False
                terminal_connected = False
                if initialized:
                    try:
                        terminal = await self.run(self.mt5.terminal_info, timeout=self.config.MT5_CONNECT_TIMEOUT_SECONDS)
                        terminal_connected = bool(terminal is not None and getattr(terminal, "connected", False))
                    except (MT5BusyError, TimeoutError):
                        terminal_connected = False
                if initialized and terminal_connected:
                    previous_failures = self._consecutive_failures
                    self._consecutive_failures = 0
                    self._set_state(ConnectionState.CONNECTED)
                    if previous_failures:
                        logger.info("mt5_connection | conexión restablecida tras %d intento(s) fallido(s)",
                                    previous_failures)
                    return
                error = self._last_error()
                if self._is_non_recoverable(error):
                    self._set_state(ConnectionState.DEGRADED)
                    raise MT5ConfigurationError(
                        f"Configuración de MT5 inválida (sin datos sensibles): código={error[0] if error else '?'}"
                    )
                self._consecutive_failures += 1
                reason = "terminal abierto pero broker desconectado" if initialized else "terminal no disponible"
                logger.warning(
                    "mt5_connection | intento de reconexión #%d falló (%s); próximo intento en %.1fs",
                    self._consecutive_failures, reason, delay,
                )
                jitter = self._rng.uniform(0, delay * 0.1)
                await self._sleep(delay + jitter)
                delay = min(delay * self.config.MT5_RECONNECT_BACKOFF_MULTIPLIER,
                            self.config.MT5_RECONNECT_MAX_DELAY_SECONDS)

    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    async def shutdown(self) -> None:
        """Release MT5 and the worker thread. Safe to call multiple times."""
        if self._shutdown_done:
            return
        self._set_state(ConnectionState.STOPPING)
        if not self._stuck:
            try:
                # Bypass self.run()'s shutdown guard: this IS the shutdown call.
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(self._executor, self.mt5.shutdown)
                await asyncio.wait_for(future, timeout=self.config.MT5_CONNECT_TIMEOUT_SECONDS)
            except (TimeoutError, RuntimeError, OSError, AttributeError) as exc:
                logger.warning("mt5_connection | shutdown() no pudo confirmarse limpiamente: %s", exc)
        else:
            logger.warning(
                "mt5_connection | no se invoca shutdown(): una llamada previa sigue bloqueada en el "
                "hilo dedicado (limitación conocida; requiere reinicio de proceso/watchdog externo)"
            )
        # wait=False: a wedged native call must never make process shutdown hang forever.
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._shutdown_done = True
        self._set_state(ConnectionState.DISCONNECTED)
        logger.info("mt5_connection | apagado ordenado completado")


_default_connection: MT5ConnectionManager | None = None


def get_default_connection() -> MT5ConnectionManager:
    """Process-wide singleton so every caller shares one worker thread/state."""
    global _default_connection
    if _default_connection is None:
        _default_connection = MT5ConnectionManager()
    return _default_connection
