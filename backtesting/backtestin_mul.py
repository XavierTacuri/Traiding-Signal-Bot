import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import MetaTrader5 as mt5
import pandas as pd
import matplotlib.pyplot as plt

from app.services.indicator_service import IndicatorService
from app.services.strategy_service import StrategyService


SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]

M15_BARS = 5000
H1_BARS = 2000
LOOKAHEAD_CANDLES = 96

INITIAL_CAPITAL = 100
RISK_PER_TRADE = 0.005


def load_symbol_data(symbol: str):
    rates_m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, M15_BARS)
    rates_h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, H1_BARS)

    if rates_m15 is None or len(rates_m15) == 0:
        raise RuntimeError(f"No se pudieron obtener datos M15 para {symbol}")

    if rates_h1 is None or len(rates_h1) == 0:
        raise RuntimeError(f"No se pudieron obtener datos H1 para {symbol}")

    df_m15 = pd.DataFrame(rates_m15)
    df_h1 = pd.DataFrame(rates_h1)

    df_m15["time"] = pd.to_datetime(df_m15["time"], unit="s")
    df_h1["time"] = pd.to_datetime(df_h1["time"], unit="s")

    return df_m15, df_h1


def run_symbol_backtest(symbol: str, indicator_service, strategy_service):
    df_m15, df_h1 = load_symbol_data(symbol)

    trades_log = []

    for i in range(200, len(df_m15) - LOOKAHEAD_CANDLES - 1):
        sample_m15 = df_m15.iloc[:i].copy()
        current_time = sample_m15.iloc[-1]["time"]

        sample_h1 = df_h1[df_h1["time"] <= current_time].copy()
        if len(sample_h1) < 50:
            continue

        indicators_m15 = indicator_service.build_indicators(sample_m15)
        indicators_h1 = indicator_service.build_indicators(sample_h1)

        signal = strategy_service.generate_signal(
            pair=symbol,
            lower_tf="15m",
            higher_tf="1h",
            lower_indicators=indicators_m15,
            higher_indicators=indicators_h1,
        )

        if signal.signal_type not in ["BUY", "SELL"]:
            continue

        entry = float(signal.entry)
        tp = float(signal.take_profit)
        sl = float(signal.stop_loss)

        future = df_m15.iloc[i:i + LOOKAHEAD_CANDLES]
        result = None

        for _, candle in future.iterrows():
            high = float(candle["high"])
            low = float(candle["low"])

            if signal.signal_type == "BUY":
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
                if high >= sl and low <= tp:
                    result = "loss"
                    break
                if high >= sl:
                    result = "loss"
                    break
                if low <= tp:
                    result = "win"
                    break

        if result is None:
            continue

        rr = abs(tp - entry) / abs(entry - sl)

        trades_log.append({
            "symbol": symbol,
            "time": current_time,
            "signal_type": signal.signal_type,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "result": result,
        })

    return trades_log


def build_equity_curve(trades_log):
    capital = INITIAL_CAPITAL
    equity_curve = []
    wins = 0
    losses = 0

    for trade in trades_log:
        risk_amount = capital * RISK_PER_TRADE

        if trade["result"] == "win":
            reward = risk_amount * trade["rr"]
            capital += reward
            wins += 1
        else:
            capital -= risk_amount
            losses += 1

        equity_curve.append(capital)

    return capital, equity_curve, wins, losses


def main():
    indicator_service = IndicatorService()
    strategy_service = StrategyService()

    if not mt5.initialize():
        raise RuntimeError(f"Error conectando MT5: {mt5.last_error()}")

    all_trades = []

    for symbol in SYMBOLS:
        print(f"Procesando {symbol}...")
        try:
            symbol_trades = run_symbol_backtest(symbol, indicator_service, strategy_service)
            all_trades.extend(symbol_trades)
            print(f"  Trades cerrados: {len(symbol_trades)}")
        except Exception as e:
            print(f"  Error en {symbol}: {e}")

    mt5.shutdown()

    if not all_trades:
        print("No hubo trades en ningún símbolo.")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df = trades_df.sort_values(by="time").reset_index(drop=True)

    final_capital, equity_curve, wins, losses = build_equity_curve(trades_df.to_dict("records"))

    total_trades = wins + losses
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0

    print("\n=== RESULTADO GLOBAL MULTI-PAIR ===")
    print(f"Símbolos: {', '.join(SYMBOLS)}")
    print(f"Trades totales: {total_trades}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Win rate: {win_rate:.2f}%")
    print(f"Capital inicial: {INITIAL_CAPITAL:.2f}")
    print(f"Capital final: {final_capital:.2f}")

    print("\n=== RESULTADOS POR SÍMBOLO ===")
    summary = trades_df.groupby(["symbol", "result"]).size().unstack(fill_value=0)

    for symbol in summary.index:
        symbol_wins = summary.loc[symbol].get("win", 0)
        symbol_losses = summary.loc[symbol].get("loss", 0)
        symbol_total = symbol_wins + symbol_losses
        symbol_wr = (symbol_wins / symbol_total) * 100 if symbol_total > 0 else 0

        print(
            f"{symbol} | Trades={symbol_total} | Wins={symbol_wins} | "
            f"Losses={symbol_losses} | WinRate={symbol_wr:.2f}%"
        )

    plt.figure(figsize=(12, 6))
    plt.plot(equity_curve)
    plt.title("Equity Curve Multi-Pair")
    plt.xlabel("Trades")
    plt.ylabel("Capital")
    plt.grid(True)
    plt.show()


if __name__ == "__main__":
    main()