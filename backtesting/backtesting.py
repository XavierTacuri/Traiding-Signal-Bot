"""CLI and event-driven backtester for the H1/M15 Forex signal strategy."""
from __future__ import annotations

import argparse
import heapq
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import FOREX_PAIRS, Settings, settings
from app.domain import SignalType
from app.schemas.signal_schema import SignalSchema
from app.services.correlation_service import CorrelationService
from app.services.indicator_service import IndicatorService
from app.services.market_data_service import (
    TIMEFRAME_DELTAS,
    MarketDataService,
    SymbolSnapshot,
)
from app.services.state_service import (
    InMemorySetupRepository,
    InMemorySignalKeyRepository,
)
from app.services.strategy_service import StrategyService

LOGGER = logging.getLogger("forex_backtest")
REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}


@dataclass(slots=True)
class HistoricalData:
    pair: str
    broker_symbol: str
    lower: pd.DataFrame
    higher: pd.DataFrame
    point: Decimal
    digits: int


@dataclass(slots=True)
class SignalCandidate:
    signal: SignalSchema
    data: HistoricalData
    lower_index: int
    setup_detected_at: datetime | None
    spread_points: float
    spread_price: float


class EventType(IntEnum):
    EXIT = 0
    ENTRY = 1


@dataclass(slots=True)
class PlannedTrade:
    candidate: SignalCandidate
    entry_time: datetime
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str
    result_r: float | None


@dataclass(slots=True)
class TradingEvent:
    timestamp: datetime
    event_type: EventType
    sequence: int
    plan: PlannedTrade


@dataclass(slots=True)
class BacktestTrade:
    pair: str
    broker_symbol: str
    direction: str
    setup_detected_at: datetime | None
    entry_time: datetime
    exit_time: datetime | None
    entry: float
    stop_loss: float
    take_profit: float
    exit_price: float | None
    exit_reason: str
    score: int
    confidence: float
    risk_reward_ratio: float
    result_r: float | None
    risk_amount: float
    pnl: float
    balance_before: float
    balance_after: float
    spread_points: float
    spread_price: float


@dataclass(slots=True)
class BacktestReport:
    start: datetime
    end: datetime
    pairs: list[str]
    initial_balance: float
    final_balance: float
    lower_bars: int = 0
    higher_bars: int = 0
    setups_detected: int = 0
    setups_confirmed: int = 0
    setups_expired: int = 0
    rejections: Counter = field(default_factory=Counter)
    trades: list[BacktestTrade] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        closed = [trade for trade in self.trades if trade.result_r is not None]
        wins = [trade for trade in closed if trade.result_r and trade.result_r > 0]
        losses = [trade for trade in closed if trade.result_r and trade.result_r < 0]
        gross_profit = sum(trade.result_r or 0 for trade in wins)
        gross_loss = abs(sum(trade.result_r or 0 for trade in losses))
        results = [trade.result_r or 0 for trade in closed]
        return {
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "pairs": self.pairs,
            "lower_bars_processed": self.lower_bars,
            "higher_bars_processed": self.higher_bars,
            "setups_detected": self.setups_detected,
            "setups_confirmed": self.setups_confirmed,
            "setups_expired": self.setups_expired,
            **{f"rejected_{name}": self.rejections[name] for name in (
                "trend", "rsi", "breakout", "candle", "extension", "volatility",
                "spread", "risk", "score", "correlation", "position_open",
                "aggregate_risk")},
            "total_trades": len(self.trades),
            "winners": len(wins),
            "losers": len(losses),
            "open": len(self.trades) - len(closed),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "expectancy_r": sum(results) / len(results) if results else 0.0,
            "total_r": sum(results),
            "average_r": sum(results) / len(results) if results else 0.0,
            "max_drawdown_percent": calculate_max_drawdown_percent(self.trades, self.initial_balance),
            "initial_balance": self.initial_balance,
            "final_balance": self.final_balance,
            "by_pair": aggregate_trades(self.trades, "pair"),
            "by_direction": aggregate_trades(self.trades, "direction"),
            "by_month": aggregate_monthly(self.trades),
        }


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("La fecha debe usar el formato YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtesting histórico de la estrategia Forex H1/M15 sin ejecutar órdenes ni Telegram."
    )
    parser.add_argument("--source", required=True, choices=("mt5", "csv"))
    parser.add_argument("--pairs", required=True, nargs="+", type=str.upper)
    parser.add_argument("--start", required=True, type=parse_date)
    parser.add_argument("--end", required=True, type=parse_date)
    parser.add_argument("--lower-timeframe", default="M15", choices=tuple(TIMEFRAME_DELTAS))
    parser.add_argument("--higher-timeframe", default="H1", choices=tuple(TIMEFRAME_DELTAS))
    parser.add_argument("--csv-directory", type=Path)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=1.0,
                        help="Porcentaje del balance arriesgado por operación (default: 1.0).")
    parser.add_argument("--spread-points", type=float)
    parser.add_argument("--output-directory", type=Path, default=Path("backtesting/results"))
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--export-json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--disable-correlation", action="store_true")
    parser.add_argument("--conservative", action=argparse.BooleanOptionalAction, default=True,
                        help="SL primero si SL y TP coinciden en una vela (default: activo).")
    return parser


