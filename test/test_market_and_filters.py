from types import SimpleNamespace

from app.schemas.signal_schema import SignalSchema
from app.services.correlation_service import CorrelationService
from app.services.market_data_service import MarketDataService
from app.services.state_service import InMemorySignalKeyRepository


class FakeMT5:
    def symbol_info(self, name):
        return None
    def symbols_get(self):
        return [SimpleNamespace(name="EURUSDm"), SimpleNamespace(name="pro.GBPUSD")]

def test_broker_suffix_and_prefix_resolution_and_asset_rejection():
    market = MarketDataService(FakeMT5(), initialize=False)
    assert market.resolve_symbol("EURUSD") == "EURUSDm"
    assert market.resolve_symbol("GBPUSD") == "pro.GBPUSD"
    try:
        market.resolve_symbol("XAUUSD")
        assert False
    except ValueError:
        pass

def signal(pair, direction, score):
    return SignalSchema(pair=pair, signal_type=direction, score=score)

def test_correlation_keeps_best_equivalent_usd_exposure_not_opposite():
    service = CorrelationService()
    accepted, rejected = service.filter([signal("EURUSD", "BUY", 80),
        signal("GBPUSD", "BUY", 90), signal("USDJPY", "BUY", 85)], 1)
    assert {item.pair for item in accepted} == {"GBPUSD", "USDJPY"}
    assert rejected[0].pair == "EURUSD"

def test_signal_key_is_marked_explicitly_only_after_send():
    repository = InMemorySignalKeyRepository()
    assert not repository.contains("key")
    repository.mark_sent("key", __import__("datetime").datetime.now())
    assert repository.contains("key")
