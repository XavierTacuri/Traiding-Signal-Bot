"""Fase 5, section 6: heartbeat/metrics persistence.

Chose SQLite (reusing app/repositories/database.py's gateway) over a
separate atomically-replaced state file:

- The dedicated-worker-thread connection, WAL mode and busy_timeout are
  already implemented, tested and proven in Fase 4 — a second persistence
  mechanism (file + its own atomic-rename/partial-write/versioning handling)
  would duplicate that work and its failure modes for no real benefit.
- The watchdog runs as a second OS process reading the same file; SQLite
  (unlike a bare JSON file) already defines safe concurrent-reader behaviour
  for exactly this shape (one writer, one or more read-only readers) via WAL.
- Section 12's alert-dedup bookkeeping (incident key, repetitions, cooldown)
  is naturally a small relational table, not something a single state file
  represents well without inventing its own mini-schema anyway.

The heartbeat is a single row (``id = 1``, upserted) — not a growing log —
so its storage cost never increases (section 3/9: bounded resource use).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from app.monitoring.models import DailyStats, HeartbeatSnapshot, WorkerStatus
from app.repositories.database import Database


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_snapshot(row: sqlite3.Row) -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        instance_id=row["instance_id"], recorded_at_utc=_parse(row["recorded_at_utc"]),
        status=WorkerStatus(row["status"]),
        last_cycle_started_at_utc=_parse(row["last_cycle_started_at_utc"]),
        last_cycle_finished_at_utc=_parse(row["last_cycle_finished_at_utc"]),
        last_cycle_succeeded_at_utc=_parse(row["last_cycle_succeeded_at_utc"]),
        last_cycle_duration_seconds=row["last_cycle_duration_seconds"],
        scan_cycles_total=row["scan_cycles_total"], scan_cycles_failed=row["scan_cycles_failed"],
        last_mt5_valid_read_at_utc=_parse(row["last_mt5_valid_read_at_utc"]),
        mt5_connection_state=row["mt5_connection_state"],
        mt5_consecutive_failures=row["mt5_consecutive_failures"],
        mt5_disconnected_seconds_total=row["mt5_disconnected_seconds_total"],
        sqlite_ok=bool(row["sqlite_ok"]), sqlite_errors_total=row["sqlite_errors_total"],
        outbox_pending_count=row["outbox_pending_count"], outbox_failed_count=row["outbox_failed_count"],
        outbox_notifications_failed_total=row["outbox_notifications_failed_total"],
        tracker_last_run_at_utc=_parse(row["tracker_last_run_at_utc"]),
        tracker_active_signals=row["tracker_active_signals"],
        tracker_events_total=row["tracker_events_total"],
        signals_generated_total=row["signals_generated_total"],
        signals_rejected_total=row["signals_rejected_total"], recent_error=row["recent_error"],
    )


class HeartbeatRepository:
    """Worker-side writer (async, via the shared Database gateway)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert(self, snapshot: HeartbeatSnapshot) -> None:
        await self._db.run(self._upsert_sync, snapshot)

    @staticmethod
    def _upsert_sync(conn: sqlite3.Connection, snapshot: HeartbeatSnapshot) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO heartbeats (id, instance_id, recorded_at_utc, status, "
                "last_cycle_started_at_utc, last_cycle_finished_at_utc, last_cycle_succeeded_at_utc, "
                "last_cycle_duration_seconds, scan_cycles_total, scan_cycles_failed, "
                "last_mt5_valid_read_at_utc, mt5_connection_state, mt5_consecutive_failures, "
                "mt5_disconnected_seconds_total, sqlite_ok, sqlite_errors_total, "
                "outbox_pending_count, outbox_failed_count, outbox_notifications_failed_total, "
                "tracker_last_run_at_utc, tracker_active_signals, tracker_events_total, "
                "signals_generated_total, signals_rejected_total, recent_error) "
                "VALUES (1, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "instance_id=excluded.instance_id, recorded_at_utc=excluded.recorded_at_utc, "
                "status=excluded.status, last_cycle_started_at_utc=excluded.last_cycle_started_at_utc, "
                "last_cycle_finished_at_utc=excluded.last_cycle_finished_at_utc, "
                "last_cycle_succeeded_at_utc=excluded.last_cycle_succeeded_at_utc, "
                "last_cycle_duration_seconds=excluded.last_cycle_duration_seconds, "
                "scan_cycles_total=excluded.scan_cycles_total, "
                "scan_cycles_failed=excluded.scan_cycles_failed, "
                "last_mt5_valid_read_at_utc=excluded.last_mt5_valid_read_at_utc, "
                "mt5_connection_state=excluded.mt5_connection_state, "
                "mt5_consecutive_failures=excluded.mt5_consecutive_failures, "
                "mt5_disconnected_seconds_total=excluded.mt5_disconnected_seconds_total, "
                "sqlite_ok=excluded.sqlite_ok, sqlite_errors_total=excluded.sqlite_errors_total, "
                "outbox_pending_count=excluded.outbox_pending_count, "
                "outbox_failed_count=excluded.outbox_failed_count, "
                "outbox_notifications_failed_total=excluded.outbox_notifications_failed_total, "
                "tracker_last_run_at_utc=excluded.tracker_last_run_at_utc, "
                "tracker_active_signals=excluded.tracker_active_signals, "
                "tracker_events_total=excluded.tracker_events_total, "
                "signals_generated_total=excluded.signals_generated_total, "
                "signals_rejected_total=excluded.signals_rejected_total, "
                "recent_error=excluded.recent_error",
                (snapshot.instance_id, _iso(snapshot.recorded_at_utc), str(snapshot.status),
                 _iso(snapshot.last_cycle_started_at_utc) if snapshot.last_cycle_started_at_utc else None,
                 _iso(snapshot.last_cycle_finished_at_utc) if snapshot.last_cycle_finished_at_utc else None,
                 _iso(snapshot.last_cycle_succeeded_at_utc) if snapshot.last_cycle_succeeded_at_utc else None,
                 snapshot.last_cycle_duration_seconds, snapshot.scan_cycles_total,
                 snapshot.scan_cycles_failed,
                 _iso(snapshot.last_mt5_valid_read_at_utc) if snapshot.last_mt5_valid_read_at_utc else None,
                 snapshot.mt5_connection_state, snapshot.mt5_consecutive_failures,
                 snapshot.mt5_disconnected_seconds_total, int(snapshot.sqlite_ok),
                 snapshot.sqlite_errors_total, snapshot.outbox_pending_count,
                 snapshot.outbox_failed_count, snapshot.outbox_notifications_failed_total,
                 _iso(snapshot.tracker_last_run_at_utc) if snapshot.tracker_last_run_at_utc else None,
                 snapshot.tracker_active_signals, snapshot.tracker_events_total,
                 snapshot.signals_generated_total, snapshot.signals_rejected_total,
                 snapshot.recent_error),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    async def get(self) -> HeartbeatSnapshot | None:
        return await self._db.run(self._get_sync)

    @staticmethod
    def _get_sync(conn: sqlite3.Connection) -> HeartbeatSnapshot | None:
        row = conn.execute("SELECT * FROM heartbeats WHERE id = 1").fetchone()
        return _row_to_snapshot(row) if row is not None else None

    async def upsert_daily_stats(self, day: date, *, cycles_delta: int = 0,
                                  cycles_failed_delta: int = 0, reconnections_delta: int = 0,
                                  critical_incidents_delta: int = 0) -> None:
        if not any((cycles_delta, cycles_failed_delta, reconnections_delta, critical_incidents_delta)):
            return
        await self._db.run(self._upsert_daily_stats_sync, day.isoformat(), cycles_delta,
                            cycles_failed_delta, reconnections_delta, critical_incidents_delta)

    @staticmethod
    def _upsert_daily_stats_sync(conn: sqlite3.Connection, date_utc: str, cycles_delta: int,
                                  cycles_failed_delta: int, reconnections_delta: int,
                                  critical_incidents_delta: int) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO daily_stats (date_utc, cycles_total, cycles_failed, "
                "mt5_reconnections, critical_incidents) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(date_utc) DO UPDATE SET "
                "cycles_total = cycles_total + excluded.cycles_total, "
                "cycles_failed = cycles_failed + excluded.cycles_failed, "
                "mt5_reconnections = mt5_reconnections + excluded.mt5_reconnections, "
                "critical_incidents = critical_incidents + excluded.critical_incidents",
                (date_utc, cycles_delta, cycles_failed_delta, reconnections_delta,
                 critical_incidents_delta),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    async def get_daily_stats(self, day: date) -> DailyStats:
        return await self._db.run(self._get_daily_stats_sync, day.isoformat())

    @staticmethod
    def _get_daily_stats_sync(conn: sqlite3.Connection, date_utc: str) -> DailyStats:
        row = conn.execute("SELECT * FROM daily_stats WHERE date_utc = ?", (date_utc,)).fetchone()
        if row is None:
            return DailyStats(date_utc=date_utc)
        return DailyStats(date_utc=row["date_utc"], cycles_total=row["cycles_total"],
                           cycles_failed=row["cycles_failed"], mt5_reconnections=row["mt5_reconnections"],
                           critical_incidents=row["critical_incidents"])

    async def has_daily_summary_been_sent(self, day: date) -> bool:
        return await self._db.run(self._has_summary_sent_sync, day.isoformat())

    @staticmethod
    def _has_summary_sent_sync(conn: sqlite3.Connection, date_utc: str) -> bool:
        row = conn.execute("SELECT 1 FROM daily_summaries_sent WHERE date_utc = ?", (date_utc,)).fetchone()
        return row is not None

    async def mark_daily_summary_sent(self, day: date, sent_at: datetime) -> bool:
        """Returns False (no-op) if this date's summary was already sent —
        section 14's uniqueness guarantee, enforced by the PRIMARY KEY."""
        return await self._db.run(self._mark_summary_sent_sync, day.isoformat(), sent_at)

    @staticmethod
    def _mark_summary_sent_sync(conn: sqlite3.Connection, date_utc: str, sent_at: datetime) -> bool:
        try:
            conn.execute("INSERT INTO daily_summaries_sent (date_utc, sent_at_utc) VALUES (?, ?)",
                         (date_utc, _iso(sent_at)))
            return True
        except sqlite3.IntegrityError:
            return False
