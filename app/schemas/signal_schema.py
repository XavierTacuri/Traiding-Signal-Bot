from decimal import Decimal

from pydantic import BaseModel
from typing import Literal

class SignalSchema(BaseModel):
    pair: str
    timeframe:str | None = None
    signal_type: Literal["BUY", "SELL","HOLD"]
    entry: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    confidence: float | None = None
    reason: str = ""
