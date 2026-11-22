"""Fase 5, section 14: daily operational summary.

Delivered via NotifierService directly, not the Fase 4 outbox: every
``notification_outbox`` row requires a ``signal_id`` (NOT NULL foreign key
to ``signals``) and this report is not about any one signal — relaxing
that constraint would be altering the outbox's schema/semantics, which the
brief explicitly forbids. A failed send is simply retried the next time
``maybe_send`` is called later the same day (it only persists "sent" after
a confirmed delivery), and if the whole day passes without one succeeding,
that day's summary is skipped — documented, not hidden.

Only ever reads persisted data (heartbeats, signals, alert_incidents) and
never fabricates activity for a period the system was not observing
(section 14: "no inventar actividad... no interpretar los periodos sin
actividad como disponibilidad garantizada").
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings, settings
from app.core.logger import logger
from app.models.signal_record import SignalStatus
from app.monitoring.alerts import AlertRepository
from app.monitoring.heartbeat_repository import HeartbeatRepository
from app.repositories.signal_repository import SignalRepository


class DailySummaryService:
    def __init__(
        self, heartbeat_repository: HeartbeatRepository, signal_repository: SignalRepository,
        notifier: object, alert_db_path: str, config: Settings = settings,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._heartbeats = heartbeat_repository
        self._signals = signal_repository
        self._notifier = notifier
        self._alert_db_path = alert_db_path
        self.config = config
        self._now = clock

    async def maybe_send(self) -> bool:
        """Returns True only if a summary was actually delivered this call."""
        if not self.config.DAILY_SUMMARY_ENABLED:
            return False
        local_now = self._now().astimezone(ZoneInfo(self.config.DAILY_SUMMARY_TIMEZONE))
        if local_now.time() < self.config.DAILY_SUMMARY_TIME:
            return False
        report_date = local_now.date()
        if await self._heartbeats.has_daily_summary_been_sent(report_date):
            return False
        message = await self.build_message(report_date)
        try:
            await self._notifier.send_telegram_message(message)
        except RuntimeError as exc:
            logger.warning("daily_summary | envío fallido; se reintentará más tarde hoy: %s", exc)
            return False
        return await self._heartbeats.mark_daily_summary_sent(report_date, self._now())

    async def build_message(self, report_date: date) -> str:
        daily = await self._heartbeats.get_daily_stats(report_date)
        heartbeat = await self._heartbeats.get()
        signals = await self._signals.get_all()
        generated_today = [s for s in signals if s.created_at_utc.date() == report_date]
        closed_today = [s for s in signals if s.closed_at_utc and s.closed_at_utc.date() == report_date]
        take_profit = sum(1 for s in closed_today if s.status == SignalStatus.TAKE_PROFIT)
        stop_loss = sum(1 for s in closed_today if s.status == SignalStatus.STOP_LOSS)
        ambiguous = sum(1 for s in closed_today if s.status == SignalStatus.AMBIGUOUS)
        critical_incidents = self._count_critical_incidents_today(report_date)
        status_text = heartbeat.status.value if heartbeat is not None else "N/D"
        outbox_pending = heartbeat.outbox_pending_count if heartbeat is not None else "N/D"
        outbox_failed = heartbeat.outbox_failed_count if heartbeat is not None else "N/D"
        return "\n".join([
            f"📋 RESUMEN DIARIO — {report_date.isoformat()} (operativo)",
            "",
            f"Estado general: {status_text}",
            f"Ciclos de escaneo: {daily.cycles_total} (fallidos: {daily.cycles_failed})",
            f"Reconexiones MT5: {daily.mt5_reconnections}",
            f"Incidentes críticos: {critical_incidents}",
            f"Outbox — pendiente: {outbox_pending} | fallido: {outbox_failed}",
            "",
            f"Señales generadas hoy: {len(generated_today)}",
            (f"Resultados virtuales cerrados hoy: TAKE_PROFIT={take_profit} "
             f"STOP_LOSS={stop_loss} AMBIGUO={ambiguous}"),
            "",
            ("Estadísticas virtuales (seguimiento simulado); no representan operaciones "
             "reales ni garantizan rentabilidad. Un periodo sin actividad no implica "
             "disponibilidad garantizada del sistema."),
        ])

    def _count_critical_incidents_today(self, report_date: date) -> int:
        repository = AlertRepository(self._alert_db_path)
        try:
            return repository.count_open_critical_today(report_date.isoformat())
        finally:
            repository.close()
