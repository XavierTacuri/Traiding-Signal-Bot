import asyncio
from datetime import UTC, datetime

from app.core.config import settings
from app.core.logger import logger
from app.schemas.signal_schema import SignalSchema
from app.services.correlation_service import CorrelationService
from app.services.notifier_service import NotifierService
from app.services.signal_service import SignalService
from app.services.state_service import InMemorySignalKeyRepository

signal_service: SignalService | None = None
notifier_service = NotifierService()
correlation_service = CorrelationService()
signal_keys = InMemorySignalKeyRepository()
pending_notifications: dict[str, SignalSchema] = {}

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

async def scan_once() -> list[SignalSchema]:
    global signal_service
    if signal_service is None:
        signal_service = SignalService()
    candidates: list[SignalSchema] = list(pending_notifications.values())
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
        except (RuntimeError, ValueError, KeyError, TypeError):
            logger.exception("%s | scan_failed", pair)
        if settings.PAIR_DELAY_SECONDS:
            await asyncio.sleep(settings.PAIR_DELAY_SECONDS)
    selected, rejected = correlation_service.filter(candidates, settings.MAX_CORRELATED_SIGNALS)
    for signal in rejected:
        logger.info("%s | signal_rejected_correlation | score=%s", signal.pair, signal.score)
    sent: list[SignalSchema] = []
    for signal in selected:
        if not signal.signal_key or signal_keys.contains(signal.signal_key):
            logger.info("%s | signal_duplicate", signal.pair)
            continue
        try:
            await notifier_service.send_telegram_message(format_signal(signal))
        except RuntimeError:
            logger.exception("%s | telegram_send_failed", signal.pair)
            pending_notifications[signal.signal_key] = signal
            continue
        signal_keys.mark_sent(signal.signal_key, datetime.now(UTC))
        pending_notifications.pop(signal.signal_key, None)
        sent.append(signal)
        logger.info("%s | signal_sent | score=%s", signal.pair, signal.score)
    return sent

async def run_scanner() -> None:
    logger.info("Bot Forex iniciado. Escaneo por cierre M15 activado.")
    while True:
        await scan_once()
        await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)
