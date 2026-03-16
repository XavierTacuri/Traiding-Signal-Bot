import asyncio
from datetime import datetime
from app.core.config import settings
from app.core.logger import logger
from app.services.signal_service import SignalService
from app.services.notifier_service import NotifierService

signal_service = SignalService()
notifier_service = NotifierService()

last_signal = {}

async def run_scanner():
    logger.info("Bot iniciado. Escaneo continuo activado.")

    while True:
        logger.info("Nuevo escaneo: %s", datetime.now().strftime('%H:%M:%S'))
        for pair in settings.PAIRS.split(","):
            try:
                signal = await signal_service.build_signal(pair)

                if signal.signal_type in ["BUY", "SELL"]:
                    logger.info(
                        "Par: %s | Tipo: %s | Entry: %s | SL: %s | TP: %s | Motivo: %s ",
                        signal.pair,
                        signal.signal_type,
                        signal.entry,
                        signal.stop_loss,
                        signal.take_profit,
                        signal.reason
                    )
                    previous_signal = last_signal.get(pair)

                    if previous_signal != signal.signal_type:
                        message = format_signal(signal)
                        await notifier_service.send_telegram_message(message)
                        last_signal[pair] = signal.signal_type
                        logger.info("Señal enviada para %s", pair)
                    else:
                        logger.info("Señal repetida omitida para %s", pair)
                else:
                    logger.info(
                        "Par: %s | Tipo: HOLD | Motivo: %s",
                        signal.pair,
                        signal.reason,
                    )
                    last_signal[pair] = "HOLD"

            except Exception as e:
                logger.exception("Error procesando %s: %s", pair, e)

            await asyncio.sleep(settings.PAIR_DELAY_SECONDS)

        logger.info("Escaneo finalizado. Esperando %s segundos", settings.SCAN_INTERVAL_SECONDS)
        await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)

def format_signal(signal):
    return (f"🚨 Señal Detectada\n\n"
            f"Par: {signal.pair}\n"
            f"TimeFram:{signal.timeframe}\n"
            f"Tipo: {signal.signal_type}\n"
            f"Entry: {signal.entry}\n"
            f"Stop Loss: {signal.stop_loss}\n"
            f"Take Profit: {signal.take_profit}\n"
            f"Confianza: {signal.confidence}\n"
            f"Motivo: {signal.reason}\n")