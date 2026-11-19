from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import Settings
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
    return SignalService(market_data_service=market, config=config)


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
    from app.services.state_service import InMemorySignalKeyRepository
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    await scanner.scan_once()  # would raise AssertionError via FakeMT5 if order execution occurred
