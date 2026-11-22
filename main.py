import asyncio
import signal
import sqlite3
from datetime import UTC, datetime

from app.core.config import settings
from app.core.logger import add_rotating_file_handler, logger
from app.monitoring.daily_summary import DailySummaryService
from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.monitoring.heartbeat_service import HeartbeatService
from app.monitoring.models import WorkerStatus
from app.monitoring.worker_state import runtime_state
from app.repositories.database import DatabaseError, get_default_database
from app.services.mt5_connection import get_default_connection
from app.workers import scanner
from app.workers.scanner import run_scanner


async def _notify_lifecycle_event(message: str) -> None:
    """Best-effort only (section 10/11): a startup/shutdown notice is not a
    trading signal and has nothing to retry against, so a Telegram failure
    here is just logged, never raised, and never routed through the Fase 4
    outbox (which requires a signal_id — see app/monitoring/daily_summary.py)."""
    try:
        await scanner.notifier_service.send_telegram_message(message)
    except RuntimeError as exc:
        logger.info("lifecycle | notificación no enviada: %s", exc)


async def _run() -> None:
    """Run the scanner until a shutdown signal arrives, then release MT5.

    Windows note: ``asyncio``'s ``add_signal_handler`` is Unix-only, so signals
    are installed with ``signal.signal`` instead (works for SIGINT/SIGBREAK on
    Windows, SIGINT/SIGTERM on Linux). Windows has no real SIGTERM delivery for
    external process termination — only Ctrl+C/SIGBREAK/console-close reliably
    reach a Python process there; SIGTERM is still registered best-effort for
    parity when running under Linux/containers.
    """
    add_rotating_file_handler(
        logger, "logs/trading_bot.log",
        max_bytes=settings.LOG_MAX_BYTES, backup_count=settings.LOG_BACKUP_COUNT,
    )

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

    # Fase 4: aplica migraciones y recupera setups/señales/outbox persistidos
    # antes de aceptar el primer ciclo de escaneo. Un fallo aquí (esquema
    # incompatible, disco inaccesible, archivo corrupto) debe detener el
    # arranque en lugar de operar silenciosamente sin persistencia (section 20).
    database = get_default_database()
    await database.initialize()

    heartbeat_task: asyncio.Task | None = None
    if settings.MONITORING_ENABLED:
        daily_summary = DailySummaryService(
            HeartbeatRepository(database), scanner.signal_repository, scanner.notifier_service,
            database.path,
        )
        heartbeat_service = HeartbeatService(
            database, runtime_state, connection=scanner.connection,
            outbox_repository=scanner.outbox_repository, signal_repository=scanner.signal_repository,
            daily_summary=daily_summary,
        )
        heartbeat_task = asyncio.create_task(
            heartbeat_service.run_forever(settings.HEARTBEAT_INTERVAL_SECONDS), name="heartbeat",
        )
        await _notify_lifecycle_event(
            f"✅ Bot Forex iniciado correctamente ({datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')})",
        )

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
        # Mark STOPPING and write one last heartbeat immediately (not
        # waiting for the next periodic tick) so the watchdog sees an
        # ordered shutdown instead of inferring an unexplained crash
        # (section 7: "periodo de gracia durante apagado ordenado").
        runtime_state.status = WorkerStatus.STOPPING
        if heartbeat_task is not None:
            try:
                await HeartbeatService(
                    database, runtime_state, connection=scanner.connection,
                    outbox_repository=scanner.outbox_repository,
                    signal_repository=scanner.signal_repository,
                ).tick()
            except (DatabaseError, sqlite3.Error, OSError, RuntimeError) as exc:
                # Best-effort: shutdown must proceed regardless of a failed
                # final heartbeat write.
                logger.warning("heartbeat | no se pudo registrar el apagado ordenado: %s", exc)
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await _notify_lifecycle_event(
                f"🛑 Apagado ordenado del bot Forex ({datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')})",
            )
        await get_default_connection().shutdown()
        await database.close()
        logger.info("Recursos liberados. Apagado ordenado completado.")


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupción de teclado (Ctrl+C) recibida directamente en asyncio.run; apagado completado.")
