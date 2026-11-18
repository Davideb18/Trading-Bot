from datetime import UTC, datetime

from scripts.prop_backtest_1to1 import HistoricalPairListAdapter


def test_future_snapshot_is_never_visible():
    adapter = HistoricalPairListAdapter(["BTC/USDT:USDT"], {"3600": ["BTC/USDT:USDT"]})
    adapter.active_dynamic = True
    adapter.current_time = datetime.fromtimestamp(1800, UTC)
    adapter.refresh_pairlist()
    assert adapter.whitelist == []
    adapter.current_time = datetime.fromtimestamp(3600, UTC)
    adapter.refresh_pairlist()
    assert adapter.whitelist == ["BTC/USDT:USDT"]
