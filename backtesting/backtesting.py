import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import MetaTrader5 as mt5
import pandas as pd
import matplotlib.pyplot as plt

from app.services.indicator_service import IndicatorService
from app.services.strategy_service import StrategyService


SYMBOL = "EURUSD"
M15_BARS = 5000
H1_BARS = 2000
LOOKAHEAD_CANDLES = 96


def main():
    indicator_service = IndicatorService()
    strategy_service = StrategyService()

    if not mt5.initialize():
        raise RuntimeError(f"Error conectando MT5: {mt5.last_error()}")

    rates_m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, M15_BARS)
    rates_h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, H1_BARS)

    mt5.shutdown()

    if rates_m15 is None or len(rates_m15) == 0:
        raise RuntimeError("No se pudieron obtener datos M15")

    if rates_h1 is None or len(rates_h1) == 0:
        raise RuntimeError("No se pudieron obtener datos H1")

    df_m15 = pd.DataFrame(rates_m15)
    df_h1 = pd.DataFrame(rates_h1)

    df_m15["time"] = pd.to_datetime(df_m15["time"], unit="s")
    df_h1["time"] = pd.to_datetime(df_h1["time"], unit="s")

    wins = 0
    losses = 0
    no_result = 0
    trades = 0
    capital = 10000
    risk_per_trade = 0.01
    equity_curve = []

    # Empezamos con suficientes velas para indicadores
    for i in range(200, len(df_m15) - LOOKAHEAD_CANDLES - 1):
        sample_m15 = df_m15.iloc[:i].copy()
        current_time = sample_m15.iloc[-1]["time"]

        # H1 solo hasta el tiempo actual del backtest
        sample_h1 = df_h1[df_h1["time"] <= current_time].copy()

        if len(sample_h1) < 50:
            continue

        indicators_m15 = indicator_service.build_indicators(sample_m15)
        indicators_h1 = indicator_service.build_indicators(sample_h1)

        signal = strategy_service.generate_signal(
            pair=SYMBOL,
            lower_tf="15m",
            higher_tf="1h",
            lower_indicators=indicators_m15,
            higher_indicators=indicators_h1,
        )

        if signal.signal_type not in ["BUY", "SELL"]:
            continue

        trades += 1

        entry = float(signal.entry)
        tp = float(signal.take_profit)
        sl = float(signal.stop_loss)

        # Empezamos desde la vela siguiente a la señal
        future = df_m15.iloc[i:i + LOOKAHEAD_CANDLES]

        result = None

        for _, candle in future.iterrows():
            high = float(candle["high"])
            low = float(candle["low"])

            if signal.signal_type == "BUY":
                # Caso ambiguo: si toca TP y SL en la misma vela, asumimos peor caso
                if low <= sl and high >= tp:
                    result = "loss"
                    break
                if low <= sl:
                    result = "loss"
                    break
                if high >= tp:
                    result = "win"
                    break

            elif signal.signal_type == "SELL":
                # Caso ambiguo: peor caso
                if high >= sl and low <= tp:
                    result = "loss"
                    break
                if high >= sl:
                    result = "loss"
                    break
                if low <= tp:
                    result = "win"
                    break

        if result == "win":
            wins += 1
            risk = capital * risk_per_trade
            reward = risk * (abs(tp - entry)/abs(entry-sl))
            capital += reward
        elif result == "loss":
            losses += 1
            risk = capital * risk_per_trade
            capital -= risk
        else:
            no_result += 1

        equity_curve.append(capital)

    print(f"Trades: {trades}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"No result: {no_result}")

    decided_trades = wins + losses
    if decided_trades > 0:
        win_rate = (wins / decided_trades) * 100
        print(f"Win rate: {win_rate:.2f}%")
    else:
        print("Win rate: 0.00%")

    plt.figure(figsize=(12, 6))
    plt.plot(equity_curve)
    plt.title("Equity Curve")
    plt.xlabel("Trades")
    plt.ylabel("Capital")
    plt.grid(True)
    plt.show()

if __name__ == "__main__":
    main()