import asyncio
from datetime import UTC, datetime

from app.core.config import settings
from app.core.logger import logger
from app.models.signal_record import NotificationType
from app.repositories.database import get_default_database
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.setup_repository import SQLiteSetupRepository
from app.repositories.signal_repository import SQLiteSignalRepository
from app.schemas.signal_schema import SignalSchema
from app.services.correlation_service import CorrelationService
from app.services.market_data_validation import (
    InvalidMarketDataError,
    StaleMarketDataError,
)
from app.services.mt5_connection import (
    MT5BusyError,
    MT5ConfigurationError,
    MT5ConnectionError,
    get_default_connection,
)
from app.services.notifier_service import NotifierService
from app.services.outbox_service import OutboxProcessor
from app.services.signal_persistence_service import SignalPersistenceService
from app.services.signal_service import SignalService
from app.services.signal_tracker_service import SignalTrackerService
from app.services.strategy_service import StrategyService

signal_service: SignalService | None = None
notifier_service = NotifierService()
correlation_service = CorrelationService()
connection = get_default_connection()

# Fase 4: persistencia y seguimiento virtual. Reemplazan el estado en memoria
# de la Fase 3 (signal_keys/pending_notifications); ver app/repositories/.
database = get_default_database()
signal_repository = SQLiteSignalRepository(database)
outbox_repository = SQLiteOutboxRepository(database)
persistence_service = SignalPersistenceService(outbox_repository)
tracker_service = SignalTrackerService(signal_repository=signal_repository, connection=connection)
_last_tracker_run: datetime | None = None


def format_signal(signal: SignalSchema) -> str:
    direction = "🟢 SEÑAL FOREX — BUY" if signal.signal_type == "BUY" else "🔴 SEÑAL FOREX — SELL"
    trend = "alcista" if signal.signal_type == "BUY" else "bajista"
    bar_time = signal.bar_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if signal.bar_time else "N/D"
    return (
        f"{direction}\n\nPar: {signal.pair}\nTemporalidad de entrada: {signal.timeframe}\n"
        f"Tendencia principal: {signal.higher_timeframe} {trend}\n\nEntrada: {signal.entry}\n"
        f"Stop Loss: {signal.stop_loss}\nTake Profit: {signal.take_profit}\n"
        f"Riesgo/beneficio: 1:{signal.risk_reward_ratio}\n\nADX H1: {signal.adx_h1:.1f}\n"
        f"RSI M15: {signal.rsi_m15:.1f}\nConfianza: {signal.score}/100\n\n"
        f"Setup:\n{signal.reason}\n\nHora de la señal:\n{bar_time}"
    )


_RESULT_HEADERS = {
    NotificationType.ACTIVATED: "🔵 ENTRADA VIRTUAL ACTIVADA (SIMULADO)",
    NotificationType.TAKE_PROFIT: "✅ TAKE PROFIT ALCANZADO (VIRTUAL)",
    NotificationType.STOP_LOSS: "⛔ STOP LOSS ALCANZADO (VIRTUAL)",
    NotificationType.EXPIRED: "⌛ SEÑAL EXPIRADA SIN ACTIVAR (VIRTUAL)",
    NotificationType.AMBIGUOUS: "⚠️ RESULTADO AMBIGUO (VIRTUAL — SL y TP en la misma vela)",
}


def format_result_notification(event) -> str:
    """Section 14: result notifications, always labelled SIMULADO/VIRTUAL —
    they describe outcomes the tracker observed, not real trade executions."""
    record = event.record
    lines = [
        _RESULT_HEADERS[event.notification_type], "",
        f"Par: {record.symbol}", f"Tipo: {record.direction}", f"Estado: {event.new_status.value}",
    ]
    if event.result_pips is not None:
        lines.append(f"Resultado: {float(event.result_pips):.1f} pips virtuales")
    if event.result_r is not None:
        lines.append(f"Resultado: {float(event.result_r):.2f}R")
    lines.append("")
    lines.append("Resultado simulado; el bot no ejecuta operaciones reales.")
    return "\n".join(lines)


def _tracker_due(now: datetime) -> bool:
    return _last_tracker_run is None or (now - _last_tracker_run).total_seconds() >= settings.TRACKER_INTERVAL_SECONDS


