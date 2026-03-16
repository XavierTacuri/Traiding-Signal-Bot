import pandas as pd

class IndicatorService:
    def add_indicator(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()

        #EMA
        data["ema_fast"] = data["close"].ewm(span=9, adjust=False).mean()
        data["ema_slow"] = data["close"].ewm(span=21, adjust=False).mean()

        #RSI
        delta = data["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()

        rs = avg_gain / avg_loss
        data["rsi"] = 100 -(100/(1 + rs))

        #MACD
        ema_12 = data["close"].ewm(span=12, adjust=False).mean()
        ema_26 = data["close"].ewm(span=26, adjust=False).mean()
        data["macd"] = ema_12 - ema_26
        data["macd_signal"] = data["macd"].ewm(span=9, adjust=False).mean()

        #ATR
        data["prev_close"] = data["close"].shift(1)

        data["tr1"] = data["high"] - data["low"]
        data["tr2"] = (data["high"] - data["prev_close"]).abs()
        data["tr3"] = (data["low"] - data["prev_close"]).abs()

        data["true_range"] = data[["tr1", "tr2", "tr3"]].max(axis=1)
        data["atr"] = data["true_range"].rolling(window=14).mean()

        # ADX (14)
        up_move = data["high"].diff()
        down_move = -data["low"].diff()

        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        atr_smooth = data["true_range"].rolling(window=14).mean()
        plus_di = 100 * (plus_dm.rolling(window=14).mean() / atr_smooth)
        minus_di = 100 * (minus_dm.rolling(window=14).mean() / atr_smooth)

        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).abs()) * 100
        data["adx"] = dx.rolling(window=14).mean()

        return data

    def build_indicators(self, df: pd.DataFrame )->dict:
        data = self.add_indicator(df)

        if len(data) < 2:
            return {
                "close": None,
                "ema_fast": None,
                "ema_slow": None,
                "rsi": None,
                "macd": None,
                "macd_signal": None,
                "atr": None,
                "volume": None,
                "time": None,
                "adx": None,
            }
        last = data.iloc[-1]
        return {"close": float(last["close"]),
                "ema_fast": float(last["ema_fast"]),
                "ema_slow": float(last["ema_slow"]),
                "rsi": float(last["rsi"]) if pd.notna(last["rsi"]) else None,
                "macd": float(last["macd"]),
                "atr": float(last["atr"]) if pd.notna(last["atr"]) else None,
                "macd_signal": float(last["macd_signal"]),
                "volume": float(last["tick_volume"]),
                "adx": float(last["adx"]) if pd.notna(last["adx"]) else None,
                "time": last["time"],}

