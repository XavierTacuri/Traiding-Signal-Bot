"""Fase 5, sections 7-10: standalone watchdog process.

Run as its own OS process, separate from the worker:

    python -m app.monitoring.watchdog

It never imports MetaTrader5 or app.services.mt5_connection, never opens a
connection to the broker, and never shares an event loop, thread or SQLite
connection object with the worker — it only reads the ``heartbeats`` row the
worker already wrote (section 9: "no realizar consultas adicionales a MT5
desde el watchdog externo" / "no crear dos procesos que intenten controlar
simultáneamente la misma sesión MT5"). This is what lets it be started,
tested and reasoned about without MT5 or a live worker at all.

``evaluate()`` is the pure decision core (no I/O, no sleep) so tests can
drive every scenario in section 18 directly; ``main()`` is the thin loop
that calls it on a timer and turns its verdicts into
``AlertManager.raise_alert``/``resolve_alert`` calls.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, settings
from app.core.logger import add_rotating_file_handler
from app.monitoring.alerts import AlertManager, AlertRepository
from app.monitoring.models import AlertSeverity, WorkerStatus
from app.monitoring.telegram_alert_sender import send_alert

logger = logging.getLogger("forex_signal_watchdog")
_CONSOLE_HANDLER_MARKER = "_forex_signal_watchdog_console_handler"

# Fixed, small set of monitored conditions (section 13: avoid unbounded/
# high-cardinality incident keys) — alert_incidents never grows past this.
HEARTBEAT_KEY = "heartbeat"
SCANNER_PROGRESS_KEY = "scanner_progress"
MT5_CONNECTION_KEY = "mt5_connection"
SQLITE_KEY = "sqlite"
OUTBOX_KEY = "outbox"

_OUTBOX_FAILED_ALERT_THRESHOLD = 3


@dataclass(slots=True)
class HeartbeatRow:
    recorded_at_utc: datetime
    status: str
    last_cycle_started_at_utc: datetime | None
    last_cycle_finished_at_utc: datetime | None
    mt5_connection_state: str | None
    mt5_consecutive_failures: int
    sqlite_ok: bool
    outbox_failed_count: int


def read_heartbeat(path: str | Path) -> HeartbeatRow | None:
    """Read-only: opens its own connection, issues a single SELECT, closes.
    Returns None if the database/table/row does not exist yet — a worker
    that has never started is reported the same way as "no data available",
    which app/monitoring/watchdog.py's startup grace period already handles."""
    try:
        # as_uri() yields a correct file:// URI on both Windows ("D:\..." ->
        # "file:///D:/...") and POSIX; building one by hand from a bare
        # Windows path would mangle the drive letter and backslashes.
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError:
        return None  # file does not exist yet
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM heartbeats WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None  # table not migrated yet
    finally:
        conn.close()
    if row is None:
        return None

    def parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    return HeartbeatRow(
        recorded_at_utc=parse(row["recorded_at_utc"]), status=row["status"],
        last_cycle_started_at_utc=parse(row["last_cycle_started_at_utc"]),
        last_cycle_finished_at_utc=parse(row["last_cycle_finished_at_utc"]),
        mt5_connection_state=row["mt5_connection_state"],
        mt5_consecutive_failures=row["mt5_consecutive_failures"],
        sqlite_ok=bool(row["sqlite_ok"]), outbox_failed_count=row["outbox_failed_count"],
    )


Judgment = tuple[str, AlertSeverity, str] | None


