from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import prop_backtest_1to1 as engine


@pytest.mark.parametrize("ram,limits", [(8, (6, 12)), (10, (6, 12)),
                         (10.5, (16, 24)), (20, (16, 24)),
                         (32, (40, 64)), (40, (40, 64)), (64, (96, 128))])
def test_physical_ram_tiers(ram, limits):
    assert engine._ram_cache_limits(ram) == limits


def test_fallback_eviction_has_no_pinned_arrays(monkeypatch):
    frame = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=2, freq="min", tz="UTC"),
                          "open": [1.01, 2.02], "high": [2.03, 3.04],
                          "low": [0.51, 1.52], "close": [1.53, 2.54]})
    calls = []

    def load(**kwargs):
        calls.append(kwargs["pair"])
        return frame

    monkeypatch.setattr(engine.history, "load_pair_history", load)
    monkeypatch.setattr(engine, "_ORIG_LOAD_PAIR_HISTORY", load)
    bt = object.__new__(engine.PropParityBacktesting)
    bt._detail_cache = OrderedDict()
    bt._detail_max_cached = 1
    bt._detail_handler = None
    bt._detail_candle_type = "futures"
    bt.timeframe_detail = "1m"
    bt.config = {"datadir": "."}
    bt.timerange = SimpleNamespace()
    original = bt._get_detail_entry("A")
    bt._get_detail_entry("B")
    restored = bt._get_detail_entry("A")
    assert calls == ["A", "B", "A"]
    assert len(bt._detail_cache) == 1
    assert not hasattr(bt, "_detail_pinned_Cache")
    for left, right in zip(original, restored):
        np.testing.assert_array_equal(left, right)


def test_arrow_release_failure_is_nonfatal(monkeypatch):
    import pyarrow

    calls = []
    monkeypatch.setattr(engine.gc, "collect", lambda: calls.append("gc"))

    def unavailable():
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(pyarrow, "default_memory_pool", unavailable)
    engine._release_unused_memory()
    assert calls == ["gc"]
