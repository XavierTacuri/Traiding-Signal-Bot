"""Parameter optimization is intentionally disabled until out-of-sample data is supplied.

Import and reuse ``BacktestReport`` instead of optimizing against the same sample.
"""
from backtesting.backtesting import BacktestReport

__all__ = ["BacktestReport"]
