"""Fase 4, sections 6/7: turns a freshly generated SignalSchema into a
persisted SignalRecord plus its first (NEW_SIGNAL) outbox entry, atomically.

This is the only place that builds a ``signals`` row from strategy output —
kept separate from app/workers/scanner.py so the mapping (and its one
genuine business decision, ``risk_distance``) has a unit-testable home
instead of living inline in the worker loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.config import Settings, settings
from app.models.signal_record import NotificationType, SignalRecord, SignalStatus
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.signal_schema import SignalSchema
from app.services.market_data_service import TIMEFRAME_DELTAS


class SignalPersistenceService:
    def __init__(self, outbox_repository: OutboxRepository, config: Settings = settings,
                 clock=None) -> None:
        self._outbox = outbox_repository
        self.config = config
        self._now = clock or (lambda: datetime.now(UTC))

    def build_record(self, signal: SignalSchema) -> SignalRecord:
        if signal.signal_key is None or signal.entry is None or signal.stop_loss is None:
            raise ValueError("La señal no tiene los campos mínimos para persistirse")
        now = self._now()
        timeframe = (signal.timeframe or "M15").upper()
        bar_delta = TIMEFRAME_DELTAS.get(timeframe, TIMEFRAME_DELTAS["M15"])
        risk_distance = abs(Decimal(str(signal.entry)) - Decimal(str(signal.stop_loss)))
        return SignalRecord(
            signal_key=signal.signal_key, symbol=signal.pair, direction=str(signal.signal_type),
            timeframe=timeframe, entry_price=str(signal.entry), stop_loss=str(signal.stop_loss),
            take_profit=str(signal.take_profit), risk_distance=str(risk_distance),
            digits=signal.digits if signal.digits is not None else 5,
            confidence_score=signal.score, created_at_utc=now,
            expires_at_utc=now + bar_delta * self.config.SIGNAL_EXPIRATION_BARS,
            status=SignalStatus.PENDING_ACTIVATION,
        )

    async def register_and_enqueue(self, signal: SignalSchema, message_text: str) -> tuple[int, int] | None:
        """Returns (signal_id, outbox_id), or None if signal_key already
        exists (the signal was already registered — no duplicate created)."""
        record = self.build_record(signal)
        event_key = f"{signal.signal_key}:{NotificationType.NEW_SIGNAL}"
        return await self._outbox.register_signal_with_notification(
            record, event_key, str(NotificationType.NEW_SIGNAL), message_text,
        )
