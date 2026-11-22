"""Fase 4, section 7: delivery via OutboxProcessor — success, backoff on
failure, and that a Telegram failure never raises out of process_due()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.signal_record import SignalRecord, SignalStatus
from app.repositories.database import Database
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.signal_repository import SQLiteSignalRepository
from app.services.outbox_service import OutboxProcessor


def _record() -> SignalRecord:
    return SignalRecord(
        signal_key="k1", symbol="EURUSD", direction="BUY", timeframe="M15",
        entry_price="1.1", stop_loss="1.09", take_profit="1.12", risk_distance="0.01",
        digits=5, created_at_utc=datetime(2026, 1, 1, tzinfo=UTC), status=SignalStatus.PENDING_ACTIVATION,
    )


class _Notifier:
    def __init__(self, fail_times=0):
        self.sent: list[str] = []
        self._fail_times = fail_times

    async def send_telegram_message(self, text: str) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append(text)


@pytest.mark.asyncio
async def test_process_due_delivers_a_pending_notification(tmp_path):
    database = Database(tmp_path / "s.db")
    outbox, signals = SQLiteOutboxRepository(database), SQLiteSignalRepository(database)
    signal_id = await signals.register(_record())
    await outbox.enqueue("k1:NEW_SIGNAL", signal_id, "NEW_SIGNAL", "hola", now=datetime(2026, 1, 1, tzinfo=UTC))
    notifier = _Notifier()
    processor = OutboxProcessor(outbox, notifier, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    delivered = await processor.process_due()

    assert delivered == [signal_id]
    assert notifier.sent == ["hola"]
    assert await outbox.due(datetime(2026, 1, 1, tzinfo=UTC)) == []


@pytest.mark.asyncio
async def test_process_due_marks_failed_with_backoff_and_does_not_raise(tmp_path):
    database = Database(tmp_path / "s.db")
    outbox, signals = SQLiteOutboxRepository(database), SQLiteSignalRepository(database)
    signal_id = await signals.register(_record())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await outbox.enqueue("k1:NEW_SIGNAL", signal_id, "NEW_SIGNAL", "hola", now=now)
    notifier = _Notifier(fail_times=99)
    processor = OutboxProcessor(outbox, notifier, clock=lambda: now)

    delivered = await processor.process_due()

    assert delivered == []
    assert await outbox.due(now) == []  # backoff pushed next_attempt_at_utc into the future
    assert len(await outbox.due(now + timedelta(minutes=2))) == 1


@pytest.mark.asyncio
async def test_a_failed_delivery_never_creates_a_second_signal(tmp_path):
    """Section 7: Telegram failing must not cause a duplicate trading signal."""
    database = Database(tmp_path / "s.db")
    outbox, signals = SQLiteOutboxRepository(database), SQLiteSignalRepository(database)
    record = _record()
    await outbox.register_signal_with_notification(record, "k1:NEW_SIGNAL", "NEW_SIGNAL", "hola")
    notifier = _Notifier(fail_times=99)
    processor = OutboxProcessor(outbox, notifier, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    await processor.process_due()
    again = await outbox.register_signal_with_notification(record, "k1:NEW_SIGNAL", "NEW_SIGNAL", "hola")

    assert again is None
    assert len(await signals.get_all()) == 1