def evaluate(
    heartbeat: HeartbeatRow | None, now: datetime, watchdog_started_at: datetime, config: Settings,
) -> dict[str, Judgment]:
    """Pure: no I/O, no sleep. A key mapped to ``None`` means "currently
    fine" (the caller resolves any open incident for it); a key mapped to a
    (component, severity, message) tuple means "raise/keep this alert". A
    key simply absent from the result means "cannot be judged this pass"
    (e.g. every signal but heartbeat-presence, while no heartbeat exists at
    all) and the caller must leave whatever incident state it had alone.
    """
    if heartbeat is None:
        since_start = (now - watchdog_started_at).total_seconds()
        if since_start > config.WATCHDOG_STARTUP_GRACE_SECONDS:
            return {HEARTBEAT_KEY: (
                "worker", AlertSeverity.CRITICAL,
                ("No se ha recibido ningún heartbeat del worker desde que el watchdog "
                 "arrancó; el proceso puede no haber iniciado o haber fallado antes de "
                 "escribir el primer heartbeat."),
            )}
        return {}  # still within startup grace — e.g. MT5 is still initializing

    judgments: dict[str, Judgment] = {}
    age = (now - heartbeat.recorded_at_utc).total_seconds()
    if age > config.HEARTBEAT_STALE_AFTER_SECONDS:
        shutdown_grace = config.HEARTBEAT_STALE_AFTER_SECONDS + config.WATCHDOG_SHUTDOWN_GRACE_SECONDS
        if heartbeat.status == WorkerStatus.STOPPING and age <= shutdown_grace:
            judgments[HEARTBEAT_KEY] = None  # ordered shutdown in progress, not a failure
        else:
            judgments[HEARTBEAT_KEY] = (
                "worker", AlertSeverity.CRITICAL,
                (f"Heartbeat obsoleto desde hace {age:.0f}s (último estado conocido: "
                 f"{heartbeat.status}); el proceso puede haberse detenido o estar bloqueado."),
            )
        return judgments  # every other signal is unreliable once the heartbeat itself is stale

    judgments[HEARTBEAT_KEY] = None

    if heartbeat.last_cycle_started_at_utc is not None:
        running_for = (now - heartbeat.last_cycle_started_at_utc).total_seconds()
        finished_after_started = (
            heartbeat.last_cycle_finished_at_utc is not None
            and heartbeat.last_cycle_finished_at_utc >= heartbeat.last_cycle_started_at_utc
        )
        pairs = max(len(config.FOREX_PAIRS), 1)
        # Generous on purpose (section 8: never below the legitimate worst
        # case) — must comfortably exceed one full pass over every pair at
        # the per-call MT5 timeout, plus the heartbeat staleness window.
        stuck_threshold = config.HEARTBEAT_STALE_AFTER_SECONDS + config.MT5_REQUEST_TIMEOUT_SECONDS * pairs * 2
        if not finished_after_started and running_for > stuck_threshold:
            judgments[SCANNER_PROGRESS_KEY] = (
                "scanner", AlertSeverity.CRITICAL,
                (f"El ciclo de escaneo iniciado hace {running_for:.0f}s no ha finalizado "
                 "(posible llamada nativa a MT5 bloqueada; limitación documentada en Fase 3: "
                 "un timeout no puede interrumpir esa llamada, solo degradar la conexión)."),
            )
        else:
            judgments[SCANNER_PROGRESS_KEY] = None

    if heartbeat.mt5_connection_state in ("DEGRADED", "RECONNECTING"):
        judgments[MT5_CONNECTION_KEY] = (
            "mt5", AlertSeverity.WARNING,
            (f"Conexión MT5 en estado {heartbeat.mt5_connection_state} "
             f"({heartbeat.mt5_consecutive_failures} fallo(s) consecutivo(s) de reconexión)."),
        )
    else:
        judgments[MT5_CONNECTION_KEY] = None

    judgments[SQLITE_KEY] = None if heartbeat.sqlite_ok else (
        "sqlite", AlertSeverity.CRITICAL, "El worker reportó un error de SQLite en su último heartbeat.",
    )

    if heartbeat.outbox_failed_count >= _OUTBOX_FAILED_ALERT_THRESHOLD:
        judgments[OUTBOX_KEY] = (
            "outbox", AlertSeverity.WARNING,
            f"{heartbeat.outbox_failed_count} notificaciones en estado FAILED en el outbox.",
        )
    else:
        judgments[OUTBOX_KEY] = None

    return judgments


def apply_judgments(judgments: dict[str, Judgment], alert_manager: AlertManager) -> None:
    for incident_key, verdict in judgments.items():
        if verdict is None:
            alert_manager.resolve_alert(incident_key, f"{incident_key}: condición resuelta")
        else:
            component, severity, message = verdict
            alert_manager.raise_alert(incident_key, component, severity, message)


def run_check_cycle(
    database_path: str | Path, alert_manager: AlertManager, watchdog_started_at: datetime,
    config: Settings, clock: Any = lambda: datetime.now(UTC),
) -> dict[str, Judgment]:
    heartbeat = read_heartbeat(database_path)
    judgments = evaluate(heartbeat, clock(), watchdog_started_at, config)
    apply_judgments(judgments, alert_manager)
    return judgments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog independiente del Trading Signal Bot")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola revisión y termina")
    args = parser.parse_args(argv)

    add_rotating_file_handler(logger, "logs/watchdog.log",
                               max_bytes=settings.LOG_MAX_BYTES, backup_count=settings.LOG_BACKUP_COUNT)
    logger.setLevel(logging.INFO)
    # Same idempotency guard as app/core/logger.py's console handler —
    # calling main() more than once in a process (e.g. test_watchdog.py's
    # --once invocations) must never duplicate console output.
    if not any(getattr(h, _CONSOLE_HANDLER_MARKER, False) for h in logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | watchdog | %(message)s"))
        setattr(stream_handler, _CONSOLE_HANDLER_MARKER, True)
        logger.addHandler(stream_handler)

    started_at = datetime.now(UTC)
    repository = AlertRepository(settings.DATABASE_PATH)
    sender = lambda text: send_alert(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, text)
    alert_manager = AlertManager(repository, sender, settings)
    logger.info("watchdog | iniciado (verificando %s)", settings.DATABASE_PATH)
    try:
        while True:
            judgments = run_check_cycle(settings.DATABASE_PATH, alert_manager, started_at, settings)
            problems = {key: verdict for key, verdict in judgments.items() if verdict is not None}
            if problems:
                logger.warning("watchdog | condiciones activas: %s", ", ".join(problems))
            else:
                logger.info("watchdog | sin anomalías")
            if args.once:
                return 0
            time.sleep(settings.WATCHDOG_CHECK_INTERVAL_SECONDS)
    finally:
        repository.close()


if __name__ == "__main__":  # pragma: no cover - exercised via run_check_cycle/evaluate in tests
    raise SystemExit(main())
