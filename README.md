# Trading Signal Bot V1

Bot de señales de trading construido con Python que analiza datos reales desde **MetaTrader 5**, calcula indicadores técnicos, genera señales con una estrategia multi-timeframe y envía alertas a **Telegram**.

## Caracteristicas

- Datos reales desde **MetaTrader 5**
- Análisis técnico con:
  - EMA rápida y lenta
  - RSI
  - MACD
  - ATR
  - ADX
- Estrategia **multi-timeframe**:
  - Setup en **M15**
  - Confirmación de tendencia en **H1**
- Stop Loss y Take Profit dinámicos con **ATR**
- Envío de señales a **Telegram**
- Filtro anti-señales duplicadas
- Backtesting histórico
- Backtesting multi-pair
- Equity curve para evaluación de estrategia

## Estructura Tecnologica

- Python
- MetaTrader5
- Pandas
- Matplotlib
- HTTPX
- Pydantic / Pydantic Settings

## Project Structure

```text
bot_trading_fastapi/
│
├── app/
│   ├── core/
│   │   ├── config.py
│   │   └── logger.py
│   │
│   ├── schemas/
│   │   └── signal_schema.py
│   │
│   ├── services/
│   │   ├── market_data_service.py
│   │   ├── indicator_service.py
│   │   ├── strategy_service.py
│   │   ├── signal_service.py
│   │   └── notifier_service.py
│   │
│   └── workers/
│       └── scanner.py
│
├── backtesting/
│   ├── backtesting.py
│   ├── optimizer.py
│   └── backtesting_mul.py
│
├── test/
│   ├── test_mt5_connection.py
│   ├── test_mt5_rates.py
│   ├── test_indicators.py
│   └── test_strategy.py
│
├── .env
├── .env.example
├── requirements.txt
├── main.py
└── README.md
```
## Vision General de la Estrategia
La estrategia actual está diseñada para buscar setups de continuación de tendencia.

- Condiciones de Entrada
  - BUY
    - Tendencia alcista en H1
    - EMA rapida > EMA lenta en M15
    - RSI fuerte
    - Otras variables y filtros
  - SELL
    - Tendencia bajista en H1
    - EMA rapida < EMA lenta en M15
    - RSI debil
    - Otras variables y filtros
##  Gestion de riesgo
- Stop Loss dinamico basado en ATR
- Take Profit Dinamico basado en ATR
## Pares Actuales

La V1 esta optimizada y probada principalmente sobre:

- EURUSD
- GBPUSD
- XAUUSD

## Alerta en Telegram

Caundo el bot detecta una señal, envia un mensaje a Telegram con:

- Par (Divisa)
- TimeFrame
- Tipo de Señal
- Punto de ingreso
- Stop Loss
- Take Profit
- Confianza
- Motivo Tecnico

## Setup
- Clone el repositorio
  ```
   git clone https://github.com/XavierTacuri/Traiding-Signal-Bot.git 
  ```
- Instale Dependencias
  ```
  pip install -r requirements.txt
  ```
- Variables .env
  - Crea un archivo .env usando .env.example como base.
- MetaTrader 5 

  - Debes tener Metatrader 5 instalado
  - MT5 debe estar abierto y logueado en tu cuenta
  - Los símbolos deben estar disponibles en Market Watch
  - Python y MT5 deben ejecutarse en la misma máquina
- Run the Bot
  ```
  python main.py
  ```
## Descargo de Responsabilidad
Este proyecto tiene fines exclusivamente educativos y de investigación.
No constituye asesoramiento financiero y no debe utilizarse como única base para tomar decisiones de trading con dinero real.
## Autor
Oscar Xavier Tacuri    
Estudiante de Ingenieria de Software  
Construido como parte de un proyecto de portafolio de backend y sistemas de trading.