"""Fase 4, sections 1/2/5: SQLiteSetupRepository as a drop-in for
InMemorySetupRepository (app/services/state_service.py's SetupRepository
Protocol) — same contract, persisted. All against temporary files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain import PendingSetup, SetupStatus, SignalType
from app.repositories.setup_repository import SQLiteSetupRepository


def _setup(pair="EURUSD", direction=SignalType.BUY, bar_time=None) -> PendingSetup:
    bar_time = bar_time or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return PendingSetup(pair=pair, direction=direction, detected_at=datetime.now(UTC),
                         detected_bar_time=bar_time, expires_after_bars=4,
                         pullback_low=1.0990, pullback_high=1.1010)


def test_save_then_get_round_trips_a_pending_setup(tmp_path):
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    setup = _setup()
    repo.save(setup)

    found = repo.get("EURUSD", SignalType.BUY)

    assert found is not None
    assert found.pair == "EURUSD" and found.direction == SignalType.BUY
    assert found.detected_bar_time == setup.detected_bar_time
    assert found.pullback_low == 1.0990 and found.pullback_high == 1.1010
    assert found.status == SetupStatus.PENDING
    repo.close()


def test_get_returns_none_when_no_pending_setup_exists(tmp_path):
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    assert repo.get("EURUSD", SignalType.BUY) is None
    repo.close()


def test_mark_consumed_removes_it_from_get(tmp_path):
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    setup = _setup()
    repo.save(setup)
    repo.mark(setup, SetupStatus.CONSUMED)
    assert repo.get("EURUSD", SignalType.BUY) is None
    repo.close()


def test_mark_expired_removes_it_and_counts_as_expired(tmp_path):
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    setup = _setup()
    repo.save(setup)
    repo.mark(setup, SetupStatus.EXPIRED)
    assert repo.get("EURUSD", SignalType.BUY) is None
    assert repo.expired_count == 1


def test_save_with_same_detected_bar_time_does_not_duplicate(tmp_path):
    """Mirrors InMemorySetupRepository.save(): re-detecting the identical
    pullback bar on a re-scan must not create a second row."""
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    setup = _setup()
    repo.save(setup)
    repo.save(_setup(bar_time=setup.detected_bar_time))  # same key
    row_count = repo._conn.execute("SELECT COUNT(*) FROM pending_setups").fetchone()[0]
    assert row_count == 1
    repo.close()


def test_setup_survives_a_simulated_restart(tmp_path):
    """Section 11: a new repository instance against the same file must see
    what a previous process-lifetime wrote."""
    path = tmp_path / "s.db"
    first = SQLiteSetupRepository(path)
    first.save(_setup())
    first.close()

    second = SQLiteSetupRepository(path)
    recovered = second.get("EURUSD", SignalType.BUY)
    assert recovered is not None
    assert recovered.status == SetupStatus.PENDING
    second.close()


def test_expired_setup_is_not_recovered_after_restart(tmp_path):
    """Section 5: only valid (non-expired) setups must be recovered; an
    expired one must never come back as PENDING."""
    path = tmp_path / "s.db"
    first = SQLiteSetupRepository(path)
    setup = _setup(bar_time=datetime.now(UTC) - timedelta(hours=5))
    first.save(setup)
    first.mark(setup, SetupStatus.EXPIRED)
    first.close()

    second = SQLiteSetupRepository(path)
    assert second.get("EURUSD", SignalType.BUY) is None
    second.close()


def test_independent_pairs_and_directions_do_not_collide(tmp_path):
    repo = SQLiteSetupRepository(tmp_path / "s.db")
    buy = _setup(pair="EURUSD", direction=SignalType.BUY)
    sell = _setup(pair="EURUSD", direction=SignalType.SELL)
    repo.save(buy)
    repo.save(sell)
    assert repo.get("EURUSD", SignalType.BUY) is not None
    assert repo.get("EURUSD", SignalType.SELL) is not None
    repo.close()
