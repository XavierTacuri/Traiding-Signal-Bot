"""Fase 4: single-process SQLite access gateway for the async worker.

sqlite3 connections must only ever be used from the thread that created them
(the stdlib enforces this by default via ``check_same_thread``). asyncio's
``to_thread``/default executor hands each call to *whichever* pool thread is
free, which would silently violate that rule the moment two calls landed on
different threads. This mirrors app/services/mt5_connection.py's fix for the
same class of problem: one dedicated worker thread owns the connection for
the lifetime of the process, and every call is funneled through it, which
also means writes are naturally serialized (no two statements ever execute
at the same instant) without any extra locking.

This is *not* the only SQLite connection in the process: SQLiteSetupRepository
(app/repositories/setup_repository.py) is called synchronously from inside
StrategyService, on the event-loop thread itself, and opens its own
connection confined to that thread. Two connections to the same file from two
different threads is an explicitly supported SQLite usage (not a shared
connection), and WAL + a non-zero busy_timeout (configured below) absorb any
brief contention between them.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from app.core.config import Settings, settings
from app.core.logger import logger
from app.repositories.migrations import apply_migrations

T = TypeVar("T")


class DatabaseError(RuntimeError):
    """Wraps sqlite3.Error so callers never need to import sqlite3 directly."""


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")


class Database:
    """Owns the dedicated worker thread and the one connection it uses."""

    def __init__(self, path: str | Path | None = None, config: Settings = settings) -> None:
        self.config = config
        self.path = Path(path if path is not None else config.DATABASE_PATH)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-io")
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    def _open_and_migrate_sync(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None disables sqlite3's implicit transaction
        # management: every BEGIN/COMMIT/ROLLBACK below (here and in each
        # repository) is explicit, as required by section 2 of the brief.
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        configure_connection(conn)
        version = apply_migrations(conn)
        self._conn = conn
        return version

    async def initialize(self) -> int:
        """Open the connection and apply pending migrations. Idempotent.

        Even the "already open" fast path must run on the dedicated worker
        thread (never the caller's thread) — sqlite3 connections reject use
        from any thread but the one that created them."""
        loop = asyncio.get_running_loop()
        if self._conn is not None:
            return await loop.run_in_executor(
                self._executor, lambda: self._conn.execute("PRAGMA user_version").fetchone()[0],
            )
        version = await loop.run_in_executor(self._executor, self._open_and_migrate_sync)
        logger.info("database | abierta en %s (schema_version=%d)", self.path, version)
        return version

    async def run(self, func: Callable[..., T], *args: Any) -> T:
        """Run ``func(connection, *args)`` on the dedicated worker thread."""
        if self._closed:
            raise DatabaseError("Base de datos cerrada; no se ejecutan más operaciones")
        if self._conn is None:
            await self.initialize()

        def _call() -> T:
            try:
                return func(self._conn, *args)
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _call)

    async def close(self) -> None:
        """Release the connection and worker thread. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, conn.close)
            except RuntimeError:
                pass
        self._executor.shutdown(wait=False)
        logger.info("database | conexión liberada")


_default_database: Database | None = None


def get_default_database() -> Database:
    """Process-wide singleton so every caller shares the one worker thread."""
    global _default_database
    if _default_database is None:
        _default_database = Database()
    return _default_database
