from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.indicator_service import IndicatorService


def candles(count=300):
    close = np.linspace(1.0, 1.2, count)
    return pd.DataFrame({"time": [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15*i) for i in range(count)],
        "open": close - .0005, "high": close + .001, "low": close - .001,
        "close": close, "tick_volume": 100})

def test_builds_h1_and_m15_without_macd_and_excludes_signal_bar_from_breakout():
    service = IndicatorService(Settings(_env_file=None))
    m15 = service.build_indicators(candles(), "M15")
    h1 = service.build_indicators(candles(), "H1")
    assert {"ema_20", "ema_50", "rsi", "atr", "previous_high_3"} <= m15.keys()
    assert {"ema_50", "ema_200", "previous_ema_50", "adx"} <= h1.keys()
    assert "macd" not in m15
    expected = candles().iloc[-4:-1].high.max()
    assert m15["previous_high_3"] == expected


def test_adx_period_is_independent_from_atr_period():
    frame = candles()
    fast = IndicatorService(Settings(_env_file=None, ADX_PERIOD=5)).add_indicators(frame, "H1")
    slow = IndicatorService(Settings(_env_file=None, ADX_PERIOD=30)).add_indicators(frame, "H1")
    assert fast["atr"].equals(slow["atr"])
    assert not fast["adx"].equals(slow["adx"])


@pytest.mark.parametrize("updates", [
    {"LOWER_TIMEFRAME": "H1"},
    {"SCAN_INTERVAL_SECONDS": 0},
    {"TREND_EMA_FAST": 200, "TREND_EMA_SLOW": 50},
    {"MIN_ADX": 50, "MAX_ADX": 20},
])
def test_invalid_strategy_configuration_fails_fast(updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)
