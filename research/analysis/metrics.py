"""Fase 6, sections 8/9: pure, independently-testable trade-metric
calculations.

Operate on plain ``result_r`` sequences or lightweight trade-like objects
exposing ``.result_r``/``.entry_time``/``.exit_time`` (structural typing —
real ``backtesting.backtesting.BacktestTrade`` instances satisfy this
without any conversion) so they can be exercised with synthetic fixtures in
tests and reused unchanged against real backtest output.

Deliberately independent of ``backtesting.backtesting.BacktestReport.
metrics()``: that method is untouched (section 2 forbids replacing or
altering the backtester) and computes aggregates over *all* trades of one
run. These functions compute the same formulas over an arbitrary *subset*
(a walk-forward window, an IS/OOS split, a single pair) that
``BacktestReport`` has no notion of.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

ND = "N/D"

_Z_SCORES = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


class TradeLike(Protocol):
    result_r: float | None


def _z_score(confidence: float) -> float:
    try:
        return _Z_SCORES[confidence]
    except KeyError as exc:
        raise ValueError(
            f"Nivel de confianza no soportado: {confidence} (use {sorted(_Z_SCORES)})",
        ) from exc


def closed_results(trades: Sequence[TradeLike]) -> list[float]:
    """Only trades with a determined result_r (never None/open/ambiguous)."""
    return [t.result_r for t in trades if t.result_r is not None]


@dataclass(slots=True)
class TradeMetrics:
    sample_size: int
    closed_count: int
    wins: int
    losses: int
    win_rate: float | str
    profit_factor: float | str
    expectancy_r: float | str
    total_r: float
    average_win_r: float | str
    average_loss_r: float | str
    reward_risk_observed: float | str
    max_drawdown_r: float
    max_winning_streak: int
    max_losing_streak: int


def max_drawdown_r(results_in_order: Sequence[float]) -> float:
    """Drawdown expressed purely in R (running cumulative sum), independent
    of any capital or position-sizing assumption — section 8 explicitly
    forbids mixing % drawdown with R without a declared sizing model."""
    cumulative = peak = max_dd = 0.0
    for r in results_in_order:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def max_streak(results_in_order: Sequence[float], *, positive: bool) -> int:
    best = current = 0
    for r in results_in_order:
        if (r > 0) if positive else (r < 0):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def compute_metrics(trades: Sequence[TradeLike]) -> TradeMetrics:
    results = closed_results(trades)
    n = len(results)
    wins = [r for r in results if r > 0]
    losses = [r for r in results if r < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win: float | str = gross_profit / len(wins) if wins else ND
    avg_loss: float | str = gross_loss / len(losses) if losses else ND
    reward_risk: float | str = (
        avg_win / avg_loss if isinstance(avg_win, float) and isinstance(avg_loss, float) and avg_loss > 0 else ND
    )
    return TradeMetrics(
        sample_size=len(trades), closed_count=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n if n else ND,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else ND,
        expectancy_r=sum(results) / n if n else ND,
        total_r=sum(results), average_win_r=avg_win, average_loss_r=avg_loss,
        reward_risk_observed=reward_risk, max_drawdown_r=max_drawdown_r(results),
        max_winning_streak=max_streak(results, positive=True),
        max_losing_streak=max_streak(results, positive=False),
    )


def wilson_confidence_interval(wins: int, n: int, confidence: float = 0.95) -> tuple[float, float] | str:
    """Wilson score interval — far better calibrated than the naive normal
    approximation for the small/moderate sample sizes this strategy
    realistically produces, and needs no scipy dependency."""
    if n == 0:
        return ND
    z = _z_score(confidence)
    phat = wins / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    return max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom)


def normal_approx_mean_ci(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float] | str:
    """Simple cross-check for the expectancy CI; the bootstrap below is the
    primary method since it does not assume normally-distributed R outcomes."""
    n = len(values)
    if n < 2:
        return ND
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(variance / n)
    z = _z_score(confidence)
    return mean - z * stderr, mean + z * stderr


def block_bootstrap_statistic(
    values_in_time_order: Sequence[float], statistic_fn: Callable[[Sequence[float]], float],
    *, block_size: int, n_resamples: int, rng: random.Random,
) -> list[float]:
    """Moving block bootstrap (Künsch 1989): resamples overlapping
    contiguous blocks of ``block_size`` consecutive (time-ordered) trades
    instead of individual trades, to approximately preserve short-range
    dependence between nearby signals (e.g. simultaneous USD-correlated
    pairs, clustered volatility regimes) — section 9 explicitly forbids a
    naive i.i.d. resample, which would assume independence that trades close
    in time plausibly do not have.
    """
    n = len(values_in_time_order)
    if n == 0 or not (0 < block_size <= n):
        raise ValueError("block_size debe estar entre 1 y la longitud de la muestra")
    blocks_needed = math.ceil(n / block_size)
    max_start = n - block_size
    results: list[float] = []
    for _ in range(n_resamples):
        resample: list[float] = []
        for _ in range(blocks_needed):
            start = rng.randint(0, max_start)
            resample.extend(values_in_time_order[start:start + block_size])
        results.append(statistic_fn(resample[:n]))
    return results


def percentile_ci(bootstrap_samples: Sequence[float], confidence: float = 0.95) -> tuple[float, float] | str:
    if not bootstrap_samples:
        return ND
    ordered = sorted(bootstrap_samples)
    n = len(ordered)
    alpha = 1 - confidence
    lo_idx = max(0, int((alpha / 2) * n))
    hi_idx = min(n - 1, int((1 - alpha / 2) * n) - 1)
    return ordered[lo_idx], ordered[hi_idx]
