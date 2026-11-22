"""Fase 3: ordered shutdown orchestration in main.py.

Simulates signal delivery by invoking the installed handler directly (exactly
how CPython itself calls a Python signal handler on the main thread — there is
no real OS-level signal involved, so this is safe to run in CI and does not
depend on platform signal-delivery quirks) instead of raising a real SIGINT,
which would be indistinguishable from an actual interrupted test run.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

import main as main_module


class FakeConnection:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakeDatabase:
    """Fase 4: stands in for app/repositories/database.Database so these
    tests never touch a real SQLite file."""

    def __init__(self) -> None:
        self.initialize_calls = 0
        self.close_calls = 0

    async def initialize(self) -> int:
        self.initialize_calls += 1
        return 1

    async def close(self) -> None:
        self.close_calls += 1


def _snapshot_handlers() -> dict[int, object]:
    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    return {
        getattr(signal, name): signal.getsignal(getattr(signal, name))
        for name in names
        if hasattr(signal, name)
    }


def _restore_handlers(snapshot: dict[int, object]) -> None:
    for number, handler in snapshot.items():
        signal.signal(number, handler)


@pytest.mark.asyncio
async def test_run_cancels_the_scanner_and_shuts_down_mt5_on_sigint(monkeypatch):
    """Fase 5: monitoring is disabled here (and the file handler stubbed
    out) — this test is about signal/shutdown orchestration, not the
    heartbeat, which has its own dedicated tests and needs a real Database
    object, not this lightweight FakeDatabase."""
    monkeypatch.setattr(main_module.settings, "MONITORING_ENABLED", False)
    monkeypatch.setattr(main_module, "add_rotating_file_handler", lambda *a, **k: None)
    snapshot = _snapshot_handlers()
    fake_connection = FakeConnection()
    fake_database = FakeDatabase()
    original_get_connection = main_module.get_default_connection
    original_get_database = main_module.get_default_database
    original_run_scanner = main_module.run_scanner
    cancelled: list[bool] = []

    async def hanging_scanner() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    main_module.get_default_connection = lambda: fake_connection
    main_module.get_default_database = lambda: fake_database
    main_module.run_scanner = hanging_scanner
    try:
        task = asyncio.create_task(main_module._run())
        await asyncio.sleep(0)  # let _run install its signal handlers and start waiting
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)  # emulate delivery without a real OS signal
        await asyncio.wait_for(task, timeout=2)
    finally:
        main_module.get_default_connection = original_get_connection
        main_module.get_default_database = original_get_database
        main_module.run_scanner = original_run_scanner
        _restore_handlers(snapshot)

    assert cancelled == [True]
    assert fake_connection.shutdown_calls == 1
    assert fake_database.initialize_calls == 1
    assert fake_database.close_calls == 1


@pytest.mark.asyncio
async def test_run_shuts_down_mt5_when_the_scanner_stops_on_its_own(monkeypatch):
    """A controlled stop (e.g. MT5ConfigurationError inside run_scanner
    returning instead of looping) must still release MT5 resources.
    Fase 5: monitoring disabled, same reasoning as the test above."""
    monkeypatch.setattr(main_module.settings, "MONITORING_ENABLED", False)
    monkeypatch.setattr(main_module, "add_rotating_file_handler", lambda *a, **k: None)
    snapshot = _snapshot_handlers()
    fake_connection = FakeConnection()
    fake_database = FakeDatabase()
    original_get_connection = main_module.get_default_connection
    original_get_database = main_module.get_default_database
    original_run_scanner = main_module.run_scanner

    async def finishing_scanner() -> None:
        return None

    main_module.get_default_connection = lambda: fake_connection
    main_module.get_default_database = lambda: fake_database
    main_module.run_scanner = finishing_scanner
    try:
        await asyncio.wait_for(main_module._run(), timeout=2)
    finally:
        main_module.get_default_connection = original_get_connection
        main_module.get_default_database = original_get_database
        main_module.run_scanner = original_run_scanner
        _restore_handlers(snapshot)

    assert fake_connection.shutdown_calls == 1
    assert fake_database.initialize_calls == 1
    assert fake_database.close_calls == 1
