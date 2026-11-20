"""Fase 3: MT5ConnectionManager — state machine, backoff, serialization, timeouts.

No test opens a real MT5 terminal: all exercise FakeMT5 doubles and injectable
sleep/clock functions, never a real prolonged wait (the single real-thread
waits used below are a few tens of milliseconds, to genuinely exercise
ThreadPoolExecutor timing behavior that a fake clock cannot substitute for).
"""

from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.mt5_connection import (
    ConnectionState,
    MT5BusyError,
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5ConnectionManager,
    get_default_connection,
)


class FakeMT5:
    def __init__(self, *, initialize_results=(True,), terminal_connected=(True,), error=(-10004, "fake")):
        self._initialize_results = list(initialize_results)
        self._terminal_connected = list(terminal_connected)
        self.error = error
        self.initialize_calls = 0
        self.terminal_info_calls = 0
        self.shutdown_calls = 0

    @staticmethod
    def _consume(sequence):
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    def initialize(self):
        self.initialize_calls += 1
        return self._consume(self._initialize_results)

    def terminal_info(self):
        self.terminal_info_calls += 1
        return SimpleNamespace(connected=self._consume(self._terminal_connected))

    def last_error(self):
        return self.error

    def shutdown(self):
        self.shutdown_calls += 1


def config(**updates) -> Settings:
    return Settings(_env_file=None, **updates)


def collecting_sleep(delays: list[float]):
    async def _sleep(seconds: float) -> None:
        delays.append(seconds)
    return _sleep


@pytest.mark.asyncio
async def test_ensure_connected_succeeds_on_first_attempt():
    mt5 = FakeMT5()
    delays: list[float] = []
    manager = MT5ConnectionManager(mt5, config(), sleep=collecting_sleep(delays))
    await manager.ensure_connected()
    assert manager.state == ConnectionState.CONNECTED
    assert delays == []
    assert mt5.initialize_calls == 1


@pytest.mark.asyncio
async def test_ensure_connected_is_a_noop_once_already_connected():
    mt5 = FakeMT5()
    manager = MT5ConnectionManager(mt5, config())
    await manager.ensure_connected()
    await manager.ensure_connected()
    assert mt5.initialize_calls == 1  # second call short-circuited, no re-initialize


@pytest.mark.asyncio
async def test_initial_connection_failure_retries_with_exponential_backoff_then_recovers():
    mt5 = FakeMT5(initialize_results=(False, False, True))
    delays: list[float] = []
    cfg = config(MT5_RECONNECT_INITIAL_DELAY_SECONDS=1, MT5_RECONNECT_BACKOFF_MULTIPLIER=2,
                 MT5_RECONNECT_MAX_DELAY_SECONDS=100)
    manager = MT5ConnectionManager(mt5, cfg, sleep=collecting_sleep(delays), rng=random.Random(0))
    await manager.ensure_connected()
    assert manager.state == ConnectionState.CONNECTED
    assert len(delays) == 2
    assert 1 <= delays[0] < 1.1  # initial delay + up to 10% jitter
    assert 2 <= delays[1] < 2.2  # doubled


@pytest.mark.asyncio
async def test_backoff_never_exceeds_the_configured_maximum_delay():
    mt5 = FakeMT5(initialize_results=(False, False, False, False, False, True))
    delays: list[float] = []
    cfg = config(MT5_RECONNECT_INITIAL_DELAY_SECONDS=5, MT5_RECONNECT_BACKOFF_MULTIPLIER=2,
                 MT5_RECONNECT_MAX_DELAY_SECONDS=12)
    manager = MT5ConnectionManager(mt5, cfg, sleep=collecting_sleep(delays), rng=random.Random(0))
    await manager.ensure_connected()
    assert len(delays) == 5
    assert all(delay <= 12 * 1.1 for delay in delays)
    assert delays[-1] >= 12  # capped value reached, not still climbing


@pytest.mark.asyncio
async def test_failure_counter_resets_after_a_successful_recovery():
    mt5 = FakeMT5(initialize_results=(False, True))
    delays: list[float] = []
    cfg = config(MT5_RECONNECT_INITIAL_DELAY_SECONDS=1, MT5_RECONNECT_BACKOFF_MULTIPLIER=3,
                 MT5_RECONNECT_MAX_DELAY_SECONDS=100)
    manager = MT5ConnectionManager(mt5, cfg, sleep=collecting_sleep(delays), rng=random.Random(0))
    await manager.ensure_connected()
    assert len(delays) == 1

    # Simulate a later, independent disconnection.
    manager._state = ConnectionState.DISCONNECTED
    mt5._initialize_results = [False, True]
    delays.clear()
    await manager.ensure_connected()
    assert len(delays) == 1
    assert 1 <= delays[0] < 1.1  # back to the initial delay, not continuing the previous backoff


