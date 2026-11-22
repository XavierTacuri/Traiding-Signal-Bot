"""Fase 5, section 4/5/6/13: HeartbeatService — status computation,
liveness vs progress, MT5/SQLite/outbox observability, daily deltas."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.monitoring.heartbeat_service import HeartbeatService, compute_self_status
from app.monitoring.models import WorkerStatus
from app.monitoring.worker_state import WorkerRuntimeState
from app.repositories.database import Database, DatabaseError
from app.services.mt5_connection import ConnectionState


class _FakeConnection:
    def __init__(self, state=ConnectionState.CONNECTED, consecutive_failures=0):
        self.state = state
        self.consecutive_failures = consecutive_failures


class _FakeOutbox:
    def __init__(self, pending=0, failed=0, error=None):
        self._result = (pending, failed)
        self._error = error

    async def count_pending_and_failed(self):
        if self._error:
            raise self._error
        return self._result


class _FakeSignals:
    def __init__(self, active=0, error=None):
        self._active = [object()] * active
        self._error = error

    async def get_active(self):
        if self._error:
            raise self._error
        return self._active


def _state(**overrides) -> WorkerRuntimeState:
    base = {"instance_id": "abc", "started_at_utc": datetime(2026, 1, 1, tzinfo=UTC)}
    base.update(overrides)
    return WorkerRuntimeState(**base)


def test_compute_self_status_is_starting_before_any_cycle_finishes():
    assert compute_self_status(_state(), ConnectionState.CONNECTED) == WorkerStatus.STARTING


def test_compute_self_status_is_healthy_after_a_cycle_finished_and_mt5_connected():
    state = _state()
    state.cycle_finished(datetime(2026, 1, 1, tzinfo=UTC), success=True)
    assert compute_self_status(state, ConnectionState.CONNECTED) == WorkerStatus.HEALTHY


def test_compute_self_status_is_degraded_while_mt5_is_reconnecting():
    state = _state()
    state.cycle_finished(datetime(2026, 1, 1, tzinfo=UTC), success=True)
    assert compute_self_status(state, ConnectionState.RECONNECTING) == WorkerStatus.DEGRADED
    assert compute_self_status(state, ConnectionState.DEGRADED) == WorkerStatus.DEGRADED


def test_compute_self_status_reports_stopping_once_set_regardless_of_mt5():
    state = _state(status=WorkerStatus.STOPPING)
    state.cycle_finished(datetime(2026, 1, 1, tzinfo=UTC), success=True)
    assert compute_self_status(state, ConnectionState.CONNECTED) == WorkerStatus.STOPPING


def test_compute_self_status_never_returns_unhealthy_or_stopped():
    """Section 4: the worker can never self-report these — only the
    watchdog infers them, from outside, once the worker is unreachable."""
    for mt5 in (ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, ConnectionState.DEGRADED):
        assert compute_self_status(_state(), mt5) not in (WorkerStatus.UNHEALTHY, WorkerStatus.STOPPED)


@pytest.mark.asyncio
async def test_tick_persists_a_snapshot_matching_the_runtime_state(tmp_path):
    database = Database(tmp_path / "s.db")
    state = _state()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state.cycle_started(now)
    state.cycle_finished(now + timedelta(seconds=2), success=True)
    service = HeartbeatService(database, state, connection=_FakeConnection(),
                                outbox_repository=_FakeOutbox(pending=1, failed=0),
                                signal_repository=_FakeSignals(active=3),
                                clock=lambda: now + timedelta(seconds=3))

    snapshot = await service.tick()

    assert snapshot.status == WorkerStatus.HEALTHY
    assert snapshot.scan_cycles_total == 1
    assert snapshot.outbox_pending_count == 1
    assert snapshot.tracker_active_signals == 3
    persisted = await HeartbeatRepository(database).get()
    assert persisted.instance_id == state.instance_id


@pytest.mark.asyncio
async def test_tick_marks_sqlite_not_ok_when_outbox_query_fails(tmp_path):
    database = Database(tmp_path / "s.db")
    service = HeartbeatService(database, _state(), outbox_repository=_FakeOutbox(error=DatabaseError("locked")),
                                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    snapshot = await service.tick()

    assert snapshot.sqlite_ok is False
    assert snapshot.sqlite_errors_total == 1


@pytest.mark.asyncio
async def test_mt5_disconnected_seconds_accumulate_across_ticks(tmp_path):
    database = Database(tmp_path / "s.db")
    connection = _FakeConnection(state=ConnectionState.RECONNECTING)
    clock_values = iter([
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC),
    ])
    monotonic_values = iter([0.0, 30.0, 60.0])
    service = HeartbeatService(database, _state(), connection=connection,
                                clock=lambda: next(clock_values), monotonic=lambda: next(monotonic_values))

    await service.tick()
    second = await service.tick()
    third = await service.tick()

    assert second.mt5_disconnected_seconds_total == 30.0
    assert third.mt5_disconnected_seconds_total == 60.0


@pytest.mark.asyncio
async def test_daily_stats_capture_cycle_deltas_between_ticks(tmp_path):
    database = Database(tmp_path / "s.db")
    state = _state()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    service = HeartbeatService(database, state, clock=lambda: now)

    await service.tick()
    state.cycle_finished(now, success=True)
    state.cycle_finished(now, success=False, error="x")
    await service.tick()

    stats = await HeartbeatRepository(database).get_daily_stats(date(2026, 1, 1))
    assert stats.cycles_total == 2
    assert stats.cycles_failed == 1


@pytest.mark.asyncio
async def test_daily_stats_count_a_reconnection_on_recovery_transition(tmp_path):
    database = Database(tmp_path / "s.db")
    connection = _FakeConnection(state=ConnectionState.RECONNECTING)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    service = HeartbeatService(database, _state(), connection=connection, clock=lambda: now)

    await service.tick()  # still disconnected
    connection.state = ConnectionState.CONNECTED
    await service.tick()  # recovered -> one reconnection counted

    stats = await HeartbeatRepository(database).get_daily_stats(date(2026, 1, 1))
    assert stats.mt5_reconnections == 1


@pytest.mark.asyncio
async def test_tick_invokes_the_optional_daily_summary_check(tmp_path):
    database = Database(tmp_path / "s.db")
    calls = []

    class _FakeSummary:
        async def maybe_send(self):
            calls.append(True)

    service = HeartbeatService(database, _state(), daily_summary=_FakeSummary(),
                                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    await service.tick()

    assert calls == [True]
