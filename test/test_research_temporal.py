"""Fase 6, sections 6/7: IS/OOS split with purging, and walk-forward
windowing — verified against synthetic, hand-checked trade timelines."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from research.analysis.temporal import (
    split_in_sample_out_of_sample,
    walk_forward_windows,
)


@dataclass(slots=True)
class _Trade:
    entry_time: datetime
    exit_time: datetime | None


def _dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def test_split_assigns_by_entry_time_relative_to_the_boundary():
    split_at = _dt(10)
    trades = [
        _Trade(_dt(5), _dt(6)),   # fully before -> IS
        _Trade(_dt(15), _dt(16)),  # fully after -> OOS
    ]
    result = split_in_sample_out_of_sample(trades, split_at)
    assert result.in_sample == [trades[0]]
    assert result.out_of_sample == [trades[1]]
    assert result.purged == []


def test_a_trade_opened_before_the_boundary_but_closed_after_is_purged_not_assigned():
    split_at = _dt(10)
    crossing = _Trade(_dt(8), _dt(12))
    result = split_in_sample_out_of_sample([crossing], split_at)
    assert result.in_sample == []
    assert result.out_of_sample == []
    assert result.purged == [crossing]


def test_a_still_open_trade_opened_before_the_boundary_is_purged():
    split_at = _dt(10)
    still_open = _Trade(_dt(8), None)
    result = split_in_sample_out_of_sample([still_open], split_at)
    assert result.purged == [still_open]


def test_a_trade_opened_exactly_at_the_boundary_is_out_of_sample():
    split_at = _dt(10)
    trade = _Trade(split_at, split_at + timedelta(hours=1))
    result = split_in_sample_out_of_sample([trade], split_at)
    assert result.out_of_sample == [trade]


def test_a_trade_exiting_exactly_at_the_boundary_is_in_sample():
    split_at = _dt(10)
    trade = _Trade(_dt(9), split_at)
    result = split_in_sample_out_of_sample([trade], split_at)
    assert result.in_sample == [trade]


def test_every_input_trade_is_accounted_for_in_exactly_one_bucket():
    split_at = _dt(10)
    trades = [_Trade(_dt(d), _dt(d) + timedelta(hours=1)) for d in range(1, 20)]
    trades.append(_Trade(_dt(9), _dt(14)))  # one crosser
    result = split_in_sample_out_of_sample(trades, split_at)
    assert len(result.in_sample) + len(result.out_of_sample) + len(result.purged) == len(trades)


def test_walk_forward_windows_cover_the_full_range_without_gaps_or_overlap():
    start, end = _dt(1), _dt(31)
    windows = walk_forward_windows([], start, end, window_length_days=10)
    assert windows[0].start == start
    assert windows[-1].end == end
    for first, second in itertools.pairwise(windows):
        assert first.end == second.start


def test_walk_forward_windows_assigns_trades_by_entry_time():
    # window0=[day1,day11) window1=[day11,day21) window2=[day21,day31)
    start, end = _dt(1), _dt(31)
    trades = [_Trade(_dt(3), _dt(4)), _Trade(_dt(12), _dt(13)), _Trade(_dt(25), None)]
    windows = walk_forward_windows(trades, start, end, window_length_days=10)
    assert len(windows) == 3
    assert [t.entry_time for t in windows[0].trades] == [_dt(3)]
    assert [t.entry_time for t in windows[1].trades] == [_dt(12)]
    assert [t.entry_time for t in windows[2].trades] == [_dt(25)]


def test_walk_forward_windows_are_never_discarded_even_with_zero_trades():
    """Section 7: no silently dropping periods without signals."""
    start, end = _dt(1), _dt(31)
    windows = walk_forward_windows([], start, end, window_length_days=10)
    assert len(windows) == 3
    assert all(w.trades == [] for w in windows)


def test_walk_forward_rejects_a_non_positive_window_length():
    with pytest.raises(ValueError):
        walk_forward_windows([], _dt(1), _dt(10), window_length_days=0)


def test_walk_forward_rejects_an_end_before_start():
    with pytest.raises(ValueError):
        walk_forward_windows([], _dt(10), _dt(1), window_length_days=5)
