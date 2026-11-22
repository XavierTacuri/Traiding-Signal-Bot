"""Fase 4, section 12: pure pip/R calculations for virtual signal tracking.

Deliberately independent of backtesting/backtesting.py (section 19: the live
tracker must not depend on the backtester, and the backtester's own
``resolve_trade`` resolves a same-bar SL+TP clash with a conservative
*assumption* — see backtesting/backtesting.py's ``conservative`` flag — which
the Fase 4 brief explicitly forbids for the live tracker (section 10: must use
AMBIGUOUS, never default to a win or a loss). Sharing that function would
either silently inherit that assumption or require changing it, and either
one risks the backtester's historical numbers (section 19 forbids changing
them). The arithmetic duplicated below is intentionally tiny and easy to keep
consistent by inspection instead.

No function here converts a result to money: the bot places no real orders
and has no real position size, so only pips and R multiples are computed
(section 12 explicitly forbids a monetary figure).
"""

from __future__ import annotations

from decimal import Decimal


def pip_size(digits: int) -> Decimal:
    """Price distance of one pip for a symbol with this many decimal digits.

    MT5 "fractional pip" brokers quote 5-digit majors (point = 1/100000) and
    3-digit JPY pairs (point = 1/1000) — one pip is 10 points on both. A
    4-digit/2-digit quote (point already equals a pip) needs no scaling.
    Using ``digits`` (not the pair's name) is what actually determines this:
    it works identically for JPY and non-JPY symbols, and for any
    broker-specific symbol prefix/suffix (section 12 forbids assuming a fixed
    pip size per pair).
    """
    point = Decimal(1).scaleb(-digits)
    return point * 10 if digits in (3, 5) else point


def price_diff_to_pips(diff: Decimal, digits: int) -> Decimal:
    size = pip_size(digits)
    if size == 0:
        raise ValueError("El tamaño de pip no puede ser cero")
    return diff / size


def calculate_result_pips(
    direction: str, entry_price: Decimal, exit_price: Decimal, digits: int,
) -> Decimal:
    """Signed pips: positive is profit, negative is loss, for either side."""
    diff = exit_price - entry_price if direction == "BUY" else entry_price - exit_price
    return price_diff_to_pips(diff, digits)


def calculate_result_r(
    direction: str, entry_price: Decimal, exit_price: Decimal, risk_distance: Decimal,
) -> Decimal:
    """Result expressed in multiples of the originally planned risk (1R).

    ``risk_distance`` must be the strategy's planned |entry - stop|, computed
    once when the signal was generated and never recomputed from a live
    quote — section 9 forbids basing R on anything but the original,
    unmodified Stop Loss. A realized exit exactly at the stop is, by
    construction, -1.0 R; exactly at the target is +RISK_REWARD_RATIO R.
    """
    if risk_distance <= 0:
        raise ValueError("La distancia de riesgo debe ser positiva para calcular R")
    diff = exit_price - entry_price if direction == "BUY" else entry_price - exit_price
    return diff / risk_distance
