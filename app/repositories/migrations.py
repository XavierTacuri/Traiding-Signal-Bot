"""Fase 4: schema creation and versioning for the local SQLite database.

Versioning uses ``PRAGMA user_version`` (a plain integer stored in the SQLite
file header) instead of a separate migrations table: it needs no bootstrap
query to discover whether it exists, and reading it never fails even on a
brand-new (version 0) database.

Each migration is a tuple of DDL statements applied inside one explicit
transaction. ``CREATE TABLE/INDEX IF NOT EXISTS`` make re-running migration 1
against a database that already has it a safe no-op (defence in depth; the
``user_version`` check already skips migrations that were applied before).
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS pending_setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_identifier TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            detected_at_utc TEXT NOT NULL,
            detected_bar_time_utc TEXT NOT NULL,
            expires_after_bars INTEGER NOT NULL,
            pullback_low TEXT,
            pullback_high TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )""",
        ("CREATE INDEX IF NOT EXISTS idx_pending_setups_lookup "
         "ON pending_setups(symbol, direction, status)"),

        """CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price TEXT NOT NULL,
            stop_loss TEXT NOT NULL,
            take_profit TEXT NOT NULL,
            risk_distance TEXT NOT NULL,
            digits INTEGER NOT NULL,
            confidence_score INTEGER,
            activation_price TEXT,
            activation_side TEXT,
            created_at_utc TEXT NOT NULL,
            sent_at_utc TEXT,
            activated_at_utc TEXT,
            closed_at_utc TEXT,
            expires_at_utc TEXT,
            last_checked_bar_time_utc TEXT,
            status TEXT NOT NULL,
            result_pips REAL,
            result_r REAL,
            close_reason TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)",
        "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)",

        """CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            message_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            sent_at_utc TEXT,
            last_error TEXT
        )""",
        ("CREATE INDEX IF NOT EXISTS idx_outbox_due "
         "ON notification_outbox(status, next_attempt_at_utc)"),
        "CREATE INDEX IF NOT EXISTS idx_outbox_signal_id ON notification_outbox(signal_id)",

        """CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            metadata TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_signal_events_signal_id ON signal_events(signal_id)",
    ),
    2: (
        # Single-row table (id is always 1, enforced by CHECK): a current
        # snapshot + cumulative counters, upserted in place — never a
        # growing history, so this table's size never changes (Fase 5
        # section 3/9: "evitar escrituras excesivas" / "mantener bajo el
        # consumo de recursos"). Read-only for the external watchdog.
        """CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            instance_id TEXT NOT NULL,
            recorded_at_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            last_cycle_started_at_utc TEXT,
            last_cycle_finished_at_utc TEXT,
            last_cycle_succeeded_at_utc TEXT,
            last_cycle_duration_seconds REAL,
            scan_cycles_total INTEGER NOT NULL DEFAULT 0,
            scan_cycles_failed INTEGER NOT NULL DEFAULT 0,
            last_mt5_valid_read_at_utc TEXT,
            mt5_connection_state TEXT,
            mt5_consecutive_failures INTEGER NOT NULL DEFAULT 0,
            mt5_disconnected_seconds_total REAL NOT NULL DEFAULT 0,
            sqlite_ok INTEGER NOT NULL DEFAULT 1,
            sqlite_errors_total INTEGER NOT NULL DEFAULT 0,
            outbox_pending_count INTEGER NOT NULL DEFAULT 0,
            outbox_failed_count INTEGER NOT NULL DEFAULT 0,
            outbox_notifications_failed_total INTEGER NOT NULL DEFAULT 0,
            tracker_last_run_at_utc TEXT,
            tracker_active_signals INTEGER NOT NULL DEFAULT 0,
            tracker_events_total INTEGER NOT NULL DEFAULT 0,
            signals_generated_total INTEGER NOT NULL DEFAULT 0,
            signals_rejected_total INTEGER NOT NULL DEFAULT 0,
            recent_error TEXT
        )""",
        # One row per UTC calendar date; used only by the daily summary
        # (section 14) so "today's" cycle/reconnection counts don't require
        # diffing the lifetime counters above.
        """CREATE TABLE IF NOT EXISTS daily_stats (
            date_utc TEXT PRIMARY KEY,
            cycles_total INTEGER NOT NULL DEFAULT 0,
            cycles_failed INTEGER NOT NULL DEFAULT 0,
            mt5_reconnections INTEGER NOT NULL DEFAULT 0,
            critical_incidents INTEGER NOT NULL DEFAULT 0
        )""",
        # Guarantees at most one daily summary is ever sent per UTC date
        # (section 14's "clave única para no generar varios resúmenes
        # equivalentes"), independent of how many times the check runs.
        """CREATE TABLE IF NOT EXISTS daily_summaries_sent (
            date_utc TEXT PRIMARY KEY,
            sent_at_utc TEXT NOT NULL
        )""",
        # The watchdog's own incident bookkeeping (section 12): one row per
        # distinct ongoing/resolved condition, deduplicated by incident_key.
        """CREATE TABLE IF NOT EXISTS alert_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_key TEXT NOT NULL UNIQUE,
            component TEXT NOT NULL,
            severity TEXT NOT NULL,
            first_detected_at_utc TEXT NOT NULL,
            last_notified_at_utc TEXT,
            notification_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',
            resolved_at_utc TEXT,
            last_message TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alert_incidents_status ON alert_incidents(status)",
    ),
}


class IncompatibleSchemaError(RuntimeError):
    """The database file was created by a newer version of this codebase."""


class MigrationError(RuntimeError):
    """A migration failed; its transaction was rolled back."""


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Create missing tables/indexes and advance ``user_version`` in order.

    Safe to call every time the database is opened: a fully up-to-date
    database simply does nothing. Each pending version is applied in its own
    transaction so a failure never leaves ``user_version`` pointing past a
    half-applied schema (section 4 of the brief: "protección frente a
    migraciones incompletas" / "rollback de transacciones fallidas").
    """
    existing = current_version(conn)
    if existing > SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"La base de datos tiene schema_version={existing}, más nueva que la "
            f"versión soportada por este código ({SCHEMA_VERSION}). Actualice el "
            "código antes de usar esta base de datos."
        )
    for version in range(existing + 1, SCHEMA_VERSION + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            # PRAGMA does not accept bound parameters; `version` is our own
            # loop counter (1..SCHEMA_VERSION), never external/user input.
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(f"Migración a la versión {version} falló: {exc}") from exc
    return current_version(conn)
