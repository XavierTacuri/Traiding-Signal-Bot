"""Fase 5, section 3: console logging (unchanged default) plus an opt-in,
rotating, UTC, secret-redacting file handler.

``setup_logger()`` keeps its exact Fase 0-4 behaviour (console only, called
once at import time below) so every existing test and log-format assertion
stays valid. File logging is a separate, explicit step
(``add_rotating_file_handler``) that callers opt into — importing this module
must never create a file on disk as a side effect, which would otherwise
happen on every test run and every ``python -c "import app..."``.
"""

from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "forex_signal_bot"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
FILE_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
HANDLER_MARKER = "_forex_signal_console_handler"
FILE_HANDLER_MARKER = "_forex_signal_file_handler"

# Telegram bot tokens look like "<digits>:<35-ish base64url chars>". Matched
# generically (not just the configured token) so a token pasted into an
# error message or a URL by any future code path is still caught — defence
# in depth on top of NotifierService's own redaction (Fase 1/3).
_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def _redact(text: str) -> str:
    from app.core.config import (
        settings,  # local import: avoids a config<->logger import cycle
    )

    token = settings.TELEGRAM_BOT_TOKEN
    if token:
        text = text.replace(token, "***")
    return _TOKEN_PATTERN.sub("***", text)


class RedactingFormatter(logging.Formatter):
    """Formats in UTC (never the host's local time) and strips anything
    token-shaped from the final rendered line — including text that only
    appears after %-interpolation or in a formatted traceback."""

    converter = staticmethod(time.gmtime)

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def setup_logger() -> logging.Logger:
    """Configure the application logger once without duplicating handlers."""
    application_logger = logging.getLogger(LOGGER_NAME)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    owned_handlers = [
        handler for handler in application_logger.handlers
        if getattr(handler, HANDLER_MARKER, False)
    ]
    if not owned_handlers:
        handler = logging.StreamHandler()
        setattr(handler, HANDLER_MARKER, True)
        handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        application_logger.addHandler(handler)
    return application_logger


def add_rotating_file_handler(
    target_logger: logging.Logger, path: str | Path, *,
    max_bytes: int, backup_count: int, level: int = logging.INFO,
) -> None:
    """Attach a size-rotated, UTF-8, UTC, secret-redacting file handler.

    Idempotent per logger (checked via a marker, same pattern as the console
    handler) so calling this more than once — e.g. a second ``add_rotating_
    file_handler`` during tests, or a module reimport — never duplicates
    the handler. ``RotatingFileHandler`` itself is not safe for several
    *processes* writing/rotating the same file concurrently; the watchdog
    (a separate process) must therefore always point at its own file, never
    this one (brief section 3).
    """
    if any(getattr(handler, FILE_HANDLER_MARKER, False) for handler in target_logger.handlers):
        return
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        file_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    setattr(handler, FILE_HANDLER_MARKER, True)
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=FILE_LOG_DATE_FORMAT))
    target_logger.addHandler(handler)


logger = setup_logger()
