"""Fase 5, section 16: restart recovery for the worker's heartbeat and the
watchdog's alert bookkeeping, against one shared temporary database file —
and confirmation that none of this touches signal/tracker state (section 16:
"la recuperación del monitoreo no debe modificar estados de señales ni
resultados virtuales")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.signal_record import SignalRecord, SignalStatus
from app.monitoring.alerts import AlertManager, AlertRepository
from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.monitoring.heartbeat_service import HeartbeatService
from app.monitoring.watchdog import (
    MT5_CONNECTION_KEY,
    apply_judgments,
    evaluate,
    read_heartbeat,
)
from app.monitoring.worker_state import WorkerRuntimeState
from app.repositories.database import Database
from app.repositories.signal_repository import SQLiteSignalRepository
from app.services.mt5_connection import ConnectionState


class _FakeConnection:
    def __init__(self, state):
        self.state = state
        self.consecutive_failures = 1


def _watchdog_config(**overrides):
    base = {"HEARTBEAT_STALE_AFTER_SECONDS": 180, "WATCHDOG_STARTUP_GRACE_SECONDS": 300,
            "WATCHDOG_SHUTDOWN_GRACE_SECONDS": 60, "SCAN_INTERVAL_SECONDS": 60,
            "MT5_REQUEST_TIMEOUT_SECONDS": 10, "FOREX_PAIRS": ["EURUSD"],
            "ALERT_COOLDOWN_SECONDS": 900, "ALERT_MAX_REMINDERS": 3}
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_worker_restart_preserves_signal_state_and_resumes_heartbeat(tmp_path):
    path = tmp_path / "signals.db"

    # --- "Process 1": registers a signal, writes a few heartbeats --------
    db1 = Database(path)
    signals1 = SQLiteSignalRepository(db1)
    await signals1.register(SignalRecord(
        signal_key="k1", symbol="EURUSD", direction="BUY", timeframe="M15",
        entry_price="1.1", stop_loss="1.09", take_profit="1.12", risk_distance="0.01",
        digits=5, created_at_utc=datetime(2026, 1, 1, tzinfo=UTC), status=SignalStatus.ACTIVE,
    ))
    state1 = WorkerRuntimeState(instance_id="run-1", started_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    now1 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    state1.cycle_started(now1)
    state1.cycle_finished(now1, success=True)
    await HeartbeatService(db1, state1, connection=_FakeConnection(ConnectionState.CONNECTED),
                            clock=lambda: now1).tick()
    await db1.close()

    # --- "Process 2": a fresh worker instance (new WorkerRuntimeState, new
    # Database object) against the same file ------------------------------
    db2 = Database(path)
    signals2 = SQLiteSignalRepository(db2)
    recovered_signal = await signals2.get_by_key("k1")
    assert recovered_signal is not None
    assert recovered_signal.status == SignalStatus.ACTIVE  # untouched by the restart

    state2 = WorkerRuntimeState(instance_id="run-2", started_at_utc=datetime(2026, 1, 1, 12, 5, tzinfo=UTC))
    now2 = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    state2.cycle_started(now2)
    state2.cycle_finished(now2, success=True)
    await HeartbeatService(db2, state2, connection=_FakeConnection(ConnectionState.CONNECTED),
                            clock=lambda: now2).tick()

    heartbeat = await HeartbeatRepository(db2).get()
    assert heartbeat.instance_id == "run-2"  # the new instance's heartbeat, not stale leftovers

    # The restart itself must never re-touch the signal.
    unchanged = await signals2.get_by_key("k1")
    assert unchanged.status == SignalStatus.ACTIVE
    await db2.close()


def test_watchdog_restart_recognizes_the_open_incident_and_does_not_duplicate(tmp_path):
    import sqlite3

    from app.repositories.migrations import apply_migrations

    path = tmp_path / "signals.db"
    conn = sqlite3.connect(str(path))
    apply_migrations(conn)
    conn.close()

    sent_messages = []

    def sender(text: str) -> bool:
        sent_messages.append(text)
        return True

    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    heartbeat = SimpleNamespace(
        recorded_at_utc=now[0] - timedelta(seconds=10), status="DEGRADED",
        last_cycle_started_at_utc=now[0] - timedelta(seconds=20),
        last_cycle_finished_at_utc=now[0] - timedelta(seconds=10),
        mt5_connection_state="RECONNECTING", mt5_consecutive_failures=3,
        sqlite_ok=True, outbox_failed_count=0,
    )
    config = _watchdog_config()

    # --- "Watchdog process 1": detects the incident, sends one alert -----
    repo1 = AlertRepository(path)
    manager1 = AlertManager(repo1, sender, config, clock=lambda: now[0])
    apply_judgments(evaluate(heartbeat, now[0], now[0] - timedelta(minutes=10), config), manager1)
    repo1.close()
    assert len(sent_messages) == 1

    # --- "Watchdog process 2": restarted mid-incident, same condition ----
    now[0] += timedelta(minutes=2)  # still well within the cooldown
    repo2 = AlertRepository(path)
    manager2 = AlertManager(repo2, sender, config, clock=lambda: now[0])
    apply_judgments(evaluate(heartbeat, now[0], now[0], config), manager2)  # fresh watchdog start_at = now
    incident = repo2.get_open(MT5_CONNECTION_KEY)
    repo2.close()

    assert len(sent_messages) == 1  # no duplicate alert just because the watchdog restarted
    assert incident is not None and incident.notification_count == 1


@pytest.mark.asyncio
async def test_a_restarted_watchdog_does_not_affect_signal_or_tracker_state(tmp_path):
    """Section 16: reading heartbeats/alert_incidents must never write to
    signals/notification_outbox."""
    import sqlite3


    path = tmp_path / "signals.db"
    database = Database(path)
    signals = SQLiteSignalRepository(database)
    signal_id = await signals.register(SignalRecord(
        signal_key="k1", symbol="EURUSD", direction="BUY", timeframe="M15",
        entry_price="1.1", stop_loss="1.09", take_profit="1.12", risk_distance="0.01",
        digits=5, created_at_utc=datetime(2026, 1, 1, tzinfo=UTC), status=SignalStatus.ACTIVE,
    ))
    await database.close()

    before = sqlite3.connect(str(path)).execute(
        "SELECT status, result_r FROM signals WHERE id = ?", (signal_id,),
    ).fetchone()

    read_heartbeat(path)  # the watchdog's only read of this file

    after = sqlite3.connect(str(path)).execute(
        "SELECT status, result_r FROM signals WHERE id = ?", (signal_id,),
    ).fetchone()
    assert before == after == ("ACTIVE", None)
