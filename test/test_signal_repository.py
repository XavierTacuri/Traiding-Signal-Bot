"""Fase 4, sections 3/6/9: persistent signal storage, deduplication,
idempotent state transitions and the audit trail. Temporary SQLite only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.signal_record import SignalRecord, SignalStatus
from app.repositories.database import Database
from app.repositories.signal_repository import SQLiteSignalRepository


def _record(key="EURUSD:M15:BUY:2026-01-01T12:00:00+00:00", **overrides) -> SignalRecord:
    base = {
        "signal_key": key, "symbol": "EURUSD", "direction": "BUY", "timeframe": "M15",
        "entry_price": "1.10000", "stop_loss": "1.09500", "take_profit": "1.11000",
        "risk_distance": "0.00500", "digits": 5, "created_at_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_at_utc": datetime(2026, 1, 1, 1, tzinfo=UTC), "status": SignalStatus.PENDING_ACTIVATION,
    }
    base.update(overrides)
    return SignalRecord(**base)


@pytest.fixture
def repo(tmp_path):
    # Plain (non-async) fixture: pytest-asyncio here runs in strict mode (see
    # pytest.ini), which requires pytest_asyncio.fixture for an async one —
    # avoided by keeping Database construction synchronous (it only opens
    # the file lazily on first `run()`/`initialize()` call).
    database = Database(tmp_path / "signals.db")
    return SQLiteSignalRepository(database)


@pytest.mark.asyncio
async def test_register_then_exists_and_get_by_key(repo):
    signal_id = await repo.register(_record())
    assert signal_id is not None
    assert await repo.exists("EURUSD:M15:BUY:2026-01-01T12:00:00+00:00") is True
    found = await repo.get_by_key("EURUSD:M15:BUY:2026-01-01T12:00:00+00:00")
    assert found is not None and found.id == signal_id
    assert found.status == SignalStatus.PENDING_ACTIVATION


@pytest.mark.asyncio
async def test_register_the_same_signal_key_twice_is_idempotent(repo):
    """Section 6: a repeated scan or a restart must never create a second
    trading signal for the same event — UNIQUE signal_key, no exception."""
    first = await repo.register(_record())
    second = await repo.register(_record())
    assert first is not None
    assert second is None  # no-op, not an error, not a duplicate row
    rows = await repo.get_all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_exists_is_false_for_an_unknown_key(repo):
    assert await repo.exists("does-not-exist") is False


@pytest.mark.asyncio
async def test_activate_transitions_pending_to_active(repo):
    signal_id = await repo.register(_record())
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    activated = await repo.activate(signal_id, "1.10010", "ASK", now)
    assert activated is True
    record = await repo.get_by_key(_record().signal_key)
    assert record.status == SignalStatus.ACTIVE
    assert record.activation_price == "1.10010"
    assert record.activation_side == "ASK"
    assert record.activated_at_utc == now


@pytest.mark.asyncio
async def test_activate_is_idempotent_against_a_repeated_call(repo):
    signal_id = await repo.register(_record())
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    first = await repo.activate(signal_id, "1.10010", "ASK", now)
    second = await repo.activate(signal_id, "1.20000", "ASK", now)  # already ACTIVE
    assert first is True
    assert second is False
    record = await repo.get_by_key(_record().signal_key)
    assert record.activation_price == "1.10010"  # untouched by the second call


@pytest.mark.asyncio
async def test_close_transitions_active_to_take_profit_with_result(repo):
    signal_id = await repo.register(_record())
    await repo.activate(signal_id, "1.10010", "ASK", datetime(2026, 1, 1, 12, 5, tzinfo=UTC))
    closed_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    closed = await repo.close(signal_id, SignalStatus.TAKE_PROFIT, closed_at, 100.0, 2.0,
                               "TAKE_PROFIT", closed_at)

    assert closed is True
    record = await repo.get_by_key(_record().signal_key)
    assert record.status == SignalStatus.TAKE_PROFIT
    assert record.result_pips == 100.0 and record.result_r == 2.0
    assert record.close_reason == "TAKE_PROFIT"
    assert record.closed_at_utc == closed_at


@pytest.mark.asyncio
async def test_a_closed_signal_cannot_be_closed_again(repo):
    """Section 9: 'una señal finalizada no debe volver a cerrarse'."""
    signal_id = await repo.register(_record())
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    await repo.activate(signal_id, "1.10010", "ASK", now)
    first = await repo.close(signal_id, SignalStatus.TAKE_PROFIT, now, 100.0, 2.0, "TAKE_PROFIT", now)
    second = await repo.close(signal_id, SignalStatus.STOP_LOSS, now, -50.0, -1.0, "STOP_LOSS", now)

    assert first is True
    assert second is False
    record = await repo.get_by_key(_record().signal_key)
    assert record.status == SignalStatus.TAKE_PROFIT  # unchanged by the rejected second close
    assert record.result_r == 2.0


@pytest.mark.asyncio
async def test_close_can_expire_a_signal_still_pending_activation(repo):
    signal_id = await repo.register(_record())
    now = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    closed = await repo.close(signal_id, SignalStatus.EXPIRED, now, None, None,
                               "NEVER_ACTIVATED_EXPIRED", None)
    assert closed is True
    record = await repo.get_by_key(_record().signal_key)
    assert record.status == SignalStatus.EXPIRED
    assert record.result_pips is None and record.result_r is None


@pytest.mark.asyncio
async def test_get_pending_activation_and_get_active_filter_by_status(repo):
    await repo.register(_record(key="pending-1"))
    active_id = await repo.register(_record(key="active-1"))
    await repo.activate(active_id, "1.1", "ASK", datetime.now(UTC))

    pending = await repo.get_pending_activation()
    active = await repo.get_active()

    assert [r.signal_key for r in pending] == ["pending-1"]
    assert [r.signal_key for r in active] == ["active-1"]


@pytest.mark.asyncio
async def test_update_last_checked_persists_the_bar_time(repo):
    signal_id = await repo.register(_record())
    bar_time = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    await repo.update_last_checked(signal_id, bar_time)
    record = await repo.get_by_key(_record().signal_key)
    assert record.last_checked_bar_time_utc == bar_time


@pytest.mark.asyncio
async def test_signal_state_survives_a_simulated_restart(tmp_path):
    path = tmp_path / "signals.db"
    first_db = Database(path)
    first_repo = SQLiteSignalRepository(first_db)
    signal_id = await first_repo.register(_record())
    await first_repo.activate(signal_id, "1.1001", "ASK", datetime(2026, 1, 1, 12, 5, tzinfo=UTC))
    await first_db.close()

    second_db = Database(path)
    second_repo = SQLiteSignalRepository(second_db)
    recovered = await second_repo.get_active()
    assert len(recovered) == 1
    assert recovered[0].activation_price == "1.1001"
    await second_db.close()
