from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class SignalSchema(BaseModel):
    pair: str
    timeframe: str | None = None
    higher_timeframe: str | None = None
    signal_type: Literal["BUY", "SELL", "HOLD"]
    entry: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    risk_reward_ratio: Decimal | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    score: int | None = Field(default=None, ge=0, le=100)
    reason: str = ""
    bar_time: datetime | None = None
    setup_type: str | None = None
    adx_h1: float | None = None
    rsi_m15: float | None = None
    signal_key: str | None = None
    broker_symbol: str | None = None
