"""Fase 5, section 5/6/13: periodic heartbeat + metrics persistence.

Runs as its own asyncio task (started alongside the scanner task in
main.py), independent of scan_once()'s own cadence. It never calls MT5
itself and never advances app/monitoring/worker_state.py's progress
fields — it only reads them and the MT5 connection's already-public state
(``connection.state`` / ``connection.consecutive_failures``, both read-only
observability additions from Fase 3's manager, section 9: "no realizar
consultas adicionales a MT5 desde el watchdog externo" — this applies with
equal force to this in-process service, which also never issues its own
MT5 call).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime

from app.core.logger import logger
from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.monitoring.models import HeartbeatSnapshot, WorkerStatus
from app.monitoring.worker_state import WorkerRuntimeState
from app.repositories.database import Database, DatabaseError
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.signal_repository import SQLiteSignalRepository
from app.services.mt5_connection import ConnectionState, MT5ConnectionManager


def compute_self_status(
    state: WorkerRuntimeState, mt5_state: ConnectionState | None,
) -> WorkerStatus:
    """The worker's own best-known status (section 4). Never returns
    UNHEALTHY/STOPPED: those are inferred externally by the watchdog from a
    stale or missing heartbeat, never self-reported by a process that, by
    definition, could not report them if it were actually in that state."""
    if state.status == WorkerStatus.STOPPING:
        return WorkerStatus.STOPPING
    if state.last_cycle_finished_at_utc is None:
        return WorkerStatus.STARTING
    if mt5_state in (ConnectionState.RECONNECTING, ConnectionState.DEGRADED, ConnectionState.CONNECTING):
        return WorkerStatus.DEGRADED
    return WorkerStatus.HEALTHY


class HeartbeatService:
    def __init__(
        self, database: Database, state: WorkerRuntimeState,
        connection: MT5ConnectionManager | None = None,
        outbox_repository: SQLiteOutboxRepository | None = None,
        signal_repository: SQLiteSignalRepository | None = None,
        daily_summary: object | None = None,
        clock=lambda: datetime.now(UTC), monotonic=time.monotonic,
    ) -> None:
        self._repository = HeartbeatRepository(database)
        self._state = state
        self._connection = connection
        self._outbox = outbox_repository
        self._signals = signal_repository
        # Optional: a DailySummaryService.maybe_send() check piggybacks on
        # this same timer (section 14) instead of needing its own task —
        # the heartbeat cadence is already far more frequent than "once a
        # day" needs to be checked.
        self._daily_summary = daily_summary
        self._now = clock
        self._monotonic = monotonic
        self._last_tick_monotonic: float | None = None
        self._previous_mt5_state: ConnectionState | None = None
        self._mt5_disconnected_seconds_total = 0.0
        self._sqlite_errors_total = 0
        self._outbox_notifications_failed_total = 0
        self._last_daily_date = None
        self._last_cycles_seen = 0
        self._last_cycles_failed_seen = 0

    async def tick(self) -> HeartbeatSnapshot:
        now = self._now()
        now_monotonic = self._monotonic()
        # `is not None`, not plain truthiness: time.monotonic() can
        # legitimately return exactly 0.0, which must not be mistaken for
        # "no previous tick yet".
        elapsed = (
            now_monotonic - self._last_tick_monotonic if self._last_tick_monotonic is not None else 0.0
        )
        self._last_tick_monotonic = now_monotonic

        mt5_state = self._connection.state if self._connection is not None else None
        mt5_failures = self._connection.consecutive_failures if self._connection is not None else 0
        if mt5_state is not None and mt5_state != ConnectionState.CONNECTED:
            self._mt5_disconnected_seconds_total += elapsed

        sqlite_ok = True
        outbox_pending = outbox_failed = 0
        if self._outbox is not None:
            try:
                outbox_pending, outbox_failed = await self._outbox.count_pending_and_failed()
            except (DatabaseError, sqlite3.Error) as exc:
                sqlite_ok = False
                self._sqlite_errors_total += 1
                self._state.sqlite_error()
                logger.warning("heartbeat | fallo leyendo el outbox: %s", exc)

        tracker_active = 0
        if self._signals is not None:
            try:
                tracker_active = len(await self._signals.get_active())
            except (DatabaseError, sqlite3.Error) as exc:
                sqlite_ok = False
                self._sqlite_errors_total += 1
                self._state.sqlite_error()
                logger.warning("heartbeat | fallo leyendo señales activas: %s", exc)

        status = compute_self_status(self._state, mt5_state)
        snapshot = HeartbeatSnapshot(
            instance_id=self._state.instance_id, recorded_at_utc=now, status=status,
            last_cycle_started_at_utc=self._state.last_cycle_started_at_utc,
            last_cycle_finished_at_utc=self._state.last_cycle_finished_at_utc,
            last_cycle_succeeded_at_utc=self._state.last_cycle_succeeded_at_utc,
            last_cycle_duration_seconds=self._state.last_cycle_duration_seconds,
            scan_cycles_total=self._state.scan_cycles_total,
            scan_cycles_failed=self._state.scan_cycles_failed,
            last_mt5_valid_read_at_utc=self._state.last_mt5_valid_read_at_utc,
            mt5_connection_state=str(mt5_state) if mt5_state is not None else None,
            mt5_consecutive_failures=mt5_failures,
            mt5_disconnected_seconds_total=self._mt5_disconnected_seconds_total,
            sqlite_ok=sqlite_ok, sqlite_errors_total=self._sqlite_errors_total,
            outbox_pending_count=outbox_pending, outbox_failed_count=outbox_failed,
            outbox_notifications_failed_total=self._outbox_notifications_failed_total,
            tracker_last_run_at_utc=self._state.tracker_last_run_at_utc,
            tracker_active_signals=tracker_active,
            tracker_events_total=self._state.tracker_events_total,
            signals_generated_total=self._state.signals_generated_total,
            signals_rejected_total=self._state.signals_rejected_total,
            recent_error=self._state.recent_error,
        )
        await self._repository.upsert(snapshot)
        await self._update_daily_stats(now, mt5_state)
        if self._daily_summary is not None:
            try:
                await self._daily_summary.maybe_send()
            except (DatabaseError, sqlite3.Error) as exc:
                logger.warning("heartbeat | resumen diario no pudo verificarse: %s", exc)
        return snapshot

    async def _update_daily_stats(self, now: datetime, mt5_state: ConnectionState | None) -> None:
        """Section 14: day-scoped deltas, computed once per tick from how
        much the lifetime counters moved since the previous tick."""
        today = now.date()
        if self._last_daily_date != today:
            self._last_cycles_seen = self._state.scan_cycles_total
            self._last_cycles_failed_seen = self._state.scan_cycles_failed
            self._last_daily_date = today
        cycles_delta = self._state.scan_cycles_total - self._last_cycles_seen
        failed_delta = self._state.scan_cycles_failed - self._last_cycles_failed_seen
        reconnection = int(
            self._previous_mt5_state is not None
            and self._previous_mt5_state != ConnectionState.CONNECTED
            and mt5_state == ConnectionState.CONNECTED,
        )
        self._previous_mt5_state = mt5_state
        self._last_cycles_seen = self._state.scan_cycles_total
        self._last_cycles_failed_seen = self._state.scan_cycles_failed
        if cycles_delta or failed_delta or reconnection:
            await self._repository.upsert_daily_stats(
                today, cycles_delta=cycles_delta, cycles_failed_delta=failed_delta,
                reconnections_delta=reconnection,
            )

    async def run_forever(self, interval_seconds: float) -> None:
        """Started as its own task; cancelled during ordered shutdown."""
        while True:
            try:
                await self.tick()
            except (DatabaseError, sqlite3.Error) as exc:
                logger.warning("heartbeat | no se pudo escribir el heartbeat: %s", exc)
            await asyncio.sleep(interval_seconds)
