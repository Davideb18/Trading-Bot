from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from freqtrade.exceptions import OperationalException
from freqtrade.plugins.pairlist.MarketCapPairList import MarketCapPairList


def provider():
    obj = MarketCapPairList.__new__(MarketCapPairList)
    obj._marketcap_cache = {}
    obj._mode = "whitelist"
    obj._categories = []
    obj._max_rank = 2
    obj._number_assets = 2
    obj._stake_currency = "USDT"
    obj._min_market_cap = 100_000_000
    obj._fallback_max_age = 3600
    obj._last_good_data = None
    obj._last_good_at = 0.0
    obj._coingecko = SimpleNamespace(get_coins_markets=Mock())
    obj._exchange = SimpleNamespace(_config={"trading_mode": "futures"})
    obj.get_markets_exchange = lambda: ["BTC/USDT:USDT", "SMALL/USDT:USDT"]
    return obj


def test_outage_uses_verified_cache_and_preserves_cap_filter():
    obj = provider()
    obj._coingecko.get_coins_markets.return_value = [
        {"symbol": "btc", "market_cap": 100_000_000},
        {"symbol": "small", "market_cap": 99_999_999},
    ]
    assert obj.filter_pairlist(obj.get_markets_exchange(), {}) == ["BTC/USDT:USDT"]
    obj._marketcap_cache.clear()
    obj._coingecko.get_coins_markets.side_effect = ConnectionError("offline")
    assert obj.filter_pairlist(obj.get_markets_exchange(), {}) == ["BTC/USDT:USDT"]


@pytest.mark.parametrize("response", [[], None])
def test_no_evidence_raises_instead_of_silent_empty_or_unverified_fallback(response):
    obj = provider()
    obj._coingecko.get_coins_markets.return_value = response
    with pytest.raises(OperationalException, match="evidence unavailable"):
        obj.filter_pairlist(obj.get_markets_exchange(), {})


def test_expired_evidence_is_not_refreshed_by_outage():
    obj = provider()
    obj._last_good_data = [{"symbol": "btc", "market_cap": 100_000_000}]
    obj._coingecko.get_coins_markets.side_effect = ConnectionError("offline")
    with pytest.raises(OperationalException):
        obj.filter_pairlist(obj.get_markets_exchange(), {})
