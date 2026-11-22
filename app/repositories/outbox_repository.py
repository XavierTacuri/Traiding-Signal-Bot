"""Fase 4, section 7: transactional outbox for Telegram notifications.

``enqueue_with_signal`` performs the signal insert and the first outbox row
insert as one SQLite transaction (section 7, steps 1-3), so a crash between
them is impossible: either both rows exist or neither does. Sending the
Telegram message itself (step 4) and recording the outcome (step 5) happen
afterward, outside that transaction — SQLite and the Telegram HTTP call
cannot share one atomic unit (brief, section 7's "IMPORTANTE" note), so a
crash after a successful send but before ``mark_sent`` can still cause a
duplicate message on the next retry. That residual risk is accepted and
documented, not hidden: delivery here is *at least once*, made safe to retry
by ``event_key``'s UNIQUE constraint only at the *registration* layer, not by
making re-delivery itself impossible.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from app.models.signal_record import OutboxItem, OutboxStatus, SignalRecord
from app.repositories.database import Database


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_item(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        id=row["id"], event_key=row["event_key"], signal_id=row["signal_id"],
        message_type=row["message_type"], payload=row["payload"],
        status=OutboxStatus(row["status"]), attempts=row["attempts"],
        next_attempt_at_utc=_parse(row["next_attempt_at_utc"]),
        created_at_utc=_parse(row["created_at_utc"]), sent_at_utc=_parse(row["sent_at_utc"]),
        last_error=row["last_error"],
    )


class OutboxRepository(Protocol):
    async def register_signal_with_notification(
        self, record: SignalRecord, event_key: str, message_type: str, payload: str,
    ) -> tuple[int, int] | None: ...
    async def enqueue(self, event_key: str, signal_id: int, message_type: str, payload: str) -> int | None: ...
    async def due(self, now: datetime) -> list[OutboxItem]: ...
    async def mark_sent(self, item_id: int, sent_at: datetime) -> None: ...
    async def mark_failed(self, item_id: int, attempts: int, next_attempt_at: datetime, error: str) -> None: ...
    async def count_pending_and_failed(self) -> tuple[int, int]: ...


class SQLiteOutboxRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def register_signal_with_notification(
        self, record: SignalRecord, event_key: str, message_type: str, payload: str,
    ) -> tuple[int, int] | None:
        """Atomic: INSERT signals + INSERT notification_outbox (+ the
        SIGNAL_GENERATED audit event), section 7 steps 1-3. Returns
        (signal_id, outbox_id), or None if signal_key already exists."""
        return await self._db.run(self._register_sync, record, event_key, message_type, payload)

    @staticmethod
    def _register_sync(conn: sqlite3.Connection, record: SignalRecord, event_key: str,
                        message_type: str, payload: str) -> tuple[int, int] | None:
        # Reuses the signal's own (injectable-clock) created_at_utc rather
        # than a fresh wall-clock read, so a brand-new signal's notification
        # is immediately due and every timestamp in this one transaction
        # agrees — this also keeps the method testable with a fixed clock.
        now = _iso(record.created_at_utc)
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
            conn.execute(
                "INSERT INTO signal_events (signal_id, event_type, previous_status, new_status, "
                "occurred_at_utc, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (signal_id, "SIGNAL_GENERATED", None, str(record.status), now, None),
            )
            outbox_cursor = conn.execute(
                "INSERT INTO notification_outbox (event_key, signal_id, message_type, payload, "
                "status, attempts, next_attempt_at_utc, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (event_key, signal_id, message_type, payload, str(OutboxStatus.PENDING), now, now),
            )
            conn.execute("COMMIT")
            return signal_id, outbox_cursor.lastrowid
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return None

    async def enqueue(self, event_key: str, signal_id: int, message_type: str, payload: str,
                       now: datetime | None = None) -> int | None:
        """Enqueue a notification for an already-registered signal (result
        notifications: ACTIVATED/TAKE_PROFIT/STOP_LOSS/EXPIRED/AMBIGUOUS).
        Idempotent per event_key: a repeated tracker pass that reaches the
        same transition twice enqueues the message only once."""
        return await self._db.run(self._enqueue_sync, event_key, signal_id, message_type, payload,
                                   now or datetime.now(UTC))

    @staticmethod
    def _enqueue_sync(conn: sqlite3.Connection, event_key: str, signal_id: int,
                       message_type: str, payload: str, now_value: datetime) -> int | None:
        now = _iso(now_value)
        try:
            cursor = conn.execute(
                "INSERT INTO notification_outbox (event_key, signal_id, message_type, payload, "
                "status, attempts, next_attempt_at_utc, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (event_key, signal_id, message_type, payload, str(OutboxStatus.PENDING), now, now),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    async def due(self, now: datetime) -> list[OutboxItem]:
        return await self._db.run(self._due_sync, now)

    @staticmethod
    def _due_sync(conn: sqlite3.Connection, now: datetime) -> list[OutboxItem]:
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE status IN (?, ?) AND next_attempt_at_utc <= ? "
            "ORDER BY id",
            (str(OutboxStatus.PENDING), str(OutboxStatus.FAILED), _iso(now)),
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    async def mark_sent(self, item_id: int, sent_at: datetime) -> None:
        await self._db.run(self._mark_sent_sync, item_id, sent_at)

    @staticmethod
    def _mark_sent_sync(conn: sqlite3.Connection, item_id: int, sent_at: datetime) -> None:
        conn.execute(
            "UPDATE notification_outbox SET status = ?, sent_at_utc = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (str(OutboxStatus.SENT), _iso(sent_at), item_id),
        )

    async def mark_failed(self, item_id: int, attempts: int, next_attempt_at: datetime, error: str) -> None:
        await self._db.run(self._mark_failed_sync, item_id, attempts, next_attempt_at, error)

    @staticmethod
    def _mark_failed_sync(conn: sqlite3.Connection, item_id: int, attempts: int,
                           next_attempt_at: datetime, error: str) -> None:
        conn.execute(
            "UPDATE notification_outbox SET status = ?, attempts = ?, next_attempt_at_utc = ?, "
            "last_error = ? WHERE id = ?",
            (str(OutboxStatus.FAILED), attempts, _iso(next_attempt_at), error, item_id),
        )

    async def count_pending_and_failed(self) -> tuple[int, int]:
        """Read-only counts for observability (Fase 5 heartbeat/metrics) —
        total rows per status regardless of ``next_attempt_at_utc``, unlike
        ``due()`` which only returns items actionable right now."""
        return await self._db.run(self._count_pending_and_failed_sync)

    @staticmethod
    def _count_pending_and_failed_sync(conn: sqlite3.Connection) -> tuple[int, int]:
        pending = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status = ?", (str(OutboxStatus.PENDING),),
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status = ?", (str(OutboxStatus.FAILED),),
        ).fetchone()[0]
        return pending, failed
