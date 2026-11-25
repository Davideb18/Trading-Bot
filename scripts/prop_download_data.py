"""Run the benchmark data downloader with the engine's Windows runtime setup."""

from __future__ import annotations

import sys
import logging
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freqtrade.main import main as freqtrade_main
from freqtrade.exchange.bybit import Bybit
from freqtrade.exchange import timeframe_to_msecs
from pandas import DataFrame
from scripts.prop_backtest_1to1 import _configure_windows_asyncio


def listing_aware_history(original):
    """Avoid requests before the launch of the specific Bybit perpetual contract."""
    def bounded(self, pair, timeframe, since_ms, candle_type, is_new_pair=False, until_ms=None):
        market = self.markets.get(pair, {})
        info = market.get("info") or {}
        try:
            launch_ms = int(info.get("launchTime", 0))
        except (ValueError, TypeError):
            launch_ms = 0
        if market.get("swap") and market.get("linear") and launch_ms > 0:
            # Retain the first partial candle: the listing can occur mid-timeframe.
            stride = timeframe_to_msecs(timeframe)
            earliest = launch_ms // stride * stride
            if since_ms < earliest:
                logging.getLogger(__name__).info(
                    "%s %s: skipping pre-listing history (%d -> %d ms).",
                    pair, timeframe, since_ms, earliest,
                )
                since_ms = earliest
            if until_ms is not None and since_ms >= until_ms:
                return DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        return original(self, pair=pair, timeframe=timeframe, since_ms=since_ms,
                        candle_type=candle_type, is_new_pair=is_new_pair, until_ms=until_ms)
    return bounded


@contextmanager
def listing_aware_downloads():
    # The override is confined to this download process; live and backtest engines are unchanged.
    original = Bybit.get_historic_ohlcv
    Bybit.get_historic_ohlcv = listing_aware_history(original)
    try:
        yield
    finally:
        Bybit.get_historic_ohlcv = original


def main(argv=None):
    # CCXT creates its aiohttp connector later, so configure the OS DNS resolver first.
    _configure_windows_asyncio()
    with listing_aware_downloads():
        return freqtrade_main(["download-data", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
