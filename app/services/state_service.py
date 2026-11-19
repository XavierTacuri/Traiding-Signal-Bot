from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain import PendingSetup, SetupStatus, SignalType


class SetupRepository(Protocol):
    def get(self, pair: str, direction: SignalType) -> PendingSetup | None: ...
    def save(self, setup: PendingSetup) -> None: ...
    def mark(self, setup: PendingSetup, status: SetupStatus) -> None: ...

class InMemorySetupRepository:
    def __init__(self) -> None:
        self._setups: dict[tuple[str, SignalType], PendingSetup] = {}
        self.detected_count = 0
        self.expired_count = 0

    def get(self, pair: str, direction: SignalType) -> PendingSetup | None:
        setup = self._setups.get((pair, direction))
        return setup if setup and setup.status == SetupStatus.PENDING else None

    def latest(self, pair: str, direction: SignalType) -> PendingSetup | None:
        return self._setups.get((pair, direction))

    def save(self, setup: PendingSetup) -> None:
        current = self.get(setup.pair, setup.direction)
        if current is None or current.detected_bar_time != setup.detected_bar_time:
            self._setups[(setup.pair, setup.direction)] = setup
            self.detected_count += 1

    def mark(self, setup: PendingSetup, status: SetupStatus) -> None:
        if status == SetupStatus.EXPIRED and setup.status != SetupStatus.EXPIRED:
            self.expired_count += 1
        setup.status = status

class SignalKeyRepository(Protocol):
    def contains(self, key: str) -> bool: ...
    def mark_sent(self, key: str, sent_at: datetime) -> None: ...

class InMemorySignalKeyRepository:
    def __init__(self) -> None:
        self._sent: dict[str, datetime] = {}

    def contains(self, key: str) -> bool:
        return key in self._sent

    def mark_sent(self, key: str, sent_at: datetime) -> None:
        self._sent[key] = sent_at
