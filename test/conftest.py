import pytest


class _NoOpConnection:
    """Stand-in for MT5ConnectionManager: no test may touch the real
    MetaTrader5 module (no terminal is installed/opened in CI or in local
    unit tests — see Fase 3 brief, section 12)."""

    async def ensure_connected(self) -> None:
        return None

    async def run(self, func, *args, timeout=None, **kwargs):
        # Deliberately never calls `func` — doing so would reach the real
        # MetaTrader5 module, violating this stub's whole purpose. Raising
        # here instead of leaving `run` undefined matters in practice:
        # outside the Fase 3/4 weekend-close window, Fase 5's tracker (run
        # as part of scanner.scan_once() whenever a signal was just
        # registered) calls this to try activating it, and must see a
        # plain, catchable "no connection" failure — the same shape
        # MT5ConnectionManager itself raises when disconnected — not an
        # AttributeError from a missing method.
        raise RuntimeError("Sin conexión MT5 (stub de pruebas)")


@pytest.fixture(autouse=True)
def _fake_mt5_connection(monkeypatch, tmp_path):
    from app.repositories.database import Database
    from app.repositories.outbox_repository import SQLiteOutboxRepository
    from app.repositories.signal_repository import SQLiteSignalRepository
    from app.services.signal_persistence_service import SignalPersistenceService
    from app.services.signal_tracker_service import SignalTrackerService
    from app.workers import scanner

    connection = _NoOpConnection()
    monkeypatch.setattr(scanner, "connection", connection)

    # Fase 4: every test gets its own temporary SQLite file (never the
    # production database at settings.DATABASE_PATH — brief section 17) and
    # a fresh SignalService/tracker bound to it, mirroring the Fase 3
    # connection reset below.
    database = Database(tmp_path / "test_signals.db")
    signal_repository = SQLiteSignalRepository(database)
    outbox_repository = SQLiteOutboxRepository(database)
    monkeypatch.setattr(scanner, "signal_service", None)
    monkeypatch.setattr(scanner, "database", database)
    monkeypatch.setattr(scanner, "signal_repository", signal_repository)
    monkeypatch.setattr(scanner, "outbox_repository", outbox_repository)
    monkeypatch.setattr(scanner, "persistence_service", SignalPersistenceService(outbox_repository))
    monkeypatch.setattr(scanner, "tracker_service",
                         SignalTrackerService(signal_repository=signal_repository, connection=connection))
    monkeypatch.setattr(scanner, "_last_tracker_run", None)
    yield
    monkeypatch.setattr(scanner, "signal_service", None)
