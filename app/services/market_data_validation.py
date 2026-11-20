"""Market-data freshness and integrity checks (Fase 3).

Pure, synchronous functions kept independent from ``MarketDataService`` so the
existing MT5-mocked tests for that class stay untouched. Called by
``SignalService`` right before indicators/strategy run, on data already
fetched through ``MT5ConnectionManager``.

These checks are an operational safeguard, not a trading-rule change: they
only decide whether the already-closed-bar data handed to the (unmodified)
strategy is trustworthy. They must never themselves distinguish a closed
market from a disconnection — callers are expected to only invoke the bar
freshness check when the market is expected to be open.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import numpy as np
import pandas as pd

TIMEFRAME_DELTAS = {"M15": timedelta(minutes=15), "H1": timedelta(hours=1)}

# Standard retail-broker Forex week: closes Friday ~21:00 UTC (US session
# close), reopens Sunday ~21:00 UTC (Sydney session open). A heuristic, not a
# per-broker calendar: it does not know about holidays or a specific broker's
# daylight-saving transition, which is why it is used only to AVOID false
# "stale data" alarms over a normal weekend close, never to declare the
# market definitively open.
_WEEKEND_CLOSE_UTC = (4, time(21, 0))  # Friday
_WEEKEND_OPEN_UTC = (6, time(21, 0))   # Sunday

_OHLC_COLUMNS = ("open", "high", "low", "close")


class InvalidMarketDataError(RuntimeError):
    """Structural problem with OHLC data: missing, non-finite, unordered, duplicated."""


class StaleMarketDataError(RuntimeError):
    """Data is structurally valid but too old to trust for a new signal."""


def validate_closed_bars(frame: pd.DataFrame, timeframe: str, *, count: int) -> None:
    """Integrity checks shared by both timeframes: never raises for a closed market.

    ``count`` is the number of bars the caller requested; fewer than that means
    insufficient history (e.g. a freshly-opened broker history or a truncated
    download), independent of whether the available bars themselves are valid.
    """
    if frame is None or frame.empty:
        raise InvalidMarketDataError(f"Sin velas {timeframe} disponibles")
    if len(frame) < count:
        raise InvalidMarketDataError(
            f"Datos históricos {timeframe} insuficientes: {len(frame)}/{count} velas"
        )
    if frame["time"].isna().any():
        raise InvalidMarketDataError(f"Timestamps faltantes en velas {timeframe}")
    if not frame["time"].is_monotonic_increasing:
        raise InvalidMarketDataError(f"Velas {timeframe} fuera de orden cronológico")
    if frame["time"].duplicated().any():
        raise InvalidMarketDataError(f"Timestamps duplicados en velas {timeframe}")
    numeric = frame[list(_OHLC_COLUMNS)].to_numpy(dtype="float64")
    if not np.isfinite(numeric).all():
        raise InvalidMarketDataError(f"Valores OHLC no finitos en velas {timeframe}")
    if (numeric <= 0).any():
        raise InvalidMarketDataError(f"Valores OHLC no positivos en velas {timeframe}")
    highs, lows = frame["high"].to_numpy(), frame["low"].to_numpy()
    if (highs < lows).any():
        raise InvalidMarketDataError(f"Vela {timeframe} con high menor que low")


def is_forex_weekend_close(now: datetime) -> bool:
    """True when ``now`` (must be UTC-aware) falls in the standard weekend
    close window. A conservative heuristic — see module docstring — meant only
    to suppress false "stale data" alarms, never to assert the market is open.
    """
    if now.tzinfo is None:
        raise ValueError("now debe ser timezone-aware (UTC)")
    now = now.astimezone(UTC)
    moment = (now.weekday(), now.timetz().replace(tzinfo=None))
    return _WEEKEND_CLOSE_UTC <= moment < _WEEKEND_OPEN_UTC


def validate_bar_freshness(
    frame: pd.DataFrame,
    timeframe: str,
    *,
    now: datetime,
    max_age_multiplier: float,
) -> None:
    """Reject bars whose last close is older than tolerated. Caller decides
    whether to even run this (e.g. skip during a known weekend/session close)."""
    delta = TIMEFRAME_DELTAS[timeframe.upper()]
    last_close = frame["time"].iloc[-1] + delta
    age = now - last_close.to_pydatetime()
    tolerance = delta * max_age_multiplier
    if age > tolerance:
        raise StaleMarketDataError(
            f"Última vela {timeframe} cerrada hace {age}, excede la tolerancia {tolerance}"
        )


def validate_tick_freshness(
    tick_time: datetime | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> None:
    """No-op (fail open) when the tick carries no timestamp metadata — the
    production MT5 tick always has one, but lightweight test doubles may not,
    and silently skipping is safer than inventing a fabricated timestamp."""
    if tick_time is None:
        return
    age = (now - tick_time).total_seconds()
    if age > max_age_seconds:
        raise StaleMarketDataError(f"Último tick con {age:.0f}s de antigüedad (máximo {max_age_seconds:.0f}s)")
