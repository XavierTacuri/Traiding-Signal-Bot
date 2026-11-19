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

## Configuración

Todos los parámetros viven en `app/core/config.py` y pueden sobrescribirse con las variables descritas en `.env.example`, organizado por categorías (general, MetaTrader 5, Telegram, pares, temporalidades, indicadores, filtros de estrategia). Solicite al menos 250 barras H1; el valor por defecto es 300. No se incluyen ajustes arbitrarios por par.

`Settings` valida de forma estricta y falla rápido al arrancar: temporalidades y pares autorizados, ausencia de duplicados, lista de pares no vacía, relación entre EMA rápida/lenta, rango de ADX, umbrales RSI coherentes entre sí, rangos de SL/TP y riesgo/beneficio, umbral de score, zonas horarias IANA válidas, y consistencia del par `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (ambos o ninguno). Ningún mensaje de error incluye valores de credenciales.

El estado de setup e idempotencia usa repositorios en memoria porque la arquitectura actual es un solo proceso y no tiene base de datos. Se perderá al reiniciar. Las interfaces permiten sustituirlos por almacenamiento persistente si se despliega con múltiples procesos o se requiere continuidad entre reinicios.

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

- La deduplicación y setups no sobreviven reinicios.
- La correlación básica solo agrupa exposición USD equivalente; no estima correlaciones estadísticas móviles.
- El backtester aporta el motor y métricas, pero requiere histórico de calidad y modelado realista de spread/slippage.
- El proyecto es un worker de consola; pese al nombre histórico del repositorio, actualmente no expone endpoints FastAPI.

> Este software genera señales basadas en reglas técnicas. No garantiza resultados ni rentabilidad. Debe validarse mediante backtesting, pruebas fuera de muestra y cuenta demo antes de utilizarse en un entorno real.
