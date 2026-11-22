"""Fase 5, section 6: heartbeat/daily_stats persistence — single-row
upsert, daily deltas, and the unique-per-day summary guard. Temporary
SQLite files only."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.monitoring.models import HeartbeatSnapshot, WorkerStatus
from app.repositories.database import Database


def _snapshot(**overrides) -> HeartbeatSnapshot:
    base = {"instance_id": "abc123", "recorded_at_utc": datetime(2026, 1, 1, tzinfo=UTC),
            "status": WorkerStatus.HEALTHY}
    base.update(overrides)
    return HeartbeatSnapshot(**base)


@pytest.fixture
def repo(tmp_path):
    return HeartbeatRepository(Database(tmp_path / "signals.db"))


@pytest.mark.asyncio
async def test_get_returns_none_before_any_upsert(repo):
    assert await repo.get() is None


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips(repo):
    await repo.upsert(_snapshot(scan_cycles_total=5, signals_generated_total=2))
    found = await repo.get()
    assert found is not None
    assert found.instance_id == "abc123"
    assert found.scan_cycles_total == 5
    assert found.signals_generated_total == 2
    assert found.status == WorkerStatus.HEALTHY


@pytest.mark.asyncio
async def test_upsert_replaces_the_single_row_rather_than_growing(repo):
    await repo.upsert(_snapshot(scan_cycles_total=1))
    await repo.upsert(_snapshot(scan_cycles_total=2))
    await repo.upsert(_snapshot(scan_cycles_total=3))

    found = await repo.get()
    assert found.scan_cycles_total == 3

    def _count(conn):
        return conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]

    count = await repo._db.run(_count)
    assert count == 1


@pytest.mark.asyncio
async def test_heartbeat_survives_a_simulated_restart(tmp_path):
    path = tmp_path / "signals.db"
    first_db = Database(path)
    await HeartbeatRepository(first_db).upsert(_snapshot(status=WorkerStatus.DEGRADED))
    await first_db.close()

    second_db = Database(path)
    recovered = await HeartbeatRepository(second_db).get()
    assert recovered is not None
    assert recovered.status == WorkerStatus.DEGRADED


@pytest.mark.asyncio
async def test_daily_stats_accumulate_across_multiple_deltas(repo):
    today = date(2026, 1, 1)
    await repo.upsert_daily_stats(today, cycles_delta=3, cycles_failed_delta=1)
    await repo.upsert_daily_stats(today, cycles_delta=2, reconnections_delta=1)

    stats = await repo.get_daily_stats(today)
    assert stats.cycles_total == 5
    assert stats.cycles_failed == 1
    assert stats.mt5_reconnections == 1


@pytest.mark.asyncio
async def test_daily_stats_for_an_unseen_date_defaults_to_zero(repo):
    stats = await repo.get_daily_stats(date(2099, 1, 1))
    assert stats.cycles_total == 0
    assert stats.mt5_reconnections == 0


@pytest.mark.asyncio
async def test_mark_daily_summary_sent_is_unique_per_date(repo):
    today = date(2026, 1, 1)
    first = await repo.mark_daily_summary_sent(today, datetime(2026, 1, 1, 7, tzinfo=UTC))
    second = await repo.mark_daily_summary_sent(today, datetime(2026, 1, 1, 7, 5, tzinfo=UTC))

    assert first is True
    assert second is False  # section 14: at most one summary per UTC date


@pytest.mark.asyncio
async def test_has_daily_summary_been_sent_reflects_the_mark(repo):
    today = date(2026, 1, 1)
    assert await repo.has_daily_summary_been_sent(today) is False
    await repo.mark_daily_summary_sent(today, datetime(2026, 1, 1, 7, tzinfo=UTC))
    assert await repo.has_daily_summary_been_sent(today) is True
