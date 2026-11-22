"""Fase 5, section 12: incident dedup, cooldown, limited reminders,
recovery, and reopening a previously resolved incident."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.monitoring.alerts import AlertManager, AlertRepository
from app.monitoring.models import AlertSeverity


@pytest.fixture
def repo(tmp_path):
    import sqlite3

    from app.repositories.migrations import apply_migrations

    path = tmp_path / "s.db"
    conn = sqlite3.connect(str(path))
    apply_migrations(conn)
    conn.close()
    return AlertRepository(path)


def _config(**overrides):
    base = {"ALERT_COOLDOWN_SECONDS": 900, "ALERT_MAX_REMINDERS": 3}
    base.update(overrides)
    return SimpleNamespace(**base)


class _Sender:
    def __init__(self, succeed=True):
        self.calls: list[str] = []
        self.succeed = succeed

    def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return self.succeed


def test_a_new_condition_sends_exactly_one_notification(repo):
    sender = _Sender()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = AlertManager(repo, sender, _config(), clock=lambda: now)

    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.WARNING, "caída detectada")

    assert len(sender.calls) == 1
    incident = repo.get_open("mt5_connection")
    assert incident is not None and incident.notification_count == 1


def test_repeated_raise_within_cooldown_sends_nothing_new(repo):
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=600), clock=lambda: now[0])

    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.CRITICAL, "caída")
    now[0] += timedelta(minutes=5)  # within cooldown
    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.CRITICAL, "caída")

    assert len(sender.calls) == 1  # no spam on every check cycle


def test_critical_incident_gets_a_reminder_after_cooldown_elapses(repo):
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=600, ALERT_MAX_REMINDERS=3),
                            clock=lambda: now[0])

    manager.raise_alert("sqlite", "sqlite", AlertSeverity.CRITICAL, "error persistente")
    now[0] += timedelta(minutes=11)  # past cooldown
    manager.raise_alert("sqlite", "sqlite", AlertSeverity.CRITICAL, "error persistente")

    assert len(sender.calls) == 2


def test_reminders_stop_once_the_cap_is_reached(repo):
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=1, ALERT_MAX_REMINDERS=2),
                            clock=lambda: now[0])

    for _ in range(6):
        manager.raise_alert("sqlite", "sqlite", AlertSeverity.CRITICAL, "error persistente")
        now[0] += timedelta(seconds=2)

    assert len(sender.calls) == 3  # initial + 2 reminders, then capped


def test_warning_severity_never_gets_a_reminder(repo):
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=1, ALERT_MAX_REMINDERS=5),
                            clock=lambda: now[0])

    for _ in range(4):
        manager.raise_alert("outbox", "outbox", AlertSeverity.WARNING, "fallos persistentes")
        now[0] += timedelta(seconds=5)

    assert len(sender.calls) == 1


def test_resolve_sends_exactly_one_recovery_message(repo):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(), clock=lambda: now)
    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.WARNING, "caída")
    sender.calls.clear()

    manager.resolve_alert("mt5_connection", "recuperado")
    manager.resolve_alert("mt5_connection", "recuperado otra vez")  # idempotent no-op

    assert len(sender.calls) == 1
    assert repo.get_open("mt5_connection") is None


def test_resolving_an_incident_that_was_never_open_sends_nothing(repo):
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(), clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    manager.resolve_alert("outbox", "todo bien")

    assert sender.calls == []


def test_reopening_a_resolved_incident_resets_notification_count(repo):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    sender = _Sender()
    manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=1), clock=lambda: now[0])

    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.WARNING, "caída #1")
    manager.resolve_alert("mt5_connection", "recuperado")
    now[0] += timedelta(hours=1)
    manager.raise_alert("mt5_connection", "mt5", AlertSeverity.WARNING, "caída #2")  # weeks later, recurring

    incident = repo.get_open("mt5_connection")
    assert incident is not None
    assert incident.notification_count == 1  # fresh episode, not inflated by the prior one


def test_watchdog_restart_mid_incident_does_not_resend_within_cooldown(repo):
    """Section 12/16/18 scenario I: a new AlertManager instance (as if the
    watchdog process restarted) against the SAME repository must still
    respect the cooldown — the state that prevents duplicates is in the
    database, not in the process."""
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    sender = _Sender()
    first_manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=900), clock=lambda: now[0])
    first_manager.raise_alert("mt5_connection", "mt5", AlertSeverity.CRITICAL, "caída")

    now[0] += timedelta(minutes=2)
    restarted_manager = AlertManager(repo, sender, _config(ALERT_COOLDOWN_SECONDS=900), clock=lambda: now[0])
    restarted_manager.raise_alert("mt5_connection", "mt5", AlertSeverity.CRITICAL, "caída")

    assert len(sender.calls) == 1


def test_a_failed_send_does_not_record_a_notification(repo):
    sender = _Sender(succeed=False)
    manager = AlertManager(repo, sender, _config(), clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    manager.raise_alert("outbox", "outbox", AlertSeverity.WARNING, "fallo")

    incident = repo.get_open("outbox")
    assert incident is not None
    assert incident.notification_count == 0  # not recorded as delivered
