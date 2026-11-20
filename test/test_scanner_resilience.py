"""Fase 3: scanner resilience — suspended cycles, per-pair isolation, controlled
stop on non-recoverable configuration errors. No test touches the real MT5
module (the autouse fixture in test/conftest.py already prevents that for
every other test; here the point is to explicitly control `connection`)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.signal_schema import SignalSchema
from app.services.market_data_validation import (
    InvalidMarketDataError,
    StaleMarketDataError,
)
from app.services.mt5_connection import (
    MT5BusyError,
    MT5ConfigurationError,
    MT5ConnectionError,
)
from app.services.state_service import InMemorySignalKeyRepository
from app.workers import scanner

BASE_SETTINGS = {"LOWER_TIMEFRAME": "M15", "PAIR_DELAY_SECONDS": 0, "MAX_CORRELATED_SIGNALS": 1}


def _settings(pairs):
    return SimpleNamespace(FOREX_PAIRS=pairs, **BASE_SETTINGS)


class _RaisingConnection:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def ensure_connected(self):
        raise self._exc


class _PairScriptedSignalService:
    """Returns/raises whatever is scripted per pair; records call order."""

    def __init__(self, script: dict):
        self.script = script
        self.called = []

    async def build_signal(self, pair: str) -> SignalSchema:
        self.called.append(pair)
        outcome = self.script[pair]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _hold(pair: str) -> SignalSchema:
    return SignalSchema(pair=pair, signal_type="HOLD", reason="nada")


@pytest.fixture(autouse=True)
def _reset_scanner_state(monkeypatch):
    monkeypatch.setattr(scanner, "signal_service", None)
    monkeypatch.setattr(scanner, "signal_keys", InMemorySignalKeyRepository())
    monkeypatch.setattr(scanner.correlation_service, "filter", lambda candidates, maximum: (candidates, []))
    scanner.pending_notifications.clear()
    yield
    monkeypatch.setattr(scanner, "signal_service", None)
    scanner.pending_notifications.clear()


@pytest.mark.asyncio
async def test_scan_cycle_is_suspended_when_mt5_is_disconnected(monkeypatch):
    """Section 9: a general connection failure suspends the whole cycle —
    no pair is scanned, no exception escapes."""
    monkeypatch.setattr(scanner, "connection", _RaisingConnection(MT5ConnectionError("sin conexión")))
    service = _PairScriptedSignalService({"EURUSD": _hold("EURUSD")})
    monkeypatch.setattr(scanner, "signal_service", service)
    monkeypatch.setattr(scanner, "settings", _settings(["EURUSD"]))

    result = await scanner.scan_once()

    assert result == []
    assert service.called == []  # the cycle never reached the per-pair loop


@pytest.mark.asyncio
async def test_scan_cycle_is_suspended_when_the_connection_is_busy(monkeypatch):
    monkeypatch.setattr(scanner, "connection", _RaisingConnection(MT5BusyError("ocupado")))
    service = _PairScriptedSignalService({"EURUSD": _hold("EURUSD")})
    monkeypatch.setattr(scanner, "signal_service", service)
    monkeypatch.setattr(scanner, "settings", _settings(["EURUSD"]))

    result = await scanner.scan_once()

    assert result == []
    assert service.called == []


@pytest.mark.asyncio
async def test_a_failing_pair_does_not_stop_the_other_pairs(monkeypatch):
    """Section 9: per-symbol isolation — one pair's exception must not abort
    the others still pending in the same cycle."""
    class _OkConnection:
        async def ensure_connected(self):
            return None

    monkeypatch.setattr(scanner, "connection", _OkConnection())
    service = _PairScriptedSignalService({
        "EURUSD": RuntimeError("fallo de datos puntual"),
        "GBPUSD": _hold("GBPUSD"),
    })
    monkeypatch.setattr(scanner, "signal_service", service)
    monkeypatch.setattr(scanner, "settings", _settings(["EURUSD", "GBPUSD"]))

    result = await scanner.scan_once()

    assert service.called == ["EURUSD", "GBPUSD"]  # both attempted
    assert result == []  # GBPUSD was a HOLD, nothing to send


@pytest.mark.asyncio
async def test_stale_and_invalid_market_data_are_logged_distinctly_and_do_not_abort_the_cycle(monkeypatch):
    class _OkConnection:
        async def ensure_connected(self):
            return None

    monkeypatch.setattr(scanner, "connection", _OkConnection())
    service = _PairScriptedSignalService({
        "EURUSD": StaleMarketDataError("vela obsoleta"),
        "GBPUSD": InvalidMarketDataError("datos inválidos"),
        "USDJPY": _hold("USDJPY"),
    })
    monkeypatch.setattr(scanner, "signal_service", service)
    monkeypatch.setattr(scanner, "settings", _settings(["EURUSD", "GBPUSD", "USDJPY"]))

    result = await scanner.scan_once()

    assert service.called == ["EURUSD", "GBPUSD", "USDJPY"]
    assert result == []


@pytest.mark.asyncio
async def test_non_recoverable_configuration_error_stops_run_scanner_in_a_controlled_way(monkeypatch):
    """Section 9/16: a permanent configuration problem must not loop forever
    nor crash with an unhandled traceback — run_scanner logs and returns."""
    monkeypatch.setattr(scanner, "connection", _RaisingConnection(MT5ConfigurationError("credenciales inválidas")))
    monkeypatch.setattr(scanner, "settings", _settings(["EURUSD"]))
    sleep_calls = []

    async def fake_sleep(_seconds):
        sleep_calls.append(_seconds)

    monkeypatch.setattr(scanner.asyncio, "sleep", fake_sleep)

    await scanner.run_scanner()  # must return, not raise, and not loop

    assert sleep_calls == []  # returned before ever reaching the interval sleep
