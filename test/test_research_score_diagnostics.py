"""Fase 6, section 10: score diagnostics, re-evaluating finding H-04 against
synthetic data with known properties."""

from __future__ import annotations

import pytest

from research.analysis.score_diagnostics import (
    ND,
    discriminatory_power,
    minimum_structural_score,
    pearson_correlation,
    score_distribution,
)


def test_score_distribution_counts_each_value():
    assert score_distribution([85, 100, 85, 85, 100]) == {85: 3, 100: 2}


def test_minimum_structural_score_matches_the_documented_h04_finding():
    """app/services/strategy_service.py's calculate_confidence_score adds
    25+20+15+15+10 unconditionally before any variable ADX component —
    this is the exact structural floor finding H-04 describes."""
    assert minimum_structural_score([25, 20, 15, 15, 10]) == 85


def test_pearson_correlation_is_one_for_a_perfectly_linear_relationship():
    xs = [1, 2, 3, 4, 5]
    ys = [2, 4, 6, 8, 10]
    assert pearson_correlation(xs, ys) == pytest.approx(1.0)


def test_pearson_correlation_is_negative_one_for_an_inverse_relationship():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 8, 6, 4, 2]
    assert pearson_correlation(xs, ys) == pytest.approx(-1.0)


def test_pearson_correlation_is_nd_when_the_score_never_varies():
    """Exactly the H-04 scenario once ADX also never varies: a constant
    predictor has no defined correlation with anything."""
    assert pearson_correlation([85, 85, 85], [1.0, -1.0, 2.0]) == ND


def test_pearson_correlation_is_nd_for_fewer_than_two_observations():
    assert pearson_correlation([85], [1.0]) == ND


def test_discriminatory_power_groups_by_score_and_flags_small_samples():
    scores = [85] * 3 + [100] * 12
    results = [1.0, -1.0, 1.0] + [2.0] * 12
    grouped = discriminatory_power(scores, results)
    assert grouped[85]["count"] == 3
    assert grouped[85]["note"] == "muestra pequeña, no concluyente"
    assert grouped[100]["count"] == 12
    assert grouped[100]["note"] is None
    assert grouped[100]["mean_result_r"] == 2.0
