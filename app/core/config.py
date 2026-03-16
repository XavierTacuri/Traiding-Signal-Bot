from typing import List

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    GEMINI_API_KEY: str

    TIMEFRAME: str
    SCAN_INTERVAL_SECONDS: int
    PAIR_DELAY_SECONDS: int

    PAIRS: str

    class Config:
        env_file = ".env"

settings = Settings()