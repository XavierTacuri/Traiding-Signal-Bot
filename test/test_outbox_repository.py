"""Fase 4, section 7: the transactional outbox — atomic signal+notification
registration, idempotent enqueue, due-item selection. Temporary SQLite only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.signal_record import OutboxStatus, SignalRecord, SignalStatus
from app.repositories.database import Database
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.signal_repository import SQLiteSignalRepository


def _record(key="k1") -> SignalRecord:
    return SignalRecord(
        signal_key=key, symbol="EURUSD", direction="BUY", timeframe="M15",
        entry_price="1.10000", stop_loss="1.09500", take_profit="1.11000",
        risk_distance="0.00500", digits=5, created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at_utc=datetime(2026, 1, 1, 1, tzinfo=UTC), status=SignalStatus.PENDING_ACTIVATION,
    )


@pytest.fixture
def outbox(tmp_path):
    database = Database(tmp_path / "signals.db")
    return SQLiteOutboxRepository(database), SQLiteSignalRepository(database)


@pytest.mark.asyncio
async def test_register_signal_with_notification_writes_both_rows_atomically(outbox):
    repo, signals = outbox
    result = await repo.register_signal_with_notification(_record(), "k1:NEW_SIGNAL", "NEW_SIGNAL", "hello")
    assert result is not None
    signal_id, outbox_id = result
    assert signal_id is not None and outbox_id is not None
    record = await signals.get_by_key("k1")
    assert record is not None and record.id == signal_id
    due = await repo.due(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(due) == 1 and due[0].id == outbox_id and due[0].payload == "hello"


@pytest.mark.asyncio
async def test_register_signal_with_notification_is_idempotent_on_duplicate_signal_key(outbox):
    repo, _signals = outbox
    first = await repo.register_signal_with_notification(_record(), "k1:NEW_SIGNAL", "NEW_SIGNAL", "a")
    second = await repo.register_signal_with_notification(_record(), "k1:NEW_SIGNAL", "NEW_SIGNAL", "b")
    assert first is not None
    assert second is None
    due = await repo.due(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(due) == 1  # the second attempt enqueued nothing


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_event_key(outbox):
    """Section 7/14: a repeated tracker pass reaching the same transition
    twice must enqueue the result notification only once."""
    repo, signals = outbox
    signal_id = await signals.register(_record())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = await repo.enqueue("k1:TAKE_PROFIT", signal_id, "TAKE_PROFIT", "tp hit", now=now)
    second = await repo.enqueue("k1:TAKE_PROFIT", signal_id, "TAKE_PROFIT", "tp hit again", now=now)
    assert first is not None
    assert second is None
    due = await repo.due(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(due) == 1 and due[0].payload == "tp hit"


@pytest.mark.asyncio
async def test_due_excludes_items_not_yet_at_their_next_attempt_time(outbox):
    repo, signals = outbox
    signal_id = await signals.register(_record())
    await repo.enqueue("k1:NEW_SIGNAL", signal_id, "NEW_SIGNAL", "msg", now=datetime(2026, 1, 1, tzinfo=UTC))
    [item] = await repo.due(datetime(2026, 1, 1, tzinfo=UTC))
    await repo.mark_failed(item.id, 1, datetime(2026, 1, 1, 1, 0, tzinfo=UTC), "boom")

    not_yet = await repo.due(datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    now_due = await repo.due(datetime(2026, 1, 1, 1, 0, tzinfo=UTC))

    assert not_yet == []
    assert len(now_due) == 1 and now_due[0].status == OutboxStatus.FAILED and now_due[0].attempts == 1


@pytest.mark.asyncio
async def test_mark_sent_removes_the_item_from_due(outbox):
    repo, signals = outbox
    signal_id = await signals.register(_record())
    item_id = await repo.enqueue("k1:NEW_SIGNAL", signal_id, "NEW_SIGNAL", "msg",
                                   now=datetime(2026, 1, 1, tzinfo=UTC))
    await repo.mark_sent(item_id, datetime(2026, 1, 1, tzinfo=UTC))
    assert await repo.due(datetime(2026, 1, 1, tzinfo=UTC)) == []


@pytest.mark.asyncio
async def test_outbox_survives_a_simulated_restart(tmp_path):
    path = tmp_path / "signals.db"
    db1 = Database(path)
    repo1 = SQLiteOutboxRepository(db1)
    await repo1.register_signal_with_notification(_record(), "k1:NEW_SIGNAL", "NEW_SIGNAL", "hello")
    await db1.close()

    db2 = Database(path)
    repo2 = SQLiteOutboxRepository(db2)
    due = await repo2.due(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(due) == 1 and due[0].payload == "hello"
    await db2.close()
