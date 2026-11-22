"""Fase 4, section 12: pip/R calculations — deterministic, no I/O."""

from decimal import Decimal

import pytest

from app.services.tracking_calculations import (
    calculate_result_pips,
    calculate_result_r,
    pip_size,
    price_diff_to_pips,
)


def test_pip_size_is_ten_points_for_five_digit_non_jpy_quotes():
    assert pip_size(5) == Decimal("0.0001")


def test_pip_size_is_ten_points_for_three_digit_jpy_quotes():
    assert pip_size(3) == Decimal("0.01")


def test_pip_size_equals_the_point_for_four_and_two_digit_quotes():
    assert pip_size(4) == Decimal("0.0001")
    assert pip_size(2) == Decimal("0.01")


def test_price_diff_to_pips_converts_a_raw_price_distance():
    assert price_diff_to_pips(Decimal("0.0050"), digits=5) == Decimal(50)


def test_calculate_result_pips_buy_profit_and_loss_non_jpy():
    entry, digits = Decimal("1.10000"), 5
    assert calculate_result_pips("BUY", entry, Decimal("1.10050"), digits) == Decimal(5)
    assert calculate_result_pips("BUY", entry, Decimal("1.09950"), digits) == Decimal(-5)


def test_calculate_result_pips_sell_profit_and_loss_jpy():
    entry, digits = Decimal("150.000"), 3
    assert calculate_result_pips("SELL", entry, Decimal("149.900"), digits) == Decimal(10)
    assert calculate_result_pips("SELL", entry, Decimal("150.100"), digits) == Decimal(-10)


def test_calculate_result_r_matches_risk_reward_ratio_at_exact_target():
    entry, stop, target = Decimal("1.1000"), Decimal("1.0950"), Decimal("1.1100")
    risk = abs(entry - stop)
    assert calculate_result_r("BUY", entry, target, risk) == Decimal(2)


def test_calculate_result_r_is_minus_one_at_exact_stop():
    entry, stop = Decimal("1.1000"), Decimal("1.0950")
    risk = abs(entry - stop)
    assert calculate_result_r("BUY", entry, stop, risk) == Decimal(-1)


def test_calculate_result_r_rejects_non_positive_risk_distance():
    with pytest.raises(ValueError, match="positiva"):
        calculate_result_r("BUY", Decimal("1.1"), Decimal("1.2"), Decimal(0))


def test_sell_direction_result_r_is_symmetric_to_buy():
    entry, stop = Decimal("1.3000"), Decimal("1.3050")  # SELL: stop above entry
    risk = abs(entry - stop)
    assert calculate_result_r("SELL", entry, Decimal("1.2900"), risk) == Decimal(2)
    assert calculate_result_r("SELL", entry, stop, risk) == Decimal(-1)
