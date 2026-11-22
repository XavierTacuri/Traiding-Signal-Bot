from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.market_data_validation import (
    InvalidMarketDataError,
    StaleMarketDataError,
)
from app.services.signal_service import SignalService


def make_bars(count: int, minutes: int, start: datetime) -> list[dict]:
    bars = []
    for index in range(count):
        close = 1.10000 + 0.00001 * index
        time = start + timedelta(minutes=minutes * index)
        bars.append({"time": int(time.timestamp()), "open": close, "high": close + 0.0002,
                     "low": close - 0.0002, "close": close})
    return bars


class FakeMT5:
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60

    def __init__(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.m15 = make_bars(300, 15, now - timedelta(minutes=15 * 300))
        self.h1 = make_bars(300, 60, now - timedelta(hours=300))

    def symbol_info(self, name):
        return SimpleNamespace(name=name, digits=5, point=0.00001)

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=1.10000, ask=1.10010)

    def symbols_get(self):
        return ()

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        bars = self.m15 if timeframe == self.TIMEFRAME_M15 else self.h1
        return bars[-count:]

    def order_send(self, *_args, **_kwargs):
        raise AssertionError("El procesamiento de señales no debe ejecutar órdenes")

    def order_check(self, *_args, **_kwargs):
        raise AssertionError("El procesamiento de señales no debe verificar órdenes")

    def positions_get(self, *_args, **_kwargs):
        raise AssertionError("El procesamiento de señales no debe consultar posiciones")


def service() -> SignalService:
    from app.services.market_data_service import MarketDataService
    config = Settings(_env_file=None)
    market = MarketDataService(FakeMT5(), initialize=False)
    # Fixed clock matching FakeMT5's bar baseline so the Fase 3 freshness
    # validation (real last-bar age vs now) does not reject fixture data whose
    # timestamps are anchored to 2026-01-01 regardless of when tests run.
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    return SignalService(market_data_service=market, config=config, clock=lambda: fixed_now)


@pytest.mark.asyncio
async def test_build_signal_runs_full_pipeline_without_crashing():
    signal = await service().build_signal("EURUSD")
    assert signal.pair == "EURUSD"
    assert signal.signal_type in ("HOLD", "BUY", "SELL")
    assert signal.timeframe == "M15"
    assert signal.higher_timeframe == "H1"


@pytest.mark.asyncio
async def test_build_signal_caches_already_analyzed_bar():
    instance = service()
    first = await instance.build_signal("EURUSD")
    second = await instance.build_signal("EURUSD")
    assert second.signal_type == "HOLD"
    assert second.reason == "Vela M15 ya analizada"
    assert second.bar_time == first.bar_time


@pytest.mark.asyncio
async def test_build_signal_rejects_insufficient_historical_bars():
    """Fase 3, section 7: fewer closed bars than configured must be rejected
    before indicators/strategy ever run, instead of silently operating on a
    truncated history."""
    from app.services.market_data_service import MarketDataService
    config = Settings(_env_file=None, LOWER_TIMEFRAME_BARS=400)  # FakeMT5 only has 300 M15 bars
    market = MarketDataService(FakeMT5(), initialize=False)
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    instance = SignalService(market_data_service=market, config=config, clock=lambda: fixed_now)
    with pytest.raises(InvalidMarketDataError, match="insuficientes"):
        await instance.build_signal("EURUSD")


@pytest.mark.asyncio
async def test_build_signal_rejects_stale_closed_bars_on_a_normal_trading_day():
    """Fase 3, section 7/8: data far older than tolerated must be rejected on
    a weekday (not a weekend close), so a real disconnection never silently
    produces a signal from old candles."""
    from app.services.market_data_service import MarketDataService
    config = Settings(_env_file=None)
    market = MarketDataService(FakeMT5(), initialize=False)
    # 2026-01-01 is a Thursday: ten hours later is still a normal trading day,
    # yet far beyond the M15/H1 staleness tolerance for data anchored at 00:00.
    stale_clock = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=10)
    instance = SignalService(market_data_service=market, config=config, clock=lambda: stale_clock)
    with pytest.raises(StaleMarketDataError):
        await instance.build_signal("EURUSD")


@pytest.mark.asyncio
async def test_build_signal_routes_every_mt5_call_through_the_injected_connection_manager():
    """Fase 3, section 5: SignalService must never call MT5 directly — every
    blocking call is handed to the connection manager."""
    from app.services.market_data_service import MarketDataService

    class RecordingConnection:
        def __init__(self):
            self.calls: list[str] = []

        async def run(self, func, *args, timeout=None):
            self.calls.append(getattr(func, "__name__", str(func)))
            return func(*args)

    config = Settings(_env_file=None)
    market = MarketDataService(FakeMT5(), initialize=False)
    connection = RecordingConnection()
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    instance = SignalService(market_data_service=market, config=config,
                              connection=connection, clock=lambda: fixed_now)
    await instance.build_signal("EURUSD")
    assert connection.calls == ["get_closed_bars", "get_closed_bars", "get_symbol_snapshot"]


@pytest.mark.asyncio
async def test_full_scan_pipeline_never_calls_order_execution_on_mt5(monkeypatch):
    """Dynamic regression (not just static grep): runs the real production entry
    point (scanner.scan_once -> SignalService -> MarketDataService -> StrategyService)
    against a MetaTrader5 mock whose order_send/order_check/positions_get raise if
    called. If any future change introduces order execution, this test fails loudly."""
    from app.workers import scanner

    class FakeNotifier:
        async def send_telegram_message(self, text):
            return None

    monkeypatch.setattr(scanner, "signal_service", service())
    monkeypatch.setattr(scanner, "notifier_service", FakeNotifier())
    monkeypatch.setattr(scanner, "settings", Settings(_env_file=None, FOREX_PAIRS="EURUSD,GBPUSD"))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: (candidates, []))
    await scanner.scan_once()  # would raise AssertionError via FakeMT5 if order execution occurred
