"""Fase 6: orchestrates (1) a baseline reproduction against the original
audit configuration and (2) an extended multi-year, 7-pair study — both
using ONLY the existing, untouched backtesting engine
(backtesting.backtesting.BacktestEngine / StrategyService / IndicatorService
/ CorrelationService). No simulation logic is reimplemented here; this
module only loads data, calls the real engine, and feeds its output
(BacktestTrade objects) into the pure analysis functions under
research/analysis/.

Run manually (never from CI — this is the "extensive, reproducible-on-demand"
study the brief separates from fast unit tests):

    python -m research.studies.run_studies --csv-directory <path> \\
        --output-directory research/reports

Reads market data from local CSV files only (collected once, read-only,
from the demo terminal in this investigation — see metadata.json next to
them). Never touches backtesting/results/trades.csv or summary.json.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import FOREX_PAIRS, settings
from backtesting.backtesting import (
    BacktestEngine,
    aggregate_monthly,
    aggregate_trades,
    load_csv_data,
    utc_period,
)
from research.analysis import metrics as metrics_mod
from research.analysis import score_diagnostics
from research.analysis.temporal import (
    split_in_sample_out_of_sample,
    walk_forward_windows,
)

ORIGINAL_BASELINE_PAIRS = ["EURUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]  # GBPUSD was absent
ORIGINAL_BASELINE_START = datetime(2025, 1, 1, tzinfo=UTC).date()
ORIGINAL_BASELINE_END = datetime(2026, 1, 1, tzinfo=UTC).date()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)


def run_baseline_reproduction(csv_directory: Path, output_directory: Path) -> dict:
    """Section 5: same pairs, same period, same (default) configuration as
    the original audit baseline — reproduced from freshly-collected CSV
    data rather than a second live MT5 pull, so the comparison is against
    a fixed, inspectable input."""
    start, end = utc_period(ORIGINAL_BASELINE_START, ORIGINAL_BASELINE_END)
    datasets = load_csv_data(ORIGINAL_BASELINE_PAIRS, csv_directory, "M15", "H1", end)
    engine = BacktestEngine(settings)  # production defaults, nothing overridden
    report = engine.run(datasets, start, end, initial_balance=10_000.0)
    result = report.metrics()
    _write_json(output_directory / "baseline_reproduction.json", result)
    return result


def run_extended_study(csv_directory: Path, output_directory: Path,
                        study_start: datetime, study_end: datetime) -> dict:
    """Sections 6-14: one real BacktestEngine run over the full extended
    period and all 7 effective pairs; every downstream analysis (IS/OOS,
    walk-forward, per-pair, score diagnostics, correlation) slices this
    SAME trade list — never a second, differently-parameterised engine run,
    since the strategy's parameters must stay fixed throughout (section 7).
    """
    start, end = study_start, study_end
    datasets = load_csv_data(list(FOREX_PAIRS), csv_directory, "M15", "H1", end)
    engine = BacktestEngine(settings)
    report = engine.run(datasets, start, end, initial_balance=10_000.0)
    _write_json(output_directory / "extended_study_summary.json", report.metrics())

    trades = report.trades
    closed = [t for t in trades if t.result_r is not None]

    # --- Section 6: IS/OOS split, last 12 months reserved as OOS ---------
    split_at = end - timedelta(days=365)
    split = split_in_sample_out_of_sample(trades, split_at)
    is_oos = {
        "split_at_utc": split_at.isoformat(),
        "rule": "entry_time < split_at -> in_sample (only if exit_time <= split_at); "
                "entry_time >= split_at -> out_of_sample; crossing trades purged from both",
        "in_sample": {"n": len(split.in_sample), "metrics": _metrics_dict(split.in_sample)},
        "out_of_sample": {"n": len(split.out_of_sample), "metrics": _metrics_dict(split.out_of_sample)},
        "purged_count": len(split.purged),
    }

    # --- Section 7: fixed-parameter walk-forward, 182-day windows --------
    windows = walk_forward_windows(trades, start, end, window_length_days=182)
    walk_forward = [
        {"index": w.index, "start": w.start.isoformat(), "end": w.end.isoformat(),
         "n_trades": len(w.trades), "metrics": _metrics_dict(w.trades)}
        for w in windows
    ]

    # --- Section 13: per-pair ---------------------------------------------
    by_pair = {
        pair: _metrics_dict([t for t in trades if t.pair == pair])
        for pair in FOREX_PAIRS
    }

    # --- Section 9: uncertainty on the full-sample and OOS-only R series --
    uncertainty = {}
    for label, sample in (("full_sample", closed),
                           ("out_of_sample_only", metrics_mod.closed_results(split.out_of_sample))):
        results_r = sample if label == "out_of_sample_only" else [t.result_r for t in sample]
        wins = sum(1 for r in results_r if r > 0)
        rng = random.Random(20261123)  # fixed seed: reproducible, not cherry-picked per run
        bootstrap_mean = metrics_mod.block_bootstrap_statistic(
            results_r, lambda s: sum(s) / len(s) if s else 0.0,
            block_size=5, n_resamples=2000, rng=rng,
        ) if len(results_r) >= 5 else []
        uncertainty[label] = {
            "n": len(results_r),
            "win_rate_wilson_95": metrics_mod.wilson_confidence_interval(wins, len(results_r)) if results_r else "N/D",
            "expectancy_normal_approx_95": metrics_mod.normal_approx_mean_ci(results_r) if results_r else "N/D",
            "expectancy_block_bootstrap_95": (
                metrics_mod.percentile_ci(bootstrap_mean) if bootstrap_mean else "N/D (muestra < 5 operaciones)"
            ),
        }

    # --- Section 10: score diagnostics ------------------------------------
    scores = [t.score for t in closed]
    results_r = [t.result_r for t in closed]
    score_report = {
        "distribution": score_diagnostics.score_distribution(scores),
        "minimum_structural_score": score_diagnostics.minimum_structural_score([25, 20, 15, 15, 10]),
        "pearson_correlation_score_vs_result_r": score_diagnostics.pearson_correlation(scores, results_r),
        "discriminatory_power_by_score": score_diagnostics.discriminatory_power(scores, results_r),
        "rejected_by_score_count": report.rejections.get("score", 0),
    }

    # --- Section 14: correlation/exposure, as already modelled by the ----
    # engine's own CorrelationService — not reimplemented, only reported.
    correlation_report = {
        "rejected_correlation_count": report.rejections.get("correlation", 0),
        "max_correlated_signals_setting": settings.MAX_CORRELATED_SIGNALS,
    }

    full_report = {
        "study_start_utc": start.isoformat(), "study_end_utc": end.isoformat(),
        "pairs": list(FOREX_PAIRS), "total_trades": len(trades), "closed_trades": len(closed),
        "is_oos": is_oos, "walk_forward_windows": walk_forward, "by_pair": by_pair,
        "uncertainty": uncertainty, "score_diagnostics": score_report,
        "correlation": correlation_report, "by_direction": aggregate_trades(trades, "direction"),
        "by_month": aggregate_monthly(trades),
    }
    _write_json(output_directory / "extended_study_analysis.json", full_report)
    return full_report


def run_sensitivity_study(csv_directory: Path, output_directory: Path,
                           study_start: datetime, study_end: datetime) -> dict:
    """Section 11: re-runs the SAME unmodified engine with varied, clearly-
    labelled cost assumptions (fixed spread in points) — never varying the
    indicators or strategy rules themselves."""
    datasets = load_csv_data(list(FOREX_PAIRS), csv_directory, "M15", "H1", study_end)
    scenarios = {
        "base_historical_spread": None,  # use each bar's own historical spread field
        "moderate_costs_1_5x_default": settings.BACKTEST_DEFAULT_SPREAD_POINTS * 1.5,
        "adverse_costs_3x_default": settings.BACKTEST_DEFAULT_SPREAD_POINTS * 3.0,
    }
    results = {}
    for label, fixed_spread in scenarios.items():
        engine = BacktestEngine(settings, fixed_spread_points=fixed_spread)
        report = engine.run(datasets, study_start, study_end, initial_balance=10_000.0)
        results[label] = {
            "fixed_spread_points": fixed_spread,
            "metrics": report.metrics(),
        }
    _write_json(output_directory / "sensitivity_study.json", results)
    return results


def _metrics_dict(trades: list) -> dict:
    m = metrics_mod.compute_metrics(trades)
    return {
        "sample_size": m.sample_size, "closed_count": m.closed_count, "wins": m.wins, "losses": m.losses,
        "win_rate": m.win_rate, "profit_factor": m.profit_factor, "expectancy_r": m.expectancy_r,
        "total_r": m.total_r, "average_win_r": m.average_win_r, "average_loss_r": m.average_loss_r,
        "reward_risk_observed": m.reward_risk_observed, "max_drawdown_r": m.max_drawdown_r,
        "max_winning_streak": m.max_winning_streak, "max_losing_streak": m.max_losing_streak,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fase 6: estudios estadísticos reproducibles")
    parser.add_argument("--csv-directory", required=True, type=Path)
    parser.add_argument("--output-directory", default=REPO_ROOT / "research" / "reports", type=Path)
    parser.add_argument("--study-start", default="2022-11-23")
    parser.add_argument("--study-end", default="2026-11-23")
    args = parser.parse_args(argv)
    study_start = datetime.fromisoformat(args.study_start).replace(tzinfo=UTC)
    study_end = datetime.fromisoformat(args.study_end).replace(tzinfo=UTC)

    print("== Reproducción del baseline original ==")
    baseline = run_baseline_reproduction(args.csv_directory, args.output_directory)
    print(json.dumps({k: baseline[k] for k in (
        "total_trades", "winners", "losers", "win_rate", "profit_factor", "expectancy_r",
        "total_r", "max_drawdown_percent")}, indent=2, default=str))

    print("\n== Estudio extendido (4 años, 7 pares) ==")
    extended = run_extended_study(args.csv_directory, args.output_directory, study_start, study_end)
    print(json.dumps(extended["by_pair"], indent=2, default=str))

    print("\n== Sensibilidad a costes ==")
    run_sensitivity_study(args.csv_directory, args.output_directory, study_start, study_end)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
