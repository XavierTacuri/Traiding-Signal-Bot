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

SCHEMA_VERSION = 1

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
