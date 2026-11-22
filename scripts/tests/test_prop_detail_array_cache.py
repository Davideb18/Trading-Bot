from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.prop_backtest_1to1 import PropParityBacktesting, history
from scripts.prop_detail_array_cache import DetailArrayCache


def test_exact_read_only_arrays_and_unpublished_entry(tmp_path):
    cache = DetailArrayCache(tmp_path, "revision")
    entry = (np.array([1, 61], dtype=np.int64),
             np.array([[1.25, 2., 1., 1.5], [1.5, 1.5, 1.5, 1.5]], dtype=np.float64))
    assert cache.load(["pair"]) is None
    restored = cache.save(["pair"], entry)
    for original, actual in zip(entry, restored):
        np.testing.assert_array_equal(original, actual)
        assert original.dtype == actual.dtype
        assert isinstance(actual, np.memmap)
        assert not actual.flags.writeable
    cache.paths(["pair"])[2].unlink()
    assert cache.load(["pair"]) is None


def test_eviction_reloads_arrays_without_history_conversion_and_source_change_invalidates(tmp_path, monkeypatch):
    source = tmp_path / "source.feather"
    source.write_bytes(b"fixture-v1")
    calls = []
    frame = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=2, freq="min", tz="UTC"),
                          "open": [1., 2.], "high": [2., 3.], "low": [0.5, 1.5], "close": [1.5, 2.5]})

    def history_load(**kwargs):
        calls.append(kwargs)
        return frame

    monkeypatch.setattr(history, "load_pair_history", history_load)
    bt = object.__new__(PropParityBacktesting)
    bt._detail_cache = OrderedDict()
    bt._detail_max_cached = 1
    bt._detail_array_cache = DetailArrayCache(tmp_path / "cache", "revision")
    bt._detail_handler = SimpleNamespace(_pair_data_filename=lambda *args: source)
    bt._detail_candle_type = "futures"
    bt.timeframe_detail = "1m"
    bt.config = {"datadir": tmp_path}
    bt.timerange = SimpleNamespace(startts=1, stopts=2, starttype="date", stoptype="date")
    original = bt._get_detail_entry("BTC/USDT:USDT")
    bt._detail_cache.clear()
    restored = bt._get_detail_entry("BTC/USDT:USDT")
    assert len(calls) == 1
    for left, right in zip(original, restored):
        np.testing.assert_array_equal(left, right)
    source.write_bytes(b"fixture-v2-changed")
    bt._detail_cache.clear()
    bt._get_detail_entry("BTC/USDT:USDT")
    assert len(calls) == 2
    assert calls[0]["fill_up_missing"] is True
