import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import MetaTrader5 as mt5
import pandas as pd

from app.services.indicator_service import IndicatorService


SYMBOL = "EURUSD"
M15_BARS = 5000
H1_BARS = 2000
LOOKAHEAD_CANDLES = 96


def generate_base_signal(lower_indicators: dict, higher_indicators: dict) -> str | None:
    close = lower_indicators.get("close")
    ema_fast = lower_indicators.get("ema_fast")
    ema_slow = lower_indicators.get("ema_slow")
    rsi = lower_indicators.get("rsi")
    macd = lower_indicators.get("macd")
    macd_signal = lower_indicators.get("macd_signal")

    htf_ema_fast = higher_indicators.get("ema_fast")
    htf_ema_slow = higher_indicators.get("ema_slow")

    required = [
        close, ema_fast, ema_slow, rsi, macd, macd_signal,
        htf_ema_fast, htf_ema_slow
    ]
    if any(v is None for v in required):
        return None

    bullish_htf = htf_ema_fast > htf_ema_slow
    bearish_htf = htf_ema_fast < htf_ema_slow

    bullish_entry = (
        bullish_htf
        and ema_fast > ema_slow
        and rsi > 55
        and macd > macd_signal
    )

    bearish_entry = (
        bearish_htf
        and ema_fast < ema_slow
        and rsi < 45
        and macd < macd_signal
    )

    if bullish_entry:
        return "BUY"
    if bearish_entry:
        return "SELL"
    return None


def run_backtest_for_params(df_m15, df_h1, sl_mult: float, tp_mult: float) -> dict:
    indicator_service = IndicatorService()

    wins = 0
    losses = 0
    no_result = 0
    trades = 0

    for i in range(200, len(df_m15) - LOOKAHEAD_CANDLES - 1):
        sample_m15 = df_m15.iloc[:i].copy()
        current_time = sample_m15.iloc[-1]["time"]

        sample_h1 = df_h1[df_h1["time"] <= current_time].copy()
        if len(sample_h1) < 50:
            continue

        indicators_m15 = indicator_service.build_indicators(sample_m15)
        indicators_h1 = indicator_service.build_indicators(sample_h1)

        signal_type = generate_base_signal(indicators_m15, indicators_h1)
        if signal_type is None:
            continue

        close = indicators_m15["close"]
        atr = indicators_m15["atr"]

        if atr is None or atr <= 0:
            continue

        trades += 1

        if signal_type == "BUY":
            entry = close
            sl = close - (atr * sl_mult)
            tp = close + (atr * tp_mult)
        else:
            entry = close
            sl = close + (atr * sl_mult)
            tp = close - (atr * tp_mult)

        future = df_m15.iloc[i:i + LOOKAHEAD_CANDLES]
        result = None

        for _, candle in future.iterrows():
            high = float(candle["high"])
            low = float(candle["low"])

            if signal_type == "BUY":
                if low <= sl and high >= tp:
                    result = "loss"
                    break
                if low <= sl:
                    result = "loss"
                    break
                if high >= tp:
                    result = "win"
                    break

            else:  # SELL
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
        elif result == "loss":
            losses += 1
        else:
            no_result += 1

    decided = wins + losses
    win_rate = (wins / decided) * 100 if decided > 0 else 0.0
    loss_rate = (losses / decided) if decided > 0 else 0.0
    win_rate_decimal = (wins / decided) if decided > 0 else 0.0

    expectancy = (win_rate_decimal * tp_mult) - (loss_rate * sl_mult)

    return {
        "sl_mult": sl_mult,
        "tp_mult": tp_mult,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "no_result": no_result,
        "win_rate": round(win_rate, 2),
        "expectancy_r": round(expectancy, 4),
    }


def main():
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

    sl_values = [0.8, 1.0, 1.2]
    tp_values = [1.5, 2.0, 2.5, 3.0]

    results = []

    for sl_mult in sl_values:
        for tp_mult in tp_values:
            result = run_backtest_for_params(df_m15, df_h1, sl_mult, tp_mult)
            results.append(result)
            print(
                f"SL={sl_mult} | TP={tp_mult} | "
                f"Trades={result['trades']} | WinRate={result['win_rate']}% | "
                f"Expectancy={result['expectancy_r']}R"
            )

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values(by="expectancy_r", ascending=False)

    print("\n=== TOP RESULTADOS ===")
    print(results_df.to_string(index=False))


if __name__ == "__main__":
    main()