"""Fase 4, section 18: end-to-end recovery across simulated process
restarts, against one temporary SQLite file shared by three independent
"process instances" (each its own Database/repository objects — never the
same Python object — so this genuinely exercises reopening the file, not
just reusing in-memory state)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.domain import PendingSetup, SetupStatus, SignalType
from app.models.signal_record import NotificationType, SignalStatus
from app.repositories.database import Database
from app.repositories.outbox_repository import SQLiteOutboxRepository
from app.repositories.setup_repository import SQLiteSetupRepository
from app.repositories.signal_repository import SQLiteSignalRepository
from app.schemas.signal_schema import SignalSchema
from app.services.signal_persistence_service import SignalPersistenceService
from app.services.signal_tracker_service import SignalTrackerService


@dataclass
class _Snapshot:
    bid: Decimal
    ask: Decimal
    tick_time: datetime | None


class _Market:
    def __init__(self, snapshot=None, bars=None):
        self.snapshot = snapshot
        self.bars = bars if bars is not None else pd.DataFrame(columns=["time", "low", "high"])

    def get_symbol_snapshot(self, _pair):
        return self.snapshot

    def get_historical_bars(self, _pair, _timeframe, _start, _end, now=None):
        return self.bars


def _signal(now: datetime) -> SignalSchema:
    return SignalSchema(
        pair="EURUSD", timeframe="M15", higher_timeframe="H1", signal_type="BUY",
        entry="1.10000", stop_loss="1.09500", take_profit="1.11000",
        score=90, digits=5, bar_time=now, adx_h1=28.0, rsi_m15=55.0,
        reason="retroceso + ruptura", signal_key=f"EURUSD:M15:BUY:{now.isoformat()}",
    )


@pytest.mark.asyncio
async def test_signal_lifecycle_survives_three_simulated_process_restarts(tmp_path):
    path = tmp_path / "signals.db"
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    signal = _signal(t0)

    # --- Instance 1: bot starts, a setup and a signal are generated -------
    db1 = Database(path)
    setups1 = SQLiteSetupRepository(path)
    outbox1 = SQLiteOutboxRepository(db1)

    setup = PendingSetup(pair="EURUSD", direction=SignalType.BUY, detected_at=t0,
                          detected_bar_time=t0 - timedelta(minutes=15), expires_after_bars=4,
                          pullback_low=1.0990, pullback_high=1.1010)
    setups1.save(setup)  # step 2: register a valid pending setup
    setups1.mark(setup, SetupStatus.CONSUMED)  # the signal below consumed it

    persistence1 = SignalPersistenceService(outbox1, clock=lambda: t0)
    registered = await persistence1.register_and_enqueue(signal, "mensaje telegram")  # steps 3-4
    assert registered is not None
    setups1.close()
    await db1.close()  # step 5: terminate the instance

    # --- Instance 2: restarts, recovers state, must not duplicate ---------
    db2 = Database(path)
    signals2 = SQLiteSignalRepository(db2)
    outbox2 = SQLiteOutboxRepository(db2)
    persistence2 = SignalPersistenceService(outbox2, clock=lambda: t0)

    recovered_pending = await signals2.get_pending_activation()
    assert len(recovered_pending) == 1 and recovered_pending[0].signal_key == signal.signal_key  # step 7

    duplicate_attempt = await persistence2.register_and_enqueue(signal, "mensaje telegram")
    assert duplicate_attempt is None  # step 8: no second signal for the same event
    assert len(await signals2.get_all()) == 1

    due = await outbox2.due(t0)
    assert len(due) == 1
    delivered_text = due[0].payload
    await outbox2.mark_sent(due[0].id, t0)  # step 9: process the pending notification with a mock
    assert delivered_text == "mensaje telegram"

    # step 10: activation, using a live quote now available after restart.
    activation_time = t0 + timedelta(minutes=5)
    tracker2 = SignalTrackerService(
        _Market(snapshot=_Snapshot(Decimal("1.10000"), Decimal("1.10010"), activation_time)),
        signals2, clock=lambda: activation_time,
    )
    events = await tracker2.evaluate_once()
    assert len(events) == 1 and events[0].notification_type == NotificationType.ACTIVATED
    await db2.close()

    # --- Instance 3: restarts again, continues tracking to a virtual close
    db3 = Database(path)
    signals3 = SQLiteSignalRepository(db3)
    close_time = t0 + timedelta(hours=1)
    bars = pd.DataFrame([{
        "time": pd.Timestamp(activation_time), "low": 1.0990, "high": 1.1105,
    }])
    tracker3 = SignalTrackerService(_Market(bars=bars), signals3, clock=lambda: close_time)

    events = await tracker3.evaluate_once()  # step 10 continued: virtual close
    assert len(events) == 1 and events[0].notification_type == NotificationType.TAKE_PROFIT

    record = await signals3.get_by_key(signal.signal_key)  # step 11: result persisted
    assert record.status == SignalStatus.TAKE_PROFIT
    assert record.result_r == pytest.approx(2.0)
    await db3.close()

    # --- A fourth read-only instance must see the same result, unchanged --
    db4 = Database(path)
    signals4 = SQLiteSignalRepository(db4)
    tracker4 = SignalTrackerService(_Market(), signals4, clock=lambda: close_time + timedelta(minutes=1))
    again = await tracker4.evaluate_once()  # must not re-evaluate a terminal signal
    final = await signals4.get_by_key(signal.signal_key)

    assert again == []
    assert final.status == SignalStatus.TAKE_PROFIT
    assert final.result_r == pytest.approx(2.0)  # not recalculated, not duplicated
    assert len(await signals4.get_all()) == 1
    await db4.close()
