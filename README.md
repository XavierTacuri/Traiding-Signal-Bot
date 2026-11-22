# Bot de señales Forex H1/M15

Bot Python que obtiene velas cerradas de MetaTrader 5, genera señales técnicas BUY/SELL y las notifica por Telegram. No abre ni administra operaciones.

## Estrategia

Solo analiza `EURUSD`, `GBPUSD`, `USDJPY`, `USDCHF`, `AUDUSD`, `USDCAD` y `NZDUSD`. El resolvedor acepta símbolos del broker con prefijo o sufijo (`EURUSDm`, `EURUSD.a`, `pro.EURUSD`) conservando el par lógico. Metales, índices, criptomonedas, energías y materias primas son rechazados.

- H1, siempre con velas cerradas: EMA 50 sobre EMA 200, pendiente EMA 50, cierre respecto a EMA 50 y ADX 14.
- M15, siempre con velas cerradas: un toque a la zona EMA 20/50 dentro de 0.20 ATR crea un setup pendiente.
- El setup dura cuatro barras y solo una barra posterior puede confirmarlo mediante recuperación RSI 52/48, ruptura de las tres barras anteriores y cuerpo mínimo de 0.25 ATR.
- Filtros: sesión y rollover en zona explícita, volatilidad ATR, distancia a EMA 20 y spread/ATR.
- SL: swing de siete barras más buffer de 0.25 ATR, limitado entre 0.5 y 2.5 ATR. TP: 2R.
- Score: tendencia 25, ADX fuerte 15, pullback 20, RSI 15, vela 15 y spread 10. Las condiciones obligatorias rechazan antes de puntuar; mínimo 75.
- Correlación: de señales simultáneas con la misma exposición USD se conserva el score superior. La exposición contraria se evalúa por separado.

MACD no se calcula ni interviene en entradas. La posición cero de MT5 (vela abierta) nunca se entrega a la estrategia. Una clave `par:timeframe:dirección:hora` evita reenvíos; solo se registra tras una respuesta exitosa de Telegram.

## Instalación y ejecución

Requiere Windows de 64 bits, **Python 3.12 (64 bits)**, terminal MetaTrader 5 instalada y autenticada, y símbolos Forex visibles/disponibles en el broker.

