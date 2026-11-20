import io
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.logger import HANDLER_MARKER, setup_logger
from app.schemas.signal_schema import SignalSchema
from app.services.state_service import InMemorySignalKeyRepository
from app.workers import scanner

BASE_SETTINGS = {"LOWER_TIMEFRAME": "M15", "PAIR_DELAY_SECONDS": 0, "MAX_CORRELATED_SIGNALS": 1}


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


def test_format_signal_renders_buy_and_sell():
    buy = SignalSchema(pair="EURUSD", timeframe="M15", higher_timeframe="H1", signal_type="BUY",
        entry="1.10000", stop_loss="1.09500", take_profit="1.11000", risk_reward_ratio="2.0",
        score=90, adx_h1=27.3, rsi_m15=55.1, reason="setup")
    sell = buy.model_copy(update={"signal_type": "SELL"})
    rendered_buy = scanner.format_signal(buy)
    rendered_sell = scanner.format_signal(sell)
    assert "BUY" in rendered_buy and "alcista" in rendered_buy
    assert "SELL" in rendered_sell and "bajista" in rendered_sell
    assert "N/D" in scanner.format_signal(buy.model_copy(update={"bar_time": None}))


@pytest.mark.asyncio
async def test_scan_once_sends_selected_signal_and_marks_it_sent(monkeypatch):
    signal = SignalSchema(pair="EURUSD", signal_type="BUY", score=90, signal_key="k1",
        broker_symbol="EURUSDm", adx_h1=25.0, rsi_m15=55.0, reason="setup")

    class FakeSignalService:
        async def build_signal(self, pair):
            return signal

    sent_messages = []

    class FakeNotifier:
        async def send_telegram_message(self, text):
            sent_messages.append(text)

    fake_logger = Mock()
    monkeypatch.setattr(scanner, "signal_service", FakeSignalService())
    monkeypatch.setattr(scanner, "notifier_service", FakeNotifier())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(FOREX_PAIRS=["EURUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: (candidates, []))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    sent = await scanner.scan_once()
    assert len(sent) == 1 and sent[0].signal_key == "k1"
    assert len(sent_messages) == 1
    assert scanner.signal_keys.contains("k1")
    assert "k1" not in scanner.pending_notifications
    assert rendered_calls(fake_logger.debug) == ["EURUSD | broker_symbol=EURUSDm"]


@pytest.mark.asyncio
async def test_scan_once_skips_already_sent_duplicate(monkeypatch):
    signal = SignalSchema(pair="EURUSD", signal_type="BUY", score=90, signal_key="dup")

    class FakeSignalService:
        async def build_signal(self, pair):
            return signal

    fake_logger = Mock()
    repository = InMemorySignalKeyRepository()
    repository.mark_sent("dup", __import__("datetime").datetime.now())
    monkeypatch.setattr(scanner, "signal_service", FakeSignalService())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(FOREX_PAIRS=["EURUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: (candidates, []))
    monkeypatch.setattr(scanner, "signal_keys", repository)
    scanner.pending_notifications.clear()
    sent = await scanner.scan_once()
    assert sent == []
    assert "EURUSD | signal_duplicate" in rendered_calls(fake_logger.info)


@pytest.mark.asyncio
async def test_scan_once_keeps_signal_pending_when_telegram_fails(monkeypatch):
    signal = SignalSchema(pair="EURUSD", signal_type="BUY", score=90, signal_key="retry-me",
        adx_h1=25.0, rsi_m15=55.0)

    class FakeSignalService:
        async def build_signal(self, pair):
            return signal

    class FailingNotifier:
        async def send_telegram_message(self, text):
            raise RuntimeError("telegram down")

    fake_logger = Mock()
    monkeypatch.setattr(scanner, "signal_service", FakeSignalService())
    monkeypatch.setattr(scanner, "notifier_service", FailingNotifier())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(FOREX_PAIRS=["EURUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: (candidates, []))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    sent = await scanner.scan_once()
    assert sent == []
    assert "retry-me" in scanner.pending_notifications
    assert rendered_calls(fake_logger.exception) == ["EURUSD | telegram_send_failed"]
    scanner.pending_notifications.clear()


@pytest.mark.asyncio
async def test_scan_once_logs_correlation_rejections(monkeypatch):
    accepted = SignalSchema(pair="EURUSD", signal_type="BUY", score=90, signal_key="acc",
        adx_h1=25.0, rsi_m15=55.0)
    rejected = SignalSchema(pair="GBPUSD", signal_type="BUY", score=80, signal_key="rej",
        adx_h1=25.0, rsi_m15=55.0)

    class FakeSignalService:
        async def build_signal(self, pair):
            return accepted if pair == "EURUSD" else rejected

    class FakeNotifier:
        async def send_telegram_message(self, text):
            return None

    fake_logger = Mock()
    monkeypatch.setattr(scanner, "signal_service", FakeSignalService())
    monkeypatch.setattr(scanner, "notifier_service", FakeNotifier())
    monkeypatch.setattr(scanner, "logger", fake_logger)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=["EURUSD", "GBPUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter",
                         lambda candidates, maximum: ([accepted], [rejected]))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    await scanner.scan_once()
    assert "GBPUSD | signal_rejected_correlation | score=80" in rendered_calls(fake_logger.info)


@pytest.mark.asyncio
async def test_scan_once_respects_pair_delay(monkeypatch):
    class FakeSignalService:
        async def build_signal(self, pair):
            return SignalSchema(pair=pair, signal_type="HOLD", reason="nada")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(scanner, "signal_service", FakeSignalService())
    monkeypatch.setattr(scanner, "logger", Mock())
    monkeypatch.setattr(scanner.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(
        FOREX_PAIRS=["EURUSD", "GBPUSD"], LOWER_TIMEFRAME="M15",
        PAIR_DELAY_SECONDS=5, MAX_CORRELATED_SIGNALS=1))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    await scanner.scan_once()
    assert sleep_calls == [5, 5]


@pytest.mark.asyncio
async def test_scan_once_lazily_constructs_signal_service(monkeypatch):
    """Covers the lazy-initialization branch without ever touching real MT5."""
    constructed = []

    class FakeSignalService:
        def __init__(self, **_kwargs):
            constructed.append(True)

        async def build_signal(self, pair):
            return SignalSchema(pair=pair, signal_type="HOLD", reason="nada")

    monkeypatch.setattr(scanner, "signal_service", None)
    monkeypatch.setattr(scanner, "SignalService", FakeSignalService)
    monkeypatch.setattr(scanner, "logger", Mock())
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(FOREX_PAIRS=["EURUSD"], **BASE_SETTINGS))
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: ([], []))
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    scanner.pending_notifications.clear()
    await scanner.scan_once()
    assert constructed == [True]
    scanner.signal_service = None  # restore module state for other tests


@pytest.mark.asyncio
async def test_run_scanner_loops_calling_scan_once_each_interval(monkeypatch):
    calls = []

    async def fake_scan_once():
        calls.append(True)
        return []

    class StopLoop(Exception):
        pass

    async def fake_sleep(seconds):
        assert seconds == 60
        raise StopLoop

    monkeypatch.setattr(scanner, "scan_once", fake_scan_once)
    monkeypatch.setattr(scanner.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scanner, "settings", SimpleNamespace(SCAN_INTERVAL_SECONDS=60))
    monkeypatch.setattr(scanner, "logger", Mock())
    with pytest.raises(StopLoop):
        await scanner.run_scanner()
    assert calls == [True]
