import logging
import traceback
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.services.notifier_service import NotifierService

FORBIDDEN_ORDER_EXECUTION_PATTERNS = (
    "order_send", "order_check", "order_close", "positions_get", "trade_request",
)

def test_production_and_backtesting_code_never_executes_orders():
    """Static safety net: the signals-only scope must survive future changes."""
    root = Path(__file__).resolve().parents[1]
    source_files = [
        path for directory in ("app", "backtesting")
        for path in (root / directory).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    offenders = [
        f"{path.relative_to(root)}:{pattern}"
        for path in source_files
        for pattern in FORBIDDEN_ORDER_EXECUTION_PATTERNS
        if pattern in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Código de ejecución de órdenes detectado: {offenders}"


class Response:
    def __init__(self, ok=True):
        self.ok = ok
    def raise_for_status(self):
        if not self.ok:
            import httpx
            raise httpx.HTTPError("temporary")
    def json(self):
        return {"ok": self.ok}

class Client:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0
    async def post(self, url, json):
        self.calls += 1
        return Response(self.calls > self.failures)

@pytest.mark.asyncio
async def test_telegram_retries_then_succeeds(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.notifier_service.asyncio.sleep", no_sleep)
    client = Client(failures=1)
    service = NotifierService(Settings(_env_file=None, TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="chat", TELEGRAM_MAX_RETRIES=3), client)
    await service.send_telegram_message("safe message")
    assert client.calls == 2

@pytest.mark.asyncio
async def test_telegram_failure_is_exposed_for_later_retry(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.notifier_service.asyncio.sleep", no_sleep)
    client = Client(failures=5)
    service = NotifierService(Settings(_env_file=None, TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="chat", TELEGRAM_MAX_RETRIES=2), client)
    with pytest.raises(RuntimeError):
        await service.send_telegram_message("safe message")
    assert client.calls == 2

class LeakyClient:
    """Simulates a lower-level error that embeds the request URL (token included)."""
    def __init__(self, token):
        self.calls = 0
        self.token = token
    async def post(self, url, json):
        self.calls += 1
        raise RuntimeError(f"connection failed for url {url}")

@pytest.mark.asyncio
async def test_telegram_token_never_leaks_through_raised_exception(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.notifier_service.asyncio.sleep", no_sleep)
    token = "123456789:FAKE-TEST-TOKEN-NOT-REAL"
    client = LeakyClient(token)
    service = NotifierService(Settings(_env_file=None, TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_CHAT_ID="chat", TELEGRAM_MAX_RETRIES=2), client)
    with pytest.raises(RuntimeError) as exc_info:
        await service.send_telegram_message("safe message")
    chain = f"{exc_info.value}\n{exc_info.value.__cause__}"
    assert token not in chain
    assert "***" in chain

@pytest.mark.asyncio
async def test_full_rendered_traceback_never_contains_token(monkeypatch):
    """Renders the exact text a real traceback.format_exception/logger.exception call
    would print (type, message, __cause__/__context__ chain and frames) and checks it."""
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.notifier_service.asyncio.sleep", no_sleep)
    token = "222333444:FAKE-TRACEBACK-TOKEN-NOT-REAL"
    client = LeakyClient(token)
    service = NotifierService(Settings(_env_file=None, TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_CHAT_ID="chat", TELEGRAM_MAX_RETRIES=2), client)
    try:
        await service.send_telegram_message("safe message")
        raise AssertionError("se esperaba RuntimeError")
    except RuntimeError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert token not in rendered
    assert "***" in rendered

def test_httpx_and_httpcore_loggers_are_silenced_at_import_time():
    """notifier_service must downgrade httpx/httpcore loggers so their own
    INFO-level request logging (URL with token included) never fires, even if
    something else later configures the root logger permissively."""
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

@pytest.mark.asyncio
async def test_httpx_internal_request_logging_never_exposes_token(caplog, monkeypatch):
    """Exercises a real httpx.AsyncClient (no real network, via MockTransport) and
    confirms httpx's own logger never emits the token, using the logger levels
    exactly as configured by notifier_service (not forced by the test)."""
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.notifier_service.asyncio.sleep", no_sleep)
    token = "555666777:FAKE-HTTPX-INTERNAL-LOG-TOKEN"

    def handler(request: httpx.Request) -> httpx.Response:
        assert token in str(request.url)  # sanity check: the real token IS in the URL
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        service = NotifierService(Settings(_env_file=None, TELEGRAM_BOT_TOKEN=token,
            TELEGRAM_CHAT_ID="chat", TELEGRAM_MAX_RETRIES=1), client)
        with caplog.at_level(logging.DEBUG):
            await service.send_telegram_message("safe message")

    leaked = [record.getMessage() for record in caplog.records if token in record.getMessage()]
    assert not leaked
