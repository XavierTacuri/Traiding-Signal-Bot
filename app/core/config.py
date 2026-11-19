from __future__ import annotations

from datetime import time
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import (
    BeforeValidator,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

FOREX_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD")

def _split_pairs(value: object) -> object:
    if isinstance(value, str):
        return [item.strip().upper() for item in value.split(",") if item.strip()]
    return value

PairList = Annotated[list[str], BeforeValidator(_split_pairs)]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_DEBUG_HOLD: bool = False
    TELEGRAM_MAX_RETRIES: PositiveInt = 3
    LOWER_TIMEFRAME: str = "M15"
    HIGHER_TIMEFRAME: str = "H1"
    SCAN_INTERVAL_SECONDS: PositiveInt = 60
    PAIR_DELAY_SECONDS: float = Field(default=0, ge=0)
    FOREX_PAIRS: PairList = Field(default_factory=lambda: list(FOREX_PAIRS))
    LOWER_TIMEFRAME_BARS: PositiveInt = 300
    HIGHER_TIMEFRAME_BARS: PositiveInt = 300
    TREND_EMA_FAST: PositiveInt = 50
    TREND_EMA_SLOW: PositiveInt = 200
    ENTRY_EMA_FAST: PositiveInt = 20
    ENTRY_EMA_SLOW: PositiveInt = 50
    RSI_PERIOD: PositiveInt = 14
    ATR_PERIOD: PositiveInt = 14
    ADX_PERIOD: PositiveInt = 14
    MIN_ADX: float = 20
    MAX_ADX: float = 45
    RSI_BUY_TRIGGER: float = 52
    RSI_SELL_TRIGGER: float = 48
    PULLBACK_DISTANCE_ATR_MULT: float = 0.20
    MIN_BODY_ATR_MULT: float = 0.25
    MAX_PRICE_EMA_DISTANCE_ATR_MULT: float = 1.20
    MIN_VOLATILITY_RATIO: float = 0.0001
    STOP_BUFFER_ATR_MULT: float = 0.25
    SWING_LOOKBACK_BARS: int = 7
    MIN_STOP_ATR_MULT: float = 0.50
    MAX_STOP_ATR_MULT: float = 2.50
    RISK_REWARD_RATIO: PositiveFloat = 2.0
    SETUP_EXPIRATION_BARS: PositiveInt = 4
    MIN_SIGNAL_SCORE: int = Field(default=75, ge=0, le=100)
    MAX_SPREAD_ATR_RATIO: float = 0.10
    MAX_CORRELATED_SIGNALS: PositiveInt = 1
    BACKTEST_DEFAULT_SPREAD_POINTS: PositiveFloat = 20.0
    MT5_TIMEZONE: str = "UTC"
    SYSTEM_TIMEZONE: str = "UTC"
    TRADING_TIMEZONE: str = "UTC"
    SESSION_START: time = time(6, 0)
    SESSION_END: time = time(18, 0)
    ROLLOVER_BLOCK_START: time = time(21, 55)
    ROLLOVER_BLOCK_END: time = time(22, 15)

    @field_validator("FOREX_PAIRS")
    @classmethod
    def validate_forex_pairs(cls, pairs: list[str]) -> list[str]:
        invalid = sorted(set(pairs) - set(FOREX_PAIRS))
        if invalid:
            raise ValueError(f"Pares no autorizados: {', '.join(invalid)}")
        return list(dict.fromkeys(pairs))

    @field_validator("MT5_TIMEZONE", "SYSTEM_TIMEZONE", "TRADING_TIMEZONE")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def validate_strategy_shape(self) -> Settings:
        if self.LOWER_TIMEFRAME.upper() != "M15" or self.HIGHER_TIMEFRAME.upper() != "H1":
            raise ValueError("La estrategia requiere LOWER_TIMEFRAME=M15 y HIGHER_TIMEFRAME=H1")
        if self.TREND_EMA_FAST >= self.TREND_EMA_SLOW:
            raise ValueError("TREND_EMA_FAST debe ser menor que TREND_EMA_SLOW")
        if self.ENTRY_EMA_FAST >= self.ENTRY_EMA_SLOW:
            raise ValueError("ENTRY_EMA_FAST debe ser menor que ENTRY_EMA_SLOW")
        if self.MIN_ADX > self.MAX_ADX:
            raise ValueError("MIN_ADX no puede ser mayor que MAX_ADX")
        if self.MIN_STOP_ATR_MULT > self.MAX_STOP_ATR_MULT:
            raise ValueError("MIN_STOP_ATR_MULT no puede ser mayor que MAX_STOP_ATR_MULT")
        return self

settings = Settings()