async def _run_tracker_and_notify() -> None:
    global _last_tracker_run
    now = datetime.now(UTC)
    if not settings.SIGNAL_TRACKING_ENABLED or not _tracker_due(now):
        return
    _last_tracker_run = now
    events = await tracker_service.evaluate_once()
    if not settings.SIGNAL_RESULT_NOTIFICATIONS_ENABLED:
        return
    for event in events:
        if event.record.id is None:
            continue
        event_key = f"{event.record.signal_key}:{event.notification_type}"
        await outbox_repository.enqueue(event_key, event.record.id, str(event.notification_type),
                                         format_result_notification(event))


async def scan_once() -> list[SignalSchema]:
    """A: generación de nuevas señales. B: seguimiento de señales anteriores.
    C: procesamiento del outbox de Telegram (section 15) — C always runs,
    even if MT5 is unavailable this cycle, since it only needs SQLite+Telegram."""
    global signal_service
    mt5_available = True
    try:
        await connection.ensure_connected()
    except MT5ConfigurationError:
        raise  # not recoverable by retrying; run_scanner stops the worker in a controlled way
    except (MT5ConnectionError, MT5BusyError) as exc:
        logger.warning("mt5_connection | ciclo de escaneo suspendido (sin conexión MT5): %s", exc)
        mt5_available = False

    newly_registered: list[SignalSchema] = []
    if mt5_available:
        if signal_service is None:
            # database.path (not settings.DATABASE_PATH) so tests that swap
            # `database` for a temporary one also redirect this repository —
            # the live process wires both to the same configured path.
            signal_service = SignalService(
                connection=connection,
                strategy_service=StrategyService(settings, setups=SQLiteSetupRepository(database.path)),
            )
        candidates: list[SignalSchema] = []
        for pair in settings.FOREX_PAIRS:
            try:
                signal = await signal_service.build_signal(pair)
                if signal.signal_type in ("BUY", "SELL"):
                    candidates.append(signal)
                    logger.info("%s | signal_%s | score=%s", pair, signal.signal_type.lower(), signal.score)
                    if signal.broker_symbol and signal.broker_symbol != pair:
                        logger.debug("%s | broker_symbol=%s", pair, signal.broker_symbol)
                else:
                    logger.info("%s | signal_hold | %s", pair, signal.reason)
            except StaleMarketDataError as exc:
                logger.info("%s | signal_rejected_stale_data | %s", pair, exc)
            except InvalidMarketDataError as exc:
                logger.info("%s | signal_rejected_invalid_data | %s", pair, exc)
            except (RuntimeError, ValueError, KeyError, TypeError):
                logger.exception("%s | scan_failed", pair)
            if settings.PAIR_DELAY_SECONDS:
                await asyncio.sleep(settings.PAIR_DELAY_SECONDS)
        selected, rejected = correlation_service.filter(candidates, settings.MAX_CORRELATED_SIGNALS)
        for signal in rejected:
            logger.info("%s | signal_rejected_correlation | score=%s", signal.pair, signal.score)
        for signal in selected:
            if not signal.signal_key:
                continue
            # register_and_enqueue is the atomic (signal + outbox) write of
            # section 7; returning None means signal_key already existed —
            # a repeated scan or a restart must never register it twice
            # (section 6), so this is a silent, expected no-op, not an error.
            result = await persistence_service.register_and_enqueue(signal, format_signal(signal))
            if result is None:
                logger.info("%s | signal_duplicate", signal.pair)
                continue
            newly_registered.append(signal)
            logger.info("%s | signal_registered | score=%s", signal.pair, signal.score)

        await _run_tracker_and_notify()

    outbox_processor = OutboxProcessor(outbox_repository, notifier_service)
    delivered_signal_ids = set(await outbox_processor.process_due())

    sent: list[SignalSchema] = []
    for signal in newly_registered:
        record = await signal_repository.get_by_key(signal.signal_key)
        if record is not None and record.id in delivered_signal_ids:
            await signal_repository.mark_sent(record.id, datetime.now(UTC))
            sent.append(signal)
            logger.info("%s | signal_sent | score=%s", signal.pair, signal.score)
    return sent


async def run_scanner() -> None:
    logger.info("Bot Forex iniciado. Escaneo por cierre M15 activado.")
    while True:
        try:
            await scan_once()
        except MT5ConfigurationError as exc:
            logger.error(
                "mt5_connection | configuración de MT5 no recuperable, el worker se detiene "
                "de forma controlada (requiere intervención manual): %s", exc,
            )
            return
        await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)
