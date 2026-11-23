"""Fase 6, section 10: diagnostics for the confidence score — re-evaluation
of audit finding H-04. Read-only analysis; never changes
``MIN_SIGNAL_SCORE``, the score formula, or its weights (those stay
untouched in ``app/services/strategy_service.py``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

ND = "N/D"


def score_distribution(scores: Sequence[int]) -> dict[int, int]:
    return dict(sorted(Counter(scores).items()))


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> float | str:
    """Correlation between score and result_r. Plain Pearson: result_r is
    continuous, not binary, so this is the appropriate statistic (not a
    point-biserial, which is for a dichotomous variable)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return ND
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return ND
    return cov / (var_x**0.5 * var_y**0.5)


def minimum_structural_score(mandatory_component_weights: Sequence[int]) -> int:
    """The score's "mandatory" components (section 10 / finding H-04) are
    added unconditionally once ``calculate_confidence_score`` runs — every
    hard gate before it (trend/pullback/RSI/breakout/candle/spread) must
    already be True for execution to reach that point. This is simply their
    fixed sum: the structural floor H-04 describes, computed from the
    weights themselves rather than from any live signal."""
    return sum(mandatory_component_weights)


def discriminatory_power(scores: Sequence[int], results_r: Sequence[float]) -> dict:
    """Mean result_r grouped by observed score value — shows whether a
    higher score is actually associated with a better outcome in this
    sample, or whether (per H-04) it mostly just reflects an ADX bonus
    unrelated to trade quality. Groups with too few trades are flagged,
    never silently dropped."""
    grouped: dict[int, list[float]] = {}
    for score, result in zip(scores, results_r, strict=True):
        grouped.setdefault(score, []).append(result)
    return {
        score: {
            "count": len(values),
            "mean_result_r": sum(values) / len(values),
            "note": None if len(values) >= 10 else "muestra pequeña, no concluyente",
        }
        for score, values in sorted(grouped.items())
    }
