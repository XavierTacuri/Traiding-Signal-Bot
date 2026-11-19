from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.market_data_service import MarketDataService, normalize_price


class FakeMT5:
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60

    def __init__(self, initialize_ok=True, rates=None, range_rates=None, info=None, tick=None):
        self.initialize_ok = initialize_ok
        self.rates = rates
        self.range_rates = range_rates
        self.info = info
        self.tick = tick

    def initialize(self):
        return self.initialize_ok

    def last_error(self):
        return (1, "fake error")

    def symbol_info(self, name):
        return SimpleNamespace(name=name, point=0.00001, digits=5) if self.info is None else self.info

    def symbols_get(self):
        return ()

    def symbol_info_tick(self, name):
        return self.tick

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        return self.rates

    def copy_rates_range(self, symbol, timeframe, start, end):
        return self.range_rates


def bar(hour, close=1.1):
    return {"time": int(datetime(2026, 1, 1, hour, tzinfo=UTC).timestamp()),
            "open": close, "high": close + 0.001, "low": close - 0.001, "close": close}


def test_init_raises_when_mt5_initialize_fails():
    with pytest.raises(RuntimeError, match="Error al conectar MT5"):
        MarketDataService(FakeMT5(initialize_ok=False), initialize=True)


def test_init_skips_initialize_when_not_requested():
    service = MarketDataService(FakeMT5(initialize_ok=False), initialize=False)
    assert service is not None


def test_validate_pair_rejects_unauthorized_symbol():
    with pytest.raises(ValueError, match="no autorizado"):
        MarketDataService.validate_pair("XAUUSD")


def test_get_closed_bars_excludes_live_candle_and_sorts():
    rates = [bar(2), bar(0), bar(1)]
    service = MarketDataService(FakeMT5(rates=rates), initialize=False)
    frame = service.get_closed_bars("EURUSD", "M15", 300)
    assert list(frame["time"].dt.hour) == [0, 1, 2]
    assert frame.attrs["broker_symbol"] == "EURUSD"


def test_get_closed_bars_raises_for_unsupported_timeframe():
    service = MarketDataService(FakeMT5(rates=[bar(0)]), initialize=False)
    with pytest.raises(ValueError, match="Timeframe no soportado"):
        service.get_closed_bars("EURUSD", "M5", 10)


def test_get_closed_bars_raises_when_mt5_returns_nothing():
    service = MarketDataService(FakeMT5(rates=None), initialize=False)
    with pytest.raises(RuntimeError, match="No se obtuvieron velas cerradas"):
        service.get_closed_bars("EURUSD", "M15", 10)


def test_get_market_data_compatibility_wrapper():
    service = MarketDataService(FakeMT5(rates=[bar(0), bar(1)]), initialize=False)
    frame = service.get_market_data("EURUSD", FakeMT5.TIMEFRAME_M15, count=10)
    assert len(frame) == 2


def test_get_market_data_rejects_unknown_mt5_timeframe_constant():
    service = MarketDataService(FakeMT5(rates=[bar(0)]), initialize=False)
    with pytest.raises(ValueError, match="Timeframe MT5 no soportado"):
        service.get_market_data("EURUSD", 999, count=10)


def test_get_historical_bars_requires_timezone_aware_dates():
    service = MarketDataService(FakeMT5(), initialize=False)
    with pytest.raises(ValueError, match="zona horaria"):
        service.get_historical_bars("EURUSD", "M15", datetime(2025, 1, 1), datetime(2025, 1, 2))


def test_get_historical_bars_requires_start_before_end():
    service = MarketDataService(FakeMT5(), initialize=False)
    start = datetime(2025, 1, 2, tzinfo=UTC)
    end = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="anterior al final"):
        service.get_historical_bars("EURUSD", "M15", start, end)


def test_get_historical_bars_filters_still_open_final_candle():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, 3, tzinfo=UTC)
    range_rates = [bar(0), bar(1), bar(2)]
    service = MarketDataService(FakeMT5(range_rates=range_rates), initialize=False)
    now = datetime(2026, 1, 1, 1, 10, tzinfo=UTC)  # only the 00:00 M15 candle has fully closed
    closed = service.get_historical_bars("EURUSD", "M15", start, end, now=now)
    assert list(closed["time"].dt.hour) == [0]


def test_get_historical_bars_raises_when_nothing_closed_yet():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, 3, tzinfo=UTC)
    service = MarketDataService(FakeMT5(range_rates=[bar(2)]), initialize=False)
    now = datetime(2026, 1, 1, 2, 5, tzinfo=UTC)  # M15 candle at 02:00 has not closed yet
    with pytest.raises(RuntimeError, match="No hay velas cerradas"):
        service.get_historical_bars("EURUSD", "M15", start, end, now=now)


def test_get_symbol_snapshot_valid():
    info = SimpleNamespace(digits=5, point=0.00001)
    tick = SimpleNamespace(bid=1.1000, ask=1.1002)
    service = MarketDataService(FakeMT5(info=info, tick=tick), initialize=False)
    snapshot = service.get_symbol_snapshot("EURUSD")
    assert snapshot.digits == 5
    assert snapshot.spread == snapshot.ask - snapshot.bid


def test_get_symbol_snapshot_rejects_crossed_bid_ask():
    info = SimpleNamespace(digits=5, point=0.00001)
    tick = SimpleNamespace(bid=1.1002, ask=1.1000)  # ask < bid: invalid
    service = MarketDataService(FakeMT5(info=info, tick=tick), initialize=False)
    with pytest.raises(RuntimeError, match="inválidos"):
        service.get_symbol_snapshot("EURUSD")


def test_get_symbol_snapshot_requires_tick_data():
    info = SimpleNamespace(digits=5, point=0.00001)
    service = MarketDataService(FakeMT5(info=info, tick=None), initialize=False)
    with pytest.raises(RuntimeError, match="Faltan especificaciones o tick"):
        service.get_symbol_snapshot("EURUSD")


def test_normalize_price_rounds_half_up():
    assert normalize_price(1.123456, 5) == Decimal("1.12346")
    assert str(normalize_price(1.00005, 4)) == "1.0001"