@pytest.mark.asyncio
async def test_terminal_open_but_broker_disconnected_is_not_treated_as_connected():
    mt5 = FakeMT5(initialize_results=(True,), terminal_connected=(False, True))
    delays: list[float] = []
    manager = MT5ConnectionManager(mt5, config(MT5_RECONNECT_INITIAL_DELAY_SECONDS=1), sleep=collecting_sleep(delays))
    await manager.ensure_connected()
    assert manager.state == ConnectionState.CONNECTED
    assert mt5.terminal_info_calls == 2
    assert len(delays) == 1


@pytest.mark.asyncio
async def test_non_recoverable_configuration_error_raises_immediately_without_retrying():
    mt5 = FakeMT5(initialize_results=(False,), error=(-6, "auth failed"))
    delays: list[float] = []
    manager = MT5ConnectionManager(mt5, config(), sleep=collecting_sleep(delays))
    with pytest.raises(MT5ConfigurationError):
        await manager.ensure_connected()
    assert delays == []
    assert manager.state == ConnectionState.DEGRADED


@pytest.mark.asyncio
async def test_ensure_connected_can_be_cancelled_during_the_backoff_sleep():
    mt5 = FakeMT5(initialize_results=(False,))

    async def hanging_sleep(_seconds: float) -> None:
        await asyncio.Event().wait()  # never resolves on its own

    manager = MT5ConnectionManager(mt5, config(), sleep=hanging_sleep)
    task = asyncio.create_task(manager.ensure_connected())
    await asyncio.sleep(0)  # let it reach the backoff sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_timeout_degrades_connection_and_blocks_new_calls_until_the_stuck_call_returns():
    manager = MT5ConnectionManager(FakeMT5(), config())

    def slow() -> str:
        time.sleep(0.2)
        return "done"

    with pytest.raises(TimeoutError):
        await manager.run(slow, timeout=0.02)
    assert manager.state == ConnectionState.DEGRADED

    with pytest.raises(MT5BusyError):
        await manager.run(lambda: "new call")

    await asyncio.sleep(0.3)  # let the stuck native call actually finish

    assert await manager.run(lambda: "ok") == "ok"


@pytest.mark.asyncio
async def test_calls_are_serialized_and_never_overlap():
    manager = MT5ConnectionManager(FakeMT5(), config())
    events: list[tuple[str, str]] = []

    def make_call(name: str):
        def _call() -> None:
            events.append((name, "start"))
            time.sleep(0.05)
            events.append((name, "end"))
        return _call

    await asyncio.gather(manager.run(make_call("a")), manager.run(make_call("b")))
    assert events == [("a", "start"), ("a", "end"), ("b", "start"), ("b", "end")]


@pytest.mark.asyncio
async def test_event_loop_remains_responsive_while_a_blocking_call_runs():
    manager = MT5ConnectionManager(FakeMT5(), config())
    ticks: list[int] = []

    async def ticker() -> None:
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks.append(1)

    def blocking() -> None:
        time.sleep(0.1)

    await asyncio.gather(manager.run(blocking), ticker())
    assert len(ticks) == 5


@pytest.mark.asyncio
async def test_shutdown_invokes_mt5_shutdown_and_is_idempotent():
    mt5 = FakeMT5()
    manager = MT5ConnectionManager(mt5, config())
    await manager.ensure_connected()
    await manager.shutdown()
    assert mt5.shutdown_calls == 1
    assert manager.state == ConnectionState.DISCONNECTED
    await manager.shutdown()  # second call must not touch the already-closed executor
    assert mt5.shutdown_calls == 1


@pytest.mark.asyncio
async def test_shutdown_does_not_hang_when_a_call_is_stuck():
    manager = MT5ConnectionManager(FakeMT5(), config())
    with pytest.raises(TimeoutError):
        await manager.run(lambda: time.sleep(0.3), timeout=0.01)
    assert manager.state == ConnectionState.DEGRADED
    await manager.shutdown()  # must return promptly, not wait for the wedged thread
    assert manager.state == ConnectionState.DISCONNECTED


@pytest.mark.asyncio
async def test_no_new_mt5_calls_are_accepted_after_shutdown():
    mt5 = FakeMT5()
    manager = MT5ConnectionManager(mt5, config())
    await manager.ensure_connected()
    await manager.shutdown()
    with pytest.raises(MT5ConnectionError):
        await manager.ensure_connected()


@pytest.mark.asyncio
async def test_ensure_connected_rejects_reconnecting_while_stopping():
    manager = MT5ConnectionManager(FakeMT5(), config())
    manager._state = ConnectionState.STOPPING
    with pytest.raises(MT5ConnectionError, match="Apagado en curso"):
        await manager.ensure_connected()


@pytest.mark.asyncio
async def test_is_connected_reflects_the_current_state():
    manager = MT5ConnectionManager(FakeMT5(), config())
    assert manager.is_connected() is False
    await manager.ensure_connected()
    assert manager.is_connected() is True


def test_get_default_connection_returns_the_same_singleton_instance():
    first = get_default_connection()
    second = get_default_connection()
    assert first is second