Python 3.12 es la versión soportada: tiene wheels oficiales verificados para `MetaTrader5`, `pandas` y `numpy` en Windows de 64 bits, con mayor recorrido en el ecosistema científico de Python que versiones más recientes. Cree siempre un entorno virtual nuevo y nativo de la máquina donde se ejecutará el bot — nunca copie la carpeta `.venv` de otro equipo.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requeriments.txt
Copy-Item .env.example .env
python main.py
```

Para desarrollo (pruebas y lint) instale además las dependencias de `requeriments-dev.txt`, que incluyen las de producción:

```powershell
python -m pip install -r requeriments-dev.txt
```

Las versiones exactas y reproducibles de todo el árbol de dependencias (directas y transitivas) quedan fijadas en `requeriments-lock.txt`, generado y verificado desde un entorno limpio.

Complete `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`. Las credenciales de la cuenta MT5 no se almacenan en este proyecto: la librería usa la sesión configurada en la terminal. Nunca confirme `.env` en Git.

> **Nota de configuración:** algunos archivos `.env` heredados de versiones anteriores pueden contener las variables `TIMEFRAME` y `PAIRS`. Ninguna de las dos corresponde a un campo real de `Settings` (los nombres correctos son `LOWER_TIMEFRAME`/`HIGHER_TIMEFRAME` y `FOREX_PAIRS`). Desde la Fase 2, el arranque **falla explícitamente** si detecta cualquiera de las dos en su `.env` o en el entorno del sistema, con un mensaje que indica el campo correcto a usar — elimínelas o renómbrelas antes de ejecutar el bot.

Los timestamps epoch de MT5 se interpretan como UTC, que es el contrato de la API de MT5. `MT5_TIMEZONE`, `SYSTEM_TIMEZONE` y `TRADING_TIMEZONE` hacen explícita la conversión; cambie `TRADING_TIMEZONE` por un nombre IANA válido si opera otra sesión. Ajuste la sesión y rollover conforme al servidor y broker, validándolo primero en demo.

## Estabilidad y reconexión MT5 (Fase 3)

`app/services/mt5_connection.py` centraliza el ciclo de vida de MetaTrader 5 en un `MT5ConnectionManager` con estados `DISCONNECTED/CONNECTING/CONNECTED/RECONNECTING/DEGRADED/STOPPING`. Toda llamada a MT5 (`initialize`, `shutdown`, `terminal_info`, `symbol_info`, `symbol_info_tick`, `copy_rates_*`) se serializa en un único hilo dedicado, así que `initialize`/`shutdown` nunca compiten con una lectura en curso y dos llamadas nativas nunca se ejecutan a la vez.

- **Reconexión**: backoff exponencial con jitter (`MT5_RECONNECT_INITIAL_DELAY_SECONDS=5`, `MT5_RECONNECT_MAX_DELAY_SECONDS=60`, `MT5_RECONNECT_BACKOFF_MULTIPLIER=2`). El contador de fallos se reinicia tras una reconexión exitosa. Un error de configuración (credenciales/parámetros inválidos) se distingue de uno recuperable y detiene los reintentos en vez de insistir indefinidamente.
- **Timeouts** (`MT5_CONNECT_TIMEOUT_SECONDS=15`, `MT5_REQUEST_TIMEOUT_SECONDS=10`): acotan cuánto se espera una respuesta, pero no cancelan la llamada nativa bloqueada (Python no puede interrumpir una extensión C en bloqueo) — superado el timeout, la conexión pasa a `DEGRADED` y no se emiten nuevas llamadas hasta que el hilo ocupado efectivamente retorne. La recuperación total de un bloqueo nativo real requiere el watchdog externo planificado para una fase posterior.
- **Frescura de datos** (`MT5_MAX_TICK_AGE_SECONDS=120`, `MT5_STALE_BAR_AGE_MULTIPLIER=3.0`): antes de evaluar la estrategia se valida integridad OHLC, orden cronológico, ausencia de duplicados/valores no finitos, cantidad mínima de velas y antigüedad del tick/última vela cerrada. Estas validaciones se omiten durante el cierre de fin de semana estándar (viernes 21:00 UTC a domingo 21:00 UTC) para no confundir el cierre normal del mercado con una desconexión. Datos obsoletos o inválidos producen `HOLD` (nunca una señal con precios viejos) y el motivo queda en el log.
- **Scanner**: `scan_once()` verifica la conexión antes de iterar los pares; si no hay conexión, suspende todo el ciclo sin tocar MT5 por par y sin busy-loop. Un error puntual en un par no detiene a los demás. Un error de configuración no recuperable detiene `run_scanner()` de forma controlada y diagnosticable.
- **Apagado ordenado**: `main.py` instala manejadores para `SIGINT`/`SIGTERM`/`SIGBREAK` (en Windows solo Ctrl+C/SIGBREAK llegan de forma fiable; no hay entrega real de SIGTERM externo), cancela la tarea del scanner y llama `mt5.shutdown()` en un `finally`, liberando el hilo dedicado incluso si una llamada quedó bloqueada.

## Persistencia y seguimiento virtual de señales (Fase 4)

El bot guarda su estado en un archivo SQLite local (`DATABASE_PATH`, por defecto `data/signals.db`; creado automáticamente, excluido de Git — no es un sistema de respaldo). Al iniciar, `main.py` aplica las migraciones pendientes (`app/repositories/migrations.py`, versionadas con `PRAGMA user_version`) y recupera setups pendientes vigentes, señales activas y notificaciones de Telegram todavía no entregadas; nada de esto reenvía señales ni reinterpreta resultados con datos futuros.

- **Señales** (`app/repositories/signal_repository.py`): cada señal queda registrada con una clave única (`signal_key`) que impide duplicados tras un reinicio o un reescaneo — la restricción `UNIQUE` de SQLite es la garantía, no una comprobación en memoria.
- **Outbox de Telegram** (`app/repositories/outbox_repository.py`, `app/services/outbox_service.py`): la señal y su primera notificación se registran en una sola transacción. El envío a Telegram ocurre después, fuera de esa transacción — SQLite y Telegram no pueden compartir una (no hay entrega exactamente una vez). Si Telegram falla, el mensaje queda pendiente con backoff y reintento; nunca se genera una segunda señal por ello. Riesgo residual documentado: si el proceso cae justo después de un envío exitoso pero antes de registrarlo, el reintento puede duplicar el mensaje de Telegram (no la señal).
- **Seguimiento virtual** (`app/services/signal_tracker_service.py`, `SignalTrackerService`): nunca abre, cierra ni modifica operaciones ni consulta posiciones reales — solo lee velas y cotizaciones de MT5. Activa la señal (BUY con `ask`, SELL con `bid`) en la primera cotización viva y fresca que observa; evalúa TP/SL con los niveles originales de la estrategia sobre velas M15 cerradas. Si SL y TP caen dentro de la misma vela sin dato intravela, el resultado queda `AMBIGUOUS` — nunca se asume una operación ganadora por defecto. Estados: `PENDING_ACTIVATION`, `ACTIVE`, `TAKE_PROFIT`, `STOP_LOSS`, `EXPIRED`, `CANCELLED`, `AMBIGUOUS`.
- **Pips y R** (`app/services/tracking_calculations.py`): el tamaño de pip depende de los decimales reales del símbolo (JPY y no-JPY), no de una tabla fija. El resultado en R usa el riesgo original (`|entrada − stop|`) definido al generar la señal, nunca recalculado. No se calculan ganancias en dinero: el bot no abre posiciones ni tiene volumen real.
- **Estadísticas** (`app/services/statistics_service.py`): win rate, expectancy y profit factor solo consideran señales `TAKE_PROFIT`/`STOP_LOSS`; cuando no hay datos suficientes se muestra `N/D`, nunca un valor inventado. Señales pendientes, vencidas o ambiguas no se cuentan como ganancia ni pérdida.
- **Notificaciones de resultado** (`SIGNAL_RESULT_NOTIFICATIONS_ENABLED`): opcionalmente informan activación/TP/SL/expiración/ambiguo, siempre etiquetadas SIMULADO/VIRTUAL.

Nuevas variables (`.env.example`): `DATABASE_PATH`, `SIGNAL_TRACKING_ENABLED`, `SIGNAL_EXPIRATION_BARS`, `TRACKER_INTERVAL_SECONDS`, `SIGNAL_RESULT_NOTIFICATIONS_ENABLED`.

El bot sigue sin ejecutar operaciones: no usa `order_send` ni equivalentes, no abre ni administra posiciones reales. Todo resultado es una simulación basada en datos de mercado observados, no una garantía de rentabilidad.

## Configuración

Todos los parámetros viven en `app/core/config.py` y pueden sobrescribirse con las variables descritas en `.env.example`, organizado por categorías (general, MetaTrader 5, Telegram, pares, temporalidades, indicadores, filtros de estrategia). Solicite al menos 250 barras H1; el valor por defecto es 300. No se incluyen ajustes arbitrarios por par.

`Settings` valida de forma estricta y falla rápido al arrancar: temporalidades y pares autorizados, ausencia de duplicados, lista de pares no vacía, relación entre EMA rápida/lenta, rango de ADX, umbrales RSI coherentes entre sí, rangos de SL/TP y riesgo/beneficio, umbral de score, zonas horarias IANA válidas, y consistencia del par `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (ambos o ninguno). Ningún mensaje de error incluye valores de credenciales.

