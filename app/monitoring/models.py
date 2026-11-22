"""Fase 5, section 4/5/10: observability data model.

Kept separate from app/models/signal_record.py: nothing here describes a
trading signal, and nothing in app/models is read by this package — nothing
here ever changes a signal's state either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkerStatus(StrEnum):
    """Self-reported by the worker (app/monitoring/heartbeat_service.py).

    The worker can report its own STARTING/HEALTHY/DEGRADED/STOPPING — but
    never STOPPED, because by the time it would be true the process writing
    it is gone. UNHEALTHY and STOPPED are inferred *externally* by the
    watchdog from a stale or missing heartbeat (section 4: a dead process
    cannot self-report its own death).
    """

    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    # Watchdog-only inferred values; the worker never writes these itself.
    UNHEALTHY = "UNHEALTHY"
    STOPPED = "STOPPED"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERY = "RECOVERY"


@dataclass(slots=True)
class HeartbeatSnapshot:
    """One row of the ``heartbeats`` table (single row, upserted — see
    app/monitoring/heartbeat_repository.py). Doubles as the operational
    metrics store (section 13): cumulative counters live here too, so no
    second table is needed for "metrics".
    """

    instance_id: str
    recorded_at_utc: datetime
    status: WorkerStatus
    last_cycle_started_at_utc: datetime | None = None
    last_cycle_finished_at_utc: datetime | None = None
    last_cycle_succeeded_at_utc: datetime | None = None
    last_cycle_duration_seconds: float | None = None
    scan_cycles_total: int = 0
    scan_cycles_failed: int = 0
    last_mt5_valid_read_at_utc: datetime | None = None
    mt5_connection_state: str | None = None
    mt5_consecutive_failures: int = 0
    mt5_disconnected_seconds_total: float = 0.0
    sqlite_ok: bool = True
    sqlite_errors_total: int = 0
    outbox_pending_count: int = 0
    outbox_failed_count: int = 0
    outbox_notifications_failed_total: int = 0
    tracker_last_run_at_utc: datetime | None = None
    tracker_active_signals: int = 0
    tracker_events_total: int = 0
    signals_generated_total: int = 0
    signals_rejected_total: int = 0
    recent_error: str | None = None


@dataclass(slots=True)
class DailyStats:
    """One row of ``daily_stats``, keyed by UTC calendar date."""

    date_utc: str
    cycles_total: int = 0
    cycles_failed: int = 0
    mt5_reconnections: int = 0
    critical_incidents: int = 0


@dataclass(slots=True)
class AlertIncident:
    """One row of ``alert_incidents`` (app/monitoring/alerts.py)."""

    incident_key: str
    component: str
    severity: AlertSeverity
    first_detected_at_utc: datetime
    status: str = "OPEN"
    id: int | None = None
    last_notified_at_utc: datetime | None = None
    notification_count: int = 0
    resolved_at_utc: datetime | None = None
    last_message: str | None = None
