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


def test_configure_windows_asyncio_forces_threaded_resolver(monkeypatch):
    import aiohttp.connector
    import aiohttp.resolver
    import freqtrade.system as ft_system

    called = []
    monkeypatch.setattr(engine.sys, "platform", "win32")
    monkeypatch.setattr(ft_system, "asyncio_setup", lambda: called.append("asyncio_setup"))

    class DummyAsyncResolver:
        pass

    monkeypatch.setattr(aiohttp.resolver, "DefaultResolver", DummyAsyncResolver)
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", DummyAsyncResolver)
    engine._configure_windows_asyncio()
    assert called == ["asyncio_setup"]
    assert aiohttp.resolver.DefaultResolver is aiohttp.resolver.ThreadedResolver
    assert aiohttp.connector.DefaultResolver is aiohttp.resolver.ThreadedResolver


def test_all_benchmark_scripts_use_explicit_utf8_text_io():
    import ast
    from pathlib import Path

    scripts = [
        "scripts/run_full_benchmark_auto.py",
        "scripts/prop_backtest_1to1.py",
        "scripts/prop_detail_array_cache.py",
        "scripts/prop_phase_checkpoint.py",
        "scripts/prop_callback_snapshot.py",
        "scripts/prop_shock_bounds.py",
        "scripts/prop_prepare_benchmark_data.py",
        "scripts/prop_archive_data.py",
        "scripts/prop_marketcap_archive.py",
    ]
    missing = []
    for rel in scripts:
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("read_text", "write_text")
            ):
                if not any(kw.arg == "encoding" for kw in node.keywords):
                    missing.append(f"{rel}:{node.lineno}: {node.func.attr}()")
    assert missing == []


def test_discover_callback_columns_survives_windows_cp1252_default(monkeypatch):
    from pathlib import Path

    orig_read_text = Path.read_text

    def cp1252_default(self, encoding=None, errors=None, newline=None):
        return orig_read_text(self, encoding=encoding or "cp1252", errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", cp1252_default)
    monkeypatch.setattr(engine, "_CALLBACK_COLS_CACHE", None)
    cols = engine._discover_callback_columns()
    assert "close" in cols and "enter_long" in cols


def test_replace_atomic_retries_windows_permission_error(tmp_path, monkeypatch):
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    attempts = [0]
    orig_replace = type(src).replace

    def flaky_replace(self, target):
        attempts[0] += 1
        if attempts[0] < 3:
            raise PermissionError("Windows Defender transient lock")
        return orig_replace(self, target)

    monkeypatch.setattr(type(src), "replace", flaky_replace)
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    engine._replace_atomic(src, dst)
    assert attempts[0] == 3
    assert dst.read_text(encoding="utf-8") == "{}"
