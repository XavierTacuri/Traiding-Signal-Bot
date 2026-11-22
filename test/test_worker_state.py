"""Fase 5, section 5: WorkerRuntimeState — the one piece of state
app/workers/scanner.py writes to directly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.monitoring.models import WorkerStatus
from app.monitoring.worker_state import WorkerRuntimeState, new_instance_id


def test_new_instance_id_is_short_and_varies():
    first, second = new_instance_id(), new_instance_id()
    assert first != second
    assert 8 <= len(first) <= 16


def test_cycle_started_then_finished_records_duration_and_success():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state.cycle_started(start)
    state.cycle_finished(start + timedelta(seconds=5), success=True)

    assert state.scan_cycles_total == 1
    assert state.scan_cycles_failed == 0
    assert state.last_cycle_succeeded_at_utc == start + timedelta(seconds=5)
    assert state.last_cycle_duration_seconds == 5.0


def test_cycle_finished_with_failure_records_the_error_without_marking_success():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state.cycle_started(now)
    state.cycle_finished(now + timedelta(seconds=1), success=False, error="boom")

    assert state.scan_cycles_total == 1
    assert state.scan_cycles_failed == 1
    assert state.last_cycle_succeeded_at_utc is None
    assert state.recent_error == "boom"


def test_a_hanging_cycle_leaves_finished_unset_while_started_remains():
    """This divergence is exactly what the watchdog's "scanner_progress"
    check looks for (section 5/8): started advances, finished does not."""
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state.cycle_started(start)

    assert state.last_cycle_started_at_utc == start
    assert state.last_cycle_finished_at_utc is None


def test_signal_generated_and_rejected_counters():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    state.signal_generated()
    state.signal_generated()
    state.signal_rejected()

    assert state.signals_generated_total == 2
    assert state.signals_rejected_total == 1


def test_tracker_ran_updates_timestamp_and_accumulates_events():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state.tracker_ran(now, 2)
    state.tracker_ran(now + timedelta(minutes=1), 1)

    assert state.tracker_last_run_at_utc == now + timedelta(minutes=1)
    assert state.tracker_events_total == 3


def test_sqlite_error_increments_counter():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    state.sqlite_error()
    state.sqlite_error()
    assert state.sqlite_errors_total == 2


def test_default_status_is_starting():
    state = WorkerRuntimeState(instance_id="x", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    assert state.status == WorkerStatus.STARTING
