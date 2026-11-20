import asyncio
import signal

from app.core.logger import logger
from app.services.mt5_connection import get_default_connection
from app.workers.scanner import run_scanner


async def _run() -> None:
    """Run the scanner until a shutdown signal arrives, then release MT5.

    Windows note: ``asyncio``'s ``add_signal_handler`` is Unix-only, so signals
    are installed with ``signal.signal`` instead (works for SIGINT/SIGBREAK on
    Windows, SIGINT/SIGTERM on Linux). Windows has no real SIGTERM delivery for
    external process termination — only Ctrl+C/SIGBREAK/console-close reliably
    reach a Python process there; SIGTERM is still registered best-effort for
    parity when running under Linux/containers.
    """
    stop_event = asyncio.Event()

    def _request_stop(signal_name: str) -> None:
        logger.info("Señal %s recibida; iniciando apagado ordenado...", signal_name)
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            signal.signal(signal_number, lambda *_args, name=signal_name: _request_stop(name))
        except (ValueError, OSError):  # pragma: no cover - platform without this signal
            logger.debug("No se pudo instalar un manejador para %s en esta plataforma", signal_name)

    scanner_task = asyncio.create_task(run_scanner(), name="scanner")
    stop_wait_task = asyncio.create_task(stop_event.wait(), name="stop_wait")
    try:
        await asyncio.wait({scanner_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED)
        if not scanner_task.done():
            scanner_task.cancel()
            try:
                await scanner_task
            except asyncio.CancelledError:
                pass
        elif scanner_task.exception() is not None:
            raise scanner_task.exception()
    finally:
        if not stop_wait_task.done():
            stop_wait_task.cancel()
        await get_default_connection().shutdown()
        logger.info("Recursos liberados. Apagado ordenado completado.")


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupción de teclado (Ctrl+C) recibida directamente en asyncio.run; apagado completado.")
