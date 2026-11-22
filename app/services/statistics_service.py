"""Fase 4, section 13: performance statistics over persisted virtual signals.

Every ratio here is computed over an explicit, documented denominator
(``_ratio`` returns "N/D" rather than dividing by zero or silently treating
an undetermined signal as a loss). PENDING_ACTIVATION, EXPIRED, CANCELLED and
AMBIGUOUS signals are counted in totals/breakdowns but never folded into
win-rate, expectancy or profit factor — only TAKE_PROFIT/STOP_LOSS results
are "determined" (app/models/signal_record.DETERMINED_RESULT_STATUSES).

These are virtual, simulated statistics: no money amount is computed (the
bot places no real orders and has no real position size), matching section
12/13 of the brief.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.models.signal_record import (
    DETERMINED_RESULT_STATUSES,
    SignalRecord,
    SignalStatus,
)
from app.repositories.signal_repository import SignalRepository

ND = "N/D"


def _ratio(numerator: float, denominator: int) -> float | str:
    return numerator / denominator if denominator else ND


@dataclass(slots=True)
class PerformanceStatistics:
    total_signals: int = 0
    pending_activation: int = 0
    active: int = 0
    take_profit: int = 0
    stop_loss: int = 0
    expired: int = 0
    cancelled: int = 0
    ambiguous: int = 0
    determined_results: int = 0
    win_rate: float | str = ND
    total_result_r: float = 0.0
    expectancy_r: float | str = ND
    profit_factor: float | str = ND
    total_pips: float = 0.0
    by_pair: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_direction: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_day: dict[str, dict[str, Any]] = field(default_factory=dict)


def _breakdown(records: list[SignalRecord]) -> dict[str, Any]:
    determined = [record for record in records if record.status in DETERMINED_RESULT_STATUSES]
    wins = [record for record in determined if (record.result_r or 0) > 0]
    total_r = sum(record.result_r or 0 for record in determined)
    return {
        "signals": len(records),
        "determined_results": len(determined),
        "wins": len(wins),
        "win_rate": _ratio(len(wins), len(determined)),
        "total_result_r": round(total_r, 4),
        "total_pips": round(sum(record.result_pips or 0 for record in determined), 2),
    }


def compute_statistics(records: list[SignalRecord]) -> PerformanceStatistics:
    stats = PerformanceStatistics(total_signals=len(records))
    determined: list[SignalRecord] = []
    by_pair: dict[str, list[SignalRecord]] = defaultdict(list)
    by_direction: dict[str, list[SignalRecord]] = defaultdict(list)
    by_day: dict[str, list[SignalRecord]] = defaultdict(list)

    for record in records:
        if record.status == SignalStatus.PENDING_ACTIVATION:
            stats.pending_activation += 1
        elif record.status == SignalStatus.ACTIVE:
            stats.active += 1
        elif record.status == SignalStatus.TAKE_PROFIT:
            stats.take_profit += 1
        elif record.status == SignalStatus.STOP_LOSS:
            stats.stop_loss += 1
        elif record.status == SignalStatus.EXPIRED:
            stats.expired += 1
        elif record.status == SignalStatus.CANCELLED:
            stats.cancelled += 1
        elif record.status == SignalStatus.AMBIGUOUS:
            stats.ambiguous += 1
        by_pair[record.symbol].append(record)
        by_direction[record.direction].append(record)
        if record.closed_at_utc is not None:
            by_day[record.closed_at_utc.date().isoformat()].append(record)
        if record.status in DETERMINED_RESULT_STATUSES:
            determined.append(record)

    stats.determined_results = len(determined)
    wins = [record for record in determined if (record.result_r or 0) > 0]
    losses = [record for record in determined if (record.result_r or 0) < 0]
    stats.win_rate = _ratio(len(wins), len(determined))
    stats.total_result_r = round(sum(record.result_r or 0 for record in determined), 4)
    stats.expectancy_r = (
        round(stats.total_result_r / len(determined), 4) if determined else ND
    )
    gross_profit = sum(record.result_r or 0 for record in wins)
    gross_loss = abs(sum(record.result_r or 0 for record in losses))
    stats.profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else ND
    stats.total_pips = round(sum(record.result_pips or 0 for record in determined), 2)
    stats.by_pair = {pair: _breakdown(group) for pair, group in by_pair.items()}
    stats.by_direction = {direction: _breakdown(group) for direction, group in by_direction.items()}
    stats.by_day = {day: _breakdown(group) for day, group in by_day.items()}
    return stats


class StatisticsService:
    def __init__(self, signal_repository: SignalRepository) -> None:
        self._signals = signal_repository

    async def compute(self) -> PerformanceStatistics:
        records = await self._signals.get_all()
        return compute_statistics(records)
