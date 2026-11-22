"""Fase 4, sections 8-10: SignalTrackerService — virtual activation and
TP/SL evaluation. Every MT5 call goes through a duck-typed fake; none of
them is order_send/order_check/positions_get (dynamic regression, mirrors
test_integration.py's static grep)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.models.signal_record import NotificationType, SignalRecord, SignalStatus
from app.repositories.database import Database
from app.repositories.signal_repository import SQLiteSignalRepository
from app.services.signal_tracker_service import SignalTrackerService


@dataclass
class _Snapshot:
    bid: Decimal
    ask: Decimal
    tick_time: datetime | None


class _FakeMarket:
    def __init__(self, snapshot=None, bars=None, snapshot_error=None, bars_error=None):
        self._snapshot = snapshot
        self._bars = bars if bars is not None else pd.DataFrame(columns=["time", "low", "high"])
        self._snapshot_error = snapshot_error
        self._bars_error = bars_error
        self.order_send_calls = 0

    def get_symbol_snapshot(self, _pair):
        if self._snapshot_error:
            raise self._snapshot_error
        return self._snapshot

    def get_historical_bars(self, _pair, _timeframe, _start, _end, now=None):
        if self._bars_error:
            raise self._bars_error
        return self._bars

    def order_send(self, *_a, **_k):  # pragma: no cover - must never be called
        self.order_send_calls += 1
        raise AssertionError("El tracker no debe ejecutar órdenes")


def _bars(rows: list[tuple[datetime, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"time": pd.Timestamp(t), "low": low, "high": high} for t, low, high in rows]
    )


async def _repo(tmp_path) -> SQLiteSignalRepository:
    return SQLiteSignalRepository(Database(tmp_path / "s.db"))


def _record(signal_id=None, **overrides) -> SignalRecord:
    base = {
        "signal_key": "k1", "symbol": "EURUSD", "direction": "BUY", "timeframe": "M15",
        "entry_price": "1.10000", "stop_loss": "1.09500", "take_profit": "1.11000",
        "risk_distance": "0.00500", "digits": 5,
        "created_at_utc": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "expires_at_utc": datetime(2026, 1, 1, 13, 0, tzinfo=UTC), "status": SignalStatus.PENDING_ACTIVATION,
    }
    base.update(overrides)
    return SignalRecord(**base)


@pytest.mark.asyncio
async def test_buy_signal_activates_using_the_ask_price(tmp_path):
    repo = await _repo(tmp_path)
    await repo.register(_record())
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)  # Thursday
    market = _FakeMarket(snapshot=_Snapshot(Decimal("1.10000"), Decimal("1.10010"), now))
    tracker = SignalTrackerService(market, repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert len(events) == 1
    assert events[0].notification_type == NotificationType.ACTIVATED
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.ACTIVE
    assert record.activation_price == "1.10010" and record.activation_side == "ASK"


@pytest.mark.asyncio
async def test_sell_signal_activates_using_the_bid_price(tmp_path):
    repo = await _repo(tmp_path)
    await repo.register(_record(direction="SELL", stop_loss="1.10500", take_profit="1.09000"))
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    market = _FakeMarket(snapshot=_Snapshot(Decimal("1.10000"), Decimal("1.10010"), now))
    tracker = SignalTrackerService(market, repo, clock=lambda: now)

    await tracker.evaluate_once()

    record = await repo.get_by_key("k1")
    assert record.activation_price == "1.10000" and record.activation_side == "BID"


@pytest.mark.asyncio
async def test_activation_is_skipped_during_a_normal_weekend_close(tmp_path):
    repo = await _repo(tmp_path)
    saturday_noon = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
    await repo.register(_record(expires_at_utc=saturday_noon + timedelta(hours=1)))
    market = _FakeMarket(snapshot=_Snapshot(Decimal("1.1"), Decimal("1.1001"), saturday_noon))
    tracker = SignalTrackerService(market, repo, clock=lambda: saturday_noon)

    events = await tracker.evaluate_once()

    assert events == []
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.PENDING_ACTIVATION


@pytest.mark.asyncio
async def test_activation_is_skipped_when_the_tick_is_stale_on_a_trading_day(tmp_path):
    repo = await _repo(tmp_path)
    await repo.register(_record())
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    stale_tick = now - timedelta(hours=1)
    market = _FakeMarket(snapshot=_Snapshot(Decimal("1.1"), Decimal("1.1001"), stale_tick))
    tracker = SignalTrackerService(market, repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert events == []
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.PENDING_ACTIVATION


@pytest.mark.asyncio
async def test_signal_expires_if_never_activated_before_its_deadline(tmp_path):
    repo = await _repo(tmp_path)
    await repo.register(_record())
    past_expiry = datetime(2026, 1, 1, 13, 30, tzinfo=UTC)
    market = _FakeMarket(snapshot_error=RuntimeError("sin tick"))
    tracker = SignalTrackerService(market, repo, clock=lambda: past_expiry)

    events = await tracker.evaluate_once()

    assert len(events) == 1 and events[0].notification_type == NotificationType.EXPIRED
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.EXPIRED


@pytest.mark.asyncio
async def test_active_buy_signal_closes_on_take_profit(tmp_path):
    repo = await _repo(tmp_path)
    signal_id = await repo.register(_record())
    activated_at = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    await repo.activate(signal_id, "1.10010", "ASK", activated_at)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    bars = _bars([(datetime(2026, 1, 1, 12, 15, tzinfo=UTC), 1.0990, 1.1105)])
    market = _FakeMarket(bars=bars)
    tracker = SignalTrackerService(market, repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert len(events) == 1
    assert events[0].notification_type == NotificationType.TAKE_PROFIT
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.TAKE_PROFIT
    assert record.result_r == pytest.approx(2.0)
    assert record.result_pips == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_active_buy_signal_closes_on_stop_loss(tmp_path):
    repo = await _repo(tmp_path)
    signal_id = await repo.register(_record())
    await repo.activate(signal_id, "1.10010", "ASK", datetime(2026, 1, 1, 12, 15, tzinfo=UTC))
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    bars = _bars([(datetime(2026, 1, 1, 12, 15, tzinfo=UTC), 1.0940, 1.1010)])
    tracker = SignalTrackerService(_FakeMarket(bars=bars), repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert events[0].notification_type == NotificationType.STOP_LOSS
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.STOP_LOSS
    assert record.result_r == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_sl_and_tp_in_the_same_bar_resolves_to_ambiguous_not_a_win(tmp_path):
    """Section 10: must never assume a winning outcome when the intrabar
    order of events is unknown."""
    repo = await _repo(tmp_path)
    signal_id = await repo.register(_record())
    await repo.activate(signal_id, "1.10010", "ASK", datetime(2026, 1, 1, 12, 15, tzinfo=UTC))
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    bars = _bars([(datetime(2026, 1, 1, 12, 15, tzinfo=UTC), 1.0940, 1.1105)])  # both levels inside range
    tracker = SignalTrackerService(_FakeMarket(bars=bars), repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert events[0].notification_type == NotificationType.AMBIGUOUS
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.AMBIGUOUS
    assert record.result_r is None and record.result_pips is None


@pytest.mark.asyncio
async def test_no_new_bars_leaves_the_signal_active_without_crashing(tmp_path):
    repo = await _repo(tmp_path)
    signal_id = await repo.register(_record())
    await repo.activate(signal_id, "1.10010", "ASK", datetime(2026, 1, 1, 12, 15, tzinfo=UTC))
    now = datetime(2026, 1, 1, 12, 20, tzinfo=UTC)
    market = _FakeMarket(bars_error=RuntimeError("No hay velas cerradas"))
    tracker = SignalTrackerService(market, repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert events == []
    record = await repo.get_by_key("k1")
    assert record.status == SignalStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_closed_signal_is_not_re_evaluated_or_re_notified(tmp_path):
    repo = await _repo(tmp_path)
    signal_id = await repo.register(_record())
    await repo.activate(signal_id, "1.10010", "ASK", datetime(2026, 1, 1, 12, 15, tzinfo=UTC))
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    bars = _bars([(datetime(2026, 1, 1, 12, 15, tzinfo=UTC), 1.0990, 1.1105)])
    tracker = SignalTrackerService(_FakeMarket(bars=bars), repo, clock=lambda: now)

    first_pass = await tracker.evaluate_once()
    second_pass = await tracker.evaluate_once()

    assert len(first_pass) == 1
    assert second_pass == []  # the signal is terminal; get_active() no longer returns it


@pytest.mark.asyncio
async def test_isolated_failure_for_one_symbol_does_not_stop_others(tmp_path):
    repo = await _repo(tmp_path)
    await repo.register(_record(signal_key="bad", symbol="GBPUSD"))
    await repo.register(_record(signal_key="good", symbol="EURUSD"))
    now = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)

    class _PerSymbolMarket(_FakeMarket):
        def get_symbol_snapshot(self, pair):
            if pair == "GBPUSD":
                raise RuntimeError("tick inválido")
            return _Snapshot(Decimal("1.1"), Decimal("1.1001"), now)

    tracker = SignalTrackerService(_PerSymbolMarket(), repo, clock=lambda: now)

    events = await tracker.evaluate_once()

    assert [event.record.symbol for event in events] == ["EURUSD"]
