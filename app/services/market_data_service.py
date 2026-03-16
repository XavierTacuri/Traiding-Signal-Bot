import MetaTrader5 as mt5
import pandas as pd

class MarketDataService:
    def __init__(self):
        if not mt5.initialize():
            raise RuntimeError(f"Error al conectar MT5:{mt5.last_error()}")

    def get_market_data(self,pair:str,timeframe,count: int = 200):

        rates = mt5.copy_rates_from_pos(pair,timeframe,0,count)

        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No se pudo obtener los datos para :{pair}")

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")

        return df
