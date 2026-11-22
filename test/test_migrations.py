"""Fase 4, section 4: schema creation and versioning, against temporary
SQLite files only (never the production database)."""

from __future__ import annotations

import sqlite3

import pytest

from app.repositories.migrations import (
    SCHEMA_VERSION,
    IncompatibleSchemaError,
    apply_migrations,
    current_version,
)


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(str(tmp_path / "schema.db"))
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_apply_migrations_creates_all_tables_from_scratch(conn):
    version = apply_migrations(conn)
    assert version == SCHEMA_VERSION
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pending_setups", "signals", "notification_outbox", "signal_events"} <= tables


def test_apply_migrations_is_idempotent_on_restart_without_recreating_tables(conn):
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO signals (signal_key, symbol, direction, timeframe, entry_price, stop_loss, "
        "take_profit, risk_distance, digits, created_at_utc, status) "
        "VALUES ('k', 'EURUSD', 'BUY', 'M15', '1.1', '1.09', '1.12', '0.01', 5, '2026-01-01T00:00:00+00:00', "
        "'PENDING_ACTIVATION')"
    )
    conn.commit()

    apply_migrations(conn)  # simulates re-running migrations on every restart

    row = conn.execute("SELECT * FROM signals WHERE signal_key = 'k'").fetchone()
    assert row is not None  # data survived a second migration pass


def test_apply_migrations_rejects_a_schema_version_from_a_newer_codebase(conn):
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(conn)


def test_foreign_key_from_outbox_to_signals_is_enforced(conn):
    apply_migrations(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification_outbox (event_key, signal_id, message_type, payload, "
            "next_attempt_at_utc, created_at_utc) VALUES ('e', 999, 'NEW_SIGNAL', 'x', 'now', 'now')"
        )


def test_signal_key_unique_constraint_is_enforced(conn):
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO signals (signal_key, symbol, direction, timeframe, entry_price, stop_loss, "
        "take_profit, risk_distance, digits, created_at_utc, status) "
        "VALUES ('dup', 'EURUSD', 'BUY', 'M15', '1.1', '1.09', '1.12', '0.01', 5, 'now', 'PENDING_ACTIVATION')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (signal_key, symbol, direction, timeframe, entry_price, stop_loss, "
            "take_profit, risk_distance, digits, created_at_utc, status) "
            "VALUES ('dup', 'EURUSD', 'SELL', 'M15', '1.1', '1.11', '1.08', '0.01', 5, 'now', "
            "'PENDING_ACTIVATION')"
        )


def test_current_version_of_a_brand_new_file_is_zero(conn):
    assert current_version(conn) == 0
