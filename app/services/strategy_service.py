from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.config import FOREX_PAIRS, Settings, settings
from app.domain import PendingSetup, SetupStatus, SetupType, SignalType, TrendDirection
from app.schemas.signal_schema import SignalSchema
from app.services.market_data_service import SymbolSnapshot, normalize_price
from app.services.state_service import InMemorySetupRepository, SetupRepository


class StrategyService:
    def __init__(self, config: Settings = settings, setups: SetupRepository | None = None) -> None:
        self.config = config
        self.setups = setups or InMemorySetupRepository()

    def _hold(self, pair: str, reason: str, bar_time: datetime | None = None) -> SignalSchema:
        return SignalSchema(pair=pair, timeframe=self.config.LOWER_TIMEFRAME,
                            higher_timeframe=self.config.HIGHER_TIMEFRAME,
                            signal_type=SignalType.HOLD, reason=reason, bar_time=bar_time)

    @staticmethod
    def validate_required_data(values: dict) -> bool:
        return bool(values) and all(value is not None and
            not (isinstance(value, (float, int)) and not math.isfinite(value))
            for value in values.values())

    def determine_higher_timeframe_trend(self, indicators: dict) -> TrendDirection:
        if not self.validate_required_data(indicators):
            return TrendDirection.NONE
        ema_50, previous = indicators["ema_50"], indicators["previous_ema_50"]
        ema_200, close, adx = indicators["ema_200"], indicators["close"], indicators["adx"]
        if adx < self.config.MIN_ADX:
            return TrendDirection.NONE
        if ema_50 > ema_200 and ema_50 > previous and close > ema_50:
            return TrendDirection.BULLISH
        if ema_50 < ema_200 and ema_50 < previous and close < ema_50:
            return TrendDirection.BEARISH
        return TrendDirection.NONE

    def detect_pullback(self, trend: TrendDirection, lower: dict) -> SignalType | None:
        tolerance = lower["atr"] * self.config.PULLBACK_DISTANCE_ATR_MULT
        # The candle range must intersect an ATR-wide zone around either EMA.
        touches = lambda ema: lower["low"] <= ema + tolerance and lower["high"] >= ema - tolerance
        if trend == TrendDirection.BULLISH and (touches(lower["ema_20"]) or touches(lower["ema_50"])) \
                and lower["close"] >= lower["ema_20"]:
            return SignalType.BUY
        if trend == TrendDirection.BEARISH and (touches(lower["ema_20"]) or touches(lower["ema_50"])) \
                and lower["close"] <= lower["ema_20"]:
            return SignalType.SELL
        return None

    def _active_setup(self, pair: str, direction: SignalType, bar_time: datetime) -> PendingSetup | None:
        setup = self.setups.get(pair, direction)
        if setup is None:
            return None
        elapsed = int((bar_time - setup.detected_bar_time).total_seconds() // (15 * 60))
        if elapsed > setup.expires_after_bars:
            self.setups.mark(setup, SetupStatus.EXPIRED)
            return None
        return setup if bar_time > setup.detected_bar_time else None

    def _create_setup(self, pair: str, direction: SignalType, lower: dict) -> PendingSetup:
        bar_time = lower["time"]
        setup = PendingSetup(pair, direction, datetime.now(UTC), bar_time,
                             self.config.SETUP_EXPIRATION_BARS,
                             pullback_low=lower["low"], pullback_high=lower["high"])
        self.setups.save(setup)
        return setup

    def detect_rsi_recovery(self, direction: SignalType, lower: dict) -> bool:
        if direction == SignalType.BUY:
            return lower["previous_rsi"] <= self.config.RSI_BUY_TRIGGER < lower["rsi"]
        return lower["previous_rsi"] >= self.config.RSI_SELL_TRIGGER > lower["rsi"]

    @staticmethod
    def detect_structure_break(direction: SignalType, lower: dict) -> bool:
        return (lower["close"] > lower["previous_high_3"] if direction == SignalType.BUY
                else lower["close"] < lower["previous_low_3"])

    def validate_confirmation_candle(self, direction: SignalType, lower: dict) -> bool:
        body = abs(lower["close"] - lower["open"])
        directional = lower["close"] > lower["open"] if direction == SignalType.BUY else lower["close"] < lower["open"]
        return directional and body >= lower["atr"] * self.config.MIN_BODY_ATR_MULT

    def validate_spread(self, snapshot: SymbolSnapshot, atr: float) -> bool:
        return atr > 0 and float(snapshot.spread) / atr <= self.config.MAX_SPREAD_ATR_RATIO

    def calculate_swing_stop(self, direction: SignalType, lower: dict) -> float | None:
        atr, entry = lower["atr"], lower["close"]
        stop = (lower["swing_low"] - atr * self.config.STOP_BUFFER_ATR_MULT if direction == SignalType.BUY
                else lower["swing_high"] + atr * self.config.STOP_BUFFER_ATR_MULT)
        risk_atr = abs(entry - stop) / atr if atr > 0 else 0
        valid_side = stop < entry if direction == SignalType.BUY else stop > entry
        if not valid_side or not self.config.MIN_STOP_ATR_MULT <= risk_atr <= self.config.MAX_STOP_ATR_MULT:
            return None
        return stop

    def calculate_confidence_score(self, higher: dict, lower: dict, snapshot: SymbolSnapshot) -> int:
        score = 25 + 20 + 15 + 15 + 10
        if higher["adx"] >= 25:
            score += 15
        if higher["adx"] > self.config.MAX_ADX and abs(lower["close"] - lower["ema_20"]) > lower["atr"]:
            score -= 5
        return max(0, min(100, score))

    def _session_open(self, bar_time: datetime) -> bool:
        aware = bar_time if bar_time.tzinfo else bar_time.replace(tzinfo=ZoneInfo(self.config.MT5_TIMEZONE))
        local_time = aware.astimezone(ZoneInfo(self.config.TRADING_TIMEZONE)).timetz().replace(tzinfo=None)
        def in_window(value, start, end):
            return start <= value < end if start <= end else value >= start or value < end
        return in_window(local_time, self.config.SESSION_START, self.config.SESSION_END) and not in_window(
            local_time, self.config.ROLLOVER_BLOCK_START, self.config.ROLLOVER_BLOCK_END)

    def generate_signal(self, pair: str, lower_tf: str, higher_tf: str,
                        lower_indicators: dict, higher_indicators: dict,
                        symbol_snapshot: SymbolSnapshot | None = None) -> SignalSchema:
        pair = pair.upper()
        bar_time = lower_indicators.get("time")
        if pair not in FOREX_PAIRS:
            return self._hold(pair, "Instrumento no autorizado; solo se analizan pares Forex", bar_time)
        if lower_tf.upper() != self.config.LOWER_TIMEFRAME or higher_tf.upper() != self.config.HIGHER_TIMEFRAME:
            return self._hold(pair, "Timeframes no configurados", bar_time)
        if not self.validate_required_data(lower_indicators) or not self.validate_required_data(higher_indicators):
            return self._hold(pair, "Datos incompletos o no finitos", bar_time)
        if lower_indicators["atr"] <= 0 or lower_indicators["close"] <= 0:
            return self._hold(pair, "ATR o precio inválido", bar_time)
        if not self._session_open(bar_time):
            return self._hold(pair, "Fuera de la sesión Forex configurada o ventana de rollover", bar_time)
        if lower_indicators["atr"] / lower_indicators["close"] < self.config.MIN_VOLATILITY_RATIO:
            return self._hold(pair, "Volatilidad ATR insuficiente", bar_time)
        trend = self.determine_higher_timeframe_trend(higher_indicators)
        if trend == TrendDirection.NONE:
            return self._hold(pair, "Tendencia H1 no confirmada", bar_time)
        direction = SignalType.BUY if trend == TrendDirection.BULLISH else SignalType.SELL
        setup = self._active_setup(pair, direction, bar_time)
        pullback = self.detect_pullback(trend, lower_indicators)
        if setup is None:
            if pullback == direction:
                self._create_setup(pair, direction, lower_indicators)
                return self._hold(pair, "Retroceso detectado; setup pendiente de confirmación", bar_time).model_copy(
                    update={"setup_type": SetupType.BULLISH_PULLBACK if direction == SignalType.BUY else SetupType.BEARISH_PULLBACK})
            return self._hold(pair, "Sin retroceso válido", bar_time)
        if abs(lower_indicators["close"] - lower_indicators["ema_20"]) > lower_indicators["atr"] * self.config.MAX_PRICE_EMA_DISTANCE_ATR_MULT:
            return self._hold(pair, "Precio demasiado extendido respecto a EMA 20", bar_time)
        if symbol_snapshot is None:
            return self._hold(pair, "Datos de spread no disponibles", bar_time)
        if not self.validate_spread(symbol_snapshot, lower_indicators["atr"]):
            return self._hold(pair, "Spread demasiado alto respecto al ATR", bar_time)
        if not self.detect_rsi_recovery(direction, lower_indicators):
            return self._hold(pair, "Sin recuperación de momentum RSI", bar_time)
        if not self.detect_structure_break(direction, lower_indicators):
            return self._hold(pair, "Sin ruptura de estructura de corto plazo", bar_time)
        if not self.validate_confirmation_candle(direction, lower_indicators):
            return self._hold(pair, "Vela de confirmación débil o contraria", bar_time)
        ema_side = lower_indicators["close"] > lower_indicators["ema_20"] if direction == SignalType.BUY else lower_indicators["close"] < lower_indicators["ema_20"]
        if not ema_side:
            return self._hold(pair, "Cierre sin recuperar la EMA 20", bar_time)
        stop = self.calculate_swing_stop(direction, lower_indicators)
        if stop is None:
            return self._hold(pair, "Stop estructural inválido o fuera de límites ATR", bar_time)
        risk = abs(lower_indicators["close"] - stop)
        if risk <= 0:
            return self._hold(pair, "Riesgo estructural inválido", bar_time)
        target = lower_indicators["close"] + risk * self.config.RISK_REWARD_RATIO * (1 if direction == SignalType.BUY else -1)
        if target <= 0:
            return self._hold(pair, "Take Profit inválido", bar_time)
        score = self.calculate_confidence_score(higher_indicators, lower_indicators, symbol_snapshot)
        if score < self.config.MIN_SIGNAL_SCORE:
            return self._hold(pair, "Score de confianza insuficiente", bar_time)
        self.setups.mark(setup, SetupStatus.CONSUMED)
        signal_key = f"{pair}:{lower_tf.upper()}:{direction}:{bar_time.isoformat()}"
        setup_type = SetupType.BULLISH_PULLBACK if direction == SignalType.BUY else SetupType.BEARISH_PULLBACK
        return SignalSchema(pair=pair, broker_symbol=symbol_snapshot.broker_symbol,
            timeframe=lower_tf.upper(), higher_timeframe=higher_tf.upper(), signal_type=direction,
            entry=normalize_price(lower_indicators["close"], symbol_snapshot.digits),
            stop_loss=normalize_price(stop, symbol_snapshot.digits),
            take_profit=normalize_price(target, symbol_snapshot.digits),
            risk_reward_ratio=Decimal(str(self.config.RISK_REWARD_RATIO)), confidence=score / 100,
            score=score, reason="Retroceso a EMA + recuperación de RSI + ruptura de estructura",
            bar_time=bar_time, setup_type=setup_type, adx_h1=higher_indicators["adx"],
            rsi_m15=lower_indicators["rsi"], signal_key=signal_key, digits=symbol_snapshot.digits)
