import pytest


class _NoOpConnection:
    """Stand-in for MT5ConnectionManager: no test may touch the real
    MetaTrader5 module (no terminal is installed/opened in CI or in local
    unit tests — see Fase 3 brief, section 12)."""

    async def ensure_connected(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fake_mt5_connection(monkeypatch):
    from app.workers import scanner

    monkeypatch.setattr(scanner, "connection", _NoOpConnection())