def validate_arguments(args: argparse.Namespace, parser: argparse.ArgumentParser | None = None) -> None:
    def fail(message: str) -> None:
        if parser:
            parser.error(message)
        raise ValueError(message)

    if args.start > args.end:
        fail("La fecha inicial no puede ser posterior a la fecha final")
    invalid = sorted(set(args.pairs) - set(FOREX_PAIRS))
    if invalid:
        fail(f"Pares no permitidos: {', '.join(invalid)}")
    if args.source == "csv" and args.csv_directory is None:
        fail("--csv-directory es obligatorio cuando --source=csv")
    if args.initial_balance <= 0:
        fail("--initial-balance debe ser mayor que cero")
    if not 0 < args.risk_per_trade <= 100:
        fail("--risk-per-trade debe estar entre 0 y 100")
    if args.spread_points is not None and args.spread_points < 0:
        fail("--spread-points no puede ser negativo")
    if args.lower_timeframe != "M15" or args.higher_timeframe != "H1":
        fail("La estrategia actual requiere --lower-timeframe M15 y --higher-timeframe H1")


def utc_period(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(start, time.min, UTC),
            datetime.combine(end + timedelta(days=1), time.min, UTC))


def normalize_frame(frame: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Columnas CSV faltantes: {', '.join(sorted(missing))}")
    result = frame.copy()
    result["time"] = pd.to_datetime(result["time"], utc=True, errors="coerce")
    numeric = ["open", "high", "low", "close"]
    optional = [column for column in ("tick_volume", "spread", "real_volume") if column in result]
    result[numeric + optional] = result[numeric + optional].apply(pd.to_numeric, errors="coerce")
    result = result.dropna(subset=["time", *numeric]).sort_values("time").drop_duplicates("time")
    if result[numeric].isin([math.inf, -math.inf]).any().any():
        raise ValueError("El histórico contiene valores infinitos")
    if (result[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("El histórico contiene precios no positivos")
    cutoff = now or datetime.now(UTC)
    result = result[result["time"] + TIMEFRAME_DELTAS[timeframe] <= cutoff].reset_index(drop=True)
    if result.empty:
        raise ValueError(f"No hay velas cerradas válidas para {timeframe}")
    return result


def default_symbol_spec(pair: str) -> tuple[Decimal, int]:
    digits = 3 if pair.endswith("JPY") else 5
    return Decimal(1).scaleb(-digits), digits


def load_csv_data(
    pairs: Sequence[str], directory: Path, lower_timeframe: str, higher_timeframe: str,
    end: datetime,
) -> dict[str, HistoricalData]:
    loaded: dict[str, HistoricalData] = {}
    for pair in pairs:
        lower_path = directory / f"{pair}_{lower_timeframe}.csv"
        higher_path = directory / f"{pair}_{higher_timeframe}.csv"
        if not lower_path.is_file() or not higher_path.is_file():
            raise FileNotFoundError(f"Faltan CSV para {pair}: {lower_path.name}, {higher_path.name}")
        lower = normalize_frame(pd.read_csv(lower_path), lower_timeframe)
        higher = normalize_frame(pd.read_csv(higher_path), higher_timeframe)
        lower = lower[lower["time"] < end].reset_index(drop=True)
        higher = higher[higher["time"] < end].reset_index(drop=True)
        point, digits = default_symbol_spec(pair)
        loaded[pair] = HistoricalData(pair, pair, lower, higher, point, digits)
    return loaded


def load_mt5_data(
    pairs: Sequence[str], start: datetime, end: datetime, lower_timeframe: str,
    higher_timeframe: str, mt5_module: Any = mt5,
) -> dict[str, HistoricalData]:
    market = MarketDataService(mt5_module=mt5_module, initialize=True)
    warmup_start = start - timedelta(days=30)
    loaded: dict[str, HistoricalData] = {}
    try:
        for pair in pairs:
            broker_symbol = market.resolve_symbol(pair)
            info = mt5_module.symbol_info(broker_symbol)
            if info is None or float(info.point) <= 0:
                raise RuntimeError(f"Especificaciones MT5 inválidas para {broker_symbol}")
            lower = market.get_historical_bars(pair, lower_timeframe, warmup_start, end)
            higher = market.get_historical_bars(pair, higher_timeframe, warmup_start, end)
            loaded[pair] = HistoricalData(pair, broker_symbol, lower, higher,
                Decimal(str(info.point)), int(info.digits))
        return loaded
    finally:
        mt5_module.shutdown()


def validate_history(data: HistoricalData, start: datetime) -> None:
    higher_before = data.higher[data.higher["time"] < start]
    if len(data.higher) < settings.TREND_EMA_SLOW + 2:
        raise ValueError(f"{data.pair}: se requieren al menos 202 velas H1")
    if len(higher_before) < 250:
        raise ValueError(f"{data.pair}: se requieren al menos 250 velas H1 previas de calentamiento")
    if len(data.lower) < 55:
        raise ValueError(f"{data.pair}: no hay suficientes velas M15")


def historical_snapshot(
    data: HistoricalData, row: pd.Series, fixed_spread_points: float | None,
    default_spread_points: float,
) -> tuple[SymbolSnapshot, float, float, bool]:
    historical = row.get("spread")
    used_fallback = False
    if historical is not None and pd.notna(historical) and float(historical) > 0:
        spread_points = float(historical)
    elif fixed_spread_points is not None:
        spread_points = fixed_spread_points
    else:
        spread_points = default_spread_points
        used_fallback = True
    spread_price = float(data.point) * spread_points
    bid = Decimal(str(row["close"]))
    snapshot = SymbolSnapshot(data.pair, data.broker_symbol, bid,
        bid + Decimal(str(spread_price)), data.point, data.digits)
    return snapshot, spread_points, spread_price, used_fallback


def find_closed_higher_index(higher: pd.DataFrame, lower_close_time: pd.Timestamp) -> int:
    """Return the latest higher-timeframe candle closed by the lower close."""
    return int(higher["close_time"].searchsorted(lower_close_time, side="right") - 1)


def rejection_category(reason: str) -> str | None:
    lowered = reason.lower()
    mapping = (("tendencia", "trend"), ("rsi", "rsi"), ("ruptura", "breakout"),
               ("vela", "candle"), ("extendido", "extension"),
               ("volatilidad", "volatility"), ("spread", "spread"),
               ("stop", "risk"), ("riesgo", "risk"), ("score", "score"))
    return next((category for token, category in mapping if token in lowered), None)


def resolve_trade(
    direction: str, entry: float, stop: float, target: float, future: pd.DataFrame,
    conservative: bool = True,
) -> tuple[float | None, datetime | None, float | None, str]:
    for row in future.itertuples(index=False):
        stop_hit = row.low <= stop if direction == "BUY" else row.high >= stop
        target_hit = row.high >= target if direction == "BUY" else row.low <= target
        exit_time = row.time + TIMEFRAME_DELTAS["M15"]
        if stop_hit and target_hit:
            if conservative:
                return -1.0, exit_time, stop, "SL_AND_TP_SAME_BAR_CONSERVATIVE_SL"
            reward = round(abs(target - entry) / abs(entry - stop), 10)
            return reward, exit_time, target, "SL_AND_TP_SAME_BAR_OPTIMISTIC_TP"
        if stop_hit:
            return -1.0, exit_time, stop, "STOP_LOSS"
        if target_hit:
            reward = round(abs(target - entry) / abs(entry - stop), 10)
            return reward, exit_time, target, "TAKE_PROFIT"
    return None, None, None, "OPEN"


def plan_trade(candidate: SignalCandidate, end: datetime, conservative: bool = True) -> PlannedTrade:
    """Resolve market outcome without applying it to the realized account balance."""
    signal = candidate.signal
    future = candidate.data.lower.iloc[candidate.lower_index + 1:].copy()
    future = future[future["time"] + TIMEFRAME_DELTAS["M15"] <= end]
    entry, stop, target = float(signal.entry), float(signal.stop_loss), float(signal.take_profit)
    result_r, exit_time, exit_price, exit_reason = resolve_trade(
        signal.signal_type, entry, stop, target, future, conservative
    )
    entry_time = signal.bar_time + TIMEFRAME_DELTAS["M15"]
    return PlannedTrade(candidate, entry_time, exit_time, exit_price, exit_reason, result_r)


def simulate_trade_events(
    plans: Sequence[PlannedTrade], report: BacktestReport, risk_per_trade: float,
    correlation: CorrelationService, maximum_correlated: int,
    correlation_enabled: bool = True,
) -> None:
    """Apply exits before entries and compound only realized PnL.

    Policy: at most one open position per logical Forex pair. Aggregate risk is
    the sum of ``risk_amount`` for positions that are open at the entry event;
    a new position is rejected if that sum would exceed realized equity.
    """
    queue: list[tuple[datetime, int, int, TradingEvent]] = []
    sequence = 0
    for plan in plans:
        event = TradingEvent(plan.entry_time, EventType.ENTRY, sequence, plan)
        heapq.heappush(queue, (event.timestamp, event.event_type, sequence, event))
        sequence += 1

    realized_balance = report.initial_balance
    open_positions: dict[str, tuple[BacktestTrade, PlannedTrade]] = {}
    while queue:
        timestamp = queue[0][0]
        events: list[TradingEvent] = []
        while queue and queue[0][0] == timestamp:
            events.append(heapq.heappop(queue)[3])

        # EventType ordering is part of the model: EXIT=0 always precedes ENTRY=1.
        for event in (item for item in events if item.event_type == EventType.EXIT):
            pair = event.plan.candidate.data.pair
            opened = open_positions.get(pair)
            if opened is None or opened[1] is not event.plan:
                continue
            trade = opened[0]
            realized_balance += trade.pnl
            trade.balance_after = realized_balance
            del open_positions[pair]

        entries = sorted(
            (item for item in events if item.event_type == EventType.ENTRY),
            key=lambda item: item.plan.candidate.signal.score or 0,
            reverse=True,
        )
        exposure_counts = Counter(
            exposure for _, plan in open_positions.values()
            if (exposure := correlation.usd_exposure(plan.candidate.signal)) is not None
        )
        for event in entries:
            plan = event.plan
            candidate, signal = plan.candidate, plan.candidate.signal
            pair = candidate.data.pair
            if pair in open_positions:
                report.rejections["position_open"] += 1
                continue
            exposure = correlation.usd_exposure(signal)
            if (correlation_enabled and exposure is not None
                    and exposure_counts[exposure] >= maximum_correlated):
                report.rejections["correlation"] += 1
                continue
            open_risk = sum(trade.risk_amount for trade, _ in open_positions.values())
            risk_amount = realized_balance * risk_per_trade / 100
            if open_risk + risk_amount > realized_balance:
                report.rejections["aggregate_risk"] += 1
                continue
            entry, stop, target = float(signal.entry), float(signal.stop_loss), float(signal.take_profit)
            pnl = risk_amount * plan.result_r if plan.result_r is not None else 0.0
            trade = BacktestTrade(pair, candidate.data.broker_symbol, signal.signal_type,
                candidate.setup_detected_at, plan.entry_time, plan.exit_time,
                entry, stop, target, plan.exit_price, plan.exit_reason, signal.score or 0,
                signal.confidence or 0, float(signal.risk_reward_ratio or 0), plan.result_r,
                risk_amount, pnl, realized_balance, realized_balance, candidate.spread_points,
                candidate.spread_price)
            report.trades.append(trade)
            open_positions[pair] = trade, plan
            if exposure is not None:
                exposure_counts[exposure] += 1
            if plan.exit_time is not None:
                exit_event = TradingEvent(plan.exit_time, EventType.EXIT, sequence, plan)
                heapq.heappush(queue, (exit_event.timestamp, exit_event.event_type,
                                       sequence, exit_event))
                sequence += 1
    report.final_balance = realized_balance


class BacktestEngine:
    def __init__(self, config: Settings = settings, *, risk_per_trade: float = 1.0,
                 fixed_spread_points: float | None = None, conservative: bool = True,
                 correlation_enabled: bool = True, verbose: bool = False) -> None:
        self.config = config
        self.risk_per_trade = risk_per_trade
        self.fixed_spread_points = fixed_spread_points
        self.conservative = conservative
        self.correlation_enabled = correlation_enabled
        self.verbose = verbose
        self.indicators = IndicatorService(config)
        self.correlation = CorrelationService()

    def run(self, datasets: dict[str, HistoricalData], start: datetime, end: datetime,
            initial_balance: float) -> BacktestReport:
        report = BacktestReport(start, end, list(datasets), initial_balance, initial_balance)
        prepared: dict[str, tuple[HistoricalData, pd.DataFrame, pd.DataFrame, StrategyService,
                                  InMemorySetupRepository]] = {}
        timeline: dict[pd.Timestamp, list[tuple[str, int]]] = defaultdict(list)
        fallback_warned: set[str] = set()
        for pair, data in datasets.items():
            validate_history(data, start)
            lower = self.indicators.add_indicators(data.lower, self.config.LOWER_TIMEFRAME)
            higher = self.indicators.add_indicators(data.higher, self.config.HIGHER_TIMEFRAME)
            lower["close_time"] = lower["time"] + TIMEFRAME_DELTAS[self.config.LOWER_TIMEFRAME]
            higher["close_time"] = higher["time"] + TIMEFRAME_DELTAS[self.config.HIGHER_TIMEFRAME]
            repository = InMemorySetupRepository()
            strategy = StrategyService(self.config, repository)
            prepared[pair] = data, lower, higher, strategy, repository
            report.higher_bars += int(((higher["close_time"] >= start) & (higher["close_time"] < end)).sum())
            for index, close_time in enumerate(lower["close_time"]):
                if start <= close_time < end:
                    timeline[close_time].append((pair, index))
                    report.lower_bars += 1

        accepted_candidates: list[SignalCandidate] = []
        signal_keys = InMemorySignalKeyRepository()
        for close_time in sorted(timeline):
            batch: list[SignalCandidate] = []
            for pair, lower_index in timeline[close_time]:
                data, lower, higher, strategy, repository = prepared[pair]
                higher_index = find_closed_higher_index(higher, close_time)
                lower_values = self.indicators.values_at(lower, self.config.LOWER_TIMEFRAME, lower_index)
                higher_values = self.indicators.values_at(higher, self.config.HIGHER_TIMEFRAME, higher_index)
                if not lower_values or not higher_values:
                    continue
                snapshot, spread_points, spread_price, fallback = historical_snapshot(
                    data, lower.iloc[lower_index], self.fixed_spread_points,
                    self.config.BACKTEST_DEFAULT_SPREAD_POINTS)
                if fallback and pair not in fallback_warned:
                    LOGGER.warning("%s sin spread histórico/fijo; usando %.1f puntos conservadores",
                                   pair, self.config.BACKTEST_DEFAULT_SPREAD_POINTS)
                    fallback_warned.add(pair)
                detected_before, expired_before = repository.detected_count, repository.expired_count
                signal = strategy.generate_signal(pair, self.config.LOWER_TIMEFRAME,
                    self.config.HIGHER_TIMEFRAME, lower_values, higher_values, snapshot)
                report.setups_detected += repository.detected_count - detected_before
                report.setups_expired += repository.expired_count - expired_before
                if signal.signal_type == "HOLD":
                    category = rejection_category(signal.reason)
                    if category:
                        report.rejections[category] += 1
                    continue
                report.setups_confirmed += 1
                if not signal.signal_key or signal_keys.contains(signal.signal_key):
                    continue
                signal_keys.mark_sent(signal.signal_key, close_time.to_pydatetime())
                latest = repository.latest(pair, SignalType(signal.signal_type))
                batch.append(SignalCandidate(signal, data, lower_index,
                (latest.detected_bar_time + TIMEFRAME_DELTAS["M15"]) if latest else None,
                spread_points, spread_price))
            accepted_candidates.extend(batch)

        plans = [plan_trade(candidate, end, self.conservative) for candidate in accepted_candidates]
        simulate_trade_events(plans, report, self.risk_per_trade, self.correlation,
                              self.config.MAX_CORRELATED_SIGNALS, self.correlation_enabled)
        return report


def aggregate_trades(trades: Sequence[BacktestTrade], attribute: str) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[str(getattr(trade, attribute))].append(trade)
    result = {}
    for key, items in grouped.items():
        closed = [item for item in items if item.result_r is not None]
        result[key] = {"trades": len(items), "wins": sum((item.result_r or 0) > 0 for item in closed),
            "losses": sum((item.result_r or 0) < 0 for item in closed),
            "open": len(items) - len(closed), "total_r": sum(item.result_r or 0 for item in closed),
            "pnl": sum(item.pnl for item in items)}
    return result


def aggregate_monthly(trades: Sequence[BacktestTrade]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.entry_time.strftime("%Y-%m")].append(trade)
    result = {}
    for month, items in grouped.items():
        closed = [item for item in items if item.result_r is not None]
        result[month] = {"trades": len(items), "total_r": sum(item.result_r or 0 for item in closed),
                         "pnl": sum(item.pnl for item in items)}
    return result


def calculate_max_drawdown_percent(trades: Sequence[BacktestTrade], initial_balance: float) -> float:
    peak, maximum = initial_balance, 0.0
    closed = sorted((trade for trade in trades if trade.exit_time is not None),
                    key=lambda trade: trade.exit_time)
    for trade in closed:
        peak = max(peak, trade.balance_after)
        if peak > 0:
            maximum = max(maximum, (peak - trade.balance_after) / peak * 100)
    return maximum


def export_results(report: BacktestReport, output: Path, export_csv: bool, export_json: bool) -> None:
    if not export_csv and not export_json:
        return
    output.mkdir(parents=True, exist_ok=True)
    if export_csv:
        columns = [item.name for item in fields(BacktestTrade)]
        pd.DataFrame([asdict(trade) for trade in report.trades], columns=columns).to_csv(
            output / "trades.csv", index=False)
    if export_json:
        with (output / "summary.json").open("w", encoding="utf-8") as handle:
            json.dump(report.metrics(), handle, indent=2, ensure_ascii=False, allow_nan=False)


def print_report(report: BacktestReport) -> None:
    metrics = report.metrics()
    labels = (
        ("Periodo", f"{report.start.date()} — {(report.end - timedelta(days=1)).date()}"),
        ("Pares", ", ".join(report.pairs)),
        ("Velas M15 procesadas", metrics["lower_bars_processed"]),
        ("Velas H1 procesadas", metrics["higher_bars_processed"]),
        ("Setups detectados", metrics["setups_detected"]),
        ("Setups confirmados", metrics["setups_confirmed"]),
        ("Setups expirados", metrics["setups_expired"]),
        ("Rechazadas por tendencia", metrics["rejected_trend"]),
        ("Rechazadas por RSI", metrics["rejected_rsi"]),
        ("Rechazadas por breakout", metrics["rejected_breakout"]),
        ("Rechazadas por vela", metrics["rejected_candle"]),
        ("Rechazadas por extensión", metrics["rejected_extension"]),
        ("Rechazadas por volatilidad", metrics["rejected_volatility"]),
        ("Rechazadas por spread", metrics["rejected_spread"]),
        ("Rechazadas por riesgo", metrics["rejected_risk"]),
        ("Rechazadas por score", metrics["rejected_score"]),
        ("Rechazadas por correlación", metrics["rejected_correlation"]),
        ("Total operaciones", metrics["total_trades"]),
        ("Ganadoras / Perdedoras / Abiertas", f"{metrics['winners']} / {metrics['losers']} / {metrics['open']}"),
        ("Win rate", f"{metrics['win_rate']:.2%}"),
        ("Profit factor", "N/D" if metrics["profit_factor"] is None else f"{metrics['profit_factor']:.3f}"),
        ("Expectancy / Total / Promedio R", f"{metrics['expectancy_r']:.3f} / {metrics['total_r']:.3f} / {metrics['average_r']:.3f}"),
        ("Drawdown máximo", f"{metrics['max_drawdown_percent']:.2f}%"),
        ("Balance inicial / final", f"{metrics['initial_balance']:.2f} / {metrics['final_balance']:.2f}"),
    )
    print("\n=== BACKTEST FOREX H1/M15 ===")
    for label, value in labels:
        print(f"{label}: {value}")
    for title, key in (("Resultado por par", "by_pair"), ("Resultado BUY/SELL", "by_direction"),
                       ("Resultado por mes", "by_month")):
        print(f"\n{title}:")
        print(json.dumps(metrics[key], indent=2, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_arguments(args, parser)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    start, end = utc_period(args.start, args.end)
    runtime_config = settings.model_copy(update={"LOWER_TIMEFRAME": args.lower_timeframe,
                                                 "HIGHER_TIMEFRAME": args.higher_timeframe})
    try:
        datasets = (load_mt5_data(args.pairs, start, end, args.lower_timeframe, args.higher_timeframe)
                    if args.source == "mt5" else
                    load_csv_data(args.pairs, args.csv_directory, args.lower_timeframe,
                                  args.higher_timeframe, end))
        engine = BacktestEngine(runtime_config, risk_per_trade=args.risk_per_trade,
            fixed_spread_points=args.spread_points, conservative=args.conservative,
            correlation_enabled=not args.disable_correlation, verbose=args.verbose)
        report = engine.run(datasets, start, end, args.initial_balance)
        print_report(report)
        export_results(report, args.output_directory, args.export_csv, args.export_json)
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("Backtest abortado: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
