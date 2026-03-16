import google.generativeai as genai
from app.core.config import settings

class AIService:
    def __init__(self):
        self.enable = bool(settings.GEMINI_API_KEY)

        if self.enable:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
        else:
            self.model = None

    async def confirm_signal(self,signal,indicators:dict)->str:
        if not self.enable or self.model is None:
            return "IA Desactivada"

        prompt = f"""
                 Eres un analista técnico de trading.

                 Analiza esta señal y responde en máximo 3 líneas.
                 Indica si la señal parece razonable o si necesita cautela.

                 Par: {signal.pair}
                 Timeframe: {signal.timeframe}
                 Tipo de señal: {signal.signal_type}
                 Precio de entrada: {signal.entry}
                 Stop Loss: {signal.stop_loss}
                 Take Profit: {signal.take_profit}
                 Confianza técnica: {signal.confidence}
                 Motivo técnico: {signal.reason}

                 Indicadores:
                 - EMA rápida: {indicators.get("ema_fast")}
                 - EMA lenta: {indicators.get("ema_slow")}
                 - RSI: {indicators.get("rsi")}
                 - MACD: {indicators.get("macd")}
                 - Volumen: {indicators.get("volume")}

                 Responde en español, breve y claro.
                 """

        try:
            reponse = await self.model.generate_content_async(prompt)
            text = getattr(reponse, "text", None)

            if not text:
                return "Sin respuesta de IA"

            return text.strip()

        except Exception as e:
            error = str(e)

            if "429" in error or "RESOURCE_EXHAUSTED" in error:
                return "IA no disponible por couta maxima"

            return f"Error IA: {error}"


