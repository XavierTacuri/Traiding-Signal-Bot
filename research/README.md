# Fase 6 — Validación estadística de la estrategia

Herramientas de investigación independientes del bot en producción. Nada aquí modifica `app/`, `backtesting/backtesting.py`, la estrategia, el score, SQLite operativo, el heartbeat ni el watchdog. No ejecuta operaciones ni envía mensajes reales.

## Qué hay aquí

- `analysis/metrics.py` — win rate, profit factor, expectancy, drawdown en R, rachas, intervalos de confianza (Wilson, aproximación normal) y bootstrap por bloques (preserva dependencia temporal; nunca un bootstrap i.i.d. ingenuo).
- `analysis/temporal.py` — separación cronológica in-sample/out-of-sample con purga de operaciones que cruzan el límite, y ventanas walk-forward de parámetros fijos (sin reoptimización).
- `analysis/score_diagnostics.py` — reevaluación del hallazgo H-04 (piso estructural del score, correlación score↔resultado, poder discriminatorio).
- `analysis/data_quality.py` — auditoría de huecos, duplicados y OHLC inválido en un histórico, sin inventar ni interpolar nada.
- `studies/run_studies.py` — orquesta el motor real (`backtesting.backtesting.BacktestEngine`) sobre datos CSV ya descargados: reproduce el baseline original, corre el estudio extendido (IS/OOS, walk-forward, por par, incertidumbre, score, correlación) y la sensibilidad a costes. Nunca reimplementa la simulación.
- `reports/` — salidas JSON de la última ejecución (no sobrescribe `backtesting/results/`).

## Origen de los datos históricos

Los CSV usados por `run_studies.py` **no están en este repositorio** (son voluminosos: ~4 años × 7 pares en M15/H1). Se descargaron una sola vez, en modo exclusivamente de lectura, desde el terminal MT5 demo ya autenticado del usuario (`mt5.copy_rates_range`, sin credenciales nuevas, sin `order_send`). El M15 debió descargarse en bloques de ~300 días: una sola llamada que abarque más de ~1000-1200 días falla con `(-2, 'Terminal: Invalid params')` en este broker — una limitación real del terminal, no del código. Cada colección incluye un `metadata.json` con proveedor, fecha de descarga, cobertura real, duplicados y huecos encontrados.

Para reproducir: coloque `{PAR}_H1.csv` y `{PAR}_M15.csv` (columnas `time,open,high,low,close,tick_volume,spread,real_volume`) en un directorio y ejecute:

```powershell
python -m research.studies.run_studies --csv-directory <ruta> --output-directory research\reports
```

No se ejecuta en GitHub Actions (puede tardar varios minutos con varios años de M15); es un estudio manual, reproducible a demanda.

## Limitaciones conocidas

- **El spread NO se deduce del resultado económico de cada operación.** `historical_snapshot()` construye un `SymbolSnapshot` (bid = close, ask = bid + spread) que `StrategyService.validate_spread()` usa exclusivamente como filtro de admisión (rechaza la señal si `spread/ATR` excede `MAX_SPREAD_ATR_RATIO`). El precio de entrada de la señal (`entry = normalize_price(close, digits)`) es el mismo para BUY y SELL y no se ajusta a bid/ask; `resolve_trade()`/`plan_trade()` calculan `result_r` solo a partir de `entry/stop/target` contra `high/low` de las velas, sin término de spread; `simulate_trade_events()` calcula `pnl = risk_amount * result_r`, también sin deducción alguna. `spread_points`/`spread_price` solo se guardan en `BacktestTrade` como campos informativos. **Consecuencia directa:** el "estudio de sensibilidad a costes" (escenarios de 20/30/60 puntos) mide cuántas señales se admiten o se rechazan bajo cada supuesto de spread, no cuánto cambiaría la rentabilidad económica de las operaciones ya ejecutadas — por eso sus métricas de PF/expectancy salen casi idénticas entre escenarios. No es un defecto introducido por la Fase 6: es una característica preexistente del motor (`backtesting/backtesting.py`, sin modificar) que esta fase se limita a documentar con precisión.
- No se modela slippage de ejecución en ningún punto del código (ni en el backtester ni en el filtro de spread).
- Sesgo de todas las métricas de rentabilidad reportadas: al no deducirse ningún coste de transacción, el resultado real esperado en una cuenta en vivo sería igual o **peor** que el reportado aquí — nunca mejor. El veredicto de "sin ventaja estadística demostrada" de esta fase es, si acaso, conservador por esta razón, no optimista.
- La columna de spread histórico del broker demo estaba vacía para los 7 pares en el periodo estudiado; todos los escenarios usan el spread fijo de respaldo (`BACKTEST_DEFAULT_SPREAD_POINTS` o un múltiplo) solo para el filtro de admisión descrito arriba, nunca como spread real medido tick a tick aplicado al PnL.
- 4 años de historial, no "varios años" ilimitados: elección deliberada por tiempo de cómputo, no por falta de datos disponibles (el broker ofrece H1 desde ~2016-2017).
- El seguimiento virtual (SignalTracker) de producción no tiene aún señales reales suficientes para comparar contra esta simulación — no se fabricó ninguna señal para completar esa comparación.
- Ningún resultado aquí implica rentabilidad real ni garantizada; son operaciones simuladas, sin coste de comisión ni slippage, y sin deducción de spread (ver punto anterior).
