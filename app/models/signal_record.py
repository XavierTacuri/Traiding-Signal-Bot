"""Fase 4: persistence-facing records and status enums for virtual signal
tracking. Kept separate from app/domain.py, which models the *strategy's* own
in-flight objects (PendingSetup, SignalType, ...); nothing here is read by
StrategyService or the indicator/strategy pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SignalStatus(StrEnum):
    """Lifecycle of one row in the ``signals`` table.

    A freshly registered signal represents an *immediate-entry* decision
    (SignalSchema.entry is already the close of a confirmed, closed candle —
    see app/services/strategy_service.py) rather than a resting limit order.
    PENDING_ACTIVATION therefore only covers the brief window until the
    tracker can observe a live, fresh bid/ask quote for the symbol; it is not
    a "waiting for price to reach entry" state.
    """

    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVE = "ACTIVE"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    AMBIGUOUS = "AMBIGUOUS"


# Once a signal reaches one of these, no further write may change its status
# (app/repositories/signal_repository.py enforces this with a WHERE clause,
# not just by convention) — section 9 of the Fase 4 brief: "una señal
# finalizada no debe volver a cerrarse".
TERMINAL_SIGNAL_STATUSES = frozenset({
    SignalStatus.TAKE_PROFIT,
    SignalStatus.STOP_LOSS,
    SignalStatus.EXPIRED,
    SignalStatus.CANCELLED,
    SignalStatus.AMBIGUOUS,
})

# Terminal statuses with a determined trading outcome (used for win-rate /
# expectancy denominators in app/services/statistics_service.py). AMBIGUOUS is
# terminal but intentionally excluded: section 10 forbids counting an
# undetermined order-of-events as either a win or a loss.
DETERMINED_RESULT_STATUSES = frozenset({SignalStatus.TAKE_PROFIT, SignalStatus.STOP_LOSS})


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationType(StrEnum):
    NEW_SIGNAL = "NEW_SIGNAL"
    ACTIVATED = "ACTIVATED"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(slots=True)
class SignalRecord:
    """Mirrors one row of the ``signals`` table.

    Price fields are kept as strings (the exact ``str(Decimal)`` already
    produced by app/services/market_data_service.normalize_price) rather than
    floats, so round-tripping through SQLite never perturbs a value the
    strategy already rounded deliberately to the broker's digits.
    """

    signal_key: str
    symbol: str
    direction: str
    timeframe: str
    entry_price: str
    stop_loss: str
    take_profit: str
    risk_distance: str
    digits: int
    created_at_utc: datetime
    status: SignalStatus
    id: int | None = None
    confidence_score: int | None = None
    activation_price: str | None = None
    activation_side: str | None = None
    sent_at_utc: datetime | None = None
    activated_at_utc: datetime | None = None
    closed_at_utc: datetime | None = None
    expires_at_utc: datetime | None = None
    last_checked_bar_time_utc: datetime | None = None
    result_pips: float | None = None
    result_r: float | None = None
    close_reason: str | None = None
    schema_version: int = 1


@dataclass(slots=True)
class OutboxItem:
    """Mirrors one row of the ``notification_outbox`` table."""

    event_key: str
    signal_id: int
    message_type: str
    payload: str
    created_at_utc: datetime
    next_attempt_at_utc: datetime
    id: int | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    sent_at_utc: datetime | None = None
    last_error: str | None = None


@dataclass(slots=True)
class SignalEvent:
    """Mirrors one row of the ``signal_events`` audit table."""

    signal_id: int
    event_type: str
    new_status: str
    occurred_at_utc: datetime
    id: int | None = None
    previous_status: str | None = None
    metadata: str | None = None
