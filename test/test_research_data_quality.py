"""Fase 6, section 3: data-quality auditing, verified against synthetic
OHLC frames with deliberately injected defects."""

from __future__ import annotations

import pandas as pd

from research.analysis.data_quality import audit_ohlc_frame

M15 = pd.Timedelta(minutes=15)


def _clean_frame(n: int = 300, start="2026-01-05") -> pd.DataFrame:
    times = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "time": times, "open": 1.1, "high": 1.102, "low": 1.098, "close": 1.101,
    })


def test_clean_frame_reports_no_issues():
    report = audit_ohlc_frame(_clean_frame(300), M15)
    assert report["duplicated_timestamps"] == 0
    assert report["invalid_ohlc_rows"] == 0
    assert report["chronological"] is True
    assert report["intraweek_gap_count"] == 0
    assert report["weekend_gap_count"] == 0
    assert report["insufficient"] is False


def test_empty_frame_is_flagged_insufficient_without_raising():
    report = audit_ohlc_frame(pd.DataFrame(columns=["time", "open", "high", "low", "close"]), M15)
    assert report["insufficient"] is True
    assert report["row_count"] == 0


def test_a_frame_smaller_than_the_minimum_is_flagged_insufficient():
    """Section 3: 300 bars alone is not declared a sufficient history."""
    report = audit_ohlc_frame(_clean_frame(60), M15, minimum_bars=250)
    assert report["insufficient"] is True


def test_duplicated_timestamps_are_detected():
    frame = _clean_frame(10)
    frame.loc[5, "time"] = frame.loc[4, "time"]
    report = audit_ohlc_frame(frame, M15)
    assert report["duplicated_timestamps"] == 1


def test_invalid_ohlc_high_below_low_is_detected():
    frame = _clean_frame(10)
    frame.loc[3, ["high", "low"]] = [1.0, 1.05]  # high < low
    report = audit_ohlc_frame(frame, M15)
    assert report["invalid_ohlc_rows"] >= 1


def test_non_positive_price_is_detected():
    frame = _clean_frame(10)
    frame.loc[2, "close"] = 0.0
    report = audit_ohlc_frame(frame, M15)
    assert report["invalid_ohlc_rows"] >= 1


def test_intraweek_gap_is_distinguished_from_a_weekend_gap():
    frame = _clean_frame(5)
    # Introduce a 2-hour intraweek gap between bar 2 and 3.
    frame.loc[3:, "time"] = frame.loc[3:, "time"] + pd.Timedelta(hours=2)
    report = audit_ohlc_frame(frame, M15)
    assert report["intraweek_gap_count"] == 1
    assert report["weekend_gap_count"] == 0


def test_a_normal_weekend_gap_is_not_counted_as_an_intraweek_gap():
    frame = _clean_frame(5)
    frame.loc[3:, "time"] = frame.loc[3:, "time"] + pd.Timedelta(hours=54)  # Fri close -> Sun open
    report = audit_ohlc_frame(frame, M15)
    assert report["weekend_gap_count"] == 1
    assert report["intraweek_gap_count"] == 0


def test_non_chronological_order_is_detected():
    frame = _clean_frame(5)
    frame.loc[2, "time"], frame.loc[3, "time"] = frame.loc[3, "time"], frame.loc[2, "time"]
    report = audit_ohlc_frame(frame, M15)
    assert report["chronological"] is False
