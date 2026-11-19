from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SignalType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class TrendDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NONE = "NONE"

class SetupType(StrEnum):
    BULLISH_PULLBACK = "BULLISH_PULLBACK"
    BEARISH_PULLBACK = "BEARISH_PULLBACK"

class SetupStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"

@dataclass(slots=True)
class PendingSetup:
    pair: str
    direction: SignalType
    detected_at: datetime
    detected_bar_time: datetime
    expires_after_bars: int
    pullback_low: float | None = None
    pullback_high: float | None = None
    status: SetupStatus = SetupStatus.PENDING

    @property
    def key(self) -> str:
        return f"{self.pair}:{self.direction}:{self.detected_bar_time.isoformat()}"
