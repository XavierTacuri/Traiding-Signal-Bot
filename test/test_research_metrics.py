"""Fase 6, section 18: pure metric formulas, verified against synthetic
data with known expected values — never used as evidence of real
profitability, only of correct arithmetic."""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from research.analysis.metrics import (
    ND,
    block_bootstrap_statistic,
    closed_results,
    compute_metrics,
    max_drawdown_r,
    max_streak,
    normal_approx_mean_ci,
    percentile_ci,
    wilson_confidence_interval,
)


@dataclass(slots=True)
class _Trade:
    result_r: float | None


def _trades(*results: float | None) -> list[_Trade]:
    return [_Trade(r) for r in results]


def test_closed_results_excludes_none():
    trades = _trades(1.0, None, -1.0, None)
    assert closed_results(trades) == [1.0, -1.0]


def test_compute_metrics_on_a_known_synthetic_sample():
    trades = _trades(2.0, -1.0, 2.0, -1.0, -1.0)  # 2 wins, 3 losses
    metrics = compute_metrics(trades)
    assert metrics.closed_count == 5
    assert metrics.wins == 2 and metrics.losses == 3
    assert metrics.win_rate == pytest.approx(0.4)
    assert metrics.total_r == pytest.approx(1.0)
    assert metrics.profit_factor == pytest.approx(4.0 / 3.0)
    assert metrics.expectancy_r == pytest.approx(0.2)
    assert metrics.average_win_r == pytest.approx(2.0)
    assert metrics.average_loss_r == pytest.approx(1.0)
    assert metrics.reward_risk_observed == pytest.approx(2.0)


def test_compute_metrics_reports_nd_for_an_empty_sample():
    metrics = compute_metrics([])
    assert metrics.win_rate == ND
    assert metrics.profit_factor == ND
    assert metrics.expectancy_r == ND


def test_profit_factor_is_nd_without_any_losses():
    metrics = compute_metrics(_trades(1.0, 2.0))
    assert metrics.profit_factor == ND


def test_open_trades_are_excluded_from_the_sample_but_counted_separately():
    trades = _trades(1.0, None, -1.0)
    metrics = compute_metrics(trades)
    assert metrics.sample_size == 3
    assert metrics.closed_count == 2


def test_max_drawdown_r_on_a_known_sequence():
    # Cumulative: 1, 3, 2, 0, -1, 1 -> peak 3, trough -1 -> drawdown 4
    results = [1.0, 2.0, -1.0, -2.0, -1.0, 2.0]
    assert max_drawdown_r(results) == pytest.approx(4.0)


def test_max_drawdown_r_is_zero_for_a_monotonically_winning_sequence():
    assert max_drawdown_r([1.0, 1.0, 1.0]) == 0.0


def test_max_streak_counts_consecutive_wins_and_losses():
    results = [1.0, 1.0, -1.0, 1.0, 1.0, 1.0, -1.0, -1.0]
    assert max_streak(results, positive=True) == 3
    assert max_streak(results, positive=False) == 2


def test_wilson_interval_contains_the_point_estimate_and_narrows_with_n():
    narrow_n = wilson_confidence_interval(wins=40, n=100)
    wide_n = wilson_confidence_interval(wins=4, n=10)
    assert narrow_n[0] <= 0.40 <= narrow_n[1]
    assert wide_n[0] <= 0.40 <= wide_n[1]
    assert (narrow_n[1] - narrow_n[0]) < (wide_n[1] - wide_n[0])


def test_wilson_interval_is_nd_for_zero_sample_size():
    assert wilson_confidence_interval(0, 0) == ND


def test_wilson_interval_rejects_an_unsupported_confidence_level():
    with pytest.raises(ValueError):
        wilson_confidence_interval(5, 10, confidence=0.5)


def test_normal_approx_ci_is_nd_below_two_observations():
    assert normal_approx_mean_ci([1.0]) == ND


def test_normal_approx_ci_contains_the_sample_mean():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    lo, hi = normal_approx_mean_ci(values)
    assert lo <= (sum(values) / len(values)) <= hi


def test_block_bootstrap_is_reproducible_with_a_seeded_rng():
    values = [1.0, -1.0, 2.0, -1.0, 1.0, -2.0, 1.0, 1.0]
    first = block_bootstrap_statistic(values, lambda s: sum(s) / len(s),
                                       block_size=2, n_resamples=200, rng=random.Random(42))
    second = block_bootstrap_statistic(values, lambda s: sum(s) / len(s),
                                        block_size=2, n_resamples=200, rng=random.Random(42))
    assert first == second


def test_block_bootstrap_rejects_a_block_size_larger_than_the_sample():
    with pytest.raises(ValueError):
        block_bootstrap_statistic([1.0, 2.0], lambda s: sum(s), block_size=5,
                                   n_resamples=10, rng=random.Random(1))


def test_percentile_ci_brackets_a_known_uniform_bootstrap_distribution():
    samples = [float(i) for i in range(1, 101)]  # 1..100
    lo, hi = percentile_ci(samples, confidence=0.95)
    assert lo <= 5
    assert hi >= 95


def test_percentile_ci_is_nd_for_an_empty_sample():
    assert percentile_ci([]) == ND
