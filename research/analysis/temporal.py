"""Fase 6, sections 6/7: chronological in-sample/out-of-sample splitting
with boundary purging, and fixed-parameter walk-forward windowing.

Both operate purely on a trade's ``entry_time``/``exit_time`` — never on
setup-detection time, never reordering trades, never using a value computed
from data that had not closed yet at that point. The strategy's parameters
are never refit per window here: section 7 explicitly requires this first
implementation to be a temporal-*stability* check (fixed rules evaluated
across consecutive periods), not an optimizing walk-forward.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, TypeVar


class TimedTrade(Protocol):
    entry_time: datetime
    exit_time: datetime | None


T = TypeVar("T", bound=TimedTrade)


@dataclass(slots=True)
class SplitResult:
    in_sample: list
    out_of_sample: list
    purged: list


def split_in_sample_out_of_sample(trades: Sequence[T], split_at: datetime) -> SplitResult:
    """A trade belongs to the period it opened in — entry is when capital/
    risk was actually committed. A trade that opened before ``split_at`` but
    only resolves on or after it is *purged* (excluded from both sets)
    rather than assigned: counting it as in-sample would let its recorded
    outcome depend on price action from the out-of-sample side of the
    boundary, exactly the "horizontes solapados" contamination section 6
    warns against. Purging only ever removes a trade from IN-sample
    consideration — an out-of-sample trade resolving using data at or past
    ``end`` of the full dataset is normal and not purged.
    """
    in_sample: list = []
    out_of_sample: list = []
    purged: list = []
    for trade in trades:
        if trade.entry_time < split_at:
            if trade.exit_time is not None and trade.exit_time <= split_at:
                in_sample.append(trade)
            else:
                purged.append(trade)
        else:
            out_of_sample.append(trade)
    return SplitResult(in_sample, out_of_sample, purged)


@dataclass(slots=True)
class Window:
    index: int
    start: datetime
    end: datetime
    trades: list


def walk_forward_windows(
    trades: Sequence[T], start: datetime, end: datetime, window_length_days: int,
) -> list[Window]:
    """Fixed-length, non-overlapping, chronologically consecutive windows
    covering ``[start, end)``, assigning each trade by ``entry_time``. Every
    window is returned unconditionally — including ones with zero trades or
    a negative result — section 7 explicitly forbids discarding unfavourable
    windows or reporting only the best ones.
    """
    if window_length_days <= 0:
        raise ValueError("window_length_days debe ser positivo")
    if end <= start:
        raise ValueError("end debe ser posterior a start")
    windows: list[Window] = []
    cursor = start
    index = 0
    step = timedelta(days=window_length_days)
    while cursor < end:
        window_end = min(cursor + step, end)
        window_trades = [t for t in trades if cursor <= t.entry_time < window_end]
        windows.append(Window(index, cursor, window_end, window_trades))
        cursor = window_end
        index += 1
    return windows
