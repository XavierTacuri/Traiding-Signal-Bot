from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.schemas.signal_schema import SignalSchema
from app.services.correlation_service import CorrelationService
from backtesting.backtesting import (
    BacktestReport,
    BacktestTrade,
    HistoricalData,
    PlannedTrade,
    SignalCandidate,
    build_parser,
    calculate_max_drawdown_percent,
    export_results,
    find_closed_higher_index,
    historical_snapshot,
    load_mt5_data,
    main,
    resolve_trade,
    simulate_trade_events,
    utc_period,
    validate_arguments,
)


def bars(*items):
    return pd.DataFrame(items)


def test_help_is_functional(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--source" in capsys.readouterr().out


def test_parser_rejects_invalid_source_and_date():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--source", "bad", "--pairs", "EURUSD", "--start", "2025-01-01", "--end", "2025-01-02"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--source", "mt5", "--pairs", "EURUSD", "--start", "01-01-2025", "--end", "2025-01-02"])


def test_argument_semantics_reject_date_order_and_pair():
    parser = build_parser()
    args = parser.parse_args(["--source", "mt5", "--pairs", "EURUSD", "--start", "2025-02-01", "--end", "2025-01-01"])
    with pytest.raises(ValueError, match="posterior"):
        validate_arguments(args)
    args = parser.parse_args(["--source", "mt5", "--pairs", "XAUUSD", "--start", "2025-01-01", "--end", "2025-02-01"])
    with pytest.raises(ValueError, match="no permitidos"):
        validate_arguments(args)


class FakeMT5:
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60

    def __init__(self):
        self.shutdown_called = False
        self.order_called = False
        self.ranges = []

    def initialize(self):
        return True

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, name):
        return SimpleNamespace(name=name, point=0.00001, digits=5)

    def symbols_get(self):
        return ()

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.ranges.append((symbol, timeframe, start, end))
        origin = datetime(2024, 12, 1, tzinfo=UTC)
        return [{"time": int((origin + timedelta(hours=index)).timestamp()), "open": 1.1,
                 "high": 1.101, "low": 1.099, "close": 1.1, "spread": 10}
                for index in range(800)]

    def order_send(self, *_args, **_kwargs):
        self.order_called = True
        raise AssertionError("El backtest no debe ejecutar órdenes")


def test_mt5_download_uses_ranges_shutdown_and_never_orders():
    fake = FakeMT5()
    start, end = utc_period(__import__("datetime").date(2025, 1, 1), __import__("datetime").date(2025, 1, 2))
    result = load_mt5_data(["EURUSD"], start, end, "M15", "H1", fake)
    assert result["EURUSD"].broker_symbol == "EURUSD"
    assert len(fake.ranges) == 2
    assert all(item[2].tzinfo is not None for item in fake.ranges)
    assert fake.shutdown_called and not fake.order_called


def test_h1_alignment_never_uses_future_close():
    higher = pd.DataFrame({"close_time": pd.to_datetime([
        "2025-01-01T10:00:00Z", "2025-01-01T11:00:00Z", "2025-01-01T12:00:00Z"])})
    index = find_closed_higher_index(higher, pd.Timestamp("2025-01-01T11:45:00Z"))
    assert index == 1
    assert higher.iloc[index].close_time <= pd.Timestamp("2025-01-01T11:45:00Z")


@pytest.mark.parametrize(("direction", "candle", "expected", "reason"), [
    ("BUY", {"low": 1.1005, "high": 1.1030}, 2.0, "TAKE_PROFIT"),
    ("BUY", {"low": 1.0985, "high": 1.1005}, -1.0, "STOP_LOSS"),
    ("SELL", {"low": 1.0970, "high": 1.0995}, 2.0, "TAKE_PROFIT"),
    ("SELL", {"low": 1.0995, "high": 1.1015}, -1.0, "STOP_LOSS"),
])
def test_trade_outcomes(direction, candle, expected, reason):
    time = pd.Timestamp("2025-01-01T12:00:00Z")
    frame = bars({"time": time, **candle})
    if direction == "BUY":
        result = resolve_trade(direction, 1.1000, 1.0990, 1.1020, frame)
    else:
        result = resolve_trade(direction, 1.1000, 1.1010, 1.0980, frame)
    assert result[0] == expected and result[3] == reason


def test_same_bar_is_conservative_by_default():
    frame = bars({"time": pd.Timestamp("2025-01-01T12:00:00Z"), "low": 1.098, "high": 1.103})
    result = resolve_trade("BUY", 1.100, 1.099, 1.102, frame)
    assert result[0] == -1 and "CONSERVATIVE" in result[3]


def dataset(row):
    frame = pd.DataFrame([row])
    return HistoricalData("EURUSD", "EURUSDm", frame, frame, Decimal("0.00001"), 5)


def planned(pair, entry_time, exit_time, result_r, direction="BUY", score=85):
    frame = pd.DataFrame(columns=["time", "low", "high"])
    data = HistoricalData(pair, pair, frame, frame, Decimal("0.00001"), 5)
    signal = SignalSchema(pair=pair, signal_type=direction, entry=Decimal("1.10000"),
        stop_loss=Decimal("1.09000"), take_profit=Decimal("1.12000"),
        confidence=.85, score=score, risk_reward_ratio=Decimal(2),
        bar_time=entry_time - timedelta(minutes=15))
    candidate = SignalCandidate(signal, data, 0, None, 10, .0001)
    return PlannedTrade(candidate, entry_time, exit_time,
                        1.12 if result_r > 0 else 1.09,
                        "TAKE_PROFIT" if result_r > 0 else "STOP_LOSS", result_r)


def empty_report():
    return BacktestReport(datetime(2025, 1, 1, tzinfo=UTC),
                          datetime(2026, 1, 1, tzinfo=UTC), [], 10_000, 10_000)


