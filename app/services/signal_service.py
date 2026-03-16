import  MetaTrader5 as mt5
from app.services.strategy_service import StrategyService
from app.services.indicator_service import IndicatorService
from app.services.market_data_service import MarketDataService

class SignalService:
    def __init__(self):
        self.market_data_service = MarketDataService()
        self.indicator_service = IndicatorService()
        self.strategy_service = StrategyService()

    async def build_signal(self, pair:str):
        #Velas de 15M y 1H
        df_m15 = self.market_data_service.get_market_data(pair,mt5.TIMEFRAME_M15,200)
        df_h1 = self.market_data_service.get_market_data(pair,mt5.TIMEFRAME_H1,200)
        #Indicadores 15M y 1H
        indicator_m15 = self.indicator_service.build_indicators(df_m15)
        indicator_h1 = self.indicator_service.build_indicators(df_h1)

        signal = self.strategy_service.generate_signal(pair = pair,
                                                       lower_tf="15m",
                                                       higher_tf="1h",
                                                       lower_indicators=indicator_m15,
                                                       higher_indicators=indicator_h1)

        return signal
