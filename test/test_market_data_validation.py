from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.services.market_data_validation import (
    InvalidMarketDataError,
    StaleMarketDataError,
    is_forex_weekend_close,
    validate_bar_freshness,
    validate_closed_bars,
    validate_tick_freshness,
)


def bars(count: int, *, minutes: int, start: datetime) -> pd.DataFrame:
    rows = []
    for index in range(count):
        close = 1.1000 + 0.0001 * index
        rows.append({"time": start + timedelta(minutes=minutes * index),
                     "open": close, "high": close + 0.0005, "low": close - 0.0005, "close": close})
    return pd.DataFrame(rows)


def test_validate_closed_bars_accepts_well_formed_data():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    validate_closed_bars(bars(300, minutes=15, start=start), "M15", count=300)


def test_validate_closed_bars_rejects_empty_frame():
    with pytest.raises(InvalidMarketDataError, match="Sin velas"):
        validate_closed_bars(pd.DataFrame(), "M15", count=1)


def test_validate_closed_bars_rejects_insufficient_history():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(InvalidMarketDataError, match="insuficientes"):
        validate_closed_bars(bars(50, minutes=15, start=start), "M15", count=300)


def test_validate_closed_bars_rejects_out_of_order_timestamps():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = bars(5, minutes=15, start=start)
    frame.loc[[1, 2]] = frame.loc[[2, 1]].values
    with pytest.raises(InvalidMarketDataError, match="orden cronológico"):
        validate_closed_bars(frame, "M15", count=5)


def test_validate_closed_bars_rejects_duplicated_timestamps():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = bars(5, minutes=15, start=start)
    frame.loc[1, "time"] = frame.loc[0, "time"]
    with pytest.raises(InvalidMarketDataError, match="duplicados"):
        validate_closed_bars(frame, "M15", count=5)


def test_validate_closed_bars_rejects_non_finite_values():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = bars(5, minutes=15, start=start)
    frame.loc[2, "close"] = float("nan")
    with pytest.raises(InvalidMarketDataError, match="no finitos"):
        validate_closed_bars(frame, "M15", count=5)


def test_validate_closed_bars_rejects_non_positive_prices():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = bars(5, minutes=15, start=start)
    frame.loc[3, "low"] = -0.001
    with pytest.raises(InvalidMarketDataError, match="no positivos"):
        validate_closed_bars(frame, "M15", count=5)


def test_validate_closed_bars_rejects_high_below_low():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = bars(5, minutes=15, start=start)
    frame.loc[4, "high"], frame.loc[4, "low"] = frame.loc[4, "low"], frame.loc[4, "high"]
    with pytest.raises(InvalidMarketDataError, match="high menor que low"):
        validate_closed_bars(frame, "M15", count=5)


def test_validate_bar_freshness_accepts_recently_closed_bar():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    frame = bars(1, minutes=15, start=start)
    now = start + timedelta(minutes=16)  # bar closes at 00:15, barely after
    validate_bar_freshness(frame, "M15", now=now, max_age_multiplier=3.0)


def test_validate_bar_freshness_rejects_stale_bar():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    frame = bars(1, minutes=15, start=start)
    now = start + timedelta(hours=5)  # far beyond 3x the M15 duration
    with pytest.raises(StaleMarketDataError):
        validate_bar_freshness(frame, "M15", now=now, max_age_multiplier=3.0)


def test_validate_tick_freshness_rejects_old_tick():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    tick_time = now - timedelta(seconds=300)
    with pytest.raises(StaleMarketDataError):
        validate_tick_freshness(tick_time, now=now, max_age_seconds=120)


def test_validate_tick_freshness_accepts_recent_tick():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    tick_time = now - timedelta(seconds=5)
    validate_tick_freshness(tick_time, now=now, max_age_seconds=120)


def test_validate_tick_freshness_skips_check_when_tick_time_is_unknown():
    """Fail open: a test double or an exotic broker tick without a timestamp
    must never be treated as stale by fabricating an age."""
    validate_tick_freshness(None, now=datetime.now(UTC), max_age_seconds=1)


@pytest.mark.parametrize("weekday_offset,hour,expected", [
    (0, 12, False),   # Monday noon: open
    (3, 12, False),   # Thursday noon: open
    (4, 20, False),   # Friday 20:00 UTC: still open
    (4, 21, True),    # Friday 21:00 UTC: closed
    (5, 12, True),    # Saturday: closed
    (6, 12, True),    # Sunday 12:00 UTC: still closed
    (6, 21, False),   # Sunday 21:00 UTC: reopened
    (6, 22, False),   # Sunday 22:00 UTC: open
])
def test_is_forex_weekend_close_matches_the_standard_retail_trading_week(weekday_offset, hour, expected):
    # 2026-01-05 is a Monday (weekday 0); offset lands on the target weekday.
    monday = datetime(2026, 1, 5, tzinfo=UTC)
    moment = monday + timedelta(days=weekday_offset, hours=hour)
    assert is_forex_weekend_close(moment) is expected


def test_is_forex_weekend_close_requires_timezone_aware_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_forex_weekend_close(datetime(2026, 1, 1))
