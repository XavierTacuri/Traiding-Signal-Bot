"""Fase 5, section 12: watchdog-side incident bookkeeping, deduplication and
cooldown — "MT5 se desconecta durante diez minutos" must produce exactly
one WARNING/CRITICAL and, later, exactly one RECOVERY, never one message
per check cycle in between.

``AlertRepository`` is synchronous and opens its own ``sqlite3`` connection:
the watchdog (app/monitoring/watchdog.py) is a separate OS process with no
event loop of its own (section 7), so there is no async gateway to share
and no cross-thread connection-sharing risk to guard against here — this
connection is only ever touched from the watchdog's own single thread.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.monitoring.models import AlertIncident, AlertSeverity


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_incident(row: sqlite3.Row) -> AlertIncident:
    return AlertIncident(
        id=row["id"], incident_key=row["incident_key"], component=row["component"],
        severity=AlertSeverity(row["severity"]), first_detected_at_utc=_parse(row["first_detected_at_utc"]),
        status=row["status"], last_notified_at_utc=_parse(row["last_notified_at_utc"]),
        notification_count=row["notification_count"], resolved_at_utc=_parse(row["resolved_at_utc"]),
        last_message=row["last_message"],
    )


class AlertRepository:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        self._conn.close()

    def get_open(self, incident_key: str) -> AlertIncident | None:
        row = self._conn.execute(
            "SELECT * FROM alert_incidents WHERE incident_key = ? AND status = 'OPEN'", (incident_key,),
        ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def open_incident(self, incident_key: str, component: str, severity: AlertSeverity,
                       now: datetime, message: str) -> AlertIncident:
        """Idempotent and reusable: ``incident_key`` is a persistent slot
        (the watchdog only ever uses a handful of fixed keys — section 13's
        "evitar alta cardinalidad" applies here too), not a one-off row per
        episode. Reopening a previously RESOLVED incident resets it to a
        fresh OPEN state (section 12: the same condition recurring weeks
        later must raise a new, properly-cooled-down alert, not be blocked
        by history). An already-OPEN incident is left untouched — this is
        also what makes the call idempotent across a watchdog restart."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO alert_incidents (incident_key, component, severity, "
                "first_detected_at_utc, status, notification_count, last_message) "
                "VALUES (?, ?, ?, ?, 'OPEN', 0, ?) "
                "ON CONFLICT(incident_key) DO UPDATE SET "
                "component = excluded.component, severity = excluded.severity, "
                "first_detected_at_utc = excluded.first_detected_at_utc, status = 'OPEN', "
                "notification_count = 0, last_notified_at_utc = NULL, resolved_at_utc = NULL, "
                "last_message = excluded.last_message "
                "WHERE alert_incidents.status != 'OPEN'",
                (incident_key, component, str(severity), _iso(now), message),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            self._conn.execute("ROLLBACK")
            raise
        return self.get_open(incident_key)

    def record_notification(self, incident_key: str, now: datetime, message: str) -> None:
        self._conn.execute(
            "UPDATE alert_incidents SET last_notified_at_utc = ?, "
            "notification_count = notification_count + 1, last_message = ? "
            "WHERE incident_key = ? AND status = 'OPEN'",
            (_iso(now), message, incident_key),
        )

    def resolve(self, incident_key: str, now: datetime) -> AlertIncident | None:
        """Returns the incident that was just resolved, or None if there
        was no OPEN incident with this key (idempotent no-op — resolving
        something that was never open, or already resolved, is not an
        error and must not send a second RECOVERY message)."""
        incident = self.get_open(incident_key)
        if incident is None:
            return None
        self._conn.execute(
            "UPDATE alert_incidents SET status = 'RESOLVED', resolved_at_utc = ? "
            "WHERE incident_key = ? AND status = 'OPEN'",
            (_iso(now), incident_key),
        )
        return incident

    def count_open_critical_today(self, today: str) -> int:
        """Used by the daily summary (section 14) via the worker, which
        only ever reads this table."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM alert_incidents WHERE severity = 'CRITICAL' "
            "AND substr(first_detected_at_utc, 1, 10) = ?", (today,),
        ).fetchone()[0]


class AlertManager:
    """Decides whether a detected condition actually results in a Telegram
    send, applying open/cooldown/reminder-cap/resolve rules (section 12)."""

    def __init__(self, repository: AlertRepository, sender: Any, config: Any,
                 clock=lambda: datetime.now(UTC)) -> None:
        self._repo = repository
        self._sender = sender
        self._config = config
        self._now = clock

    def raise_alert(self, incident_key: str, component: str, severity: AlertSeverity, message: str) -> None:
        now = self._now()
        incident = self._repo.get_open(incident_key)
        if incident is None:
            incident = self._repo.open_incident(incident_key, component, severity, now, message)
            self._send_and_record(incident_key, severity, component, message, now)
            return
        elapsed = (
            (now - incident.last_notified_at_utc).total_seconds()
            if incident.last_notified_at_utc else float("inf")
        )
        is_reminder_eligible = (
            severity == AlertSeverity.CRITICAL
            and elapsed >= self._config.ALERT_COOLDOWN_SECONDS
            and incident.notification_count <= self._config.ALERT_MAX_REMINDERS
        )
        if is_reminder_eligible:
            self._send_and_record(incident_key, severity, component, message, now)
        # else: condition persists but we are within cooldown, or this
        # severity gets no reminders, or the reminder cap was reached —
        # deliberately silent here (brief section 12: no per-cycle spam).

    def resolve_alert(self, incident_key: str, message: str) -> None:
        now = self._now()
        incident = self._repo.resolve(incident_key, now)
        if incident is None:
            return  # nothing was open; not a real recovery, no message
        self._send(incident.component, AlertSeverity.RECOVERY, message, now)

    def _send_and_record(self, incident_key: str, severity: AlertSeverity, component: str,
                          message: str, now: datetime) -> None:
        if self._send(component, severity, message, now):
            self._repo.record_notification(incident_key, now, message)

    def _send(self, component: str, severity: AlertSeverity, message: str, now: datetime) -> bool:
        text = (
            f"[{severity.value}] {component}\n{message}\n"
            f"Hora UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return bool(self._sender(text))
