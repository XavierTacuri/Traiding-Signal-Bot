class AIService:
    """Deprecated compatibility service; AI is not part of strategy decisions."""

    async def confirm_signal(self, signal: object, indicators: dict) -> str:
        return "Confirmación IA desactivada; no participa en la estrategia"
