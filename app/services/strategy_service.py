from decimal import Decimal
from app.schemas.signal_schema import SignalSchema


class StrategyService:
    def generate_signal(
        self,
        pair: str,
        lower_tf: str,
        higher_tf: str,
        lower_indicators: dict,
        higher_indicators: dict,
    ) -> SignalSchema:
        close = lower_indicators.get("close")
        ema_fast = lower_indicators.get("ema_fast")
        ema_slow = lower_indicators.get("ema_slow")
        rsi = lower_indicators.get("rsi")
        macd = lower_indicators.get("macd")
        macd_signal = lower_indicators.get("macd_signal")
        atr = lower_indicators.get("atr")
        adx = lower_indicators.get("adx")

        htf_ema_fast = higher_indicators.get("ema_fast")
        htf_ema_slow = higher_indicators.get("ema_slow")

        required = [
            close,
            ema_fast,
            ema_slow,
            rsi,
            macd,
            macd_signal,
            htf_ema_fast,
            htf_ema_slow,
        ]

        if any(value is None for value in required):
            return SignalSchema(
                pair=pair,
                timeframe=lower_tf,
                signal_type="HOLD",
                reason="Datos incompletos",
            )

        # 1. Filtro de mercado lateral por distancia entre EMAs
        trend_strength = abs(ema_fast - ema_slow)
        if trend_strength < atr * 0.25:
            return SignalSchema(
                    pair=pair,
                    timeframe=lower_tf,
                    signal_type="HOLD",
                    reason="Mercado lateral (EMAs muy juntas)",
            )

        # 2. Filtro de volatilidad mínima
        if atr < close * 0.00035:
            return SignalSchema(
                    pair=pair,
                    timeframe=lower_tf,
                    signal_type="HOLD",
                    reason="Volatilidad baja",
            )

        # 2. Filtro de volatilidad mínima
        if adx < 25:
            return SignalSchema(
                pair=pair,
                timeframe=lower_tf,
                signal_type="HOLD",
                reason="Tendencia débil (ADX < 20)",
            )

        bullish_htf = htf_ema_fast > htf_ema_slow
        bearish_htf = htf_ema_fast < htf_ema_slow

        bullish_entry = (
            bullish_htf
            and ema_fast > ema_slow
            and rsi > 58
            and macd > macd_signal
        )

        bearish_entry = (
            bearish_htf
            and ema_fast < ema_slow
            and rsi < 42
            and macd < macd_signal
        )

        #Niveles de riesgo
        sl_risk = 1.2
        tp_risk = 1.5
        if bullish_entry:
            stop_lost = close - (atr * sl_risk)
            take_profit = close + (atr * tp_risk)
            return SignalSchema(
                pair=pair,
                timeframe=lower_tf,
                signal_type="BUY",
                entry=Decimal(str(round(close, 5))),
                stop_loss=Decimal(str(round(stop_lost, 5))),
                take_profit=Decimal(str(round(take_profit, 5))),
                confidence=0.80,
                reason="EMA alcista + RSI fuerte + MACD bullish + H1 alcista",
            )

        if bearish_entry:
            stop_lost = close + (atr * sl_risk)
            take_profit = close - (atr * tp_risk)
            return SignalSchema(
                pair=pair,
                timeframe=lower_tf,
                signal_type="SELL",
                entry=Decimal(str(round(close, 5))),
                stop_loss=Decimal(str(round(stop_lost, 5))),
                take_profit=Decimal(str(round(take_profit, 5))),
                confidence=0.80,
                reason="EMA bajista + RSI débil + MACD bearish + H1 bajista",
            )

        return SignalSchema(
            pair=pair,
            timeframe=lower_tf,
            signal_type="HOLD",
            reason="Sin setup fuerte",
        )