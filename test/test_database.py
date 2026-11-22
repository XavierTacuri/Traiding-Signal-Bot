"""Fase 4, section 2: the SQLite gateway — PRAGMAs, migrations-on-open,
serialized access, safe close. All against temporary files."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.repositories.database import Database, DatabaseError
from app.repositories.migrations import SCHEMA_VERSION


@pytest.mark.asyncio
async def test_initialize_creates_the_file_and_applies_migrations(tmp_path):
    path = tmp_path / "sub" / "signals.db"
    database = Database(path)
    version = await database.initialize()
    assert version == SCHEMA_VERSION
    assert path.exists()
    await database.close()


@pytest.mark.asyncio
async def test_initialize_is_idempotent(tmp_path):
    database = Database(tmp_path / "signals.db")
    await database.initialize()
    second = await database.initialize()  # must not reopen or re-migrate destructively
    assert second == SCHEMA_VERSION
    await database.close()


@pytest.mark.asyncio
async def test_pragmas_are_configured_on_open(tmp_path):
    database = Database(tmp_path / "signals.db")
    await database.initialize()

    def _read_pragmas(conn: sqlite3.Connection) -> tuple[int, str]:
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return foreign_keys, journal_mode

    foreign_keys, journal_mode = await database.run(_read_pragmas)
    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"
    await database.close()


@pytest.mark.asyncio
async def test_run_executes_against_the_single_connection(tmp_path):
    database = Database(tmp_path / "signals.db")

    def _insert_and_count(conn: sqlite3.Connection, value: str) -> int:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO signals (signal_key, symbol, direction, timeframe, entry_price, stop_loss, "
            "take_profit, risk_distance, digits, created_at_utc, status) "
            "VALUES (?, 'EURUSD', 'BUY', 'M15', '1.1', '1.09', '1.12', '0.01', 5, 'now', "
            "'PENDING_ACTIVATION')",
            (value,),
        )
        conn.execute("COMMIT")
        return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    count = await database.run(_insert_and_count, "a")
    assert count == 1
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_calls_never_run_at_the_same_instant(tmp_path):
    """Section 2: writes must be serialized — no two statements execute at
    once even when many coroutines call run() concurrently."""
    database = Database(tmp_path / "signals.db")
    await database.initialize()
    overlap_detected = False
    in_flight = 0

    def _slow_call(_conn: sqlite3.Connection) -> None:
        nonlocal overlap_detected, in_flight
        in_flight += 1
        if in_flight > 1:
            overlap_detected = True
        import time
        time.sleep(0.01)
        in_flight -= 1

    await asyncio.gather(*(database.run(_slow_call) for _ in range(10)))
    assert overlap_detected is False
    await database.close()


@pytest.mark.asyncio
async def test_run_after_close_raises_database_error(tmp_path):
    database = Database(tmp_path / "signals.db")
    await database.initialize()
    await database.close()
    with pytest.raises(DatabaseError):
        await database.run(lambda conn: conn.execute("SELECT 1"))


@pytest.mark.asyncio
async def test_close_is_safe_to_call_more_than_once(tmp_path):
    database = Database(tmp_path / "signals.db")
    await database.initialize()
    await database.close()
    await database.close()  # must not raise
