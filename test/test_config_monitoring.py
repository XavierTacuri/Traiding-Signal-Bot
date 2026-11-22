"""Fase 5, section 15: coherence validation for the new monitoring settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_are_internally_coherent():
    Settings(_env_file=None)  # must not raise


def test_rejects_heartbeat_stale_not_greater_than_interval():
    with pytest.raises(ValidationError, match="HEARTBEAT_STALE_AFTER_SECONDS"):
        Settings(_env_file=None, HEARTBEAT_INTERVAL_SECONDS=60, HEARTBEAT_STALE_AFTER_SECONDS=60)


def test_rejects_watchdog_check_interval_longer_than_stale_threshold():
    with pytest.raises(ValidationError, match="WATCHDOG_CHECK_INTERVAL_SECONDS"):
        Settings(_env_file=None, HEARTBEAT_STALE_AFTER_SECONDS=60, WATCHDOG_CHECK_INTERVAL_SECONDS=120)


def test_rejects_startup_grace_shorter_than_stale_threshold():
    with pytest.raises(ValidationError, match="WATCHDOG_STARTUP_GRACE_SECONDS"):
        Settings(_env_file=None, HEARTBEAT_STALE_AFTER_SECONDS=180, WATCHDOG_STARTUP_GRACE_SECONDS=60)


def test_rejects_alert_cooldown_shorter_than_watchdog_check_interval():
    with pytest.raises(ValidationError, match="ALERT_COOLDOWN_SECONDS"):
        Settings(_env_file=None, WATCHDOG_CHECK_INTERVAL_SECONDS=120, ALERT_COOLDOWN_SECONDS=60)


def test_rejects_invalid_log_level():
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(_env_file=None, LOG_LEVEL="VERBOSE")


def test_log_level_is_normalized_to_uppercase():
    assert Settings(_env_file=None, LOG_LEVEL="debug").LOG_LEVEL == "DEBUG"


def test_rejects_invalid_daily_summary_timezone():
    with pytest.raises(ValidationError, match="Zona horaria"):
        Settings(_env_file=None, DAILY_SUMMARY_TIMEZONE="Nowhere/Fake")


def test_daily_summary_disabled_by_default():
    assert Settings(_env_file=None).DAILY_SUMMARY_ENABLED is False


def test_monitoring_enabled_by_default():
    assert Settings(_env_file=None).MONITORING_ENABLED is True
