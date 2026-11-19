from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.domain import SignalType, TrendDirection
from app.services.market_data_service import SymbolSnapshot, normalize_price
from app.services.strategy_service import StrategyService

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

def config(**updates) -> Settings:
    return Settings(_env_file=None, **updates)

def higher(bullish=True, adx=27.0):
    return {"close": 1.12 if bullish else 1.08, "ema_50": 1.11 if bullish else 1.09,
            "previous_ema_50": 1.109 if bullish else 1.091,
            "ema_200": 1.10, "adx": adx, "time": NOW}

def lower(time=NOW, buy=True, pullback=False):
    if buy:
        values = {"open": 1.1005, "high": 1.1012, "low": 1.0998 if pullback else 1.1004,
            "close": 1.1002 if pullback else 1.1010, "ema_20": 1.1000 if pullback else 1.1003,
            "ema_50": 1.0995, "rsi": 54.0, "previous_rsi": 51.0,
            "previous_high_3": 1.1008, "previous_low_3": 1.0990,
            "swing_low": 1.0995, "swing_high": 1.1012, "atr": 0.001}
    else:
        values = {"open": 1.0995, "high": 1.1002 if pullback else 1.0996, "low": 1.0988,
            "close": 1.0998 if pullback else 1.0990, "ema_20": 1.1000 if pullback else 1.0997,
            "ema_50": 1.1005, "rsi": 46.0, "previous_rsi": 49.0,
            "previous_high_3": 1.1010, "previous_low_3": 1.0992,
            "swing_low": 1.0988, "swing_high": 1.1005, "atr": 0.001}
    return values | {"time": time}

def snapshot(digits=5, spread="0.00008"):
    bid = Decimal("1.10000")
    return SymbolSnapshot("EURUSD", "EURUSDm", bid, bid + Decimal(spread),
                          Decimal("0.00001"), digits)

def create_setup(service, buy=True):
    signal = service.generate_signal("EURUSD", "M15", "H1", lower(pullback=True, buy=buy),
                                     higher(bullish=buy), snapshot())
    assert signal.signal_type == "HOLD" and "setup pendiente" in signal.reason

def test_h1_bullish_bearish_and_no_trend():
    service = StrategyService(config())
    assert service.determine_higher_timeframe_trend(higher(True)) == TrendDirection.BULLISH
    assert service.determine_higher_timeframe_trend(higher(False)) == TrendDirection.BEARISH
    assert service.determine_higher_timeframe_trend(higher(True, 10)) == TrendDirection.NONE

@pytest.mark.parametrize(("buy", "expected"), [(True, SignalType.BUY), (False, SignalType.SELL)])
def test_pullback_detection(buy, expected):
    service = StrategyService(config())
    trend = TrendDirection.BULLISH if buy else TrendDirection.BEARISH
    assert service.detect_pullback(trend, lower(buy=buy, pullback=True)) == expected

@pytest.mark.parametrize("buy", [True, False])
def test_setup_requires_later_confirmation_and_generates_signal(buy):
    service = StrategyService(config())
    create_setup(service, buy)
    signal = service.generate_signal("EURUSD", "M15", "H1",
        lower(NOW + timedelta(minutes=15), buy=buy), higher(bullish=buy), snapshot())
    assert signal.signal_type == ("BUY" if buy else "SELL")
    assert signal.score == 100 and signal.confidence == 1
    assert signal.risk_reward_ratio == Decimal("2.0")

def test_setup_expires_after_four_bars():
    service = StrategyService(config())
    create_setup(service)
    signal = service.generate_signal("EURUSD", "M15", "H1",
        lower(NOW + timedelta(minutes=75)), higher(), snapshot())
    assert signal.signal_type == "HOLD" and "retroceso" in signal.reason.lower()

def test_rejections_for_rsi_weak_candle_extension_spread_and_zero_atr():
    cases = [
        ({"previous_rsi": 53.0}, snapshot(), "RSI"),
        ({"open": 1.1009}, snapshot(), "Vela"),
        ({"close": 1.1020}, snapshot(), "extendido"),
        ({}, snapshot(spread="0.0002"), "Spread"),
    ]
    for changes, tick, reason in cases:
        service = StrategyService(config())
        create_setup(service)
        data = lower(NOW + timedelta(minutes=15)) | changes
        result = service.generate_signal("EURUSD", "M15", "H1", data, higher(), tick)
        assert reason.lower() in result.reason.lower()
    service = StrategyService(config())
    data = lower() | {"atr": 0.0}
    assert "ATR" in service.generate_signal("EURUSD", "M15", "H1", data, higher(), snapshot()).reason

def test_structural_stops_take_profit_and_precision():
    service = StrategyService(config())
    buy_stop = service.calculate_swing_stop(SignalType.BUY, lower())
    sell_stop = service.calculate_swing_stop(SignalType.SELL, lower(buy=False))
    assert buy_stop < lower()["close"] and sell_stop > lower(buy=False)["close"]
    assert normalize_price(1.123456, 5) == Decimal("1.12346")
    assert normalize_price(151.1236, 3) == Decimal("151.124")

def test_unauthorized_assets_and_missing_data():
    service = StrategyService(config())
    for symbol in ("XAUUSD", "XAGUSD", "USOIL"):
        assert "no autorizado" in service.generate_signal(symbol, "M15", "H1", lower(), higher(), snapshot()).reason
    assert "incompletos" in service.generate_signal("EURUSD", "M15", "H1", {}, higher(), snapshot()).reason
