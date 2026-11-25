"""Downloader bootstrap tests; no exchange calls or historical downloads."""

import sys
from types import SimpleNamespace

import pytest

from scripts import prop_download_data as downloader


def test_download_configures_windows_runtime_before_freqtrade(monkeypatch):
    calls = []
    monkeypatch.setattr(downloader, "_configure_windows_asyncio", lambda: calls.append("runtime"))
    monkeypatch.setattr(downloader, "freqtrade_main", lambda args: calls.append(args))
    args = ["--config", "config.json", "--timeframes", "1m", "1h", "--prepend"]
    downloader.main(args)
    assert calls == ["runtime", ["download-data", *args]]


def test_download_forwards_actual_cli_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prop_download_data.py", "--help"])
    monkeypatch.setattr(downloader, "_configure_windows_asyncio", lambda: None)
    monkeypatch.setattr(downloader, "freqtrade_main", lambda args: args)
    assert downloader.main() == ["download-data", "--help"]


@pytest.mark.parametrize("timeframe,stride", [("1m", 60000), ("1h", 3600000)])
def test_skip_prelisting_requests_keeps_first_partial_candle(timeframe, stride):
    launch = 1758175736000
    exchange = SimpleNamespace(markets={"0G/USDT:USDT": {
        "swap": True, "linear": True, "info": {"launchTime": str(launch)}}})
    calls = []
    bounded = downloader.listing_aware_history(lambda self, **kw: calls.append(kw))
    bounded(exchange, "0G/USDT:USDT", timeframe, 1719792000000, "futures", until_ms=launch + 86400000)
    assert calls[0]["since_ms"] == launch // stride * stride
    assert calls[0]["until_ms"] == launch + 86400000


def test_fully_prelisting_interval_makes_no_network_call():
    exchange = SimpleNamespace(markets={"NEW": {"swap": True, "linear": True, "info": {"launchTime": "1758175736000"}}})
    bounded = downloader.listing_aware_history(lambda *a, **kw: pytest.fail("Unnecessary request"))
    result = bounded(exchange, "NEW", "1m", 1719792000000, "futures", until_ms=1750000000000)
    assert result.empty
    assert list(result.columns) == ["date", "open", "high", "low", "close", "volume"]


@pytest.mark.parametrize("launch", [None, "invalid", "0"])
def test_unknown_listing_preserves_original_request(launch):
    exchange = SimpleNamespace(markets={"PAIR": {"swap": True, "linear": True, "info": {"launchTime": launch}}})
    bounded = downloader.listing_aware_history(lambda self, **kw: kw)
    assert bounded(exchange, "PAIR", "1m", 123000, "futures")["since_ms"] == 123000


def test_existing_later_start_is_preserved_and_override_is_restored(monkeypatch):
    exchange = SimpleNamespace(markets={"PAIR": {"swap": True, "linear": True, "info": {"launchTime": "60000"}}})
    bounded = downloader.listing_aware_history(lambda self, **kw: kw)
    assert bounded(exchange, "PAIR", "1m", 123000, "futures")["since_ms"] == 123000
    original = downloader.Bybit.get_historic_ohlcv
    with pytest.raises(RuntimeError):
        with downloader.listing_aware_downloads():
            assert downloader.Bybit.get_historic_ohlcv is not original
            raise RuntimeError("interrupted")
    assert downloader.Bybit.get_historic_ohlcv is original
