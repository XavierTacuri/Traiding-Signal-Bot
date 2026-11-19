from __future__ import annotations

from app.core.config import Settings, settings
from app.schemas.signal_schema import SignalSchema
from app.services.indicator_service import IndicatorService
from app.services.market_data_service import MarketDataService
from app.services.strategy_service import StrategyService


class SignalService:
    def __init__(self, market_data_service: MarketDataService | None = None,
                 indicator_service: IndicatorService | None = None,
                 strategy_service: StrategyService | None = None,
                 config: Settings = settings) -> None:
        self.config = config
        self.market = market_data_service or MarketDataService()
        self.indicators = indicator_service or IndicatorService(config)
        self.strategy = strategy_service or StrategyService(config)
        self._last_analyzed_bar: dict[str, object] = {}

    async def build_signal(self, pair: str) -> SignalSchema:
        lower = self.market.get_closed_bars(pair, self.config.LOWER_TIMEFRAME,
                                            self.config.LOWER_TIMEFRAME_BARS)
        latest_time = lower.iloc[-1]["time"]
        if self._last_analyzed_bar.get(pair) == latest_time:
            return SignalSchema(pair=pair, timeframe=self.config.LOWER_TIMEFRAME,
                                higher_timeframe=self.config.HIGHER_TIMEFRAME,
                                signal_type="HOLD", reason="Vela M15 ya analizada", bar_time=latest_time)
        higher = self.market.get_closed_bars(pair, self.config.HIGHER_TIMEFRAME,
                                             self.config.HIGHER_TIMEFRAME_BARS)
        lower_values = self.indicators.build_indicators(lower, self.config.LOWER_TIMEFRAME)
        higher_values = self.indicators.build_indicators(higher, self.config.HIGHER_TIMEFRAME)
        snapshot = self.market.get_symbol_snapshot(pair)
        signal = self.strategy.generate_signal(pair, self.config.LOWER_TIMEFRAME,
            self.config.HIGHER_TIMEFRAME, lower_values, higher_values, snapshot)
        self._last_analyzed_bar[pair] = latest_time
        return signal
