import MetaTrader5 as mt5
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.market_data_service import MarketDataService
from app.services.indicator_service import IndicatorService

market_service = MarketDataService()
indicator_service = IndicatorService()

df = market_service.get_market_data("EURUSD", mt5.TIMEFRAME_M15, 200)
indicators = indicator_service.build_indicators(df)

print(indicators)