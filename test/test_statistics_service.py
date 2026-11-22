"""Fase 4, section 13: performance statistics — explicit denominators, N/D
instead of invented numbers, ambiguous/pending/expired never counted as a
win or a loss."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.signal_record import SignalRecord, SignalStatus
from app.services.statistics_service import ND, compute_statistics


def _record(status, symbol="EURUSD", direction="BUY", result_r=None, result_pips=None,
            closed_at=None, key=None) -> SignalRecord:
    return SignalRecord(
        signal_key=key or f"{symbol}-{status}-{result_r}", symbol=symbol, direction=direction,
        timeframe="M15", entry_price="1.1", stop_loss="1.09", take_profit="1.12",
        risk_distance="0.01", digits=5, created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        status=status, result_r=result_r, result_pips=result_pips, closed_at_utc=closed_at,
    )


def test_empty_history_reports_nd_instead_of_dividing_by_zero():
    stats = compute_statistics([])
    assert stats.total_signals == 0
    assert stats.win_rate == ND
    assert stats.expectancy_r == ND
    assert stats.profit_factor == ND


def test_counts_signals_by_status():
    records = [
        _record(SignalStatus.PENDING_ACTIVATION),
        _record(SignalStatus.ACTIVE),
        _record(SignalStatus.TAKE_PROFIT, result_r=2.0, result_pips=100.0),
        _record(SignalStatus.STOP_LOSS, result_r=-1.0, result_pips=-50.0),
        _record(SignalStatus.EXPIRED),
        _record(SignalStatus.AMBIGUOUS),
        _record(SignalStatus.CANCELLED),
    ]
    stats = compute_statistics(records)
    assert stats.total_signals == 7
    assert stats.pending_activation == 1
    assert stats.active == 1
    assert stats.take_profit == 1
    assert stats.stop_loss == 1
    assert stats.expired == 1
    assert stats.ambiguous == 1
    assert stats.cancelled == 1


def test_win_rate_and_expectancy_use_only_determined_results():
    records = [
        _record(SignalStatus.TAKE_PROFIT, result_r=2.0, result_pips=100.0, key="a"),
        _record(SignalStatus.STOP_LOSS, result_r=-1.0, result_pips=-50.0, key="b"),
        _record(SignalStatus.AMBIGUOUS, key="c"),  # must not count toward win rate
        _record(SignalStatus.PENDING_ACTIVATION, key="d"),  # must not count either
    ]
    stats = compute_statistics(records)
    assert stats.determined_results == 2
    assert stats.win_rate == 0.5
    assert stats.expectancy_r == 0.5  # (2.0 + -1.0) / 2
    assert stats.total_result_r == 1.0
    assert stats.total_pips == 50.0


def test_profit_factor_is_nd_when_there_are_no_losses():
    records = [_record(SignalStatus.TAKE_PROFIT, result_r=2.0, result_pips=100.0, key="a")]
    stats = compute_statistics(records)
    assert stats.profit_factor == ND  # no losses -> gross_loss is 0, division undefined


def test_profit_factor_is_computed_when_both_sides_exist():
    records = [
        _record(SignalStatus.TAKE_PROFIT, result_r=2.0, result_pips=100.0, key="a"),
        _record(SignalStatus.STOP_LOSS, result_r=-1.0, result_pips=-50.0, key="b"),
    ]
    stats = compute_statistics(records)
    assert stats.profit_factor == 2.0  # gross_profit(2.0) / gross_loss(1.0)


def test_breakdown_by_pair_and_direction_excludes_undetermined_from_win_rate():
    records = [
        _record(SignalStatus.TAKE_PROFIT, symbol="EURUSD", direction="BUY", result_r=2.0,
                result_pips=100.0, key="a"),
        _record(SignalStatus.STOP_LOSS, symbol="GBPUSD", direction="SELL", result_r=-1.0,
                result_pips=-50.0, key="b"),
        _record(SignalStatus.AMBIGUOUS, symbol="EURUSD", direction="BUY", key="c"),
    ]
    stats = compute_statistics(records)
    assert stats.by_pair["EURUSD"]["signals"] == 2
    assert stats.by_pair["EURUSD"]["determined_results"] == 1
    assert stats.by_pair["EURUSD"]["win_rate"] == 1.0
    assert stats.by_direction["SELL"]["win_rate"] == 0.0


def test_breakdown_by_day_groups_by_closed_at_date():
    day1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    day2 = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
    records = [
        _record(SignalStatus.TAKE_PROFIT, result_r=2.0, result_pips=100.0, closed_at=day1, key="a"),
        _record(SignalStatus.STOP_LOSS, result_r=-1.0, result_pips=-50.0, closed_at=day2, key="b"),
    ]
    stats = compute_statistics(records)
    assert set(stats.by_day) == {"2026-01-01", "2026-01-02"}
