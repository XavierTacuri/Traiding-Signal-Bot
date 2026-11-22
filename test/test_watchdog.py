"""Fase 5, section 17/18: the watchdog's pure decision core — scenarios
A-J from the brief. No MT5, no real Telegram, no real sleeps: evaluate()
takes no I/O and apply_judgments() is driven against a fake AlertManager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.monitoring import watchdog as watchdog_module
from app.monitoring.models import AlertSeverity
from app.monitoring.watchdog import (
    HEARTBEAT_KEY,
    MT5_CONNECTION_KEY,
    OUTBOX_KEY,
    SCANNER_PROGRESS_KEY,
    SQLITE_KEY,
    HeartbeatRow,
    apply_judgments,
    evaluate,
    main,
    read_heartbeat,
)


def _config(**overrides):
    base = {
        "HEARTBEAT_STALE_AFTER_SECONDS": 180, "WATCHDOG_STARTUP_GRACE_SECONDS": 300,
        "WATCHDOG_SHUTDOWN_GRACE_SECONDS": 60, "SCAN_INTERVAL_SECONDS": 60,
        "MT5_REQUEST_TIMEOUT_SECONDS": 10, "FOREX_PAIRS": ["EURUSD", "GBPUSD"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _heartbeat(**overrides) -> HeartbeatRow:
    base = {
        "recorded_at_utc": datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "status": "HEALTHY",
        "last_cycle_started_at_utc": datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
        "last_cycle_finished_at_utc": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "mt5_connection_state": "CONNECTED", "mt5_consecutive_failures": 0,
        "sqlite_ok": True, "outbox_failed_count": 0,
    }
    base.update(overrides)
    return HeartbeatRow(**base)


class _FakeAlertManager:
    def __init__(self):
        self.raised: list[tuple] = []
        self.resolved: list[tuple] = []

    def raise_alert(self, key, component, severity, message):
        self.raised.append((key, component, severity))

    def resolve_alert(self, key, message):
        self.resolved.append((key,))


# --- Escenario A: funcionamiento normal ------------------------------------

def test_scenario_a_normal_operation_raises_no_alerts():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    judgments = evaluate(_heartbeat(), now, now - timedelta(minutes=10), _config())
    assert all(verdict is None for verdict in judgments.values())


# --- Escenario B: MT5 pierde conexión y entra en reconexión ----------------

def test_scenario_b_mt5_reconnecting_is_a_warning_not_critical():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    judgments = evaluate(_heartbeat(mt5_connection_state="RECONNECTING", mt5_consecutive_failures=2),
                          now, now - timedelta(minutes=10), _config())
    component, severity, _message = judgments[MT5_CONNECTION_KEY]
    assert component == "mt5"
    assert severity.value == "WARNING"
    assert judgments[HEARTBEAT_KEY] is None  # the worker itself is still alive and reporting


# --- Escenario C: MT5 recupera conexión ------------------------------------

def test_scenario_c_mt5_recovered_resolves_the_open_incident():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    config = _config()
    manager = _FakeAlertManager()
    apply_judgments(evaluate(_heartbeat(mt5_connection_state="RECONNECTING"), now,
                              now - timedelta(minutes=10), config), manager)
    apply_judgments(evaluate(_heartbeat(mt5_connection_state="CONNECTED"), now,
                              now - timedelta(minutes=10), config), manager)

    assert (MT5_CONNECTION_KEY, "mt5", AlertSeverity.WARNING) in manager.raised
    assert (MT5_CONNECTION_KEY,) in manager.resolved


# --- Escenario D: el worker deja de actualizar heartbeat -------------------

def test_scenario_d_stale_heartbeat_without_stopping_status_is_critical():
    now = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)  # 10 minutes after the recorded heartbeat
    judgments = evaluate(_heartbeat(status="HEALTHY"), now, now - timedelta(hours=1), _config())
    component, severity, message = judgments[HEARTBEAT_KEY]
    assert component == "worker"
    assert severity.value == "CRITICAL"
    assert "obsoleto" in message


# --- Escenario E: el proceso termina inesperadamente (nunca escribió nada) -

def test_scenario_e_no_heartbeat_ever_past_startup_grace_is_critical():
    now = datetime(2026, 1, 1, 12, 20, tzinfo=UTC)
    started = now - timedelta(minutes=10)
    judgments = evaluate(None, now, started, _config(WATCHDOG_STARTUP_GRACE_SECONDS=300))
    component, severity, _message = judgments[HEARTBEAT_KEY]
    assert component == "worker" and severity.value == "CRITICAL"


def test_no_heartbeat_within_startup_grace_raises_nothing():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    started = now - timedelta(seconds=10)
    judgments = evaluate(None, now, started, _config(WATCHDOG_STARTUP_GRACE_SECONDS=300))
    assert judgments == {}


# --- Escenario F (bounded Telegram retries) is covered in
# test_telegram_alert_sender.py; AlertManager's own failure handling in
# test_alerts.py::test_a_failed_send_does_not_record_a_notification.


# --- Escenario G: SQLite con errores temporales ----------------------------

def test_scenario_g_sqlite_not_ok_is_critical_and_not_treated_as_healthy():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    judgments = evaluate(_heartbeat(sqlite_ok=False), now, now - timedelta(minutes=10), _config())
    component, severity, _message = judgments[SQLITE_KEY]
    assert component == "sqlite" and severity.value == "CRITICAL"


# --- Escenario H: mercado cerrado normalmente ------------------------------

def test_scenario_h_market_closed_does_not_fabricate_alerts():
    """Cycles keep completing (scan_once still runs every pair, just
    returns HOLD) and MT5 stays CONNECTED over a weekend close — evaluate()
    never looks at signal counts, so there is nothing to false-positive on."""
    now = datetime(2026, 1, 3, 12, 0, 30, tzinfo=UTC)  # a Saturday
    heartbeat = _heartbeat(
        recorded_at_utc=now - timedelta(seconds=10),
        last_cycle_started_at_utc=now - timedelta(seconds=20),
        last_cycle_finished_at_utc=now - timedelta(seconds=10),
    )
    judgments = evaluate(heartbeat, now, now - timedelta(hours=10), _config())
    assert all(verdict is None for verdict in judgments.values())


# --- Escenario I: reinicio del watchdog durante un incidente --------------
# Covered end-to-end in test_alerts.py (a second AlertManager instance
# against the same repository must not resend within cooldown) — the same
# guarantee this module relies on via apply_judgments().


# --- Escenario J: apagado ordenado -----------------------------------------

def test_scenario_j_stopping_status_within_shutdown_grace_raises_nothing():
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)  # 5 minutes after a stale-but-STOPPING heartbeat
    heartbeat = _heartbeat(status="STOPPING", recorded_at_utc=now - timedelta(minutes=5))
    judgments = evaluate(heartbeat, now, now - timedelta(hours=1),
                          _config(HEARTBEAT_STALE_AFTER_SECONDS=60, WATCHDOG_SHUTDOWN_GRACE_SECONDS=600))
    assert judgments[HEARTBEAT_KEY] is None


def test_stopping_status_past_the_shutdown_grace_is_still_flagged():
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)  # far beyond any reasonable shutdown
    heartbeat = _heartbeat(status="STOPPING", recorded_at_utc=now - timedelta(hours=1))
    judgments = evaluate(heartbeat, now, now - timedelta(hours=2),
                          _config(HEARTBEAT_STALE_AFTER_SECONDS=60, WATCHDOG_SHUTDOWN_GRACE_SECONDS=60))
    assert judgments[HEARTBEAT_KEY] is not None


# --- Progreso del scanner ---------------------------------------------------

def test_scanner_stuck_cycle_is_flagged_once_past_the_generous_threshold():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    heartbeat = _heartbeat(
        last_cycle_started_at_utc=now - timedelta(hours=1),
        last_cycle_finished_at_utc=now - timedelta(hours=2),  # older than the current start
        recorded_at_utc=now - timedelta(seconds=5),
    )
    judgments = evaluate(heartbeat, now, now - timedelta(hours=3), _config())
    component, severity, message = judgments[SCANNER_PROGRESS_KEY]
    assert component == "scanner" and severity.value == "CRITICAL"
    assert "Fase 3" in message


def test_scanner_cycle_still_within_a_legitimate_processing_window_is_fine():
    now = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    heartbeat = _heartbeat(last_cycle_started_at_utc=now - timedelta(seconds=5),
                            last_cycle_finished_at_utc=None, recorded_at_utc=now)
    judgments = evaluate(heartbeat, now, now - timedelta(minutes=10), _config())
    assert judgments[SCANNER_PROGRESS_KEY] is None


# --- Outbox ------------------------------------------------------------------

def test_outbox_with_several_failed_notifications_is_a_warning():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    judgments = evaluate(_heartbeat(outbox_failed_count=5), now, now - timedelta(minutes=10), _config())
    component, severity, _message = judgments[OUTBOX_KEY]
    assert component == "outbox" and severity.value == "WARNING"


def test_outbox_with_a_couple_of_failures_is_not_yet_alerted():
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    judgments = evaluate(_heartbeat(outbox_failed_count=1), now, now - timedelta(minutes=10), _config())
    assert judgments[OUTBOX_KEY] is None


# --- read_heartbeat ---------------------------------------------------------

def test_read_heartbeat_returns_none_for_a_missing_file(tmp_path):
    assert read_heartbeat(tmp_path / "does_not_exist.db") is None


def test_read_heartbeat_returns_none_for_a_file_without_the_table(tmp_path):
    import sqlite3

    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    assert read_heartbeat(path) is None


@pytest.mark.asyncio
async def test_read_heartbeat_reads_back_a_real_upsert(tmp_path):
    from app.monitoring.heartbeat_repository import HeartbeatRepository
    from app.monitoring.models import HeartbeatSnapshot, WorkerStatus
    from app.repositories.database import Database

    path = tmp_path / "s.db"
    database = Database(path)
    await HeartbeatRepository(database).upsert(HeartbeatSnapshot(
        instance_id="abc", recorded_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        status=WorkerStatus.DEGRADED, mt5_connection_state="RECONNECTING",
    ))
    await database.close()

    row = read_heartbeat(path)
    assert row is not None
    assert row.status == "DEGRADED"
    assert row.mt5_connection_state == "RECONNECTING"


# --- main() CLI entry point -------------------------------------------------

def test_main_runs_a_single_check_cycle_and_exits_cleanly(tmp_path, monkeypatch):
    """python -m app.monitoring.watchdog --once: must run, log, and return
    without touching real MT5, real Telegram, or the production database."""
    import sqlite3

    from app.repositories.migrations import apply_migrations

    db_path = tmp_path / "signals.db"
    conn = sqlite3.connect(str(db_path))
    apply_migrations(conn)
    conn.close()

    monkeypatch.setattr(watchdog_module.settings, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(watchdog_module.settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(watchdog_module.settings, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(watchdog_module, "add_rotating_file_handler", lambda *a, **k: None)

    exit_code = main(["--once"])

    assert exit_code == 0
