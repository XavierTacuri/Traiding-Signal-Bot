from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    FOREX_PAIRS,
    Settings,
    _detect_obsolete_env_vars,
    warn_obsolete_env_vars,
)


def config(**updates) -> Settings:
    return Settings(_env_file=None, **updates)


def test_forex_pairs_loads_correctly_from_a_real_dotenv_file(tmp_path: Path):
    """Regression: pydantic-settings treats list[str] as a 'complex' type and
    tries json.loads() on raw env strings before any validator runs, which broke
    FOREX_PAIRS whenever it came from an actual .env file (not from kwargs)."""
    env_file = tmp_path / ".env"
    env_file.write_text("FOREX_PAIRS=EURUSD,GBPUSD,USDJPY\n", encoding="utf-8")
    settings = Settings(_env_file=str(env_file))
    assert settings.FOREX_PAIRS == ["EURUSD", "GBPUSD", "USDJPY"]


def test_default_settings_are_valid_and_match_effective_configuration():
    settings = config()
    assert settings.LOWER_TIMEFRAME == "M15"
    assert settings.HIGHER_TIMEFRAME == "H1"
    assert settings.FOREX_PAIRS == list(FOREX_PAIRS)
    assert len(settings.FOREX_PAIRS) == 7


@pytest.mark.parametrize("updates", [
    {"LOWER_TIMEFRAME": "H1"},
    {"HIGHER_TIMEFRAME": "M15"},
    {"LOWER_TIMEFRAME": "H1", "HIGHER_TIMEFRAME": "M15"},
])
def test_invalid_timeframe_relationship_fails_fast(updates):
    with pytest.raises(ValidationError):
        config(**updates)


@pytest.mark.parametrize("updates", [
    {"TREND_EMA_FAST": 200, "TREND_EMA_SLOW": 50},
    {"ENTRY_EMA_FAST": 50, "ENTRY_EMA_SLOW": 20},
    {"TREND_EMA_FAST": 50, "TREND_EMA_SLOW": 50},
])
def test_inconsistent_ema_periods_fail_fast(updates):
    with pytest.raises(ValidationError):
        config(**updates)


def test_duplicate_pairs_are_rejected():
    with pytest.raises(ValidationError, match="duplicados"):
        config(FOREX_PAIRS="EURUSD,GBPUSD,EURUSD")


def test_empty_pair_list_is_rejected():
    with pytest.raises(ValidationError, match="vacío"):
        config(FOREX_PAIRS="")


def test_unauthorized_symbol_is_rejected():
    with pytest.raises(ValidationError, match="no autorizados"):
        config(FOREX_PAIRS="EURUSD,XAUUSD")


@pytest.mark.parametrize("updates", [
    {"SCAN_INTERVAL_SECONDS": 0},
    {"SCAN_INTERVAL_SECONDS": -5},
])
def test_invalid_scan_interval_fails_fast(updates):
    with pytest.raises(ValidationError):
        config(**updates)


@pytest.mark.parametrize("score", [-1, 101])
def test_invalid_score_threshold_fails_fast(score):
    with pytest.raises(ValidationError):
        config(MIN_SIGNAL_SCORE=score)


@pytest.mark.parametrize("updates", [
    {"RISK_REWARD_RATIO": 0},
    {"RISK_REWARD_RATIO": -1.0},
])
def test_invalid_risk_reward_ratio_fails_fast(updates):
    with pytest.raises(ValidationError):
        config(**updates)


def test_inverted_rsi_triggers_fail_fast():
    with pytest.raises(ValidationError, match="RSI_BUY_TRIGGER"):
        config(RSI_BUY_TRIGGER=40, RSI_SELL_TRIGGER=60)


def test_negative_atr_multiplier_fails_fast():
    with pytest.raises(ValidationError):
        config(STOP_BUFFER_ATR_MULT=-0.1)


def test_invalid_timezone_fails_fast():
    with pytest.raises(ValidationError):
        config(MT5_TIMEZONE="Not/AZone")


def test_mt5_reconnect_max_delay_below_initial_delay_fails_fast():
    with pytest.raises(ValidationError, match="MT5_RECONNECT_MAX_DELAY_SECONDS"):
        config(MT5_RECONNECT_INITIAL_DELAY_SECONDS=30, MT5_RECONNECT_MAX_DELAY_SECONDS=10)


def test_mt5_resilience_defaults_are_valid():
    settings = config()
    assert settings.MT5_RECONNECT_INITIAL_DELAY_SECONDS == 5.0
    assert settings.MT5_RECONNECT_MAX_DELAY_SECONDS == 60.0
    assert settings.MT5_RECONNECT_BACKOFF_MULTIPLIER == 2.0
    assert settings.MT5_MAX_TICK_AGE_SECONDS == 120
    assert settings.MT5_STALE_BAR_AGE_MULTIPLIER == 3.0


def test_telegram_requires_both_fields_or_neither():
    config(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")  # neither: valid (e.g. backtesting)
    config(TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="y")  # both: valid
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        config(TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="")


def test_telegram_validation_error_never_exposes_the_token_value():
    fake_token = "123456789:SUPER-SECRET-NOT-REAL-TOKEN"
    with pytest.raises(ValidationError) as exc_info:
        config(TELEGRAM_BOT_TOKEN=fake_token, TELEGRAM_CHAT_ID="")
    assert fake_token not in str(exc_info.value)


def test_obsolete_env_vars_detected_from_os_environ(monkeypatch):
    monkeypatch.setenv("TIMEFRAME", "15M")
    assert _detect_obsolete_env_vars(env_file=None) == ["TIMEFRAME"]
    with pytest.raises(RuntimeError, match="TIMEFRAME"):
        warn_obsolete_env_vars(env_file=None)


def test_obsolete_env_vars_detected_from_dotenv_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("PAIRS=EURUSD,XAGUSD\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    assert _detect_obsolete_env_vars(env_file=str(env_file)) == ["PAIRS"]


def test_obsolete_env_var_message_never_contains_its_value(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PAIRS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("PAIRS=EURUSD,XAGUSD,SECRET_LOOKING_VALUE\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        warn_obsolete_env_vars(env_file=str(env_file))
    assert "SECRET_LOOKING_VALUE" not in str(exc_info.value)
    assert "FOREX_PAIRS" in str(exc_info.value)


def test_unrelated_os_and_ci_environment_variables_do_not_trigger_false_positives(monkeypatch):
    for name, value in (("CI", "true"), ("GITHUB_ACTIONS", "true"), ("RANDOM_TOOL_VAR", "1")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TIMEFRAME", raising=False)
    monkeypatch.delenv("PAIRS", raising=False)
    assert _detect_obsolete_env_vars(env_file=None) == []
    warn_obsolete_env_vars(env_file=None)  # must not raise


def test_no_env_file_present_does_not_raise(monkeypatch):
    monkeypatch.delenv("TIMEFRAME", raising=False)
    monkeypatch.delenv("PAIRS", raising=False)
    warn_obsolete_env_vars(env_file=str(Path("this_file_does_not_exist.env")))
