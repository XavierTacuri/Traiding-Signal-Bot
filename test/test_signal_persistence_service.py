"""Fase 4, sections 6/7: SignalSchema -> SignalRecord mapping and the
atomic register-and-enqueue entry point."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.signal_record import SignalStatus
from app.repositories.database import Database
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.signal_repository import SQLiteSignalRepository
from app.schemas.signal_schema import SignalSchema
from app.services.signal_persistence_service import SignalPersistenceService


def _signal(**overrides) -> SignalSchema:
    base = {"pair": "EURUSD", "timeframe": "M15", "signal_type": "BUY", "entry": "1.10000",
            "stop_loss": "1.09500", "take_profit": "1.11000", "score": 90, "digits": 5,
            "signal_key": "EURUSD:M15:BUY:2026-01-01T12:00:00+00:00"}
    base.update(overrides)
    return SignalSchema(**base)


def _service(config=None) -> SignalPersistenceService:
    # build_record() never touches the outbox repository; None is enough for
    # these pure-mapping tests (persistence itself is covered separately
    # below and in test_outbox_repository.py).
    return SignalPersistenceService(None, config=config or SimpleNamespace(SIGNAL_EXPIRATION_BARS=4),
                                      clock=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


def test_build_record_computes_risk_distance_from_entry_and_stop():
    record = _service().build_record(_signal())
    assert Decimal(record.risk_distance) == Decimal("0.00500")
    assert record.status == SignalStatus.PENDING_ACTIVATION
    assert record.digits == 5


def test_build_record_expires_after_configured_bars_of_the_signal_timeframe():
    record = _service().build_record(_signal())
    # M15 timeframe, SIGNAL_EXPIRATION_BARS=4 -> 1 hour after created_at_utc.
    assert record.expires_at_utc == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


def test_build_record_defaults_digits_when_missing():
    record = _service().build_record(_signal(digits=None))
    assert record.digits == 5


def test_build_record_rejects_a_signal_without_entry_or_stop():
    with pytest.raises(ValueError):
        _service().build_record(_signal(entry=None))


@pytest.mark.asyncio
async def test_register_and_enqueue_persists_signal_and_outbox_atomically(tmp_path):
    database = Database(tmp_path / "s.db")
    outbox = SQLiteOutboxRepository(database)
    signals = SQLiteSignalRepository(database)
    service = SignalPersistenceService(outbox, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    result = await service.register_and_enqueue(_signal(), "mensaje de telegram")

    assert result is not None
    record = await signals.get_by_key(_signal().signal_key)
    assert record is not None
    due = await outbox.due(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(due) == 1 and due[0].payload == "mensaje de telegram"


@pytest.mark.asyncio
async def test_register_and_enqueue_is_idempotent(tmp_path):
    database = Database(tmp_path / "s.db")
    outbox = SQLiteOutboxRepository(database)
    service = SignalPersistenceService(outbox, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    first = await service.register_and_enqueue(_signal(), "msg")
    second = await service.register_and_enqueue(_signal(), "msg")

    assert first is not None
    assert second is None
