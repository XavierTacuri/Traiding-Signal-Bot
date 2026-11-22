"""Fase 4, sections 1/2/5: persistent pending-setup storage.

``StrategyService.generate_signal`` (app/services/strategy_service.py) calls
``self.setups.get/save/mark`` synchronously — it is a plain function, not a
coroutine, and nothing wraps it in ``await``/``to_thread``. It always runs on
the asyncio event-loop thread. This repository therefore uses a plain,
synchronous ``sqlite3.Connection`` confined to that same thread: there is no
cross-thread sharing to guard against, and no ``await`` is available at the
call site to hand the work to app/repositories/database.py's dedicated
worker thread instead.

The trade-off is a few sub-millisecond local-disk round trips per pair per
scan cycle happening directly on the event loop. Given SCAN_INTERVAL_SECONDS
is tens of seconds and pairs are already processed sequentially (Fase 3), this
is negligible — unlike the unbounded native MT5 IPC calls Fase 3 had to move
off-thread, a local SQLite read/write here is bounded and fast. Not pretending
this is "async": it is a deliberate, documented, bounded block (brief section 2).

This is a drop-in replacement for InMemorySetupRepository
(app/services/state_service.py) wherever StrategyService is constructed for
the live worker: the Protocol it satisfies, and StrategyService's code, are
both untouched.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings, settings
from app.domain import PendingSetup, SetupStatus, SignalType
from app.repositories.migrations import apply_migrations


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_setup(row: sqlite3.Row) -> PendingSetup:
    return PendingSetup(
        pair=row["symbol"],
        direction=SignalType(row["direction"]),
        detected_at=_parse(row["detected_at_utc"]),
        detected_bar_time=_parse(row["detected_bar_time_utc"]),
        expires_after_bars=row["expires_after_bars"],
        pullback_low=float(row["pullback_low"]) if row["pullback_low"] is not None else None,
        pullback_high=float(row["pullback_high"]) if row["pullback_high"] is not None else None,
        status=SetupStatus(row["status"]),
    )


class SQLiteSetupRepository:
    """SetupRepository (app/services/state_service.py Protocol) backed by SQLite."""

    def __init__(self, path: str | Path | None = None, config: Settings = settings) -> None:
        resolved = Path(path if path is not None else config.DATABASE_PATH)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(resolved), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        apply_migrations(self._conn)
        self.detected_count = 0
        self.expired_count = 0

    def close(self) -> None:
        self._conn.close()

    def get(self, pair: str, direction: SignalType) -> PendingSetup | None:
        # ORDER BY id DESC is defensive: the call path that creates a new
        # setup only runs once any prior one for the same (pair, direction)
        # has left PENDING status, so at most one row should match — this
        # just makes the (otherwise impossible) tie deterministic.
        row = self._conn.execute(
            "SELECT * FROM pending_setups WHERE symbol = ? AND direction = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (pair, str(direction), str(SetupStatus.PENDING)),
        ).fetchone()
        return _row_to_setup(row) if row is not None else None

    def latest(self, pair: str, direction: SignalType) -> PendingSetup | None:
        row = self._conn.execute(
            "SELECT * FROM pending_setups WHERE symbol = ? AND direction = ? "
            "ORDER BY id DESC LIMIT 1",
            (pair, str(direction)),
        ).fetchone()
        return _row_to_setup(row) if row is not None else None

    def save(self, setup: PendingSetup) -> None:
        current = self.get(setup.pair, setup.direction)
        if current is not None and current.detected_bar_time == setup.detected_bar_time:
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO pending_setups "
                "(setup_identifier, symbol, direction, detected_at_utc, detected_bar_time_utc, "
                " expires_after_bars, pullback_low, pullback_high, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(setup_identifier) DO NOTHING",
                (setup.key, setup.pair, str(setup.direction), _iso(setup.detected_at),
                 _iso(setup.detected_bar_time), setup.expires_after_bars,
                 str(setup.pullback_low) if setup.pullback_low is not None else None,
                 str(setup.pullback_high) if setup.pullback_high is not None else None,
                 str(setup.status)),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            self._conn.execute("ROLLBACK")
            raise
        self.detected_count += 1

    def mark(self, setup: PendingSetup, status: SetupStatus) -> None:
        if status == SetupStatus.EXPIRED and setup.status != SetupStatus.EXPIRED:
            self.expired_count += 1
        setup.status = status
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "UPDATE pending_setups SET status = ? WHERE setup_identifier = ?",
                (str(status), setup.key),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            self._conn.execute("ROLLBACK")
            raise
