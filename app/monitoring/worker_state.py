"""Fase 5, section 5: in-process, mutable runtime state.

This is the one piece of plain shared state app/workers/scanner.py writes
to directly (a handful of one-line calls at existing log points — see the
module docstring there). ``HeartbeatService`` only *reads* it on its own
timer and persists a snapshot; it never advances any of these fields itself.

This split is what makes "heartbeat de vida" and "heartbeat de progreso"
genuinely different signals (section 5 A/B): the heartbeat task's own
execution never counts as progress — only scan_once() actually finishing
does. If scan_once() hangs on a stuck MT5 call (Fase 3's documented
limitation), ``last_cycle_started_at_utc`` stops being followed by a new
``last_cycle_finished_at_utc`` while the heartbeat keeps ticking — exactly
the divergence the watchdog looks for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.monitoring.models import WorkerStatus


@dataclass(slots=True)
class WorkerRuntimeState:
    instance_id: str
    started_at_utc: datetime
    status: WorkerStatus = WorkerStatus.STARTING
    last_cycle_started_at_utc: datetime | None = None
    last_cycle_finished_at_utc: datetime | None = None
    last_cycle_succeeded_at_utc: datetime | None = None
    last_cycle_duration_seconds: float | None = None
    scan_cycles_total: int = 0
    scan_cycles_failed: int = 0
    last_mt5_valid_read_at_utc: datetime | None = None
    tracker_last_run_at_utc: datetime | None = None
    tracker_events_total: int = 0
    signals_generated_total: int = 0
    signals_rejected_total: int = 0
    sqlite_errors_total: int = 0
    recent_error: str | None = None

    def cycle_started(self, now: datetime) -> None:
        self.last_cycle_started_at_utc = now

    def cycle_finished(self, now: datetime, *, success: bool, error: str | None = None) -> None:
        self.last_cycle_finished_at_utc = now
        self.scan_cycles_total += 1
        if self.last_cycle_started_at_utc is not None:
            self.last_cycle_duration_seconds = (now - self.last_cycle_started_at_utc).total_seconds()
        if success:
            self.last_cycle_succeeded_at_utc = now
        else:
            self.scan_cycles_failed += 1
            if error:
                self.recent_error = error

    def mt5_read_ok(self, now: datetime) -> None:
        self.last_mt5_valid_read_at_utc = now

    def signal_generated(self) -> None:
        self.signals_generated_total += 1

    def signal_rejected(self) -> None:
        self.signals_rejected_total += 1

    def tracker_ran(self, now: datetime, events_count: int) -> None:
        # tracker_active_signals is deliberately NOT set here: HeartbeatService
        # already queries SignalRepository.get_active() live for that figure,
        # which is strictly more accurate than caching a count here that could
        # go stale between tracker passes.
        self.tracker_last_run_at_utc = now
        self.tracker_events_total += events_count

    def sqlite_error(self) -> None:
        self.sqlite_errors_total += 1


def new_instance_id() -> str:
    return uuid.uuid4().hex[:12]


runtime_state = WorkerRuntimeState(instance_id=new_instance_id(), started_at_utc=datetime.now(UTC))