Desde la Fase 4, el estado de setups, señales y deduplicación se persiste en SQLite (ver sección siguiente) en vez de perderse al reiniciar. Las implementaciones en memoria (`InMemory*`) siguen existiendo y se usan en pruebas unitarias; la arquitectura sigue siendo de un solo proceso.

## Tests y calidad

Estos son los mismos comandos que ejecuta GitHub Actions (ver abajo); córralos localmente antes de subir cambios:

```powershell
python -m pip install -r requeriments-dev-lock.txt
python -m pip check
python -m pytest -q --cov=app --cov=backtesting --cov-report=term-missing --cov-branch
ruff check .
python -m compileall -q app backtesting test main.py
python -m pip_audit
```

Las pruebas usan fixtures/mocks: no conectan a una cuenta MT5 real, no envían mensajes reales a Telegram y no ejecutan órdenes (hay pruebas estáticas y una dinámica dedicadas que verifican esto último, tanto para el worker como para el backtester). El repositorio no tiene configuración de mypy, Black, FastAPI, SQLAlchemy, Alembic ni Docker; por tanto no hay migraciones que aplicar. Comando de migración: **no aplica**.

Detección preventiva de secretos ([Gitleaks](https://github.com/gitleaks/gitleaks), MIT, gratuito, binario local o GitHub Action):

```powershell
gitleaks git --redact -v .          # historial
gitleaks dir --redact -v -c .gitleaks.toml .   # árbol de trabajo
```

Inteligencia artificial: `app/services/ai_service.py` existe únicamente como stub de compatibilidad, deshabilitado y no importado por ningún módulo activo. El bot no requiere ni utiliza ninguna clave de API de IA para generar señales.

## Integración continua (GitHub Actions)

`.github/workflows/ci.yml` se ejecuta en cada `push`, en los Pull Request hacia `main` y manualmente (`workflow_dispatch`). Tiene dos jobs:

- **test** (Windows, Python 3.12 x64 — MetaTrader5 solo publica wheels para Windows): instala dependencias fijadas desde `requeriments-dev-lock.txt`, y corre `pip check`, Ruff, pytest con cobertura, `compileall` y `pip-audit`. No instala el terminal de MetaTrader 5 ni usa credenciales reales; todas las pruebas usan un MetaTrader5 simulado.
- **secrets**: escanea el repositorio con Gitleaks en cada push/PR.

No hay despliegue automático configurado; esta fase es exclusivamente integración continua.

## Backtesting

El backtester usa exactamente `IndicatorService`, `StrategyService`, el repositorio de setups y el filtro de correlación de la aplicación. No importa el scanner ni el notificador, no ejecuta `order_send` y no modifica la cuenta.

### Requisitos MT5

1. Instale y abra MetaTrader 5 en Windows.
2. Inicie sesión manualmente en la terminal con una cuenta que permita consultar históricos; se recomienda demo.
3. Confirme que los pares están disponibles en el broker y aumente `Max. bars in chart` si el terminal entrega poco histórico.
4. Instale `MetaTrader5` mediante `python -m pip install -r requeriments.txt`.

La conexión usa la sesión activa de la terminal, igual que el bot. No hacen falta ni deben hardcodearse login, password o servidor. Pruebe primero la interfaz, sin conectarse:

```powershell
python -m backtesting.backtesting --help
```

Backtest de un par:

```powershell
python -m backtesting.backtesting `
  --source mt5 `
  --pairs EURUSD `
  --start 2025-01-01 `
  --end 2025-12-31
```

Backtest de los siete pares con exportaciones:

```powershell
python -m backtesting.backtesting `
  --source mt5 `
  --pairs EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD `
  --start 2024-01-01 `
  --end 2025-12-31 `
  --export-csv `
  --export-json
```

Para CSV, coloque archivos como `EURUSD_M15.csv` y `EURUSD_H1.csv`, con `time`, `open`, `high`, `low`, `close` y opcionalmente `tick_volume`, `spread`, `real_volume`:

```powershell
python -m backtesting.backtesting `
  --source csv `
  --csv-directory C:\historicos\forex `
  --pairs EURUSD `
  --start 2025-01-01 `
  --end 2025-12-31 `
  --spread-points 15
```

Los resultados se guardan por defecto en `backtesting/results/trades.csv` y `backtesting/results/summary.json`. `--risk-per-trade 1.0` arriesga 1% del balance actualizado; `--disable-correlation` permite comparar resultados sin el filtro USD. El modo conservador está activo: puede desactivarse explícitamente con `--no-conservative`, aunque hacerlo introduce una hipótesis optimista.

El resumen incluye setups, rechazos por etapa, operaciones cerradas/abiertas, win rate, profit factor, expectancy, R total/promedio, drawdown porcentual, balance y desgloses por par, dirección y mes. Profit factor es R positivo bruto dividido por R negativo bruto; expectancy es el promedio de R de operaciones cerradas.

Las velas se procesan por su hora de cierre. Una M15 solo puede consultar una H1 cuyo cierre ya haya ocurrido. EMA, RSI, ATR y ADX se calculan como series causales, la ruptura excluye la vela de señal y el resultado comienza a evaluarse en la vela posterior. Esto evita look-ahead bias en las reglas implementadas.

La cartera se simula mediante eventos cronológicos. Si coinciden en el mismo instante, las salidas se procesan antes que las entradas; el PnL se incorpora al balance realizado únicamente al salir y el riesgo de una entrada se calcula sobre ese balance realizado. Se permite como máximo una posición abierta por par. El límite de correlación USD y el riesgo agregado consideran solo posiciones que siguen abiertas en el evento de entrada.

Si SL y TP aparecen dentro de la misma M15 no se conoce el orden intrabar sin datos menores; por defecto se registra SL (-1R). El spread histórico se interpreta en puntos. Si falta, se usa `--spread-points`; en su ausencia se emite una advertencia y se aplica `BACKTEST_DEFAULT_SPREAD_POINTS=20` como aproximación conservadora configurable.

Los resultados dependen de la calidad del histórico, spread, slippage omitido y supuestos intrabar. Un backtest favorable no garantiza rendimiento futuro y debe complementarse con pruebas fuera de muestra y cuenta demo.

## Limitaciones y advertencia

- La correlación básica solo agrupa exposición USD equivalente; no estima correlaciones estadísticas móviles.
- El backtester aporta el motor y métricas, pero requiere histórico de calidad y modelado realista de spread/slippage.
- El proyecto es un worker de consola; pese al nombre histórico del repositorio, actualmente no expone endpoints FastAPI.
- El seguimiento virtual depende de que MT5 entregue velas/cotizaciones; si no lo hace durante mucho tiempo, una señal activa puede quedar sin resolver indefinidamente (igual que un trade "OPEN" en el backtester). La recuperación ante un bloqueo nativo real de MT5 sigue dependiendo del watchdog externo planificado para una fase posterior — no implementado aún.
- SQLite se usa como base de datos local de un solo proceso; no sustituye a un sistema de respaldo ni está pensado para múltiples procesos escribiendo a la vez.

> Este software genera señales basadas en reglas técnicas. No garantiza resultados ni rentabilidad. Debe validarse mediante backtesting, pruebas fuera de muestra y cuenta demo antes de utilizarse en un entorno real.
