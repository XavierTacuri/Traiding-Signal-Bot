from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import MetaTrader5 as mt5
import pandas as pd

from app.core.config import FOREX_PAIRS

TIMEFRAMES = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
TIMEFRAME_DELTAS = {"M15": timedelta(minutes=15), "H1": timedelta(hours=1)}

@dataclass(frozen=True, slots=True)
class SymbolSnapshot:
    logical_pair: str
    broker_symbol: str
    bid: Decimal
    ask: Decimal
    point: Decimal
    digits: int

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

def normalize_price(value: float | Decimal, digits: int) -> Decimal:
    quantum = Decimal(1).scaleb(-digits)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)

class MarketDataService:
    def __init__(self, mt5_module: Any = mt5, initialize: bool = True) -> None:
        self.mt5 = mt5_module
        if initialize and not self.mt5.initialize():
            raise RuntimeError(f"Error al conectar MT5: {self.mt5.last_error()}")

    @staticmethod
    def validate_pair(pair: str) -> str:
        logical = pair.strip().upper()
        if logical not in FOREX_PAIRS:
            raise ValueError(f"Instrumento no autorizado: {pair}")
        return logical

    def resolve_symbol(self, pair: str) -> str:
        logical = self.validate_pair(pair)
        exact = self.mt5.symbol_info(logical)
        if exact is not None:
            return logical
        symbols = self.mt5.symbols_get() or ()
        matches = []
        for info in symbols:
            name = info.name.upper()
            position = name.find(logical)
            if position >= 0:
                extra = len(name) - len(logical)
                matches.append((extra, position, info.name))
        if not matches:
            raise RuntimeError(f"No se encontró el símbolo MT5 para {logical}")
        matches.sort()
        best = matches[0]
        if len(matches) > 1 and matches[1][:2] == best[:2]:
            raise RuntimeError(f"Resolución ambigua del símbolo MT5 para {logical}")
        return best[2]

    def get_closed_bars(self, pair: str, timeframe: str, count: int) -> pd.DataFrame:
        broker_symbol = self.resolve_symbol(pair)
        try:
            mt5_timeframe = TIMEFRAMES[timeframe.upper()]
        except KeyError as exc:
            raise ValueError(f"Timeframe no soportado: {timeframe}") from exc
        # start_pos=1 excludes the still-forming bar at position zero.
        rates = self.mt5.copy_rates_from_pos(broker_symbol, mt5_timeframe, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No se obtuvieron velas cerradas para {broker_symbol} {timeframe}")
        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frame.attrs["broker_symbol"] = broker_symbol
        return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)

    def get_historical_bars(
        self,
        pair: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        now: datetime | None = None,
    ) -> pd.DataFrame:
        """Download a UTC range and retain only candles closed by ``now``.

        MT5 candle timestamps identify candle opens. A historical final candle is
        removed only when its calculated close is later than the current instant.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Las fechas históricas deben incluir zona horaria")
        if start >= end:
            raise ValueError("El inicio histórico debe ser anterior al final")
        name = timeframe.upper()
        if name not in TIMEFRAMES:
            raise ValueError(f"Timeframe no soportado: {timeframe}")
        broker_symbol = self.resolve_symbol(pair)
        rates = self.mt5.copy_rates_range(
            broker_symbol,
            TIMEFRAMES[name],
            start.astimezone(UTC),
            end.astimezone(UTC),
        )
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No se obtuvieron velas históricas para {broker_symbol} {name}")
        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        numeric = ["open", "high", "low", "close"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        frame = frame.dropna(subset=["time", *numeric]).sort_values("time").drop_duplicates("time")
        cutoff = (now or datetime.now(UTC)).astimezone(UTC)
        closed = frame[frame["time"] + TIMEFRAME_DELTAS[name] <= cutoff].reset_index(drop=True)
        if closed.empty:
            raise RuntimeError(f"No hay velas cerradas para {broker_symbol} {name}")
        closed.attrs["broker_symbol"] = broker_symbol
        return closed

    def get_symbol_snapshot(self, pair: str) -> SymbolSnapshot:
        logical = self.validate_pair(pair)
        broker_symbol = self.resolve_symbol(logical)
        info = self.mt5.symbol_info(broker_symbol)
        tick = self.mt5.symbol_info_tick(broker_symbol)
        if info is None or tick is None:
            raise RuntimeError(f"Faltan especificaciones o tick para {broker_symbol}")
        digits = int(info.digits)
        point = Decimal(str(info.point))
        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        if point <= 0 or bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError(f"Datos bid/ask inválidos para {broker_symbol}")
        return SymbolSnapshot(logical, broker_symbol, bid, ask, point, digits)

    def get_market_data(self, pair: str, timeframe: int, count: int = 200) -> pd.DataFrame:
        """Compatibility wrapper; still excludes the live candle."""
        name = next((key for key, value in TIMEFRAMES.items() if value == timeframe), None)
        if name is None:
            raise ValueError(f"Timeframe MT5 no soportado: {timeframe}")
        return self.get_closed_bars(pair, name, count)
