"""Fase 4, sections 3/6/9: persistent signal storage, deduplication and the
auditable event trail.

``SignalKeyRepository`` (app/services/state_service.py) is a synchronous
Protocol and stays untouched for anything that still wants a lightweight
in-memory dedup stand-in. The persistent path defined here is a new,
async-native Protocol instead of a forced retrofit: every call this
repository makes crosses to the dedicated SQLite worker thread
(app/repositories/database.py), which requires ``await``, and
``app/workers/scanner.py`` already runs inside a coroutine, so there is no
reason to pretend the old synchronous shape still fits.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from app.models.signal_record import (
    DETERMINED_RESULT_STATUSES,
    TERMINAL_SIGNAL_STATUSES,
    SignalRecord,
    SignalStatus,
)
from app.repositories.database import Database


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_record(row: sqlite3.Row) -> SignalRecord:
    return SignalRecord(
        id=row["id"], signal_key=row["signal_key"], symbol=row["symbol"],
        direction=row["direction"], timeframe=row["timeframe"],
        entry_price=row["entry_price"], stop_loss=row["stop_loss"],
        take_profit=row["take_profit"], risk_distance=row["risk_distance"],
        digits=row["digits"], confidence_score=row["confidence_score"],
        activation_price=row["activation_price"], activation_side=row["activation_side"],
        created_at_utc=_parse(row["created_at_utc"]), sent_at_utc=_parse(row["sent_at_utc"]),
        activated_at_utc=_parse(row["activated_at_utc"]), closed_at_utc=_parse(row["closed_at_utc"]),
        expires_at_utc=_parse(row["expires_at_utc"]),
        last_checked_bar_time_utc=_parse(row["last_checked_bar_time_utc"]),
        status=SignalStatus(row["status"]), result_pips=row["result_pips"],
        result_r=row["result_r"], close_reason=row["close_reason"],
        schema_version=row["schema_version"],
    )


def _record_event(conn: sqlite3.Connection, signal_id: int, event_type: str,
                   previous_status: str | None, new_status: str,
                   metadata: str | None = None) -> None:
    conn.execute(
        "INSERT INTO signal_events "
        "(signal_id, event_type, previous_status, new_status, occurred_at_utc, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (signal_id, event_type, previous_status, new_status, _iso(datetime.now(UTC)), metadata),
    )


class SignalRepository(Protocol):
    async def exists(self, signal_key: str) -> bool: ...
    async def get_by_key(self, signal_key: str) -> SignalRecord | None: ...
    async def register(self, record: SignalRecord) -> int | None: ...
    async def mark_sent(self, signal_id: int, sent_at: datetime) -> None: ...
    async def get_pending_activation(self) -> list[SignalRecord]: ...
    async def get_active(self) -> list[SignalRecord]: ...
    async def activate(self, signal_id: int, activation_price: str,
                        activation_side: str, activated_at: datetime) -> bool: ...
    async def close(self, signal_id: int, status: SignalStatus, closed_at: datetime,
                     result_pips: float | None, result_r: float | None,
                     close_reason: str, last_checked_bar_time: datetime | None) -> bool: ...
    async def update_last_checked(self, signal_id: int, bar_time: datetime) -> None: ...
    async def get_all(self) -> list[SignalRecord]: ...


class SQLiteSignalRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def exists(self, signal_key: str) -> bool:
        return await self._db.run(self._exists_sync, signal_key)

    @staticmethod
    def _exists_sync(conn: sqlite3.Connection, signal_key: str) -> bool:
        row = conn.execute("SELECT 1 FROM signals WHERE signal_key = ?", (signal_key,)).fetchone()
        return row is not None

    async def get_by_key(self, signal_key: str) -> SignalRecord | None:
        return await self._db.run(self._get_by_key_sync, signal_key)

    @staticmethod
    def _get_by_key_sync(conn: sqlite3.Connection, signal_key: str) -> SignalRecord | None:
        row = conn.execute("SELECT * FROM signals WHERE signal_key = ?", (signal_key,)).fetchone()
        return _row_to_record(row) if row is not None else None

    async def register(self, record: SignalRecord) -> int | None:
        """Insert the signal; returns its id, or None if signal_key already
        exists (idempotent no-op — section 6: a repeated scan or a restart
        must never create a second trading signal for the same event)."""
        return await self._db.run(self._register_sync, record)

    @staticmethod
    def _register_sync(conn: sqlite3.Connection, record: SignalRecord) -> int | None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO signals (signal_key, symbol, direction, timeframe, entry_price, "
                "stop_loss, take_profit, risk_distance, digits, confidence_score, created_at_utc, "
                "expires_at_utc, status, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.signal_key, record.symbol, record.direction, record.timeframe,
                 record.entry_price, record.stop_loss, record.take_profit, record.risk_distance,
                 record.digits, record.confidence_score, _iso(record.created_at_utc),
                 _iso(record.expires_at_utc) if record.expires_at_utc else None,
                 str(record.status), record.schema_version),
            )
            signal_id = cursor.lastrowid
            _record_event(conn, signal_id, "SIGNAL_GENERATED", None, str(record.status))
            conn.execute("COMMIT")
            return signal_id
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return None

    async def mark_sent(self, signal_id: int, sent_at: datetime) -> None:
        await self._db.run(self._mark_sent_sync, signal_id, sent_at)

    @staticmethod
    def _mark_sent_sync(conn: sqlite3.Connection, signal_id: int, sent_at: datetime) -> None:
        conn.execute("UPDATE signals SET sent_at_utc = ? WHERE id = ? AND sent_at_utc IS NULL",
                     (_iso(sent_at), signal_id))

    async def get_pending_activation(self) -> list[SignalRecord]:
        return await self._db.run(self._get_by_status_sync, str(SignalStatus.PENDING_ACTIVATION))

    async def get_active(self) -> list[SignalRecord]:
        return await self._db.run(self._get_by_status_sync, str(SignalStatus.ACTIVE))

    @staticmethod
    def _get_by_status_sync(conn: sqlite3.Connection, status: str) -> list[SignalRecord]:
        rows = conn.execute("SELECT * FROM signals WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [_row_to_record(row) for row in rows]

    async def activate(self, signal_id: int, activation_price: str,
                        activation_side: str, activated_at: datetime) -> bool:
        """Transition PENDING_ACTIVATION -> ACTIVE. Returns False (no-op) if
        the signal was not in that state (already active/closed/expired) —
        makes the transition idempotent against a repeated tracker pass."""
        return await self._db.run(
            self._transition_sync, signal_id, str(SignalStatus.PENDING_ACTIVATION),
            "SIGNAL_ACTIVATED", str(SignalStatus.ACTIVE),
            "UPDATE signals SET status = ?, activation_price = ?, activation_side = ?, "
            "activated_at_utc = ?, last_checked_bar_time_utc = ? WHERE id = ? AND status = ?",
            (str(SignalStatus.ACTIVE), activation_price, activation_side, _iso(activated_at),
             _iso(activated_at), signal_id, str(SignalStatus.PENDING_ACTIVATION)),
        )

    @staticmethod
    def _transition_sync(conn: sqlite3.Connection, signal_id: int, expected_status: str,
                          event_type: str, new_status: str, sql: str, params: tuple) -> bool:
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM signals WHERE id = ?", (signal_id,)).fetchone()
            if current is None or current["status"] != expected_status:
                conn.execute("ROLLBACK")
                return False
            conn.execute(sql, params)
            _record_event(conn, signal_id, event_type, expected_status, new_status)
            conn.execute("COMMIT")
            return True
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    async def close(self, signal_id: int, status: SignalStatus, closed_at: datetime,
                     result_pips: float | None, result_r: float | None,
                     close_reason: str, last_checked_bar_time: datetime | None) -> bool:
        """Transition ACTIVE (or PENDING_ACTIVATION, for EXPIRED-before-activation)
        into a terminal status. Returns False if the signal was already
        terminal — section 9: "una señal finalizada no debe volver a cerrarse"."""
        return await self._db.run(
            self._close_sync, signal_id, status, closed_at, result_pips, result_r,
            close_reason, last_checked_bar_time,
        )

    @staticmethod
    def _close_sync(conn: sqlite3.Connection, signal_id: int, status: SignalStatus,
                     closed_at: datetime, result_pips: float | None, result_r: float | None,
                     close_reason: str, last_checked_bar_time: datetime | None) -> bool:
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM signals WHERE id = ?", (signal_id,)).fetchone()
            if current is None or current["status"] in TERMINAL_SIGNAL_STATUSES:
                conn.execute("ROLLBACK")
                return False
            previous = current["status"]
            conn.execute(
                "UPDATE signals SET status = ?, closed_at_utc = ?, result_pips = ?, result_r = ?, "
                "close_reason = ?, last_checked_bar_time_utc = COALESCE(?, last_checked_bar_time_utc) "
                "WHERE id = ?",
                (str(status), _iso(closed_at), result_pips, result_r, close_reason,
                 _iso(last_checked_bar_time) if last_checked_bar_time else None, signal_id),
            )
            _record_event(conn, signal_id, f"SIGNAL_CLOSED_{status}", previous, str(status), close_reason)
            conn.execute("COMMIT")
            return True
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    async def update_last_checked(self, signal_id: int, bar_time: datetime) -> None:
        await self._db.run(self._update_last_checked_sync, signal_id, bar_time)

    @staticmethod
    def _update_last_checked_sync(conn: sqlite3.Connection, signal_id: int, bar_time: datetime) -> None:
        conn.execute("UPDATE signals SET last_checked_bar_time_utc = ? WHERE id = ?",
                     (_iso(bar_time), signal_id))

    async def get_all(self) -> list[SignalRecord]:
        return await self._db.run(self._get_all_sync)

    @staticmethod
    def _get_all_sync(conn: sqlite3.Connection) -> list[SignalRecord]:
        rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
        return [_row_to_record(row) for row in rows]


def is_determined_result(record: SignalRecord) -> bool:
    return record.status in DETERMINED_RESULT_STATUSES
