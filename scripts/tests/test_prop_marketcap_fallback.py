from types import SimpleNamespace
from unittest.mock import Mock, patch

import json

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


def test_disk_cache_survives_restart_and_preserves_incoming_volume_order(tmp_path):
    cache_file = tmp_path / "mc_cache.json"
    obj = provider()
    obj._max_rank = 10
    obj._number_assets = 10
    obj._preserve_order = True
    obj._cache_file = cache_file
    obj.get_markets_exchange = lambda: ["SOL/USDT:USDT", "BTC/USDT:USDT", "SMALL/USDT:USDT"]
    obj._coingecko.get_coins_markets.return_value = [
        {"symbol": "btc", "market_cap": 900_000_000_000},
        {"symbol": "sol", "market_cap": 200_000_000},
        {"symbol": "small", "market_cap": 50_000_000},
    ]
    assert obj.filter_pairlist(["SOL/USDT:USDT", "BTC/USDT:USDT", "SMALL/USDT:USDT"], {}) == [
        "SOL/USDT:USDT",
        "BTC/USDT:USDT",
    ]
    assert cache_file.is_file()

    # Simulate cold restart while CoinGecko is down
    restarted = provider()
    restarted._max_rank = 10
    restarted._number_assets = 10
    restarted._preserve_order = True
    restarted._cache_file = cache_file
    restarted.get_markets_exchange = obj.get_markets_exchange
    restarted._coingecko.get_coins_markets.side_effect = ConnectionError("offline")
    assert restarted.filter_pairlist(["SOL/USDT:USDT", "BTC/USDT:USDT", "SMALL/USDT:USDT"], {}) == [
        "SOL/USDT:USDT",
        "BTC/USDT:USDT",
    ]


def test_disk_cache_age_survives_short_system_uptime(tmp_path):
    obj = provider()
    obj._cache_file = tmp_path / "cache.json"
    obj._cache_file.write_text(json.dumps({"saved_epoch": 6500, "data": [
        {"symbol": "btc", "market_cap": 100_000_000}]}))
    obj._coingecko.get_coins_markets.side_effect = ConnectionError("offline")
    with patch("time.time", return_value=10000), patch("time.monotonic", return_value=100):
        assert obj.filter_pairlist(obj.get_markets_exchange(), {}) == ["BTC/USDT:USDT"]
    with patch("time.time", return_value=10101), patch("time.monotonic", return_value=201):
        with pytest.raises(OperationalException):
            obj.filter_pairlist(obj.get_markets_exchange(), {})


@pytest.mark.parametrize("data,epoch", [([], 9900), ([{}], 9900),
    ([{"symbol": "btc", "market_cap": float("inf")}], 9900),
    ([{"symbol": "btc", "market_cap": 100_000_000}], 10001)])
def test_invalid_disk_evidence_fails_closed(tmp_path, data, epoch):
    obj = provider()
    obj._cache_file = tmp_path / "cache.json"
    obj._cache_file.write_text(json.dumps({"saved_epoch": epoch, "data": data}))
    obj._coingecko.get_coins_markets.side_effect = ConnectionError("offline")
    with patch("time.time", return_value=10000), pytest.raises(OperationalException):
        obj.filter_pairlist(obj.get_markets_exchange(), {})
