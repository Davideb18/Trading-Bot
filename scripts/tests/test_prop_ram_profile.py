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


def test_detail_eviction_avoids_hot_loop_gc_collect(monkeypatch):
    frame = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=2, freq="min", tz="UTC"),
                          "open": [1.0, 2.0], "high": [2.0, 3.0],
                          "low": [0.5, 1.5], "close": [1.5, 2.5]})
    monkeypatch.setattr(engine.history, "load_pair_history", lambda **kwargs: frame)
    monkeypatch.setattr(engine, "_ORIG_LOAD_PAIR_HISTORY", lambda **kwargs: frame)
    gc_calls = []
    monkeypatch.setattr(engine.gc, "collect", lambda: gc_calls.append(1))
    bt = object.__new__(engine.PropParityBacktesting)
    bt._detail_cache = OrderedDict()
    bt._detail_max_cached = 1
    bt._detail_handler = None
    bt._detail_candle_type = "futures"
    bt.timeframe_detail = "1m"
    bt.config = {"datadir": "."}
    bt.timerange = SimpleNamespace()
    bt._get_detail_entry("A")
    bt._get_detail_entry("B")
    bt._get_detail_entry("C")
    assert len(bt._detail_cache) == 1
    assert gc_calls == []


def test_fast_analyzed_table_demote_and_reload_parity(tmp_path):
    df = pd.DataFrame({
        "close": [10.0, 11.0, 12.0],
        "RSI_14": [40.0, 50.0, 60.0],
        "EMA_20": [9.5, 10.5, 11.5],
    })
    feather_path = tmp_path / "an_BTC_USDT_USDT.feather"
    df.to_feather(feather_path)
    promoted = []
    tbl = engine._FastAnalyzedTable.load_feather(
        feather_path,
        needed_cols={"close", "RSI_14", "EMA_20"},
        on_full_load=lambda t: promoted.append(t),
    )
    assert not tbl._full_loaded
    assert set(tbl.cols.keys()) == {"close"}
    frame = engine._FastAnalyzedFrame(tbl, 3)
    assert frame.iloc[-1]["close"] == 12.0
    assert not tbl._full_loaded
    assert frame.iloc[-1]["RSI_14"] == 60.0
    assert tbl._full_loaded
    assert len(promoted) == 1
    tbl.demote_to_tier1()
    assert not tbl._full_loaded
    assert set(tbl.cols.keys()) == {"close"}
    assert tbl._row_a is None and tbl._row_b is None
    assert frame.iloc[-2]["EMA_20"] == 10.5
    assert tbl._full_loaded
    assert len(promoted) == 2
    tbl.release()
    assert tbl.cols == {}
    assert tbl._row_a is None and tbl._row_b is None
