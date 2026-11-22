"""Fase 4, sections 8-10: virtual-only signal tracking.

This service only ever *reads* market data (closed bars, bid/ask snapshots)
through the same MT5ConnectionManager/MarketDataService Fase 3 already uses.
It never places, checks or closes a real order, and never queries real
positions — there is no code path in this module that calls any such MT5
API, and test/test_signal_tracker_service.py asserts this against a mock
whose implementations of those functions raise if ever invoked.

Activation semantics (section 8): a signal here already represents an
*immediate* decision — ``entry_price`` is the close of an already-closed,
already-confirmed candle (see StrategyService.generate_signal), not a resting
limit order waiting for price to arrive. So "activation" is not about price
reaching a level; it is the first moment a fresh, tradable two-sided quote is
observed for the symbol, which becomes the documented virtual fill
(BUY -> ask, SELL -> bid, per section 9). The original entry/stop/target are
never touched by this: TP/SL monitoring below evaluates the strategy's exact,
unmodified levels, exactly as the (independent) backtester does for its own
historical trades.

Same-bar SL+TP conflicts (section 10) resolve to AMBIGUOUS, never to an
assumed winner — this is the one deliberate behavioural difference from
backtesting/backtesting.py's ``resolve_trade``, which defaults to a
conservative assumption instead (see tracking_calculations.py's module
docstring for why that function is not reused here).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import Settings, settings
from app.core.logger import logger
from app.models.signal_record import NotificationType, SignalRecord, SignalStatus
from app.repositories.signal_repository import SignalRepository
from app.services.market_data_service import TIMEFRAME_DELTAS, MarketDataService
from app.services.market_data_validation import (
    StaleMarketDataError,
    is_forex_weekend_close,
    validate_tick_freshness,
)
from app.services.tracking_calculations import calculate_result_pips, calculate_result_r


@dataclass(slots=True)
class TrackerEvent:
    """One state transition the tracker made this pass; the scanner turns
    these into outbox notifications (section 14)."""

    record: SignalRecord
    new_status: SignalStatus
    notification_type: NotificationType
    activation_price: Decimal | None = None
    result_pips: Decimal | None = None
    result_r: Decimal | None = None


class SignalTrackerService:
    def __init__(
        self,
        market_data_service: MarketDataService | None = None,
        signal_repository: SignalRepository | None = None,
        config: Settings = settings,
        connection: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.market = market_data_service or MarketDataService(initialize=False)
        self.signals = signal_repository
        self.config = config
        self.connection = connection
        self._now = clock or (lambda: datetime.now(UTC))

    async def _call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if self.connection is not None:
            return await self.connection.run(func, *args, timeout=self.config.MT5_REQUEST_TIMEOUT_SECONDS,
                                               **kwargs)
        return await asyncio.to_thread(func, *args, **kwargs)

    async def evaluate_once(self) -> list[TrackerEvent]:
        """One bounded pass over every PENDING_ACTIVATION/ACTIVE signal.

        Each signal is isolated in its own try/except (mirrors the per-pair
        isolation in app/workers/scanner.py): bad data for one symbol must
        never prevent evaluating the others in the same pass (section 15)."""
        events: list[TrackerEvent] = []
        for record in await self.signals.get_pending_activation():
            try:
                event = await self._try_activate(record)
            except (RuntimeError, ValueError) as exc:
                logger.info("tracker | %s | activación suspendida: %s", record.symbol, exc)
                continue
            if event is not None:
                events.append(event)
        for record in await self.signals.get_active():
            try:
                events.extend(await self._evaluate_active(record))
            except (RuntimeError, ValueError) as exc:
                logger.info("tracker | %s | evaluación suspendida: %s", record.symbol, exc)
        return events

    async def _try_activate(self, record: SignalRecord) -> TrackerEvent | None:
        now = self._now()
        if record.expires_at_utc is not None and now >= record.expires_at_utc:
            closed = await self.signals.close(
                record.id, SignalStatus.EXPIRED, now, None, None, "NEVER_ACTIVATED_EXPIRED", None,
            )
            if closed:
                logger.info("tracker | %s | señal expirada sin activación virtual", record.symbol)
                return TrackerEvent(record, SignalStatus.EXPIRED, NotificationType.EXPIRED)
            return None
        if is_forex_weekend_close(now):
            return None  # no se puede asumir una cotización operable; se reintenta en el siguiente ciclo
        try:
            snapshot = await self._call(self.market.get_symbol_snapshot, record.symbol)
        except (RuntimeError, ValueError) as exc:
            logger.info("tracker | %s | sin cotización disponible para activar: %s", record.symbol, exc)
            return None
        try:
            validate_tick_freshness(snapshot.tick_time, now=now,
                                     max_age_seconds=self.config.MT5_MAX_TICK_AGE_SECONDS)
        except StaleMarketDataError as exc:
            logger.info("tracker | %s | cotización obsoleta, se espera al próximo ciclo: %s",
                        record.symbol, exc)
            return None
        is_buy = record.direction == "BUY"
        price, side = (snapshot.ask, "ASK") if is_buy else (snapshot.bid, "BID")
        activated = await self.signals.activate(record.id, str(price), side, now)
        if not activated:
            return None
        logger.info("tracker | %s | activación virtual (%s) @ %s", record.symbol, side, price)
        return TrackerEvent(record, SignalStatus.ACTIVE, NotificationType.ACTIVATED, activation_price=price)

    async def _evaluate_active(self, record: SignalRecord) -> list[TrackerEvent]:
        now = self._now()
        start = record.last_checked_bar_time_utc or record.activated_at_utc
        if start is None or now <= start:
            return []
        try:
            bars = await self._call(self.market.get_historical_bars, record.symbol,
                                     record.timeframe, start, now, now=now)
        except (RuntimeError, ValueError) as exc:
            logger.debug("tracker | %s | sin velas nuevas todavía: %s", record.symbol, exc)
            return []
        direction = record.direction
        is_buy = direction == "BUY"
        stop, target = Decimal(record.stop_loss), Decimal(record.take_profit)
        last_bar_time = start
        kind: str | None = None
        for row in bars.itertuples(index=False):
            low, high = Decimal(str(row.low)), Decimal(str(row.high))
            last_bar_time = row.time.to_pydatetime()
            stop_hit = low <= stop if is_buy else high >= stop
            target_hit = high >= target if is_buy else low <= target
            if stop_hit and target_hit:
                kind = "AMBIGUOUS"
                break
            if stop_hit:
                kind = "STOP_LOSS"
                break
            if target_hit:
                kind = "TAKE_PROFIT"
                break
        if kind is None:
            await self.signals.update_last_checked(record.id, last_bar_time)
            return []
        closed_at = last_bar_time + TIMEFRAME_DELTAS.get(record.timeframe, TIMEFRAME_DELTAS["M15"])
        if kind == "AMBIGUOUS":
            closed = await self.signals.close(
                record.id, SignalStatus.AMBIGUOUS, closed_at, None, None,
                "SL_AND_TP_SAME_BAR_AMBIGUOUS", last_bar_time,
            )
            if not closed:
                return []
            logger.info("tracker | %s | resultado AMBIGUO: SL y TP en la misma vela", record.symbol)
            return [TrackerEvent(record, SignalStatus.AMBIGUOUS, NotificationType.AMBIGUOUS)]
        entry = Decimal(record.entry_price)
        risk = Decimal(record.risk_distance)
        exit_price = stop if kind == "STOP_LOSS" else target
        pips = calculate_result_pips(direction, entry, exit_price, record.digits)
        result_r = calculate_result_r(direction, entry, exit_price, risk)
        status = SignalStatus.TAKE_PROFIT if kind == "TAKE_PROFIT" else SignalStatus.STOP_LOSS
        closed = await self.signals.close(
            record.id, status, closed_at, float(pips), float(result_r), kind, last_bar_time,
        )
        if not closed:
            return []
        logger.info("tracker | %s | cierre virtual %s | %.1f pips | %.2fR", record.symbol, kind, pips, result_r)
        return [TrackerEvent(record, status, NotificationType(kind), result_pips=pips, result_r=result_r)]
