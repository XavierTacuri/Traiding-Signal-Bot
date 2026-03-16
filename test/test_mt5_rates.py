import MetaTrader5 as mt5
import pandas as pd

if not mt5.initialize():
    print("Error al conectar")
    print(mt5.last_error())
    quit()

pair = "EURUSD"
timeframe = mt5.TIMEFRAME_M15

rates = mt5.copy_rates_from_pos(pair, timeframe,0,10)
if rates is None:
    print(f"No se pudo obtener la velas {pair}")
    print(mt5.last_error())
else:
    df = pd.DataFrame(rates)
    print(df[["time","open","high","low","close","tick_volume"]])

mt5.shutdown()