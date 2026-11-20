from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, settings
from app.schemas.signal_schema import SignalSchema
from app.services.indicator_service import IndicatorService
from app.services.market_data_service import MarketDataService
from app.services.market_data_validation import (
    is_forex_weekend_close,
    validate_bar_freshness,
    validate_closed_bars,
    validate_tick_freshness,
)
from app.services.mt5_connection import MT5ConnectionManager
from app.services.strategy_service import StrategyService


class SignalService:
    def __init__(self, market_data_service: MarketDataService | None = None,
                 indicator_service: IndicatorService | None = None,
                 strategy_service: StrategyService | None = None,
                 config: Settings = settings,
                 connection: MT5ConnectionManager | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.config = config
        self.market = market_data_service or MarketDataService(initialize=False)
        self.indicators = indicator_service or IndicatorService(config)
        self.strategy = strategy_service or StrategyService(config)
        self.connection = connection
        self._now = clock or (lambda: datetime.now(UTC))
        self._last_analyzed_bar: dict[str, object] = {}

    async def _call(self, func: Any, *args: Any) -> Any:
        """Run a blocking MT5 call off the event loop (H-13).

        Routed through the shared connection manager when one is configured
        (production path: calls are serialized with initialize/shutdown and
        bounded by MT5_REQUEST_TIMEOUT_SECONDS). Falls back to a plain
        ``asyncio.to_thread`` when no manager is injected, which is what every
        existing unit test exercises against synchronous MT5 test doubles.
        """
        if self.connection is not None:
            return await self.connection.run(func, *args, timeout=self.config.MT5_REQUEST_TIMEOUT_SECONDS)
        return await asyncio.to_thread(func, *args)

    def _validate_market_data(self, pair: str, lower, higher, snapshot) -> None:
        now = self._now()
        validate_closed_bars(lower, self.config.LOWER_TIMEFRAME, count=self.config.LOWER_TIMEFRAME_BARS)
        validate_closed_bars(higher, self.config.HIGHER_TIMEFRAME, count=self.config.HIGHER_TIMEFRAME_BARS)
        # A normal weekend close looks identical to stale data (no new candles,
        # no new ticks) — only enforce freshness when the market is expected
        # to be trading, so Fase 3 never confuses market closure with a
        # disconnection (section 8 of the brief).
        if not is_forex_weekend_close(now):
            validate_bar_freshness(lower, self.config.LOWER_TIMEFRAME, now=now,
                                    max_age_multiplier=self.config.MT5_STALE_BAR_AGE_MULTIPLIER)
            validate_bar_freshness(higher, self.config.HIGHER_TIMEFRAME, now=now,
                                    max_age_multiplier=self.config.MT5_STALE_BAR_AGE_MULTIPLIER)
            validate_tick_freshness(getattr(snapshot, "tick_time", None), now=now,
                                     max_age_seconds=self.config.MT5_MAX_TICK_AGE_SECONDS)

    async def build_signal(self, pair: str) -> SignalSchema:
        lower = await self._call(self.market.get_closed_bars, pair, self.config.LOWER_TIMEFRAME,
                                  self.config.LOWER_TIMEFRAME_BARS)
        latest_time = lower.iloc[-1]["time"]
        if self._last_analyzed_bar.get(pair) == latest_time:
            return SignalSchema(pair=pair, timeframe=self.config.LOWER_TIMEFRAME,
                                higher_timeframe=self.config.HIGHER_TIMEFRAME,
                                signal_type="HOLD", reason="Vela M15 ya analizada", bar_time=latest_time)
        higher = await self._call(self.market.get_closed_bars, pair, self.config.HIGHER_TIMEFRAME,
                                   self.config.HIGHER_TIMEFRAME_BARS)
        snapshot = await self._call(self.market.get_symbol_snapshot, pair)
        self._validate_market_data(pair, lower, higher, snapshot)
        lower_values = self.indicators.build_indicators(lower, self.config.LOWER_TIMEFRAME)
        higher_values = self.indicators.build_indicators(higher, self.config.HIGHER_TIMEFRAME)
        signal = self.strategy.generate_signal(pair, self.config.LOWER_TIMEFRAME,
            self.config.HIGHER_TIMEFRAME, lower_values, higher_values, snapshot)
        self._last_analyzed_bar[pair] = latest_time
        return signal
