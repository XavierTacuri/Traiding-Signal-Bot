from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import (
    BeforeValidator,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

FOREX_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD")

# Known renamed/removed settings from before the config audit (Fase 2). Keys are the
# obsolete names once seen in .env; values point to the field that replaced them.
# pydantic-settings' extra="ignore" drops unknown keys silently, so without this check
# an obsolete TIMEFRAME/PAIRS line looks like effective configuration but has zero
# effect. Intentionally a short, explicit list (not generic typo detection) to avoid
# false positives on unrelated OS/CI environment variables.
_OBSOLETE_ENV_VARS = {
    "TIMEFRAME": "LOWER_TIMEFRAME / HIGHER_TIMEFRAME",
    "PAIRS": "FOREX_PAIRS",
}

def _split_pairs(value: object) -> object:
    if isinstance(value, str):
        return [item.strip().upper() for item in value.split(",") if item.strip()]
    return value

# NoDecode tells pydantic-settings to skip its own JSON-decode attempt for this
# env var (it otherwise treats list[str] as a "complex" type and tries json.loads()
# on the raw string before any validator runs, which fails for a plain comma-separated
# value like "EURUSD,GBPUSD"). This left FOREX_PAIRS unusable from a real .env file.
PairList = Annotated[list[str], NoDecode, BeforeValidator(_split_pairs)]

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
    RSI_BUY_TRIGGER: float = Field(default=52, ge=0, le=100)
    RSI_SELL_TRIGGER: float = Field(default=48, ge=0, le=100)
    PULLBACK_DISTANCE_ATR_MULT: NonNegativeFloat = 0.20
    MIN_BODY_ATR_MULT: NonNegativeFloat = 0.25
    MAX_PRICE_EMA_DISTANCE_ATR_MULT: NonNegativeFloat = 1.20
    MIN_VOLATILITY_RATIO: NonNegativeFloat = 0.0001
    STOP_BUFFER_ATR_MULT: NonNegativeFloat = 0.25
    SWING_LOOKBACK_BARS: PositiveInt = 7
    MIN_STOP_ATR_MULT: NonNegativeFloat = 0.50
    MAX_STOP_ATR_MULT: NonNegativeFloat = 2.50
    RISK_REWARD_RATIO: PositiveFloat = 2.0
    SETUP_EXPIRATION_BARS: PositiveInt = 4
    MIN_SIGNAL_SCORE: int = Field(default=75, ge=0, le=100)
    MAX_SPREAD_ATR_RATIO: NonNegativeFloat = 0.10
    MAX_CORRELATED_SIGNALS: PositiveInt = 1
    BACKTEST_DEFAULT_SPREAD_POINTS: PositiveFloat = 20.0
    MT5_TIMEZONE: str = "UTC"
    SYSTEM_TIMEZONE: str = "UTC"
    TRADING_TIMEZONE: str = "UTC"
    SESSION_START: time = time(6, 0)
    SESSION_END: time = time(18, 0)
    ROLLOVER_BLOCK_START: time = time(21, 55)
    ROLLOVER_BLOCK_END: time = time(22, 15)

    # ==============================
    # MT5 — reconexión y timeouts (Fase 3)
    # ==============================
    # Backoff exponencial con jitter: 5s -> 10s -> 20s -> 40s -> 60s (tope), nunca
    # instantáneo ni sin límite superior, para no martillar el terminal/broker
    # durante una caída prolongada ni bloquear el event loop con reintentos.
    MT5_RECONNECT_INITIAL_DELAY_SECONDS: PositiveFloat = 5.0
    MT5_RECONNECT_MAX_DELAY_SECONDS: PositiveFloat = 60.0
    MT5_RECONNECT_BACKOFF_MULTIPLIER: float = Field(default=2.0, gt=1.0)
    # Límite de espera para initialize()/terminal_info() de conexión y para una
    # consulta individual (symbol_info, copy_rates_*, symbol_info_tick). No cancela
    # la llamada nativa bloqueada (ver app/services/mt5_connection.py); solo acota
    # cuánto tiempo se espera antes de declarar la operación degradada.
    MT5_CONNECT_TIMEOUT_SECONDS: PositiveFloat = 15.0
    MT5_REQUEST_TIMEOUT_SECONDS: PositiveFloat = 10.0
    # Antigüedad máxima aceptada del último tick (bid/ask) antes de rechazar una
    # señal por datos obsoletos.
    MT5_MAX_TICK_AGE_SECONDS: PositiveInt = 120
    # Una vela cerrada se considera obsoleta si su cierre teórico quedó más atrás
    # que (duración del timeframe * este múltiplo). >1 para tolerar latencia normal
    # de publicación del broker sin confundirla con desconexión.
    MT5_STALE_BAR_AGE_MULTIPLIER: float = Field(default=3.0, ge=1.0)

    @field_validator("FOREX_PAIRS")
    @classmethod
    def validate_forex_pairs(cls, pairs: list[str]) -> list[str]:
        if not pairs:
            raise ValueError("FOREX_PAIRS no puede estar vacío")
        invalid = sorted(set(pairs) - set(FOREX_PAIRS))
        if invalid:
            raise ValueError(f"Pares no autorizados: {', '.join(invalid)}")
        duplicates = sorted({pair for pair in pairs if pairs.count(pair) > 1})
        if duplicates:
            raise ValueError(f"Pares duplicados en FOREX_PAIRS: {', '.join(duplicates)}")
        return pairs

    @field_validator("MT5_TIMEZONE", "SYSTEM_TIMEZONE", "TRADING_TIMEZONE")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Zona horaria IANA inválida: {value}") from exc
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
        if self.RSI_BUY_TRIGGER <= self.RSI_SELL_TRIGGER:
            raise ValueError("RSI_BUY_TRIGGER debe ser mayor que RSI_SELL_TRIGGER")
        if bool(self.TELEGRAM_BOT_TOKEN) != bool(self.TELEGRAM_CHAT_ID):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben configurarse ambos o ninguno"
            )
        if self.MT5_RECONNECT_MAX_DELAY_SECONDS < self.MT5_RECONNECT_INITIAL_DELAY_SECONDS:
            raise ValueError(
                "MT5_RECONNECT_MAX_DELAY_SECONDS no puede ser menor que "
                "MT5_RECONNECT_INITIAL_DELAY_SECONDS"
            )
        return self

def _detect_obsolete_env_vars(env_file: str | None) -> list[str]:
    """Look for renamed/removed setting names in both the OS environment and the
    .env file content (pydantic-settings reads both, but only the latter is not
    reflected in os.environ). Returns the sorted obsolete names actually present."""
    sources: dict[str, str | None] = dict(os.environ)
    if env_file and Path(env_file).is_file():
        sources |= dotenv_values(env_file)
    return sorted(name for name in _OBSOLETE_ENV_VARS if name in sources)

def warn_obsolete_env_vars(env_file: str | None = ".env") -> None:
    """Fail fast with an actionable message instead of letting extra='ignore'
    silently discard TIMEFRAME/PAIRS and create a false impression of effective
    configuration. Never includes the variables' values, only their names."""
    found = _detect_obsolete_env_vars(env_file)
    if found:
        hints = "; ".join(f"{name} -> use {_OBSOLETE_ENV_VARS[name]}" for name in found)
        raise RuntimeError(
            "Variables de entorno obsoletas detectadas (sin efecto real: "
            f"pydantic-settings las descarta silenciosamente): {hints}. "
            "Elimínelas o renómbrelas en su .env antes de continuar."
        )

settings = Settings()
warn_obsolete_env_vars()
