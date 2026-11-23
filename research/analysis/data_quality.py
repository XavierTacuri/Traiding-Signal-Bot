"""Fase 6, section 3: read-only historical data quality auditing.

Never fills a gap, never interpolates a missing bar, never fabricates a
value — this only *reports* what is actually present in a given OHLC
DataFrame (the same ``time, open, high, low, close`` shape
``backtesting.backtesting.normalize_frame`` already expects).
"""

from __future__ import annotations

import pandas as pd

WEEKEND_GAP_THRESHOLD = pd.Timedelta(hours=50)


def audit_ohlc_frame(frame: pd.DataFrame, expected_bar_delta: pd.Timedelta,
                      minimum_bars: int = 250) -> dict:
    """``minimum_bars`` is just a reporting floor (flags ``insufficient``),
    not a value the strategy itself uses — section 3 explicitly warns that
    300 bars (the earlier smoke test) is not a statistically sufficient
    history on its own."""
    if frame.empty:
        return {"row_count": 0, "insufficient": True, "reason": "dataframe vacío"}

    duplicated = int(frame["time"].duplicated().sum())
    chronological = bool(frame["time"].is_monotonic_increasing)
    invalid_ohlc = (
        (frame["high"] < frame["low"])
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    )
    gaps = frame["time"].diff().dropna()
    intraweek_gaps = gaps[(gaps > expected_bar_delta) & (gaps < WEEKEND_GAP_THRESHOLD)]
    weekend_gaps = gaps[gaps >= WEEKEND_GAP_THRESHOLD]

    return {
        "row_count": len(frame),
        "first_time": frame["time"].iloc[0],
        "last_time": frame["time"].iloc[-1],
        "duplicated_timestamps": duplicated,
        "chronological": chronological,
        "invalid_ohlc_rows": int(invalid_ohlc.sum()),
        "intraweek_gap_count": len(intraweek_gaps),
        "max_intraweek_gap_seconds": (
            float(intraweek_gaps.max().total_seconds()) if len(intraweek_gaps) else 0.0
        ),
        "weekend_gap_count": len(weekend_gaps),
        "insufficient": len(frame) < minimum_bars,
    }
