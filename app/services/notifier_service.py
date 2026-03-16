import httpx
from app.core.config import settings

class NotifierService:
    async def send_telegram_message(self,text:str):

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

        payload = {
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                    "text": text }
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)