def test_overlapping_entry_uses_only_realized_balance():
    operation_a = planned("USDCAD", datetime(2025, 12, 12, 7, 45, tzinfo=UTC),
                          datetime(2025, 12, 12, 10, 45, tzinfo=UTC), 2, "SELL")
    operation_b = planned("EURUSD", datetime(2025, 12, 12, 8, 0, tzinfo=UTC),
                          datetime(2025, 12, 12, 11, 0, tzinfo=UTC), -1)
    report = empty_report()
    simulate_trade_events([operation_a, operation_b], report, 1, CorrelationService(), 1,
                          correlation_enabled=False)
    a, b = report.trades
    assert a.balance_before == 10_000
    assert b.balance_before == 10_000
    assert b.risk_amount == 100
    assert a.balance_after == 10_200
    assert report.final_balance == 10_100


def test_exit_precedes_entry_and_duplicate_symbol_is_rejected():
    first = planned("AUDUSD", datetime(2025, 10, 13, 7, 45, tzinfo=UTC),
                    datetime(2025, 10, 13, 10, 45, tzinfo=UTC), 2)
    overlapping = planned("AUDUSD", datetime(2025, 10, 13, 8, 0, tzinfo=UTC),
                          datetime(2025, 10, 13, 9, 0, tzinfo=UTC), 2)
    after_exit = planned("AUDUSD", datetime(2025, 10, 13, 10, 45, tzinfo=UTC),
                         datetime(2025, 10, 13, 11, 0, tzinfo=UTC), -1)
    report = empty_report()
    simulate_trade_events([first, overlapping, after_exit], report, 1,
                          CorrelationService(), 1)
    assert len(report.trades) == 2
    assert report.rejections["position_open"] == 1
    assert report.trades[1].balance_before == 10_200
    assert report.trades[1].risk_amount == 102


def test_correlation_filter_counts_positions_already_open():
    usdcad = planned("USDCAD", datetime(2025, 12, 12, 7, 45, tzinfo=UTC),
                     datetime(2025, 12, 12, 10, 45, tzinfo=UTC), 2, "SELL")
    eurusd = planned("EURUSD", datetime(2025, 12, 12, 8, 0, tzinfo=UTC),
                     datetime(2025, 12, 12, 11, 0, tzinfo=UTC), -1, "BUY")
    report = empty_report()
    simulate_trade_events([usdcad, eurusd], report, 1, CorrelationService(), 1)
    assert [trade.pair for trade in report.trades] == ["USDCAD"]
    assert report.rejections["correlation"] == 1


def test_historical_and_fixed_spread():
    historical = pd.Series({"close": 1.1, "spread": 13})
    _, points, price, fallback = historical_snapshot(dataset(historical), historical, 30, 20)
    assert points == 13 and price == pytest.approx(0.00013) and not fallback
    fixed = pd.Series({"close": 1.1})
    _, points, _, fallback = historical_snapshot(dataset(fixed), fixed, 17, 20)
    assert points == 17 and not fallback


def trade(result_r, before, after, pair="EURUSD", direction="BUY"):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    return BacktestTrade(pair, pair, direction, now, now, now, 1.1, 1.0, 1.3, 1.3,
        "TAKE_PROFIT" if result_r > 0 else "STOP_LOSS", 90, .9, 2.0, result_r,
        100, after - before, before, after, 10, .0001)


def test_profit_factor_and_drawdown():
    start, end = utc_period(__import__("datetime").date(2025, 1, 1), __import__("datetime").date(2025, 1, 2))
    trades = [trade(2, 10_000, 12_000), trade(-1, 12_000, 10_800)]
    report = BacktestReport(start, end, ["EURUSD"], 10_000, 10_800, trades=trades)
    assert report.metrics()["profit_factor"] == 2
    assert calculate_max_drawdown_percent(trades, 10_000) == 10


def test_csv_and_json_exports(tmp_path: Path):
    start, end = utc_period(__import__("datetime").date(2025, 1, 1), __import__("datetime").date(2025, 1, 2))
    report = BacktestReport(start, end, ["EURUSD"], 10_000, 10_200,
                            trades=[trade(2, 10_000, 10_200)])
    export_results(report, tmp_path, True, True)
    exported = pd.read_csv(tmp_path / "trades.csv")
    summary = __import__("json").loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert {"pair", "entry", "result_r", "spread_price"} <= set(exported.columns)
    assert summary["total_trades"] == 1


def test_small_csv_cli_fixture_never_calls_telegram(tmp_path: Path, monkeypatch):
    from app.services.notifier_service import NotifierService

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("El backtest no debe enviar Telegram")

    monkeypatch.setattr(NotifierService, "send_telegram_message", forbidden)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    h1_time = pd.date_range("2024-12-15", periods=500, freq="1h", tz="UTC")
    m15_time = pd.date_range("2024-12-25", periods=1000, freq="15min", tz="UTC")
    for timeframe, timestamps in (("H1", h1_time), ("M15", m15_time)):
        values = pd.Series(range(len(timestamps)), dtype=float) * 0.00001 + 1.1
        pd.DataFrame({"time": timestamps, "open": values, "high": values + 0.0002,
            "low": values - 0.0002, "close": values, "spread": 10}).to_csv(
                csv_dir / f"EURUSD_{timeframe}.csv", index=False)
    output = tmp_path / "results"
    exit_code = main(["--source", "csv", "--pairs", "EURUSD", "--start", "2025-01-01",
        "--end", "2025-01-02", "--csv-directory", str(csv_dir),
        "--output-directory", str(output), "--export-json"])
    assert exit_code == 0 and (output / "summary.json").is_file()
