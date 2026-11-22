"""Fase 4, section 15: scan_once() wiring of A (new signals) / B (tracker) /
C (outbox) together — format_result_notification and the TRACKER_INTERVAL_SECONDS
throttle, which the lower-level unit tests don't exercise through the worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.schemas.signal_schema import SignalSchema
from app.services.signal_tracker_service import SignalTrackerService
from app.workers import scanner

BASE_SETTINGS = {
    "LOWER_TIMEFRAME": "M15", "PAIR_DELAY_SECONDS": 0, "MAX_CORRELATED_SIGNALS": 1,
    "SIGNAL_TRACKING_ENABLED": True, "TRACKER_INTERVAL_SECONDS": 0,
    "SIGNAL_RESULT_NOTIFICATIONS_ENABLED": True,
}


@dataclass
class _Snapshot:
    bid: Decimal
    ask: Decimal
    tick_time: datetime | None


class _FakeMarket:
    def __init__(self, bars):
        self._bars = bars

    def get_historical_bars(self, _pair, _timeframe, _start, _end, now=None):
        return self._bars


class _HoldSignalService:
    async def build_signal(self, pair):
        return SignalSchema(pair=pair, signal_type="HOLD", reason="nada")


@pytest.mark.asyncio
async def test_scan_once_delivers_a_take_profit_result_notification(monkeypatch):
    """Full A/B/C pass: an already-ACTIVE signal reaches TAKE_PROFIT during
    the tracker step, and its result notification goes out through the same
    outbox/notifier path as a brand-new signal."""
    signal = SignalSchema(pair="EURUSD", signal_type="BUY", score=90, signal_key="k1",
                           entry="1.10000", stop_loss="1.09500", take_profit="1.11000", digits=5)
    activated_at = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    record = scanner.persistence_service.build_record(signal)
    signal_id = await scanner.signal_repository.register(record)
    await scanner.signal_repository.activate(signal_id, "1.10010", "ASK", activated_at)

    bars = pd.DataFrame([{"time": pd.Timestamp(activated_at), "low": 1.0990, "high": 1.1105}])
    monkeypatch.setattr(scanner, "tracker_service", SignalTrackerService(
        _FakeMarket(bars), scanner.signal_repository, clock=lambda: now))
    monkeypatch.setattr(scanner, "_last_tracker_run", None)

    sent_messages: list[str] = []

    class _Notifier:
        async def send_telegram_message(self, text):
            sent_messages.append(text)

    monkeypatch.setattr(scanner, "signal_service", _HoldSignalService())
    monkeypatch.setattr(scanner, "notifier_service", _Notifier())
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(FOREX_PAIRS=["EURUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))

    await scanner.scan_once()

    assert len(sent_messages) == 1
    assert "TAKE PROFIT" in sent_messages[0]
    assert "VIRTUAL" in sent_messages[0] or "SIMULADO" in sent_messages[0]
    assert "2.00R" in sent_messages[0]


@pytest.mark.asyncio
async def test_tracker_is_skipped_when_disabled(monkeypatch):
    calls = []

    class _CountingTracker:
        async def evaluate_once(self):
            calls.append(True)
            return []

    monkeypatch.setattr(scanner, "tracker_service", _CountingTracker())
    monkeypatch.setattr(scanner, "_last_tracker_run", None)
    monkeypatch.setattr(scanner, "signal_service", _HoldSignalService())
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=["EURUSD"], **{**BASE_SETTINGS, "SIGNAL_TRACKING_ENABLED": False}))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))

    await scanner.scan_once()

    assert calls == []


@pytest.mark.asyncio
async def test_tracker_runs_at_most_once_per_configured_interval(monkeypatch):
    calls = []

    class _CountingTracker:
        async def evaluate_once(self):
            calls.append(True)
            return []

    monkeypatch.setattr(scanner, "tracker_service", _CountingTracker())
    monkeypatch.setattr(scanner, "_last_tracker_run", datetime.now(UTC))
    monkeypatch.setattr(scanner, "signal_service", _HoldSignalService())
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=["EURUSD"], **{**BASE_SETTINGS, "TRACKER_INTERVAL_SECONDS": 3600}))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))

    await scanner.scan_once()  # _last_tracker_run was "just now" -> still within the interval

    assert calls == []
