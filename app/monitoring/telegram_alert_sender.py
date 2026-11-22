"""Fase 5, section 11: a minimal Telegram sender independent of
NotifierService/the outbox.

The worker cannot alert about its own death — by the time that would be
needed, its event loop and outbox are gone. The watchdog is a separate
process (section 7) with no event loop, so this uses a plain synchronous
``httpx.Client`` call instead of NotifierService's ``AsyncClient`` — not a
duplicate of it, a deliberately independent, much smaller path that keeps
working precisely when the worker's own path cannot.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("forex_signal_watchdog")

# Bounded: section 12 explicitly forbids an infinite retry loop for one
# alert. A real retry happens naturally on the watchdog's own next check
# cycle (already cooldown-gated by app/monitoring/alerts.py), not here.
MAX_ATTEMPTS = 2
TIMEOUT_SECONDS = 10.0


def _redact(message: str, token: str) -> str:
    return message.replace(token, "***") if token else message


def send_alert(token: str, chat_id: str, text: str) -> bool:
    """Returns True only on a confirmed Telegram delivery. Never raises —
    every failure is caught, redacted and logged (never the URL, which
    embeds the token)."""
    if not token or not chat_id:
        logger.warning("watchdog | Telegram no configurado; alerta solo registrada en logs")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last_error: str | None = None
    for _attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            if response.json().get("ok"):
                return True
            last_error = "Telegram rechazó el mensaje"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = _redact(str(exc), token)
    logger.warning("watchdog | no se pudo enviar la alerta a Telegram: %s", last_error)
    return False
