"""Fase 6, sections 2/18: the historical baseline files must stay byte-for-
byte identical, and the new research/ package must never execute a trading
order — mirrors test_integration.py's existing static-grep pattern."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA256 = {
    "backtesting/results/summary.json": "4a32e9be0ea554432f4a6164af1913666ea29974a68d6fdaa8ea79ceea12f69e",
    "backtesting/results/trades.csv": "912195611183d7525a7c515d36d96178d080eb9cb586333c56335c6e779ce70d",
}
FORBIDDEN_ORDER_EXECUTION_PATTERNS = (
    "order_send", "order_check", "order_close", "positions_get", "trade_request",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_historical_baseline_files_are_unmodified():
    for relative_path, expected in EXPECTED_SHA256.items():
        path = REPO_ROOT / relative_path
        assert path.is_file(), f"Archivo histórico esperado no encontrado: {relative_path}"
        assert _sha256(path) == expected, (
            f"{relative_path} cambió de hash — Fase 6 nunca debe sobrescribir resultados históricos"
        )


def test_research_package_never_executes_a_trading_order():
    research_dir = REPO_ROOT / "research"
    source_files = [path for path in research_dir.rglob("*.py") if "__pycache__" not in path.parts]
    assert source_files, "se esperaba encontrar archivos .py bajo research/"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{pattern}"
        for path in source_files
        for pattern in FORBIDDEN_ORDER_EXECUTION_PATTERNS
        if pattern in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Código de ejecución de órdenes detectado en research/: {offenders}"


def test_research_analysis_modules_never_import_metatrader5():
    """The pure analysis layer (metrics/temporal/score_diagnostics/
    data_quality) must stay usable and testable without MT5 installed —
    only research/studies' orchestration scripts (never run in CI) touch it
    indirectly via backtesting.backtesting."""
    analysis_dir = REPO_ROOT / "research" / "analysis"
    for path in analysis_dir.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "MetaTrader5" not in content, f"{path.name} no debería depender de MetaTrader5"
