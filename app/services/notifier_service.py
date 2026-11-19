import asyncio
import logging

import httpx

from app.core.config import Settings, settings

# httpx/httpcore log the full request URL (bot token included) at INFO level on
# their own loggers, independently of any exception handling in this module.
# Silencing them here is defense in depth: it holds even if a future caller
# configures the root logger permissively (e.g. logging.basicConfig(INFO)).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class NotifierService:
    def __init__(self, config: Settings = settings, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client

    @staticmethod
    def _redact(message: str, token: str) -> str:
        """Strip the bot token from an error message before it reaches logs or exception chains."""
        return message.replace(token, "***") if token else message

    async def send_telegram_message(self, text: str) -> None:
        if not self.config.TELEGRAM_BOT_TOKEN or not self.config.TELEGRAM_CHAT_ID:
            raise RuntimeError("Telegram no está configurado")
        url = f"https://api.telegram.org/bot{self.config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": self.config.TELEGRAM_CHAT_ID, "text": text}
        last_error: Exception | None = None
        for attempt in range(self.config.TELEGRAM_MAX_RETRIES):
            try:
                if self.client is not None:
                    response = await self.client.post(url, json=payload)
                else:
                    async with httpx.AsyncClient(timeout=15) as client:
                        response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
                if not body.get("ok"):
                    raise RuntimeError("Telegram rechazó el mensaje")
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last_error = RuntimeError(self._redact(str(exc), self.config.TELEGRAM_BOT_TOKEN))
                if attempt + 1 < self.config.TELEGRAM_MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError("No se pudo enviar la señal a Telegram") from last_error
