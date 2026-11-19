from __future__ import annotations

from collections import defaultdict

from app.schemas.signal_schema import SignalSchema


class CorrelationService:
    """Selects the highest scoring signals sharing the same USD exposure."""

    @staticmethod
    def currency_exposure(signal: SignalSchema) -> tuple[str, str]:
        base, quote = signal.pair[:3], signal.pair[3:6]
        if signal.signal_type == "BUY":
            return base, quote
        return quote, base

    @classmethod
    def usd_exposure(cls, signal: SignalSchema) -> str | None:
        bought, sold = cls.currency_exposure(signal)
        if bought == "USD":
            return "LONG_USD"
        if sold == "USD":
            return "SHORT_USD"
        return None

    def filter(self, signals: list[SignalSchema], maximum: int) -> tuple[list[SignalSchema], list[SignalSchema]]:
        grouped: dict[str, list[SignalSchema]] = defaultdict(list)
        accepted: list[SignalSchema] = []
        for signal in signals:
            exposure = self.usd_exposure(signal)
            if exposure is None:
                accepted.append(signal)
            else:
                grouped[exposure].append(signal)
        rejected: list[SignalSchema] = []
        for candidates in grouped.values():
            candidates.sort(key=lambda item: item.score or 0, reverse=True)
            accepted.extend(candidates[:maximum])
            rejected.extend(candidates[maximum:])
        for signal in rejected:
            signal.reason = "Descartada por correlación: exposición USD equivalente"
        return accepted, rejected
