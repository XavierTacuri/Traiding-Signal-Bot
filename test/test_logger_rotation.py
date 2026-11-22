"""Fase 5, section 3: rotating file handler, secret redaction, idempotent
initialization, UTC timestamps."""

from __future__ import annotations

import logging

import pytest

from app.core.logger import FILE_HANDLER_MARKER, add_rotating_file_handler, logger


@pytest.fixture(autouse=True)
def _clean_file_handlers():
    yield
    for handler in list(logger.handlers):
        if getattr(handler, FILE_HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()


def test_add_rotating_file_handler_creates_the_log_file(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=2)
    logger.info("hola")
    for handler in logger.handlers:
        handler.flush()
    assert log_file.exists()
    assert "hola" in log_file.read_text(encoding="utf-8")


def test_add_rotating_file_handler_is_idempotent(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=2)
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=2)
    file_handlers = [h for h in logger.handlers if getattr(h, FILE_HANDLER_MARKER, False)]
    assert len(file_handlers) == 1


def test_rotation_creates_a_backup_file_once_max_bytes_is_exceeded(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=200, backup_count=2)
    for _ in range(50):
        logger.info("x" * 20)
    for handler in logger.handlers:
        handler.flush()
    assert log_file.exists()
    assert (tmp_path / "trading_bot.log.1").exists()


def test_backup_count_bounds_the_number_of_rotated_files(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=100, backup_count=2)
    for _ in range(400):
        logger.info("x" * 20)
    for handler in logger.handlers:
        handler.flush()
    assert (tmp_path / "trading_bot.log.2").exists()
    assert not (tmp_path / "trading_bot.log.3").exists()


def test_file_handler_uses_utf8_and_renders_accented_characters(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=1)
    logger.info("señal rechazada: EURUSD")
    for handler in logger.handlers:
        handler.flush()
    assert "señal rechazada: EURUSD" in log_file.read_text(encoding="utf-8")


def test_telegram_bot_token_is_redacted_from_logged_messages(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456789:AAFAKETOKENFORTESTINGPURPOSESONLY12345")
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=1)
    logger.info("fallo enviando a https://api.telegram.org/bot123456789:AAFAKETOKENFORTESTINGPURPOSESONLY12345/sendMessage")
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "AAFAKETOKENFORTESTINGPURPOSESONLY12345" not in content
    assert "***" in content


def test_generic_token_shaped_string_is_redacted_even_without_configured_token(tmp_path):
    log_file = tmp_path / "trading_bot.log"
    add_rotating_file_handler(logger, log_file, max_bytes=1_000_000, backup_count=1)
    logger.info("token filtrado: 987654321:FAKEzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "987654321:FAKEzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz" not in content
    assert "***" in content


def test_httpx_and_httpcore_loggers_stay_silenced_after_file_handler_setup(tmp_path):
    """Fase 1/3 protection (notifier_service.py) must still hold after
    Fase 5 adds its own handlers/loggers."""
    add_rotating_file_handler(logger, tmp_path / "trading_bot.log", max_bytes=1_000_000, backup_count=1)
    import app.services.notifier_service  # noqa: F401 - import side effect sets the levels

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
