"""Fase 5, section 14: daily summary — disabled by default, sent at most
once per UTC date, built only from persisted data."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.models.signal_record import SignalRecord, SignalStatus
from app.monitoring.daily_summary import DailySummaryService
from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.repositories.database import Database
from app.repositories.signal_repository import SQLiteSignalRepository


def _config(**overrides):
    from types import SimpleNamespace
    base = {"DAILY_SUMMARY_ENABLED": True, "DAILY_SUMMARY_TIME": time(7, 0),
            "DAILY_SUMMARY_TIMEZONE": "UTC"}
    base.update(overrides)
    return SimpleNamespace(**base)


class _Notifier:
    def __init__(self, fail=False):
        self.sent: list[str] = []
        self._fail = fail

    async def send_telegram_message(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


def _record(key, status=SignalStatus.PENDING_ACTIVATION, created=None, closed=None) -> SignalRecord:
    return SignalRecord(
        signal_key=key, symbol="EURUSD", direction="BUY", timeframe="M15",
        entry_price="1.1", stop_loss="1.09", take_profit="1.12", risk_distance="0.01",
        digits=5, created_at_utc=created or datetime(2026, 1, 1, 8, tzinfo=UTC),
        status=status, closed_at_utc=closed,
    )


@pytest.mark.asyncio
async def test_disabled_by_default_sends_nothing(tmp_path):
    database = Database(tmp_path / "s.db")
    notifier = _Notifier()
    service = DailySummaryService(HeartbeatRepository(database), SQLiteSignalRepository(database),
                                   notifier, tmp_path / "s.db", config=_config(DAILY_SUMMARY_ENABLED=False),
                                   clock=lambda: datetime(2026, 1, 1, 8, tzinfo=UTC))
    sent = await service.maybe_send()
    assert sent is False
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_not_sent_before_the_configured_local_time(tmp_path):
    database = Database(tmp_path / "s.db")
    notifier = _Notifier()
    service = DailySummaryService(HeartbeatRepository(database), SQLiteSignalRepository(database),
                                   notifier, tmp_path / "s.db", config=_config(),
                                   clock=lambda: datetime(2026, 1, 1, 6, tzinfo=UTC))
    assert await service.maybe_send() is False
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_sent_once_past_the_configured_time_and_not_sent_again_same_day(tmp_path):
    database = Database(tmp_path / "s.db")
    notifier = _Notifier()
    service = DailySummaryService(HeartbeatRepository(database), SQLiteSignalRepository(database),
                                   notifier, tmp_path / "s.db", config=_config(),
                                   clock=lambda: datetime(2026, 1, 1, 8, tzinfo=UTC))

    first = await service.maybe_send()
    second = await service.maybe_send()

    assert first is True
    assert second is False  # section 14: unique key per UTC date
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_a_failed_send_is_not_marked_sent_and_can_retry_later_the_same_day(tmp_path):
    database = Database(tmp_path / "s.db")
    notifier = _Notifier(fail=True)
    service = DailySummaryService(HeartbeatRepository(database), SQLiteSignalRepository(database),
                                   notifier, tmp_path / "s.db", config=_config(),
                                   clock=lambda: datetime(2026, 1, 1, 8, tzinfo=UTC))

    first = await service.maybe_send()
    assert first is False
    assert await HeartbeatRepository(database).has_daily_summary_been_sent(date(2026, 1, 1)) is False


@pytest.mark.asyncio
async def test_message_reports_signals_generated_and_closed_on_the_report_date_only(tmp_path):
    database = Database(tmp_path / "s.db")
    signals = SQLiteSignalRepository(database)
    await signals.register(_record("a", created=datetime(2026, 1, 1, 8, tzinfo=UTC)))
    await signals.register(_record("b", created=datetime(2025, 12, 31, 8, tzinfo=UTC)))  # a different day
    # register() only ever inserts PENDING_ACTIVATION rows (closed_at_utc is
    # not one of its columns by design); drive the real activate/close
    # transitions instead of faking a pre-closed row directly.
    tp_id = await signals.register(_record("c", created=datetime(2026, 1, 1, 1, tzinfo=UTC)))
    await signals.activate(tp_id, "1.1001", "ASK", datetime(2026, 1, 1, 1, 5, tzinfo=UTC))
    await signals.close(tp_id, SignalStatus.TAKE_PROFIT, datetime(2026, 1, 1, 9, tzinfo=UTC),
                         100.0, 2.0, "TAKE_PROFIT", None)

    message = await DailySummaryService(
        HeartbeatRepository(database), signals, _Notifier(), tmp_path / "s.db", config=_config(),
        clock=lambda: datetime(2026, 1, 1, 8, tzinfo=UTC),
    ).build_message(date(2026, 1, 1))

    assert "Señales generadas hoy: 2" in message
    assert "TAKE_PROFIT=1" in message
    assert "SIMULADO" not in message or "virtuales" in message  # labelled as virtual/simulated somewhere
    assert "operaciones" in message  # the disclaimer is present


@pytest.mark.asyncio
async def test_message_shows_nd_when_heartbeat_has_never_been_written(tmp_path):
    database = Database(tmp_path / "s.db")
    service = DailySummaryService(HeartbeatRepository(database), SQLiteSignalRepository(database),
                                   _Notifier(), tmp_path / "s.db", config=_config(),
                                   clock=lambda: datetime(2026, 1, 1, 8, tzinfo=UTC))
    message = await service.build_message(date(2026, 1, 1))
    assert "Estado general: N/D" in message


@pytest.mark.asyncio
async def test_respects_the_configured_timezone_for_the_send_time_check(tmp_path):
    """07:00 local in UTC-5 is 12:00 UTC — using the clock's bare UTC time
    against DAILY_SUMMARY_TIME without the timezone conversion would send
    five hours early."""
    database = Database(tmp_path / "s.db")
    notifier = _Notifier()
    service = DailySummaryService(
        HeartbeatRepository(database), SQLiteSignalRepository(database), notifier, tmp_path / "s.db",
        config=_config(DAILY_SUMMARY_TIMEZONE="America/Bogota"),  # UTC-5, no DST
        clock=lambda: datetime(2026, 1, 1, 11, 0, tzinfo=UTC),  # 06:00 local
    )
    assert await service.maybe_send() is False
