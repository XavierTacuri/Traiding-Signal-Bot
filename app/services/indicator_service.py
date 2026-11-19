from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.config import Settings, settings


class IndicatorService:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config

    @staticmethod
    def _rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        rs = gain / loss.replace(0, np.nan)
        result = 100 - (100 / (1 + rs))
        return result.where(loss != 0, 100.0)

    @staticmethod
    def _atr_adx(data: pd.DataFrame, atr_period: int, adx_period: int) -> tuple[pd.Series, pd.Series]:
        previous_close = data["close"].shift(1)
        true_range = pd.concat(
            [(data["high"] - data["low"]), (data["high"] - previous_close).abs(),
             (data["low"] - previous_close).abs()], axis=1
        ).max(axis=1)
        atr = true_range.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
        up_move = data["high"].diff()
        down_move = -data["low"].diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        adx_atr = true_range.ewm(
            alpha=1 / adx_period, adjust=False, min_periods=adx_period
        ).mean()
        plus_di = 100 * plus_dm.ewm(
            alpha=1 / adx_period, adjust=False, min_periods=adx_period
        ).mean() / adx_atr
        minus_di = 100 * minus_dm.ewm(
            alpha=1 / adx_period, adjust=False, min_periods=adx_period
        ).mean() / adx_atr
        denominator = (plus_di + minus_di).replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / denominator
        adx = dx.ewm(alpha=1 / adx_period, adjust=False, min_periods=adx_period).mean()
        return atr, adx

    def add_indicators(self, df: pd.DataFrame, timeframe: str = "M15") -> pd.DataFrame:
        required = {"time", "open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Columnas OHLC faltantes: {', '.join(sorted(missing))}")
        data = df.copy().sort_values("time").reset_index(drop=True)
        if timeframe.upper() == "H1":
            data["ema_50"] = data["close"].ewm(span=self.config.TREND_EMA_FAST, adjust=False).mean()
            data["ema_200"] = data["close"].ewm(span=self.config.TREND_EMA_SLOW, adjust=False).mean()
        else:
            data["ema_20"] = data["close"].ewm(span=self.config.ENTRY_EMA_FAST, adjust=False).mean()
            data["ema_50"] = data["close"].ewm(span=self.config.ENTRY_EMA_SLOW, adjust=False).mean()
            data["rsi"] = self._rsi(data["close"], self.config.RSI_PERIOD)
        data["atr"], data["adx"] = self._atr_adx(
            data, self.config.ATR_PERIOD, self.config.ADX_PERIOD
        )
        return data

    def build_indicators(self, df: pd.DataFrame, timeframe: str = "M15") -> dict:
        data = self.add_indicators(df, timeframe)
        return self.values_at(data, timeframe)

    def values_at(self, data: pd.DataFrame, timeframe: str = "M15", index: int = -1) -> dict:
        """Extract causal values at an index from a precomputed indicator frame."""
        position = index if index >= 0 else len(data) + index
        minimum = self.config.TREND_EMA_SLOW + 2 if timeframe.upper() == "H1" else 55
        if position + 1 < minimum or position < 1:
            return {}
        last, previous = data.iloc[position], data.iloc[position - 1]
        if timeframe.upper() == "H1":
            keys = ["close", "ema_50", "ema_200", "adx"]
            if any(pd.isna(last[key]) for key in keys) or pd.isna(previous["ema_50"]):
                return {}
            return {"close": float(last.close), "ema_50": float(last.ema_50),
                    "previous_ema_50": float(previous.ema_50), "ema_200": float(last.ema_200),
                    "adx": float(last.adx), "time": last.time}
        previous_three = data.iloc[position - 3:position]
        swing = data.iloc[max(0, position - self.config.SWING_LOOKBACK_BARS + 1):position + 1]
        keys = ["open", "high", "low", "close", "ema_20", "ema_50", "rsi", "atr"]
        if any(pd.isna(last[key]) for key in keys) or pd.isna(previous.rsi):
            return {}
        return {key: float(last[key]) for key in keys} | {
            "previous_rsi": float(previous.rsi), "previous_high_3": float(previous_three.high.max()),
            "previous_low_3": float(previous_three.low.min()), "swing_low": float(swing.low.min()),
            "swing_high": float(swing.high.max()), "time": last.time,
        }
