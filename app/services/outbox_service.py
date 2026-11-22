"""Fase 4, section 7: delivers due notification_outbox items via the
existing NotifierService, without ever touching SQLite and Telegram in the
same transaction (they cannot share one — see outbox_repository.py).

Delivery here is at-least-once, not exactly-once: ``NotifierService`` already
retries transiently within one call (TELEGRAM_MAX_RETRIES, Fase 2/3); once
those are exhausted this records a FAILED attempt with a backoff-delayed
``next_attempt_at_utc`` and tries again on a later scan cycle. If Telegram
accepts a message but the process crashes before ``mark_sent`` commits, the
same item is retried and the message is sent again — this residual risk is
intentional and documented (brief section 7), not eliminated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logger import logger
from app.repositories.outbox_repository import OutboxRepository

# Linear backoff capped at 15 minutes: generous enough not to hammer the
# Telegram API during an extended outage, short enough that a transient blip
# still gets flushed within one SCAN_INTERVAL_SECONDS-scaled few cycles.
_BACKOFF_STEP_SECONDS = 60
_BACKOFF_MAX_SECONDS = 900


class OutboxProcessor:
    def __init__(self, outbox_repository: OutboxRepository, notifier: Any,
                 clock: Any | None = None) -> None:
        self._outbox = outbox_repository
        self._notifier = notifier
        self._now = clock or (lambda: datetime.now(UTC))

    async def process_due(self) -> list[int]:
        """Attempt delivery of every due item once. Returns the signal_ids
        of items that were newly marked SENT during this call."""
        now = self._now()
        delivered_signal_ids: list[int] = []
        for item in await self._outbox.due(now):
            try:
                await self._notifier.send_telegram_message(item.payload)
            except RuntimeError as exc:
                attempts = item.attempts + 1
                delay = min(_BACKOFF_STEP_SECONDS * attempts, _BACKOFF_MAX_SECONDS)
                await self._outbox.mark_failed(item.id, attempts, now + timedelta(seconds=delay), str(exc))
                logger.warning("outbox | %s | envío fallido (intento #%d, reintento en %ds): %s",
                                item.message_type, attempts, delay, exc)
                continue
            await self._outbox.mark_sent(item.id, now)
            delivered_signal_ids.append(item.signal_id)
            logger.info("outbox | %s | notificación entregada (signal_id=%d)",
                        item.message_type, item.signal_id)
        return delivered_signal_ids
