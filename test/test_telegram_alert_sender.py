"""Fase 5, section 11: the watchdog's independent Telegram sender — bounded
retries, no token in logs, never raises. No real network/messages."""

from __future__ import annotations

import httpx

from app.monitoring import telegram_alert_sender
from app.monitoring.telegram_alert_sender import MAX_ATTEMPTS, send_alert


class _Response:
    def __init__(self, ok=True, status=200):
        self.ok = ok
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return {"ok": self.ok}


def test_send_alert_returns_false_without_configured_token():
    assert send_alert("", "chat", "hola") is False


def test_send_alert_returns_true_on_a_successful_response(monkeypatch):
    monkeypatch.setattr(telegram_alert_sender.httpx, "post", lambda *a, **k: _Response(ok=True))
    assert send_alert("token123", "chat1", "hola") is True


def test_send_alert_retries_a_bounded_number_of_times_then_gives_up(monkeypatch, caplog):
    calls = []

    def fake_post(*_a, **_k):
        calls.append(1)
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(telegram_alert_sender.httpx, "post", fake_post)
    result = send_alert("token123", "chat1", "hola")

    assert result is False
    assert len(calls) == MAX_ATTEMPTS  # bounded, never infinite (section 12)


def test_send_alert_never_logs_the_token(monkeypatch, caplog):
    token = "999999999:FAKE-WATCHDOG-TOKEN-NOT-REAL"

    def fake_post(url, **_k):
        raise httpx.ConnectError(f"connection failed for url {url}")

    monkeypatch.setattr(telegram_alert_sender.httpx, "post", fake_post)
    with caplog.at_level("WARNING", logger="forex_signal_watchdog"):
        send_alert(token, "chat1", "hola")

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in rendered
    assert "***" in rendered


def test_send_alert_treats_a_rejected_message_as_failure(monkeypatch):
    monkeypatch.setattr(telegram_alert_sender.httpx, "post", lambda *a, **k: _Response(ok=False))
    assert send_alert("token123", "chat1", "hola") is False
