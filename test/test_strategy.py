import MetaTrader5 as mt5
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.market_data_service import MarketDataService
from app.services.indicator_service import IndicatorService
from app.services.strategy_service import StrategyService


market = MarketDataService()
indicator = IndicatorService()
strategy = StrategyService()

df = market.get_market_data("EURUSD", mt5.TIMEFRAME_M15, 200)
indicators = indicator.build_indicators(df)

signal = strategy.generate_signal("EURUSD", indicators)

print("Indicators:", indicators)
print("Signal:", signal)