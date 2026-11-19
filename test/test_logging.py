import io
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.logger import HANDLER_MARKER, setup_logger
from app.schemas.signal_schema import SignalSchema
from app.services.state_service import InMemorySignalKeyRepository
from app.workers import scanner


def rendered_calls(mock_method: Mock) -> list[str]:
    return [call.args[0] % call.args[1:] for call in mock_method.call_args_list]


def test_log_timestamp_is_time_only_without_fraction():
    logger = setup_logger()
    stream = io.StringIO()
    handler = logger.handlers[0]
    original_stream = handler.stream
    handler.setStream(stream)
    try:
        logger.info("EURUSD | signal_hold")
    finally:
        handler.setStream(original_stream)
    output = stream.getvalue().strip()
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2} \| INFO \| forex_signal_bot \| EURUSD \| signal_hold", output)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", output)
    assert not re.search(r"\d{2}:\d{2}:\d{2}[,.]\d+", output)


def test_setup_logger_does_not_duplicate_handlers():
    logger = setup_logger()
    owned = lambda: [handler for handler in logger.handlers if getattr(handler, HANDLER_MARKER, False)]
    count = len(owned())
    assert setup_logger() is logger
    assert len(owned()) == count == 1


@pytest.mark.asyncio
async def test_scanner_logs_hold_buy_and_sell_with_logical_pair(monkeypatch):
    signals = {
        "EURUSD": SignalSchema(pair="EURUSD", signal_type="HOLD", reason="Sin setup fuerte"),
        "GBPUSD": SignalSchema(pair="GBPUSD", signal_type="BUY", score=82, signal_key="buy"),
        "USDJPY": SignalSchema(pair="USDJPY", signal_type="SELL", score=79, signal_key="sell"),
    }

    class SignalService:
        async def build_signal(self, pair):
            return signals[pair]

    fake_logger = Mock()
    monkeypatch.setattr(scanner, "signal_service", SignalService())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=list(signals), LOWER_TIMEFRAME="M15", PAIR_DELAY_SECONDS=0,
        MAX_CORRELATED_SIGNALS=1))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    await scanner.scan_once()
    messages = rendered_calls(fake_logger.info)
    assert "EURUSD | signal_hold | Sin setup fuerte" in messages
    assert "GBPUSD | signal_buy | score=82" in messages
    assert "USDJPY | signal_sell | score=79" in messages


@pytest.mark.asyncio
async def test_scanner_exception_contains_pair(monkeypatch):
    class FailingSignalService:
        async def build_signal(self, pair):
            raise RuntimeError("MT5 unavailable")

    fake_logger = Mock()
    monkeypatch.setattr(scanner, "signal_service", FailingSignalService())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=["USDCHF"], LOWER_TIMEFRAME="M15", PAIR_DELAY_SECONDS=0,
        MAX_CORRELATED_SIGNALS=1))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))
    scanner.pending_notifications.clear()
    await scanner.scan_once()
    assert rendered_calls(fake_logger.exception) == ["USDCHF | scan_failed"]
