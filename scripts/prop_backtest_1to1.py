"""Faithful 1:1 HyroTrader One-Step Backtest Engine for NostalgiaForInfinityX8_Prop.

Does NOT modify any production strategy, config, or Freqtrade core files.
Instead, subclasses `Backtesting` in-memory to replicate exact live behavior:
  - Dynamic point-in-time VolumePairList (top 40, >= $20M 24h Bybit USDT turnover,
    refreshed every 30m) + AgeFilter (>= 60d listed on Bybit) + pair_blacklist
  - PropRiskBudget admission caps & DCA/initial stake clipping
    (max $666/slot, max $1000 aggregate, required_headroom >= 300 bps * agg + $30)
  - PropRiskGuard intraday drawdown monitoring (-3.6% daily / -5.2% overall internal
    floors vs -4% / -6% official, checked at 00:00 UTC rollover on floating equity)
  - BTC Macro Crash Pause (-4.0% 1h / -8.0% 24h trigger; >-2.5% / >-6.0% recovery)
  - HyroTrader 40% daily consistency cap ($175.00 live pause until 00:00 UTC,
    partial-exit profit attribution; qualifying days require fully closed trades,
    combined notional >= $250 and aggregate absolute gross return >= 1%)
  - 30d inactivity breach check without artificial keep-alive trades
  - Multi-cohort rolling start simulation + execution slippage stress testing
"""

from __future__ import annotations

import argparse
import bisect
import copy
import gc
import hashlib
import json
import logging
import math
import os
import pickle
import re
import statistics
import sys
import time
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.feather as _pa_feather

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from freqtrade.configuration import Configuration
from freqtrade.data import history
from freqtrade.data.btanalysis import get_tick_size_over_time
from freqtrade.data.converter import trim_dataframe
from freqtrade.enums import CandleType, ExitType, RunMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange import price_to_precision, timeframe_to_seconds
from freqtrade.optimize.backtesting import (
    CLOSE_IDX,
    DATE_IDX,
    ELONG_IDX,
    ENTER_TAG_IDX,
    ESHORT_IDX,
    EXIT_TAG_IDX,
    HIGH_IDX,
    LONG_IDX,
    LOW_IDX,
    OPEN_IDX,
    SHORT_IDX,
    Backtesting,
)
from freqtrade.persistence import LocalTrade
from freqtrade.prop_profile import build_program_profiles, floors, profile_from_config
from freqtrade.prop_qualification import qualifying_close_day
from scripts.prop_phase_checkpoint import configure_checkpoints, phase_checkpoint
from scripts.prop_detail_array_cache import DetailArrayCache

logger = logging.getLogger("prop_backtest_1to1")

_ORIG_LOAD_PAIR_HISTORY = history.load_pair_history


def _detect_total_ram_gb() -> float:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024.0 ** 3)
    except Exception:
        pass
    return 8.0


def _install_fast_custom_data_index() -> None:
    """Replace O(N) linear scan in CustomDataWrapper.get_custom_data with O(1) dict index."""
    from freqtrade.persistence.custom_data import CustomDataWrapper

    if getattr(CustomDataWrapper, "_fast_indexed", False):
        return

    _by_trade_key: dict[tuple[int, str], list[Any]] = {}
    _by_trade: dict[int, list[Any]] = defaultdict(list)
    _seen_count: list[int] = [0]

    def _sync_index() -> None:
        data = CustomDataWrapper.custom_data
        n = len(data)
        if n < _seen_count[0]:
            _by_trade_key.clear()
            _by_trade.clear()
            _seen_count[0] = 0
        if n > _seen_count[0]:
            for item in data[_seen_count[0] : n]:
                tid = int(item.ft_trade_id)
                k = item.cd_key
                tk = (tid, k)
                if tk not in _by_trade_key:
                    _by_trade_key[tk] = [item]
                    _by_trade[tid].append(item)
                else:
                    _by_trade_key[tk][0] = item
            _seen_count[0] = n

    @staticmethod
    def _fast_get_custom_data(*, trade_id: int, key: str | None = None) -> list[Any]:
        if not CustomDataWrapper.use_db:
            _sync_index()
            if key is not None:
                return _by_trade_key.get((int(trade_id), key), [])
            return _by_trade.get(int(trade_id), [])
        if key is None:
            return CustomDataWrapper._get_custom_data(trade_id=trade_id)
        return CustomDataWrapper._get_custom_data(trade_id=trade_id, key=key)

    orig_reset = CustomDataWrapper.reset_custom_data

    @staticmethod
    def _fast_reset_custom_data() -> None:
        orig_reset()
        _by_trade_key.clear()
        _by_trade.clear()
        _seen_count[0] = 0

    CustomDataWrapper.get_custom_data = _fast_get_custom_data
    CustomDataWrapper.reset_custom_data = _fast_reset_custom_data
    CustomDataWrapper._fast_indexed = True


class _FastCandleRow:
    """Zero-copy scalar row accessor replacing pandas Series in df.iloc[-1]/df.iloc[-2]."""

    __slots__ = ("_cols", "_idx", "_cache")

    def __init__(self, cols: dict[str, np.ndarray], idx: int) -> None:
        self._cols = cols
        self._idx = idx
        self._cache: dict[str, Any] = {}

    def squeeze(self) -> "_FastCandleRow":
        return self

    def __getitem__(self, key: str) -> Any:
        val = self._cache.get(key)
        if val is None and key not in self._cache:
            val = self._cols[key][self._idx]
            self._cache[key] = val
        return val

    def get(self, key: str, default: Any = None) -> Any:
        val = self._cache.get(key)
        if val is None and key not in self._cache:
            arr = self._cols.get(key)
            if arr is None:
                return default
            val = arr[self._idx]
            self._cache[key] = val
        return val

    def __contains__(self, key: str) -> bool:
        return key in self._cols


class _FastAnalyzedIloc:
    __slots__ = ("_table", "_len")

    def __init__(self, table: "_FastAnalyzedTable", length: int) -> None:
        self._table = table
        self._len = length

    def __getitem__(self, idx: int) -> _FastCandleRow:
        real_idx = self._len + idx if idx < 0 else idx
        if real_idx < 0 or real_idx >= self._len:
            raise IndexError(idx)
        tbl = self._table
        if real_idx == tbl._row_a_idx and tbl._row_a is not None:
            return tbl._row_a
        if real_idx == tbl._row_b_idx and tbl._row_b is not None:
            return tbl._row_b
        row = _FastCandleRow(tbl.cols, real_idx)
        tbl._row_b_idx = tbl._row_a_idx
        tbl._row_b = tbl._row_a
        tbl._row_a_idx = real_idx
        tbl._row_a = row
        return row


class _FastAnalyzedFrame:
    """O(1) zero-allocation view over _FastAnalyzedTable up to length."""

    __slots__ = ("_table", "_len")

    def __init__(self, table: "_FastAnalyzedTable", length: int) -> None:
        self._table = table
        self._len = length

    def __len__(self) -> int:
        return self._len

    def __bool__(self) -> bool:
        return self._len > 0

    @property
    def empty(self) -> bool:
        return self._len == 0

    @property
    def columns(self):
        return self._table.cols.keys()

    @property
    def iloc(self) -> _FastAnalyzedIloc:
        return _FastAnalyzedIloc(self._table, self._len)


class _FastAnalyzedTable:
    """Compact column-dict representation of an analyzed feather file (loads only callback cols)."""

    __slots__ = ("cols", "n_rows", "last_date", "_row_a_idx", "_row_a", "_row_b_idx", "_row_b")
    _VALID_COLS_BY_DIR: dict[Path, list[str]] = {}

    def __init__(self, cols: dict[str, np.ndarray], n_rows: int, last_date: datetime) -> None:
        self.cols = cols
        self.n_rows = n_rows
        self.last_date = last_date
        self._row_a_idx: int = -1
        self._row_a: _FastCandleRow | None = None
        self._row_b_idx: int = -1
        self._row_b: _FastCandleRow | None = None

    @classmethod
    def load_feather(cls, path: Path, needed_cols: set[str] | None = None) -> "_FastAnalyzedTable":
        cols_to_read: list[str] | None = None
        if needed_cols:
            parent = path.parent
            cols_to_read = cls._VALID_COLS_BY_DIR.get(parent)
            if cols_to_read is None:
                import pyarrow.ipc as _pa_ipc

                with _pa_ipc.open_file(path) as reader:
                    avail = set(reader.schema.names)
                cols_to_read = sorted(needed_cols & avail)
                cls._VALID_COLS_BY_DIR[parent] = cols_to_read
        table = _pa_feather.read_table(path, columns=cols_to_read)
        n_rows = table.num_rows
        cols: dict[str, np.ndarray] = {}
        last_date = datetime.fromtimestamp(0, tz=UTC)
        for name in table.column_names:
            col = table.column(name)
            if name == "date":
                s = col.to_pandas()
                if n_rows > 0:
                    last_date = s.iloc[-1].to_pydatetime()
                cols[name] = s.to_numpy()
            else:
                try:
                    cols[name] = col.to_numpy(zero_copy_only=False)
                except Exception:
                    cols[name] = col.to_pandas().to_numpy()
        return cls(cols, n_rows, last_date)


_CALLBACK_COLS_CACHE: set[str] | None = None


def _discover_callback_columns() -> set[str]:
    """Static scan of NostalgiaForInfinityX8_Prop.py to find all columns accessed outside populate_*."""
    global _CALLBACK_COLS_CACHE
    if _CALLBACK_COLS_CACHE is not None:
        return _CALLBACK_COLS_CACHE
    strat_path = REPO / "user_data" / "strategies" / "NostalgiaForInfinityX8_Prop.py"
    src = strat_path.read_text()
    cols = set(
        re.findall(
            r'candle(?:_\d+)?(?:\[\s*|\.get\(\s*)["\']([A-Za-z0-9_.-]+)["\']',
            src,
        )
    )
    cols.update(
        {
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "enter_long",
            "enter_tag",
            "exit_long",
            "exit_tag",
            "enter_short",
            "exit_short",
        }
    )
    _CALLBACK_COLS_CACHE = cols
    return cols


def compute_hyrotrader_ledger(
    open_trades: list[LocalTrade],
    closed_trades: list[dict[str, Any]],
    initial_balance: float,
    profit_target_percent: float,
    daily_profit_cap_percent: float,
    phase_start: datetime | None,
) -> dict[str, Any]:
    """Exact port of `_hyrotrader_rules_loop` (`scripts/llm_risk_manager_prop.py`)."""
    min_qualifying_stake = initial_balance * 0.05  # $250 on $5,000
    day_profits: dict[str, float] = {}
    qualifying_days: set[str] = set()
    activity_dts: list[datetime] = []

    all_records: list[dict[str, Any]] = []
    for t in closed_trades:
        all_records.append(t)
    for t in open_trades:
        all_records.append(_localtrade_to_record(t))

    for tr in all_records:
        orders = tr["orders"]
        open_dt = tr["open_dt"]
        if phase_start and open_dt and open_dt < phase_start:
            continue

        for o in orders:
            if o["dt"] is not None and (not phase_start or o["dt"] >= phase_start):
                activity_dts.append(o["dt"])

        has_exit = any((not o["is_entry"]) and o["dt"] is not None for o in orders)
        if has_exit:
            sorted_orders = sorted(orders, key=lambda o: (o["dt"], o["idx"]))
            direction = -1.0 if tr["is_short"] else 1.0
            cur_qty = 0.0
            cur_entry_cost = 0.0
            cur_weighted_price_sum = 0.0
            exit_events = []
            fallback_open_rate = float(tr["open_rate"] or 0.0)
            for o in sorted_orders:
                if o["is_entry"]:
                    cur_qty += o["filled"]
                    cur_entry_cost += o["cost"]
                    if o["price"] > 0:
                        cur_weighted_price_sum += o["filled"] * o["price"]
                else:
                    if cur_qty <= 1e-12:
                        continue
                    avg_entry = (
                        cur_weighted_price_sum / cur_qty
                        if cur_weighted_price_sum > 0
                        else fallback_open_rate
                    )
                    close_qty = min(o["filled"], cur_qty)
                    frac = close_qty / cur_qty
                    entry_notional = cur_entry_cost * frac
                    if entry_notional <= 0 and avg_entry > 0:
                        entry_notional = close_qty * avg_entry
                    exit_price = (
                        o["price"]
                        if o["price"] > 0
                        else (o["cost"] / close_qty if close_qty > 0 else 0.0)
                    )
                    exit_notional = o["cost"] if o["cost"] > 0 else close_qty * exit_price
                    gross_pnl = (
                        direction * close_qty * (exit_price - avg_entry)
                        if avg_entry > 0 and exit_price > 0
                        else 0.0
                    )
                    gross_ratio = (gross_pnl / entry_notional) if entry_notional > 0 else 0.0
                    cur_qty = max(0.0, cur_qty - close_qty)
                    cur_entry_cost = max(0.0, cur_entry_cost - entry_notional)
                    if avg_entry > 0:
                        cur_weighted_price_sum = max(
                            0.0, cur_weighted_price_sum - close_qty * avg_entry
                        )
                    exit_dt = o["dt"]
                    if exit_dt is not None and (not phase_start or exit_dt >= phase_start):
                        exit_events.append(
                            {
                                "day": exit_dt.strftime("%Y-%m-%d"),
                                "entry_notional": entry_notional,
                                "weight": max(entry_notional + exit_notional, 1e-9),
                                "gross_pnl": gross_pnl,
                                "gross_ratio": gross_ratio,
                            }
                        )

            if not exit_events:
                continue
            sum_gross = sum(ev["gross_pnl"] for ev in exit_events)
            if tr["is_open"]:
                raw_realized = tr["realized_profit"]
            else:
                raw_realized = tr["profit_abs"] if tr["profit_abs"] is not None else tr["realized_profit"]
            trade_realized = float(raw_realized) if raw_realized is not None else sum_gross
            residual = trade_realized - sum_gross
            total_weight = sum(ev["weight"] for ev in exit_events)
            for ev in exit_events:
                ev_net = ev["gross_pnl"] + (
                    residual * (ev["weight"] / total_weight) if total_weight > 0 else 0.0
                )
                day_profits[ev["day"]] = day_profits.get(ev["day"], 0.0) + ev_net
            qualifying_day = qualifying_close_day(
                is_open=tr["is_open"], close_dt=tr["close_dt"], remaining_qty=cur_qty,
                exit_events=exit_events, initial_balance=initial_balance,
            )
            if qualifying_day:
                qualifying_days.add(qualifying_day)
        elif not tr["is_open"]:
            close_dt = tr["close_dt"]
            if close_dt is None or (phase_start and close_dt < phase_start):
                continue
            tr_day = close_dt.strftime("%Y-%m-%d")
            pnl = float(tr["profit_abs"] or 0.0)
            day_profits[tr_day] = day_profits.get(tr_day, 0.0) + pnl
            cost = float(tr["cost"] or tr["max_stake_amount"] or tr["stake_amount"] or 0.0)
            # Missing fills cannot prove pre-fee PnL or aggregate traded notional.

    target_profit = initial_balance * profit_target_percent
    daily_cap = target_profit * daily_profit_cap_percent
    if daily_profit_cap_percent >= 1.0:
        counted_profit = sum(day_profits.values())
        daily_pause_threshold = float("inf")
    else:
        counted_profit = sum(min(v, daily_cap) if v > 0 else v for v in day_profits.values())
        daily_pause_threshold = (
            min(daily_cap * 0.875, daily_cap - 25.0) if daily_cap > 30.0 else daily_cap * 0.85
        )
    return {
        "day_profits": day_profits,
        "qualifying_days": sorted(qualifying_days),
        "counted_profit": counted_profit,
        "target_profit": target_profit,
        "daily_cap": daily_cap,
        "daily_pause_threshold": daily_pause_threshold,
        "latest_activity_dt": max(activity_dts) if activity_dts else phase_start,
    }


def _to_utc(dt_val: Any) -> datetime | None:
    if dt_val is None:
        return None
    if isinstance(dt_val, pd.Timestamp):
        dt_val = dt_val.to_pydatetime()
    if dt_val.tzinfo is None:
        return dt_val.replace(tzinfo=UTC)
    return dt_val.astimezone(UTC)


def _localtrade_to_record(tr: LocalTrade) -> dict[str, Any]:
    orders_out = []
    for idx, o in enumerate(tr.orders):
        filled = float(getattr(o, "filled", 0.0) or 0.0)
        if filled <= 0:
            continue
        dt = _to_utc(
            getattr(o, "order_filled_date", None)
            or getattr(o, "order_update_date", None)
            or getattr(o, "order_date", None)
        )
        price = float(
            getattr(o, "average", None)
            or getattr(o, "safe_price", None)
            or getattr(o, "price", None)
            or 0.0
        )
        cost = float(getattr(o, "cost", 0.0) or 0.0)
        if cost <= 0 and price > 0:
            cost = filled * price
        is_entry = getattr(o, "ft_order_side", None) == tr.entry_side
        orders_out.append(
            {
                "idx": idx,
                "dt": dt,
                "is_entry": bool(is_entry),
                "filled": filled,
                "price": price,
                "cost": cost,
            }
        )
    return {
        "trade_id": tr.id,
        "pair": tr.pair,
        "is_open": bool(tr.is_open),
        "is_short": bool(tr.is_short),
        "open_rate": float(tr.open_rate or 0.0),
        "open_dt": _to_utc(tr.open_date_utc or tr.open_date),
        "close_dt": _to_utc(tr.close_date_utc or tr.close_date),
        "realized_profit": float(tr.realized_profit or 0.0),
        "profit_abs": float(tr.close_profit_abs) if tr.close_profit_abs is not None else None,
        "profit_ratio": float(tr.close_profit) if tr.close_profit is not None else None,
        "max_stake_amount": float(tr.max_stake_amount or 0.0),
        "stake_amount": float(tr.stake_amount or 0.0),
        "cost": float(tr.open_trade_value or tr.stake_amount or 0.0),
        "orders": orders_out,
    }


class CompactOhlcvArray(Sequence):
    """Memory-compact numpy-backed sequence (~52B/row vs ~304B/row) preserving exact 11-col rows."""

    __slots__ = ("_ts_s", "_ohlc", "_sigs", "_etags", "_xtags", "_tags")

    def __init__(
        self,
        ts_s: np.ndarray,
        ohlc: np.ndarray,
        sigs: np.ndarray,
        etags: np.ndarray,
        xtags: np.ndarray,
        tags: tuple[Any, ...],
    ) -> None:
        self._ts_s = ts_s
        self._ohlc = ohlc
        self._sigs = sigs
        self._etags = etags
        self._xtags = xtags
        self._tags = tags

    @classmethod
    def from_rows(cls, rows: list[list[Any]]) -> "CompactOhlcvArray":
        n = len(rows)
        ts_s = np.empty(n, dtype=np.int64)
        ohlc = np.empty((n, 4), dtype=np.float64)
        sigs = np.empty((n, 4), dtype=np.int16)
        etags = np.empty(n, dtype=np.int32)
        xtags = np.empty(n, dtype=np.int32)
        tag_map: dict[Any, int] = {None: 0}
        tags_list: list[Any] = [None]

        def _tid(val: Any) -> int:
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return 0
            tid = tag_map.get(val)
            if tid is None:
                tid = len(tags_list)
                tag_map[val] = tid
                tags_list.append(val)
            return tid

        for i, r in enumerate(rows):
            ts_s[i] = int(r[0].timestamp())
            ohlc[i, 0] = r[1]
            ohlc[i, 1] = r[2]
            ohlc[i, 2] = r[3]
            ohlc[i, 3] = r[4]
            sigs[i, 0] = int(r[5] or 0)
            sigs[i, 1] = int(r[6] or 0)
            sigs[i, 2] = int(r[7] or 0)
            sigs[i, 3] = int(r[8] or 0)
            etags[i] = _tid(r[9])
            xtags[i] = _tid(r[10])
        return cls(ts_s, ohlc, sigs, etags, xtags, tuple(tags_list))

    def save_npz(self, path: Path) -> None:
        np.savez_compressed(
            path,
            ts_s=self._ts_s,
            ohlc=self._ohlc,
            sigs=self._sigs,
            etags=self._etags,
            xtags=self._xtags,
            tags_json=np.array(json.dumps(list(self._tags))),
        )

    @classmethod
    def load_npz(cls, path: Path) -> "CompactOhlcvArray":
        with np.load(path, allow_pickle=False) as d:
            tags = tuple(json.loads(str(d["tags_json"].item())))
            return cls(
                d["ts_s"],
                d["ohlc"],
                d["sigs"],
                d["etags"],
                d["xtags"],
                tags,
            )

    def __len__(self) -> int:
        return len(self._ts_s)

    def __getitem__(self, idx: int | slice) -> Any:
        if isinstance(idx, slice):
            return [self[k] for k in range(*idx.indices(len(self._ts_s)))]
        if idx < 0:
            idx += len(self._ts_s)
        if idx < 0 or idx >= len(self._ts_s):
            raise IndexError(idx)
        return [
            pd.Timestamp(int(self._ts_s[idx]), unit="s", tz=UTC),
            float(self._ohlc[idx, 0]),
            float(self._ohlc[idx, 1]),
            float(self._ohlc[idx, 2]),
            float(self._ohlc[idx, 3]),
            int(self._sigs[idx, 0]),
            int(self._sigs[idx, 1]),
            int(self._sigs[idx, 2]),
            int(self._sigs[idx, 3]),
            self._tags[int(self._etags[idx])],
            self._tags[int(self._xtags[idx])],
        ]


class HistoricalPairListAdapter:
    """Drop-in replacement for PairListManager driven by point-in-time 30m snapshots."""

    def __init__(self, universe_pairs: list[str], schedule: dict[str, list[str]] | None):
        self._universe = list(universe_pairs)
        self._schedule = schedule
        self._sorted_keys = sorted(int(k) for k in schedule.keys()) if schedule else []
        self._whitelist = list(universe_pairs)
        self.current_time: datetime | None = None
        self.active_dynamic = False
        self._last_bucket: int | None = None

    @property
    def whitelist(self) -> list[str]:
        return self._whitelist

    @property
    def name_list(self) -> list[str]:
        return ["StaticPairList"]

    def short_desc(self) -> list[dict[str, str]]:
        return [{"HistoricalPropPairList": f"{len(self._universe)} universe pairs"}]

    def refresh_pairlist(self, only_first: bool = False, pairs: list[str] | None = None) -> None:
        if not self.active_dynamic or self._schedule is None or self.current_time is None:
            base = self._universe
        else:
            ts = int(_to_utc(self.current_time).timestamp())
            bucket = (ts // 1800) * 1800
            if bucket == self._last_bucket:
                return
            self._last_bucket = bucket
            key = str(bucket)
            if key in self._schedule:
                base = self._schedule[key]
            elif self._sorted_keys:
                pos = bisect.bisect_right(self._sorted_keys, bucket) - 1
                base = self._schedule[str(self._sorted_keys[pos])] if pos >= 0 else []
            else:
                base = []
        if pairs is not None:
            avail = set(pairs)
            self._whitelist = [p for p in base if p in avail]
        else:
            self._whitelist = list(base)


class PropParityBacktesting(Backtesting):
    """Exact 1:1 HyroTrader One-Step Backtesting subclass."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        universe_pairs: list[str],
        pairlist_schedule: dict[str, list[str]] | None,
        prop_profile: dict[str, Any],
        prop_budget_policy: dict[str, Any],
        btc_close_map: dict[int, float] | None = None,
        extra_slippage_bps_roundtrip: float = 0.0,
        stop_on_halt: bool = True,
        stop_on_pass: bool = False,
        strict_wick_guard: bool = False,
    ) -> None:
        self._universe_pairs = list(universe_pairs)
        self._pairlist_schedule = pairlist_schedule
        self._prop_profile = dict(prop_profile)
        self._prop_policy = dict(prop_budget_policy)
        self._btc_close_map = btc_close_map or {}
        self._extra_slippage_bps = float(extra_slippage_bps_roundtrip)
        self._stop_on_halt = stop_on_halt
        self._stop_on_pass = stop_on_pass
        self._strict_wick_guard = strict_wick_guard
        self._ignore_target_pause: bool = False
        self._sim_main_time: datetime | None = None
        # Configurable external risk controls (all outside strategy logic)
        self._macro_enabled: bool = True
        self._macro_1h_trigger: float = -4.0
        self._macro_24h_trigger: float = -8.0
        self._macro_1h_recover: float = -2.5
        self._macro_24h_recover: float = -6.0
        self._dynamic_shield_enabled: bool = False
        self._dynamic_shield_trigger_dd_usdt: float = 100.0
        self._dynamic_shield_recover_dd_usdt: float = 50.0
        # In-memory fallback provenance for archived delisted symbols (never written to disk)
        self.prop_synthetic_market_pairs: list[str] = []
        self.prop_synthetic_leverage_tier_pairs: list[str] = []
        self.prop_synthetic_mark_pairs: list[str] = []
        self.prop_synthetic_funding_pairs: list[str] = []

        super().__init__(config)
        self._ensure_delisted_markets(self._universe_pairs)
        self._ensure_leverage_tiers(self._universe_pairs)
        self._base_fee: float = float(self.fee) - (max(0.0, self._extra_slippage_bps) / 20000.0)
        self._detail_cache: OrderedDict[str, tuple[np.ndarray, np.ndarray] | None] = OrderedDict()
        self._detail_max_cached: int = 24
        self._detail_handler = None
        self._detail_candle_type = self.config.get("candle_type_def", CandleType.SPOT)

        # Install dynamic historical pairlist adapter AFTER ExchangeResolver & validation
        self._hist_pairlists = HistoricalPairListAdapter(self._universe_pairs, self._pairlist_schedule)
        self.pairlists = self._hist_pairlists
        self.dataprovider.add_pairlisthandler(self.pairlists)
        self.dynamic_pairlist = self._pairlist_schedule is not None

        self._reset_prop_state(datetime.now(UTC))

    def _validate_pairlists_for_backtesting(self):
        return

    def _ensure_delisted_markets(self, universe: list[str]) -> None:
        """Populate conservative in-memory linear market descriptors for archived delisted symbols."""
        try:
            markets = self.exchange.markets
        except Exception:
            markets = getattr(self.exchange, "_markets", None)
        if not isinstance(markets, dict):
            return
        template = markets.get("BTC/USDT:USDT")
        if template is None:
            for m in markets.values():
                if isinstance(m, dict) and m.get("linear") and m.get("quote") == "USDT":
                    template = m
                    break
        api_markets = getattr(getattr(self.exchange, "_api", None), "markets", None)
        for pair in universe:
            if pair in markets:
                continue
            base = pair.split("/")[0]
            if isinstance(template, dict):
                entry = copy.deepcopy(template)
            else:
                entry = {
                    "type": "swap",
                    "spot": False,
                    "margin": False,
                    "swap": True,
                    "future": False,
                    "option": False,
                    "contract": True,
                    "linear": True,
                    "inverse": False,
                    "taker": 0.00055,
                    "maker": 0.0002,
                    "percentage": True,
                    "tierBased": False,
                }
            entry.update(
                {
                    "id": f"{base}USDT",
                    "symbol": pair,
                    "base": base,
                    "quote": "USDT",
                    "settle": "USDT",
                    "baseId": base,
                    "quoteId": "USDT",
                    "settleId": "USDT",
                    "active": True,
                    "contract": True,
                    "linear": True,
                    "swap": True,
                    "contractSize": 1.0,
                    "precision": {"amount": 0.001, "price": 0.0001},
                    "limits": {
                        "amount": {"min": 0.001, "max": 10_000_000.0},
                        "cost": {"min": 5.0, "max": 2_000_000.0},
                        "price": {"min": 0.0001, "max": 1_000_000.0},
                        "leverage": {"min": 1.0, "max": 5.0},
                    },
                }
            )
            markets[pair] = entry
            if isinstance(api_markets, dict) and pair not in api_markets:
                api_markets[pair] = entry
            self.prop_synthetic_market_pairs.append(pair)
        if self.prop_synthetic_market_pairs:
            logger.warning(
                "Synthesized in-memory linear market descriptors for %d delisted pairs: %s",
                len(self.prop_synthetic_market_pairs),
                self.prop_synthetic_market_pairs,
            )

    def _ensure_leverage_tiers(self, universe: list[str]) -> None:
        """Guard against CCXT Bybit 50-page truncation and populate 1x in-memory tiers for delisted pairs."""
        missing = [p for p in universe if p not in self.exchange._leverage_tiers]
        if not missing:
            return
        api = getattr(self.exchange, "_api", None)
        if api is not None and hasattr(api, "publicGetV5MarketRiskLimit"):
            try:
                logger.info("Fetching full paginated Bybit leverage tiers (%d missing in cache)...", len(missing))
                rows: list[dict[str, Any]] = []
                cursor = ""
                while True:
                    params: dict[str, Any] = {"category": "linear", "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    res = api.publicGetV5MarketRiskLimit(params)["result"]
                    rows.extend(res["list"])
                    cursor = res.get("nextPageCursor") or ""
                    if not cursor:
                        break
                parsed = api.parse_leverage_tiers(rows, None, "symbol")
                parse_fn = getattr(self.exchange, "parse_leverage_tier", None)
                for p, tiers in parsed.items():
                    self.exchange._leverage_tiers[p] = (
                        [parse_fn(t) for t in tiers] if callable(parse_fn) else tiers
                    )
                cache_file = self.config["datadir"] / "futures" / f"leverage_tiers_{self.config['stake_currency']}.json"
                if cache_file.exists():
                    try:
                        cur = json.loads(cache_file.read_text())
                        cur.setdefault("data", {}).update(parsed)
                        cur["updated"] = datetime.now(UTC).isoformat()
                        cache_file.write_text(json.dumps(cur))
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Could not refresh live Bybit leverage tiers (%s); falling back to cached/in-memory tiers.", exc)

        # Delisted archived symbols are absent from live Bybit risk limits: assign conservative 1x tier in-memory ONLY.
        still_missing = [p for p in universe if p not in self.exchange._leverage_tiers]
        for pair in still_missing:
            self.exchange._leverage_tiers[pair] = [
                {
                    "minNotional": 0.0,
                    "maxNotional": 200_000.0,
                    "maintenanceMarginRate": 0.01,
                    "maxLeverage": 5.0,
                    "maintAmt": 0.0,
                }
            ]
            self.prop_synthetic_leverage_tier_pairs.append(pair)
        # Ensure every cached/synthesized tier has Freqtrade's required 'maintAmt' key
        for pair in universe:
            tiers = self.exchange._leverage_tiers.get(pair)
            if isinstance(tiers, list):
                for t in tiers:
                    if isinstance(t, dict) and "maintAmt" not in t:
                        info = t.get("info") if isinstance(t.get("info"), dict) else {}
                        t["maintAmt"] = float(info["cum"]) if "cum" in info else 0.0
        if self.prop_synthetic_leverage_tier_pairs:
            logger.warning(
                "Assigned conservative in-memory 1x leverage tiers for %d delisted pairs: %s",
                len(self.prop_synthetic_leverage_tier_pairs),
                self.prop_synthetic_leverage_tier_pairs,
            )

    def validate_row(self, data: dict, pair: str, row_index: int, current_time: datetime):
        pair_rows = data.get(pair)
        if isinstance(pair_rows, CompactOhlcvArray):
            if row_index >= len(pair_rows._ts_s):
                return None
            if pair_rows._ts_s[row_index] > int(current_time.timestamp()):
                return None
            return pair_rows[row_index]
        return super().validate_row(data, pair, row_index, current_time)

    def _sync_pair_index(
        self, data: dict, pair: str, row_index: int, current_time: datetime
    ) -> int:
        """Advance stale row_index in O(log N) whenever pair starts/jumps ahead of row_index."""
        pair_rows = data.get(pair)
        if isinstance(pair_rows, CompactOhlcvArray):
            ts_s = pair_rows._ts_s
            n = len(ts_s)
            if row_index >= n:
                return row_index
            cur_s = int(current_time.timestamp())
            if ts_s[row_index] >= cur_s:
                return row_index
            return row_index + int(np.searchsorted(ts_s[row_index:], cur_s, side="left"))
        if not pair_rows or row_index >= len(pair_rows):
            return row_index
        if pair_rows[row_index][DATE_IDX] >= current_time:
            return row_index
        lo = row_index
        hi = len(pair_rows)
        while lo < hi:
            mid = (lo + hi) // 2
            if pair_rows[mid][DATE_IDX] < current_time:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def set_fee(self):
        if hasattr(self, "_base_fee"):
            self.fee = float(self._base_fee) + (max(0.0, float(self._extra_slippage_bps)) / 20000.0)
        else:
            super().set_fee()
            if self._extra_slippage_bps > 0:
                self.fee = self.fee + (self._extra_slippage_bps / 20000.0)
        if self._extra_slippage_bps > 0:
            self.log_once(
                f"Applied execution slippage +{self._extra_slippage_bps:.1f} bps RT -> effective fee {self.fee:.4%}/side."
            )

    def _load_bt_data_detail(self) -> None:
        """Load futures funding/mark eagerly (with in-memory fallback for delisted pairs), keep 1m detail lazy + LRU."""
        self.detail_data = {}
        self.prop_synthetic_mark_pairs.clear()
        self.prop_synthetic_funding_pairs.clear()

        if self.trading_mode == TradingMode.FUTURES:
            funding_fee_timeframe: str = self.exchange.get_option("funding_fee_timeframe")
            self.funding_fee_timeframe_secs: int = timeframe_to_seconds(funding_fee_timeframe)
            mark_timeframe: str = self.exchange.get_option("mark_ohlcv_timeframe")

            funding_rates_dict = history.load_data(
                datadir=self.config["datadir"],
                pairs=self.pairlists.whitelist,
                timeframe=funding_fee_timeframe,
                timerange=self.timerange,
                startup_candles=0,
                fail_without_data=False,
                fill_up_missing=False,
                data_format=self.config["dataformat_ohlcv"],
                candle_type=CandleType.FUNDING_RATE,
            )
            mark_rates_dict = history.load_data(
                datadir=self.config["datadir"],
                pairs=self.pairlists.whitelist,
                timeframe=mark_timeframe,
                timerange=self.timerange,
                startup_candles=0,
                fail_without_data=False,
                data_format=self.config["dataformat_ohlcv"],
                candle_type=CandleType.from_string(self.exchange.get_option("mark_ohlcv_price")),
            )

            self.futures_data = {}
            unavailable_pairs: list[str] = []
            uses_leverage_tiers = self.exchange.get_option("uses_leverage_tiers", True)
            for pair in self.pairlists.whitelist:
                if uses_leverage_tiers and pair not in self.exchange._leverage_tiers:
                    unavailable_pairs.append(pair)
                    continue
                mark_df = mark_rates_dict.get(pair)
                if mark_df is None or mark_df.empty:
                    # Archived delisted pair without mark feather: use 1h trade OHLCV in-memory ONLY.
                    fallback_mark = history.load_pair_history(
                        pair=pair,
                        timeframe=mark_timeframe,
                        datadir=self.config["datadir"],
                        timerange=self.timerange,
                        startup_candles=0,
                        data_format=self.config["dataformat_ohlcv"],
                        candle_type=CandleType.FUTURES,
                    )
                    if fallback_mark.empty:
                        continue
                    mark_df = fallback_mark
                    self.prop_synthetic_mark_pairs.append(pair)

                fund_df = funding_rates_dict.get(pair)
                if fund_df is None or fund_df.empty:
                    # Conservative 0.01% per 8h (00:00/08:00/16:00 UTC) long funding in-memory ONLY.
                    dates = pd.to_datetime(mark_df["date"], utc=True)
                    fund_df = pd.DataFrame(
                        {
                            "date": dates,
                            "open": np.where(dates.dt.hour % 8 == 0, 0.0001, 0.0),
                            "high": 0.0,
                            "low": 0.0,
                            "close": 0.0,
                            "volume": 0.0,
                        }
                    )
                    self.prop_synthetic_funding_pairs.append(pair)

                self.futures_data[pair] = self.exchange.combine_funding_and_mark(
                    funding_rates=fund_df,
                    mark_rates=mark_df,
                    futures_funding_rate=self.config.get("futures_funding_rate", None),
                )

            if unavailable_pairs:
                raise OperationalException(
                    f"Pairs {', '.join(unavailable_pairs)} got no leverage tiers available. "
                    "It is therefore impossible to backtest with this pair at the moment."
                )
            if self.prop_synthetic_mark_pairs or self.prop_synthetic_funding_pairs:
                logger.warning(
                    "In-memory fallback applied for archived delisted pairs (no disk fabrication): "
                    "mark=%s funding=%s",
                    self.prop_synthetic_mark_pairs,
                    self.prop_synthetic_funding_pairs,
                )
        else:
            self.futures_data = {}

        self._detail_cache.clear()
        saved_detail_tf = self.timeframe_detail
        if saved_detail_tf:
            self._detail_candle_type = self.config.get("candle_type_def", CandleType.SPOT)
            self._detail_handler = history.get_datahandler(
                self.config["datadir"], self.config["dataformat_ohlcv"]
            )
            avail = {}
            for pair in self.pairlists.whitelist:
                fname = self._detail_handler._pair_data_filename(
                    self.config["datadir"], pair, saved_detail_tf, self._detail_candle_type
                )
                if fname.exists():
                    avail[pair] = True
            if not avail:
                raise OperationalException("No detail data found. Terminating.")
            self.detail_data = avail

    def _get_detail_entry(self, pair: str) -> tuple[np.ndarray, np.ndarray] | None:
        if pair in self._detail_cache:
            self._detail_cache.move_to_end(pair)
            return self._detail_cache[pair]
        disk_cache = getattr(self, "_detail_array_cache", None)
        source = (
            self._detail_handler._pair_data_filename(
                self.config["datadir"], pair, self.timeframe_detail, self._detail_candle_type
            )
            if self._detail_handler is not None
            else None
        )
        cache_key = None
        if disk_cache is not None and source is not None:
            stat = source.stat() if source.exists() else None
            cache_key = [str(source.resolve()), stat.st_size if stat else None,
                         stat.st_mtime_ns if stat else None, self.timerange.startts,
                         self.timerange.stopts, self.timerange.starttype, self.timerange.stoptype]
            entry = disk_cache.load(cache_key)
            if entry is not None:
                self._detail_cache[pair] = entry
                while len(self._detail_cache) > self._detail_max_cached:
                    self._detail_cache.popitem(last=False)
                return entry

        # Fast path: load 1m feather directly with PyArrow if contiguous (no missing 1m bars)
        entry = None
        loaded_fast = False
        if (
            history.load_pair_history is _ORIG_LOAD_PAIR_HISTORY
            and self.timeframe_detail == "1m"
            and source is not None
            and source.exists()
        ):
            try:
                tbl = _pa_feather.read_table(source, columns=["date", "open", "high", "low", "close"])
                if tbl.num_rows == 0:
                    entry = None
                    loaded_fast = True
                else:
                    ts_raw = (
                        tbl.column("date")
                        .cast("timestamp[s]")
                        .cast("int64")
                        .to_numpy(zero_copy_only=False)
                    )
                    i0 = 0
                    i1 = len(ts_raw)
                    if self.timerange:
                        if self.timerange.starttype == "date" and self.timerange.startts:
                            i0 = int(np.searchsorted(ts_raw, int(self.timerange.startts), side="left"))
                        if self.timerange.stoptype == "date" and self.timerange.stopts:
                            i1 = int(np.searchsorted(ts_raw, int(self.timerange.stopts), side="right"))
                    ts_slice = ts_raw[i0:i1]
                    if len(ts_slice) == 0:
                        entry = None
                        loaded_fast = True
                    elif len(ts_slice) == 1 or (ts_slice[-1] - ts_slice[0] == (len(ts_slice) - 1) * 60):
                        ts_s = np.ascontiguousarray(ts_slice, dtype=np.int64)
                        ohlc = np.empty((len(ts_s), 4), dtype=np.float64)
                        ohlc[:, 0] = tbl.column("open").to_numpy(zero_copy_only=False)[i0:i1]
                        ohlc[:, 1] = tbl.column("high").to_numpy(zero_copy_only=False)[i0:i1]
                        ohlc[:, 2] = tbl.column("low").to_numpy(zero_copy_only=False)[i0:i1]
                        ohlc[:, 3] = tbl.column("close").to_numpy(zero_copy_only=False)[i0:i1]
                        entry = (ts_s, ohlc)
                        loaded_fast = True
            except Exception:
                loaded_fast = False

        if not loaded_fast:
            pinned = getattr(self, "_detail_pinned_Cache", None)
            if pinned is None:
                pinned = {}
                self._detail_pinned_Cache = pinned
            if pair in pinned:
                entry = pinned[pair]
            else:
                df = history.load_pair_history(
                    pair=pair,
                    timeframe=self.timeframe_detail,
                    datadir=self.config["datadir"],
                    timerange=self.timerange,
                    fill_up_missing=True,
                    startup_candles=0,
                    data_handler=self._detail_handler,
                    candle_type=self._detail_candle_type,
                )
                if df.empty:
                    entry = None
                else:
                    ts_s = df["date"].dt.as_unit("s").to_numpy(dtype=np.int64)
                    ohlc = df[["open", "high", "low", "close"]].to_numpy(dtype=np.float64)
                    entry = (ts_s, ohlc)
                if history.load_pair_history is _ORIG_LOAD_PAIR_HISTORY and disk_cache is None:
                    pinned[pair] = entry
        if entry is not None and disk_cache is not None and cache_key is not None:
            entry = disk_cache.save(cache_key, entry)
        self._detail_cache[pair] = entry
        while len(self._detail_cache) > self._detail_max_cached:
            self._detail_cache.popitem(last=False)
        return entry

    def get_detail_data(self, pair: str, row: tuple) -> list[tuple] | None:
        """Fast numpy-backed replacement for Backtesting.get_detail_data with identical rows."""
        entry = self._get_detail_entry(pair)
        if entry is None:
            return None
        ts_s, ohlc = entry
        current_detail_time: datetime = row[DATE_IDX]
        start_s = int(current_detail_time.timestamp())
        end_s = int((current_detail_time + self.timeframe_td).timestamp())
        start_idx = int(np.searchsorted(ts_s, start_s, side="left"))
        end_idx = int(np.searchsorted(ts_s, end_s, side="left"))
        if end_idx <= start_idx:
            return None
        signals = [
            row[LONG_IDX],
            row[ELONG_IDX],
            row[SHORT_IDX],
            row[ESHORT_IDX],
            row[ENTER_TAG_IDX],
            row[EXIT_TAG_IDX],
        ]
        return [
            [
                pd.Timestamp(int(ts_s[i]), unit="s", tz="UTC"),
                float(ohlc[i, 0]),
                float(ohlc[i, 1]),
                float(ohlc[i, 2]),
                float(ohlc[i, 3]),
            ]
            + signals
            for i in range(start_idx, end_idx)
        ]

    def _reset_prop_state(self, start_dt: datetime):
        start_utc = _to_utc(start_dt)
        init_bal = float(self._prop_profile["initial_balance"])
        self.prop_phase_start: datetime = start_utc
        self.prop_midnight_equity: float = init_bal
        self.prop_min_day_equity_wick: float = init_bal
        self.prop_current_day: str = start_utc.strftime("%Y-%m-%d")
        self._prop_current_day_int: int = int(start_utc.timestamp()) // 86400
        self._prop_limits_cache: dict[str, float] = floors(self._prop_profile, init_bal)
        self.prop_first_enter_attempt_ts: datetime | None = None
        self.prop_flat_unpaused_midnights: set[str] = {self.prop_current_day}
        self.prop_halted: bool = False
        self.prop_halt_reason: str | None = None
        self.prop_halt_dt: datetime | None = None
        self.prop_passed: bool = False
        self.prop_pass_dt: datetime | None = None
        self.prop_milestone_pass_dt: datetime | None = None
        self.prop_consistency_pause_days: set[str] = set()
        self.prop_current_month: str = start_utc.strftime("%Y-%m")
        self.prop_month_start_equity: float = init_bal
        self.prop_max_month_dd_close: float = 0.0
        self.prop_max_month_dd_wick: float = 0.0
        self.prop_month_max_dd_close_by_month: dict[str, float] = {}
        self.prop_month_max_dd_wick_by_month: dict[str, float] = {}
        self.prop_inactivity_breach: bool = False
        self.prop_macro_paused: bool = False
        self.prop_macro_clean_streak: int = 0
        self.prop_rules_paused: bool = False
        self.prop_rules_pause_reason: str | None = None
        self.prop_closed_records: list[dict[str, Any]] = []
        self.prop_latest_bars: dict[str, tuple[float, float, float, float]] = {}
        self.prop_keepalive_events: list[datetime] = []
        self.prop_latest_activity_dt: datetime = start_utc
        self.prop_last_keepalive_day: str | None = None
        self.prop_budget_denials: dict[str, int] = defaultdict(int)
        self.prop_macro_pause_bars: int = 0
        self.prop_rules_pause_bars: int = 0
        self.prop_dynamic_shield_active: bool = False
        self.prop_dynamic_shield_bars: int = 0

        # Telemetry metrics
        self.prop_min_equity_close: float = init_bal
        self.prop_min_equity_wick: float = init_bal
        self.prop_max_equity_close: float = init_bal
        self.prop_max_daily_dd_close: float = 0.0
        self.prop_max_daily_dd_wick: float = 0.0
        self.prop_max_overall_dd_close: float = 0.0
        self.prop_max_overall_dd_wick: float = 0.0
        self.prop_official_breach_close: str | None = None
        self.prop_official_breach_wick: str | None = None
        self.prop_internal_wick_touch: str | None = None
        self.prop_official_daily_breach_wick_bars: int = 0
        self.prop_official_overall_breach_wick_bars: int = 0
        self.prop_funded_margin_gt_25pct_bars: int = 0
        self.prop_funded_notional_gt_2x_bars: int = 0
        self.prop_max_aggregate_notional_observed: float = 0.0
        self.prop_max_slot_notional_observed: float = 0.0
        self.prop_shock_1coin_35_breach_bars: int = 0
        self.prop_shock_1coin_50_breach_bars: int = 0
        self.prop_shock_2coin_25_breach_bars: int = 0
        self.prop_min_post_shock_50_headroom_usd: float = init_bal
        self.prop_daily_curve: list[dict[str, Any]] = []
        self._last_eval_det_ts: datetime | None = None
        self._cached_ledger: dict[str, Any] = compute_hyrotrader_ledger(
            [], [], init_bal, self._prop_profile["profit_target_percent"],
            self._prop_profile["daily_profit_cap_percent"], start_utc
        )
        if getattr(self, "strategy", None) is not None:
            if hasattr(self.strategy, "_backtest_profit_snapshot"):
                self.strategy._backtest_profit_snapshot = None
            tpc = getattr(self.strategy, "target_profit_cache", None)
            if tpc is not None and hasattr(tpc, "clear"):
                tpc.clear()
            if hasattr(self.strategy, "_grind_entry_tag"):
                self.strategy._grind_entry_tag = ""
        if hasattr(self, "_hist_pairlists"):
            self._hist_pairlists.active_dynamic = False
            self._hist_pairlists._last_bucket = None
            self._hist_pairlists.current_time = start_utc

    def _current_floating_equity(self, use_low: bool = False) -> float:
        wallet_tot = float(self.wallets.get_total(self.config["stake_currency"]))
        unrealized = 0.0
        for tr in LocalTrade.bt_trades_open:
            bar = self.prop_latest_bars.get(tr.pair)
            if bar is None:
                px = float(tr.open_rate or 0.0)
            else:
                # bar = (open, high, low, close)
                px = bar[2] if use_low else bar[3]
            if px > 0:
                unrealized += float(tr.calc_profit(px))
        return wallet_tot + unrealized

    def _prop_snapshot_notionals(self, req_pair: str) -> tuple[float, float, float]:
        existing_n = 0.0
        pending_n = 0.0
        pair_n = 0.0
        for tr in LocalTrade.bt_trades_open:
            bar = self.prop_latest_bars.get(tr.pair)
            px = bar[3] if bar is not None else float(tr.open_rate or 0.0)
            if tr.amount and px > 0:
                val = float(tr.amount) * float(px)
                existing_n += val
                if tr.pair == req_pair:
                    pair_n += val
            for o in tr.open_orders:
                if o.ft_order_side == tr.entry_side:
                    rem = max(0.0, float(o.amount or 0.0) - float(o.filled or 0.0))
                    op = float(o.price or o.ft_price or px or 0.0)
                    if rem > 0 and op > 0:
                        oval = rem * op
                        pending_n += oval
                        if tr.pair == req_pair:
                            pair_n += oval
        return existing_n, pending_n, pair_n

    def _prop_remaining_headroom(self, pair: str) -> float:
        max_agg = float(self._prop_policy["max_aggregate_notional_usdt"])
        max_slot = float(self._prop_policy["max_slot_notional_usdt"])
        existing_n, pending_n, pair_n = self._prop_snapshot_notionals(pair)
        rem_agg = max(0.0, max_agg - existing_n - pending_n)
        rem_slot = max(0.0, max_slot - pair_n)
        return max(0.0, min(rem_agg, rem_slot) * 0.995)

    def _prop_budget_allows(self, pair: str, stake_amount: float) -> tuple[bool, str]:
        if stake_amount <= 0 or not math.isfinite(stake_amount):
            return False, "invalid_stake"
        existing_n, pending_n, pair_n = self._prop_snapshot_notionals(pair)
        pair_after = pair_n + stake_amount
        agg_after = existing_n + pending_n + stake_amount
        max_slot = float(self._prop_policy["max_slot_notional_usdt"])
        max_agg = float(self._prop_policy["max_aggregate_notional_usdt"])
        if pair_after > max_slot:
            return False, "slot_notional_cap_exceeded"
        if agg_after > max_agg:
            return False, "aggregate_notional_cap_exceeded"
        limits = self._prop_limits_cache
        op_floor = max(limits["operating_daily_floor"], limits["operating_overall_floor"])
        eq = self._current_floating_equity(use_low=False)
        headroom = max(0.0, eq - op_floor)
        total_bps = (
            float(self._prop_policy["roundtrip_fee_bps"])
            + float(self._prop_policy["funding_bound_bps"])
            + float(self._prop_policy["execution_slippage_bps"])
            + float(self._prop_policy["latency_gap_stress_bps"])
        )
        req_headroom = (agg_after * total_bps) / 10000.0 + float(
            self._prop_policy["min_headroom_buffer_usdt"]
        )
        if headroom <= 0.0:
            return False, "operating_headroom_exhausted"
        if req_headroom > headroom:
            return False, "insufficient_operating_headroom_for_stress_bound"
        return True, "authorized"

    def _is_entry_paused(self) -> bool:
        return (
            self.prop_halted
            or self.prop_macro_paused
            or self.prop_rules_paused
            or self.prop_passed
        )

    def _update_btc_macro(self, current_time: datetime) -> None:
        if not self._macro_enabled:
            self.prop_macro_paused = False
            self.prop_macro_clean_streak = 0
            return
        if not self._btc_close_map:
            return
        ts = int(_to_utc(current_time).timestamp())
        # BTC close lookup using latest closed candle <= ts
        c_now = self._btc_lookup(ts)
        c_1h = self._btc_lookup(ts - 3600)
        c_24h = self._btc_lookup(ts - 86400)
        if c_now is None or c_1h is None or c_24h is None or c_1h <= 0 or c_24h <= 0:
            return
        pct_1h = (c_now - c_1h) / c_1h * 100.0
        pct_24h = (c_now - c_24h) / c_24h * 100.0
        if pct_1h <= self._macro_1h_trigger or pct_24h <= self._macro_24h_trigger:
            self.prop_macro_paused = True
            self.prop_macro_clean_streak = 0
        elif pct_1h > self._macro_1h_recover and pct_24h > self._macro_24h_recover:
            self.prop_macro_clean_streak += 1
            if self.prop_macro_clean_streak >= 1:
                self.prop_macro_paused = False
        else:
            self.prop_macro_clean_streak = 0

    def _btc_lookup(self, target_ts: int) -> float | None:
        # Snap to latest closed 5m candle open timestamp (< target_ts), works on 5m & 1m ticks
        aligned = ((target_ts - 1) // 300) * 300
        for step in range(5):
            v = self._btc_close_map.get(aligned - step * 300)
            if v is not None:
                return v
        return None

    def _recompute_ledger(self, current_time: datetime) -> None:
        ledger = compute_hyrotrader_ledger(
            list(LocalTrade.bt_trades_open),
            self.prop_closed_records,
            float(self._prop_profile["initial_balance"]),
            float(self._prop_profile["profit_target_percent"]),
            float(self._prop_profile["daily_profit_cap_percent"]),
            self.prop_phase_start,
        )
        self._cached_ledger = ledger
        if ledger["latest_activity_dt"] and ledger["latest_activity_dt"] > self.prop_latest_activity_dt:
            self.prop_latest_activity_dt = ledger["latest_activity_dt"]

        day_str = _to_utc(current_time).strftime("%Y-%m-%d")
        today_pnl = ledger["day_profits"].get(day_str, 0.0)
        target_hit = (
            round(ledger["counted_profit"], 6) >= round(ledger["target_profit"], 6)
            and len(ledger["qualifying_days"]) >= int(self._prop_profile["min_trading_days"])
        )
        if target_hit and self.prop_milestone_pass_dt is None:
            self.prop_milestone_pass_dt = _to_utc(current_time)

        if not self.prop_passed and not self._ignore_target_pause and target_hit:
            self.prop_passed = True
            self.prop_pass_dt = self.prop_milestone_pass_dt
            self.prop_rules_paused = True
            self.prop_rules_pause_reason = "target_reached"
        elif today_pnl >= ledger["daily_pause_threshold"]:
            self.prop_rules_paused = True
            self.prop_rules_pause_reason = "consistency_cap"
            self.prop_consistency_pause_days.add(day_str)
        elif not self.prop_passed:
            self.prop_rules_paused = False
            self.prop_rules_pause_reason = None

    def _emergency_close_all(self, current_time: datetime, reason: str, use_low: bool = False) -> None:
        if self.prop_halted:
            return
        self.prop_halted = True
        self.prop_halt_reason = reason
        self.prop_halt_dt = _to_utc(current_time)

        for tr in list(LocalTrade.bt_trades_open):
            if tr.has_open_orders:
                self.cancel_open_orders(tr, current_time)
            if not tr.has_open_position or tr.amount <= 0:
                LocalTrade.remove_bt_trade(tr)
                continue
            bar = self.prop_latest_bars.get(tr.pair)
            px = (bar[2] if use_low else bar[3]) if bar else float(tr.open_rate or 0.0)
            px = price_to_precision(px, tr.price_precision, tr.precision_mode_price)
            fake_row = (
                pd.Timestamp(_to_utc(current_time)),
                px,
                max(px, bar[1] if bar else px),
                min(px, bar[2] if bar else px),
                px,
                0,
                0,
                0,
                0,
                None,
                None,
            )
            self._run_funding_fees(tr, current_time, force=True)
            exit_trade = self._exit_trade(
                tr, fake_row, px, tr.amount, ExitType.FORCE_EXIT.value
            )
            if exit_trade is not None and exit_trade.orders:
                ord_obj = exit_trade.orders[-1]
                self._process_exit_order(ord_obj, exit_trade, current_time, fake_row, tr.pair)
        self.wallets.update()

    def _on_tick_rollover_and_guard(self, current_time: datetime, eval_drawdown: bool) -> None:
        dt_utc = _to_utc(current_time)
        ts_sec = int(dt_utc.timestamp())
        day_int = ts_sec // 86400

        # 1. Midnight UTC baseline rollover (must settle funding up to 00:00 UTC)
        if day_int != self._prop_current_day_int:
            day_str = dt_utc.strftime("%Y-%m-%d")
            self._prop_current_day_int = day_int
            for tr in LocalTrade.bt_trades_open:
                self._run_funding_fees(tr, current_time, force=True)
            # Use synchronized closing mark at end of previous day (23:55/23:59 close = 00:00 UTC mark)
            wallet_tot = float(self.wallets.get_total(self.config["stake_currency"]))
            unreal_open = 0.0
            for tr in LocalTrade.bt_trades_open:
                bar = self.prop_latest_bars.get(tr.pair)
                px_open = bar[3] if bar is not None else float(tr.open_rate or 0.0)
                if px_open > 0:
                    unreal_open += float(tr.calc_profit(px_open))
            end_prev_eq = wallet_tot + unreal_open
            self.prop_daily_curve.append(
                {
                    "date": self.prop_current_day,
                    "midnight_equity": round(end_prev_eq, 2),
                    "min_day_equity_wick": self.prop_min_day_equity_wick,
                    "counted_profit": round(self._cached_ledger["counted_profit"], 2),
                    "qualifying_days": len(self._cached_ledger["qualifying_days"]),
                    "open_trades": len(LocalTrade.bt_trades_open),
                }
            )
            self.prop_midnight_equity = end_prev_eq
            self.prop_min_day_equity_wick = end_prev_eq
            self.prop_current_day = day_str
            self._prop_limits_cache = floors(self._prop_profile, end_prev_eq)
            month_str = day_str[:7]
            if month_str != self.prop_current_month:
                self.prop_current_month = month_str
                self.prop_month_start_equity = end_prev_eq
            if not self.prop_passed and not self.prop_halted:
                self.prop_rules_paused = False
                self.prop_rules_pause_reason = None
                self._recompute_ledger(current_time)
            if (
                self.prop_first_enter_attempt_ts is None
                and not self.prop_macro_paused
                and not self.prop_inactivity_breach
            ):
                self.prop_flat_unpaused_midnights.add(day_str)

        if not eval_drawdown or self.prop_halted:
            return

        if not LocalTrade.bt_trades_open and not self.prop_closed_records and not self._dynamic_shield_enabled:
            inact_days = (dt_utc - self.prop_latest_activity_dt).total_seconds() / 86400.0
            if inact_days >= 30.0:
                self.prop_inactivity_breach = True
            return

        # 2. Evaluate RiskGuard on real floating equity (close AND conservative intrabar low)
        if not LocalTrade.bt_trades_open:
            eq_close = float(self.wallets.get_total(self.config["stake_currency"]))
            eq_wick = eq_close
        else:
            eq_close = self._current_floating_equity(use_low=False)
            eq_wick = self._current_floating_equity(use_low=True)
        self.prop_min_day_equity_wick = min(self.prop_min_day_equity_wick, eq_wick)
        limits = self._prop_limits_cache
        init_bal = float(self._prop_profile["initial_balance"])

        self.prop_min_equity_close = min(self.prop_min_equity_close, eq_close)
        self.prop_min_equity_wick = min(self.prop_min_equity_wick, eq_wick)
        self.prop_max_equity_close = max(self.prop_max_equity_close, eq_close)

        daily_dd_close = self.prop_midnight_equity - eq_close
        daily_dd_wick = self.prop_midnight_equity - eq_wick
        overall_dd_close = init_bal - eq_close
        overall_dd_wick = init_bal - eq_wick
        self.prop_max_daily_dd_close = max(self.prop_max_daily_dd_close, daily_dd_close)
        self.prop_max_daily_dd_wick = max(self.prop_max_daily_dd_wick, daily_dd_wick)
        self.prop_max_overall_dd_close = max(self.prop_max_overall_dd_close, overall_dd_close)
        self.prop_max_overall_dd_wick = max(self.prop_max_overall_dd_wick, overall_dd_wick)
        month_dd_close = self.prop_month_start_equity - eq_close
        month_dd_wick = self.prop_month_start_equity - eq_wick
        self.prop_max_month_dd_close = max(self.prop_max_month_dd_close, month_dd_close)
        self.prop_max_month_dd_wick = max(self.prop_max_month_dd_wick, month_dd_wick)
        m_key = self.prop_current_month
        self.prop_month_max_dd_close_by_month[m_key] = max(
            self.prop_month_max_dd_close_by_month.get(m_key, 0.0), month_dd_close
        )
        self.prop_month_max_dd_wick_by_month[m_key] = max(
            self.prop_month_max_dd_wick_by_month.get(m_key, 0.0), month_dd_wick
        )

        if self.prop_official_breach_close is None:
            if eq_close <= limits["official_overall_floor"]:
                self.prop_official_breach_close = f"official_overall_close ({eq_close:.2f} <= {limits['official_overall_floor']:.2f})"
            elif eq_close <= limits["official_daily_floor"]:
                self.prop_official_breach_close = f"official_daily_close ({eq_close:.2f} <= {limits['official_daily_floor']:.2f})"

        if self.prop_official_breach_wick is None:
            if eq_wick <= limits["official_overall_floor"]:
                self.prop_official_breach_wick = f"official_overall_wick ({eq_wick:.2f} <= {limits['official_overall_floor']:.2f})"
            elif eq_wick <= limits["official_daily_floor"]:
                self.prop_official_breach_wick = f"official_daily_wick ({eq_wick:.2f} <= {limits['official_daily_floor']:.2f})"

        if eq_wick <= limits["official_overall_floor"]:
            self.prop_official_overall_breach_wick_bars += 1
        if eq_wick <= limits["official_daily_floor"]:
            self.prop_official_daily_breach_wick_bars += 1

        if LocalTrade.bt_trades_open:
            slot_vals: list[float] = []
            agg_notional = 0.0
            for tr in LocalTrade.bt_trades_open:
                bar = self.prop_latest_bars.get(tr.pair)
                px = bar[3] if bar is not None else float(tr.open_rate or 0.0)
                t_val = (float(tr.amount) * float(px)) if (tr.amount and px > 0) else 0.0
                for o in tr.open_orders:
                    if o.ft_order_side == tr.entry_side:
                        rem = max(0.0, float(o.amount or 0.0) - float(o.filled or 0.0))
                        op = float(o.price or o.ft_price or px or 0.0)
                        if rem > 0 and op > 0:
                            t_val += rem * op
                if t_val > 0:
                    slot_vals.append(t_val)
                    agg_notional += t_val
            if slot_vals:
                slot_vals.sort(reverse=True)
                self.prop_max_slot_notional_observed = max(self.prop_max_slot_notional_observed, slot_vals[0])
                self.prop_max_aggregate_notional_observed = max(self.prop_max_aggregate_notional_observed, agg_notional)
                if agg_notional > init_bal * 0.25 + 1e-6:
                    self.prop_funded_margin_gt_25pct_bars += 1
                if agg_notional > init_bal * 2.0 + 1e-6:
                    self.prop_funded_notional_gt_2x_bars += 1
                off_floor = max(limits["official_daily_floor"], limits["official_overall_floor"])
                friction = (28.0 + 11.0) / 10000.0
                loss_35 = slot_vals[0] * (0.35 + friction)
                loss_50 = slot_vals[0] * (0.50 + friction)
                if eq_wick - loss_35 <= off_floor:
                    self.prop_shock_1coin_35_breach_bars += 1
                if eq_wick - loss_50 <= off_floor:
                    self.prop_shock_1coin_50_breach_bars += 1
                self.prop_min_post_shock_50_headroom_usd = min(
                    self.prop_min_post_shock_50_headroom_usd, (eq_wick - loss_50) - off_floor
                )
                if len(slot_vals) >= 2:
                    loss_2x25 = (slot_vals[0] + slot_vals[1]) * (0.25 + friction)
                    if eq_wick - loss_2x25 <= off_floor:
                        self.prop_shock_2coin_25_breach_bars += 1

        if self.prop_internal_wick_touch is None:
            if eq_wick <= limits["operating_overall_floor"]:
                self.prop_internal_wick_touch = f"operating_overall_wick ({eq_wick:.2f} <= {limits['operating_overall_floor']:.2f})"
            elif eq_wick <= limits["operating_daily_floor"]:
                self.prop_internal_wick_touch = f"operating_daily_wick ({eq_wick:.2f} <= {limits['operating_daily_floor']:.2f})"

        halt_reason = None
        if self._strict_wick_guard:
            if eq_wick <= limits["operating_overall_floor"]:
                halt_reason = f"operating_overall_floor_wick ({eq_wick:.2f} <= {limits['operating_overall_floor']:.2f})"
            elif eq_wick <= limits["operating_daily_floor"]:
                halt_reason = f"operating_daily_floor_wick ({eq_wick:.2f} <= {limits['operating_daily_floor']:.2f})"
        else:
            if eq_close <= limits["operating_overall_floor"]:
                halt_reason = f"operating_overall_floor_close ({eq_close:.2f} <= {limits['operating_overall_floor']:.2f})"
            elif eq_close <= limits["operating_daily_floor"]:
                halt_reason = f"operating_daily_floor_close ({eq_close:.2f} <= {limits['operating_daily_floor']:.2f})"

        if halt_reason:
            self._emergency_close_all(current_time, halt_reason, use_low=self._strict_wick_guard)
            return

        # 2b. Optional Dynamic Equity Shield: throttle new entries to (max_open_trades - 1) during drawdown
        if self._dynamic_shield_enabled:
            if eq_close <= init_bal - self._dynamic_shield_trigger_dd_usdt:
                self.prop_dynamic_shield_active = True
            elif eq_close >= init_bal - self._dynamic_shield_recover_dd_usdt:
                self.prop_dynamic_shield_active = False
        else:
            self.prop_dynamic_shield_active = False

        # 3. 24-day Inactivity Keep-Alive check & 30d breach sentinel
        inact_days = (dt_utc - self.prop_latest_activity_dt).total_seconds() / 86400.0
        if inact_days >= 30.0:
            self.prop_inactivity_breach = True
        # Artificial micro-trades must never reset the inactivity clock.

    def _time_generator(self, start_date: datetime, end_date: datetime):
        for ct in super()._time_generator(start_date, end_date):
            self._sim_main_time = ct
            self._hist_pairlists.current_time = ct
            self._hist_pairlists.active_dynamic = True
            self._update_btc_macro(ct)
            if self.prop_macro_paused:
                self.prop_macro_pause_bars += 1
            if self.prop_rules_paused:
                self.prop_rules_pause_bars += 1
            if self.prop_dynamic_shield_active:
                self.prop_dynamic_shield_bars += 1
            yield ct
            # End of main 5m tick: evaluate guard once on the latest detail/main tick timestamp
            eval_ts = self._last_eval_det_ts if self._last_eval_det_ts is not None else ct
            self._on_tick_rollover_and_guard(eval_ts, eval_drawdown=True)
            self._last_eval_det_ts = None
            if (self._stop_on_halt and self.prop_halted) or (
                self._stop_on_pass and self.prop_passed
            ):
                return

    def _can_attempt_new_entry(self, is_last_row: bool) -> bool:
        if is_last_row or self._is_entry_paused():
            return False
        if not self.trade_slot_available(LocalTrade.bt_open_open_trade_count):
            return False
        if self.prop_dynamic_shield_active:
            reduced_slots = max(1, int(self.config.get("max_open_trades", 1)) - 1)
            if len(LocalTrade.bt_trades_open) >= reduced_slots:
                return False
        return True

    def time_pair_generator(
        self,
        start_date: datetime,
        end_date: datetime,
        pairs: list[str],
        data: dict[str, list[tuple]],
    ):
        """Sparse 1:1 replacement for Backtesting.time_pair_generator skipping flat no-signal rows."""
        indexes: dict[str, int] = defaultdict(int)
        has_detail = bool(self.timeframe_detail_td)
        detail_td = self.timeframe_detail_td
        req_startup = self.required_startup

        for current_time in self._time_generator(start_date, end_date):
            if self._last_eval_det_ts is not None and current_time != self._last_eval_det_ts:
                self._on_tick_rollover_and_guard(self._last_eval_det_ts, eval_drawdown=True)
            self._last_eval_det_ts = current_time
            self._on_tick_rollover_and_guard(current_time, eval_drawdown=False)

            self.check_abort()
            if self.dynamic_pairlist and self.pairlists:
                self.pairlists.refresh_pairlist(pairs=self.available_pairs)
                pairs = self.pairlists.whitelist

            self.strategy.bot_loop_start(current_time=current_time)
            pair_detail_cache: dict[str, list[tuple]] = {}
            pair_tradedir_cache: dict[str, str | None] = {}
            signal_main_rows: dict[str, Any] = {}
            pairs_with_open_trades = [t.pair for t in LocalTrade.bt_trades_open]
            new_pairlist = list(dict.fromkeys(pairs_with_open_trades + list(pairs)))
            cur_s = int(current_time.timestamp())
            is_last_main = current_time == end_date

            # 1. Main candle pass (is_first = True, idx = 0)
            for pair in new_pairlist:
                pair_rows = data.get(pair)
                if pair_rows is None:
                    continue
                row_index = indexes[pair]
                pair_has_open_trades = len(LocalTrade.bt_trades_open_pp[pair]) > 0

                if isinstance(pair_rows, CompactOhlcvArray):
                    ts_s = pair_rows._ts_s
                    n = len(ts_s)
                    if row_index < n and ts_s[row_index] < cur_s:
                        row_index += int(np.searchsorted(ts_s[row_index:], cur_s, side="left"))
                        indexes[pair] = row_index
                    if row_index >= n or ts_s[row_index] > cur_s:
                        continue
                    raw_idx = row_index
                    row_index += 1
                    indexes[pair] = row_index
                    self.dataprovider._set_dataframe_max_index(pair, req_startup + row_index)
                    sigs = pair_rows._sigs
                    has_sig = sigs[raw_idx, 0] == 1 or sigs[raw_idx, 2] == 1
                    if not pair_has_open_trades and not has_sig and pair not in pairs_with_open_trades:
                        continue
                    row = pair_rows[raw_idx]
                else:
                    row_index = self._sync_pair_index(data, pair, row_index, current_time)
                    row = self.validate_row(data, pair, row_index, current_time)
                    if not row:
                        continue
                    row_index += 1
                    indexes[pair] = row_index
                    self.dataprovider._set_dataframe_max_index(pair, req_startup + row_index)

                trade_dir = self.check_for_trade_entry(row)
                pair_tradedir_cache[pair] = trade_dir
                self.dataprovider._set_dataframe_max_date(current_time)

                pair_has_open_trades = len(LocalTrade.bt_trades_open_pp[pair]) > 0
                if pair in pairs_with_open_trades and not pair_has_open_trades:
                    continue
                if pair_has_open_trades and pair not in pairs_with_open_trades:
                    pairs_with_open_trades.append(pair)

                if trade_dir is None and not pair_has_open_trades:
                    continue

                signal_main_rows[pair] = row
                if not pair_has_open_trades and not self._can_attempt_new_entry(is_last_main):
                    continue

                if (
                    has_detail
                    and pair not in pair_detail_cache
                    and pair in self.detail_data
                    and row
                ):
                    pair_detail = self.get_detail_data(pair, row)
                    if pair_detail is not None:
                        pair_detail_cache[pair] = pair_detail
                        row = pair_detail[0]

                yield current_time, pair, row, is_last_main, trade_dir
                if len(LocalTrade.bt_trades_open_pp[pair]) > 0 and pair not in pairs_with_open_trades:
                    pairs_with_open_trades.append(pair)

            # 2. Detail candle pass (idx >= 1), only when at least one pair has active detail candles
            if has_detail and pair_detail_cache and detail_td is not None:
                max_det_len = max(len(v) for v in pair_detail_cache.values())
                det_end_time = current_time + self.timeframe_td
                idx = 1
                while idx < max_det_len:
                    current_time_det = current_time + detail_td * idx
                    if current_time_det > det_end_time:
                        break
                    is_last_row = current_time_det == end_date
                    det_pairlist = list(
                        dict.fromkeys(
                            [t.pair for t in LocalTrade.bt_trades_open] + list(signal_main_rows.keys())
                        )
                    )
                    for pair in det_pairlist:
                        pair_has_open_trades = len(LocalTrade.bt_trades_open_pp[pair]) > 0
                        if pair in pairs_with_open_trades and not pair_has_open_trades:
                            continue
                        if not pair_has_open_trades and not self._can_attempt_new_entry(is_last_row):
                            continue
                        if pair not in pair_detail_cache:
                            m_row = signal_main_rows.get(pair)
                            if m_row is not None and pair in self.detail_data:
                                p_det = self.get_detail_data(pair, m_row)
                                if p_det is not None:
                                    pair_detail_cache[pair] = p_det
                                    if len(p_det) > max_det_len:
                                        max_det_len = len(p_det)
                        detail_data = pair_detail_cache.get(pair)
                        if detail_data is None or len(detail_data) <= idx:
                            continue
                        row = detail_data[idx]
                        trade_dir = pair_tradedir_cache.get(pair)
                        if self.strategy.ignore_expired_candle(
                            current_time - self.timeframe_td,
                            current_time_det,
                            self.timeframe_secs,
                            trade_dir is not None,
                        ):
                            trade_dir = None
                        self.dataprovider._set_dataframe_max_date(current_time_det)
                        if pair_has_open_trades and pair not in pairs_with_open_trades:
                            pairs_with_open_trades.append(pair)
                        yield current_time_det, pair, row, is_last_row, trade_dir
                        if len(LocalTrade.bt_trades_open_pp[pair]) > 0 and pair not in pairs_with_open_trades:
                            pairs_with_open_trades.append(pair)
                    idx += 1

    def _enter_trade(self, pair: str, row: list, direction: str, *args, **kwargs):
        if self.prop_first_enter_attempt_ts is None:
            self.prop_first_enter_attempt_ts = _to_utc(row[DATE_IDX])
        return super()._enter_trade(pair, row, direction, *args, **kwargs)

    def get_valid_entry_price_and_stake(
        self,
        pair: str,
        row: tuple,
        propose_rate: float,
        stake_amount: float,
        direction: str,
        current_time: datetime,
        entry_tag: str | None,
        trade: LocalTrade | None,
        order_type: str,
        price_precision: float | None,
        precision_mode_price: int,
    ) -> tuple[float, float, float, float]:
        rate, stake_val, lev, min_stake = super().get_valid_entry_price_and_stake(
            pair,
            row,
            propose_rate,
            stake_amount,
            direction,
            current_time,
            entry_tag,
            trade,
            order_type,
            price_precision,
            precision_mode_price,
        )
        if not stake_val or stake_val <= 0:
            return rate, 0.0, lev, min_stake

        self.prop_latest_bars[pair] = (row[OPEN_IDX], row[HIGH_IDX], row[LOW_IDX], row[CLOSE_IDX])
        prop_headroom = self._prop_remaining_headroom(pair)
        if stake_val > prop_headroom:
            if trade is None and not self.config.get("amend_last_stake_amount", False):
                self.prop_budget_denials["headroom_below_initial_stake"] += 1
                return rate, 0.0, lev, min_stake
            stake_val = prop_headroom
        if min_stake and stake_val < min_stake:
            self.prop_budget_denials["headroom_below_min_stake"] += 1
            return rate, 0.0, lev, min_stake

        ok, reason = self._prop_budget_allows(pair, stake_val)
        if not ok:
            self.prop_budget_denials[reason] += 1
            return rate, 0.0, lev, min_stake
        return rate, stake_val, lev, min_stake

    def _check_adjust_trade_for_candle(
        self, trade: LocalTrade, row: tuple, current_time: datetime
    ) -> LocalTrade:
        self.prop_latest_bars[trade.pair] = (row[OPEN_IDX], row[HIGH_IDX], row[LOW_IDX], row[CLOSE_IDX])
        current_rate: float = row[OPEN_IDX]
        current_profit = trade.calc_profit_ratio(current_rate)
        min_stake = self.exchange.get_min_pair_stake_amount(trade.pair, current_rate, -0.1)
        max_stake = self.exchange.get_max_pair_stake_amount(trade.pair, current_rate)
        stake_available = min(
            self.wallets.get_available_stake_amount(),
            self._prop_remaining_headroom(trade.pair),
        )
        stake_amount, order_tag = self.strategy._adjust_trade_position_internal(
            trade=trade,
            current_time=current_time,
            current_rate=current_rate,
            current_profit=current_profit,
            min_stake=min_stake,
            max_stake=min(max_stake, stake_available),
            current_entry_rate=current_rate,
            current_exit_rate=current_rate,
            current_entry_profit=current_profit,
            current_exit_profit=current_profit,
        )

        if stake_amount is not None and stake_amount > 0.0:
            if self._is_entry_paused():
                self.prop_budget_denials["paused_aborted_dca"] += 1
                return trade
            prop_headroom = self._prop_remaining_headroom(trade.pair)
            if stake_amount > prop_headroom:
                stake_amount = prop_headroom
            if min_stake and stake_amount < min_stake:
                self.prop_budget_denials["dca_below_min_stake"] += 1
                return trade
            ok, reason = self._prop_budget_allows(trade.pair, stake_amount)
            if not ok:
                self.prop_budget_denials[f"dca_{reason}"] += 1
                return trade

            check_adjust_entry = True
            if self.strategy.max_entry_position_adjustment > -1:
                check_adjust_entry = (
                    trade.nr_of_successful_entries <= self.strategy.max_entry_position_adjustment
                )
            if check_adjust_entry:
                pos_trade = self._enter_trade(
                    trade.pair,
                    row,
                    "short" if trade.is_short else "long",
                    stake_amount,
                    trade,
                    entry_tag1=order_tag,
                )
                if pos_trade is not None:
                    self.wallets.update()
                    return pos_trade

        if stake_amount is not None and stake_amount < 0.0:
            from freqtrade.exchange import amount_to_contract_precision
            from freqtrade.util import FtPrecise
            from freqtrade.enums import ExitCheckTuple

            amount = amount_to_contract_precision(
                abs(
                    float(
                        FtPrecise(stake_amount)
                        * FtPrecise(trade.amount)
                        / FtPrecise(trade.stake_amount)
                    )
                ),
                trade.amount_precision,
                self.precision_mode,
                trade.contract_size,
            )
            if amount == 0.0:
                return trade
            remaining = (trade.amount - amount) * current_rate
            if min_stake and remaining != 0 and remaining < min_stake:
                return trade
            exit_ = ExitCheckTuple(ExitType.PARTIAL_EXIT, order_tag)
            pos_trade = self._get_exit_for_signal(trade, row, exit_, current_time, amount)
            if pos_trade is not None:
                order = pos_trade.orders[-1]
                self._process_exit_order(order, pos_trade, current_time, row, trade.pair)
                return pos_trade

        return trade

    def _try_close_open_order(self, order, trade: LocalTrade, current_date: datetime, row: tuple) -> bool:
        filled = super()._try_close_open_order(order, trade, current_date, row)
        if filled:
            dt_utc = _to_utc(current_date)
            if dt_utc > self.prop_latest_activity_dt:
                self.prop_latest_activity_dt = dt_utc
        return filled

    def _process_exit_order(self, order, trade: LocalTrade, current_time: datetime, row: tuple, pair: str):
        was_open = trade.is_open
        super()._process_exit_order(order, trade, current_time, row, pair)
        if not order.ft_is_open and order.filled:
            if was_open and not trade.is_open:
                self.prop_closed_records.append(_localtrade_to_record(trade))
            self._recompute_ledger(current_time)

    def backtest_loop(
        self,
        row: tuple,
        pair: str,
        current_time: datetime,
        trade_dir: str | None,
        can_enter: bool,
    ) -> str | None:
        # Evaluate guard on previous det tick & perform 00:00 UTC rollover BEFORE overwriting bar cache
        if self._last_eval_det_ts is not None and current_time != self._last_eval_det_ts:
            self._on_tick_rollover_and_guard(self._last_eval_det_ts, eval_drawdown=True)
            self._last_eval_det_ts = current_time
        elif self._last_eval_det_ts is None:
            self._last_eval_det_ts = current_time
        self._on_tick_rollover_and_guard(current_time, eval_drawdown=False)
        self.prop_latest_bars[pair] = (row[OPEN_IDX], row[HIGH_IDX], row[LOW_IDX], row[CLOSE_IDX])

        if self.prop_halted or (self._stop_on_pass and self.prop_passed):
            return None

        effective_can_enter = can_enter and (not self._is_entry_paused())
        if effective_can_enter and self.prop_dynamic_shield_active:
            reduced_slots = max(1, int(self.config.get("max_open_trades", 1)) - 1)
            if len(LocalTrade.bt_trades_open) >= reduced_slots:
                effective_can_enter = False
        res = super().backtest_loop(row, pair, current_time, trade_dir, effective_can_enter)
        if LocalTrade.bt_trades_open:
            # Update bar cache if pair stayed open
            self.prop_latest_bars[pair] = (row[OPEN_IDX], row[HIGH_IDX], row[LOW_IDX], row[CLOSE_IDX])
        return res

    @phase_checkpoint
    def run_cached_cohort(
        self,
        ohclv_lists: dict[str, list[tuple]],
        analyzed_dfs: dict[str, pd.DataFrame],
        start_date: datetime,
        end_date: datetime,
    ) -> dict[str, Any]:
        """Run a fresh One-Step challenge cohort reusing pre-computed indicators."""
        self.reset_backtest(self.enable_protections)
        self._reset_prop_state(start_date)
        self._hist_pairlists._last_bucket = None
        self.wallets.update()
        # Restore dataprovider analyzed dataframe cache so custom_exit/adjust_trade_position work
        for pair, df_an in analyzed_dfs.items():
            self.dataprovider._set_cached_df(
                pair, self.timeframe, df_an, self.config["candle_type_def"]
            )

        for (
            current_time,
            pair,
            row,
            is_last_row,
            trade_dir,
        ) in self.time_pair_generator(start_date, end_date, list(ohclv_lists.keys()), ohclv_lists):
            self.backtest_loop(row, pair, current_time, trade_dir, not is_last_row)
            if (self._stop_on_halt and self.prop_halted) or (
                self._stop_on_pass and self.prop_passed
            ):
                break

        if not self.prop_halted and not self.prop_passed:
            self.handle_left_open(LocalTrade.bt_trades_open_pp, data=ohclv_lists)
            self.wallets.update()
            self._recompute_ledger(end_date)

        final_eq = float(self.wallets.get_total(self.config["stake_currency"]))
        outcome = (
            "PASS"
            if self.prop_passed
            else (
                "FAIL_INACTIVITY_30D"
                if self.prop_inactivity_breach
                else (
                    "FAIL_OFFICIAL"
                    if (
                        self.prop_official_breach_close
                        or (self._strict_wick_guard and self.prop_official_breach_wick)
                    )
                    else ("HALT_INTERNAL_GUARD" if self.prop_halted else "OPEN_END")
                )
            )
        )
        end_ref = self.prop_pass_dt or self.prop_halt_dt or _to_utc(end_date)
        days_elapsed = max(0.0, (end_ref - self.prop_phase_start).total_seconds() / 86400.0)
        return {
            "cohort_start": self.prop_phase_start.strftime("%Y-%m-%d"),
            "outcome": outcome,
            "days_elapsed": round(days_elapsed, 2),
            "pass_dt": str(self.prop_pass_dt) if self.prop_pass_dt else None,
            "halt_dt": str(self.prop_halt_dt) if self.prop_halt_dt else None,
            "first_enter_attempt_ts": (
                self.prop_first_enter_attempt_ts.isoformat()
                if self.prop_first_enter_attempt_ts
                else None
            ),
            "flat_unpaused_midnights": sorted(self.prop_flat_unpaused_midnights),
            "final_equity": round(final_eq, 2),
            "net_pnl": round(final_eq - float(self._prop_profile["initial_balance"]), 2),
            "counted_profit": round(self._cached_ledger["counted_profit"], 2),
            "qualifying_days": len(self._cached_ledger["qualifying_days"]),
            "total_trades": len(LocalTrade.bt_trades),
            "max_daily_dd_close": round(self.prop_max_daily_dd_close, 2),
            "max_daily_dd_wick": round(self.prop_max_daily_dd_wick, 2),
            "max_overall_dd_close": round(self.prop_max_overall_dd_close, 2),
            "max_overall_dd_wick": round(self.prop_max_overall_dd_wick, 2),
            "official_breach_close": self.prop_official_breach_close,
            "official_breach_wick": self.prop_official_breach_wick,
            "official_daily_breach_wick_bars": self.prop_official_daily_breach_wick_bars,
            "official_overall_breach_wick_bars": self.prop_official_overall_breach_wick_bars,
            "funded_margin_gt_25pct_bars": self.prop_funded_margin_gt_25pct_bars,
            "funded_notional_gt_2x_bars": self.prop_funded_notional_gt_2x_bars,
            "max_slot_notional_observed": round(self.prop_max_slot_notional_observed, 2),
            "max_aggregate_notional_observed": round(self.prop_max_aggregate_notional_observed, 2),
            "shock_1coin_35_breach_bars": self.prop_shock_1coin_35_breach_bars,
            "shock_1coin_50_breach_bars": self.prop_shock_1coin_50_breach_bars,
            "shock_2coin_25_breach_bars": self.prop_shock_2coin_25_breach_bars,
            "min_post_shock_50_headroom_usd": round(self.prop_min_post_shock_50_headroom_usd, 2),
            "internal_wick_touch": self.prop_internal_wick_touch,
            "halt_reason": self.prop_halt_reason,
            "keepalive_count": len(self.prop_keepalive_events),
            "macro_pause_bars": self.prop_macro_pause_bars,
            "rules_pause_bars": self.prop_rules_pause_bars,
            "dynamic_shield_bars": self.prop_dynamic_shield_bars,
            "budget_denials": dict(self.prop_budget_denials),
        }


def build_bt_config(
    config_path: Path,
    universe_pairs: list[str],
    timerange_str: str,
    timeframe_detail: str = "",
    start_iso: str = "2023-11-15",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_cfg = json.loads(config_path.read_text())
    pg = raw_cfg.setdefault("prop_guard", {})
    if not pg.get("account_id"):
        pg["account_id"] = "backtest_sim_acct"
    if not pg.get("challenge_id"):
        pg["challenge_id"] = "backtest_sim_chal"
    clean_start = start_iso.replace("-", "")
    start_ts_iso = f"{clean_start[:4]}-{clean_start[4:6]}-{clean_start[6:8]}T00:00:00+00:00"
    if not pg.get("challenge_start_utc"):
        pg["challenge_start_utc"] = start_ts_iso
    if not pg.get("phase_start_utc"):
        pg["phase_start_utc"] = pg["challenge_start_utc"]
    prop_profile = profile_from_config(raw_cfg)
    prop_policy = copy.deepcopy(raw_cfg["prop_risk_budget"])

    cfg = Configuration.from_files([str(config_path)])
    cfg["runmode"] = RunMode.BACKTEST
    cfg["dry_run"] = True
    cfg["exchange"]["demo_trading"] = False
    cfg["exchange"]["key"] = ""
    cfg["exchange"]["secret"] = ""
    cfg["pairlists"] = [{"method": "StaticPairList"}]
    cfg["exchange"]["pair_whitelist"] = list(universe_pairs)
    cfg["timerange"] = timerange_str
    cfg["enable_dynamic_pairlist"] = False  # enabled post-init on HistoricalPairListAdapter
    cfg["export"] = "none"
    cfg["backtest_cache"] = "none"
    if timeframe_detail:
        cfg["timeframe_detail"] = timeframe_detail
    return cfg, prop_profile, prop_policy


def load_btc_close_map(datadir: Path) -> dict[int, float]:
    df = history.load_pair_history(
        pair="BTC/USDT:USDT",
        timeframe="5m",
        datadir=datadir,
        candle_type="futures",
    )
    if df.empty:
        return {}
    ts_secs = df["date"].dt.as_unit("s").astype("int64").tolist()
    closes = df["close"].astype(float).tolist()
    return dict(zip(ts_secs, closes))


def _resolve_cache_dir(args: argparse.Namespace, universe: list[str]) -> Path:
    if args.cache_dir:
        base = Path(args.cache_dir)
    else:
        scratch_root = Path("/Users/davide/.gemini/antigravity/brain/f09e073b-7450-46db-bb0e-727f475c8468/scratch")
        root = scratch_root / "analyzed_cache" if scratch_root.exists() else REPO / ".backtest_cache"
        u_hash = hashlib.sha1(",".join(sorted(universe)).encode()).hexdigest()[:8]
        strat_mtime = int((REPO / "user_data" / "strategies" / "NostalgiaForInfinityX8_Prop.py").stat().st_mtime)
        base = root / f"{args.start}_{args.end}_{len(universe)}_{u_hash}_{strat_mtime}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _prepare_universe_cached(
    bt: PropParityBacktesting,
    cfg: dict[str, Any],
    universe: list[str],
    cache_dir: Path,
    chunk_size: int = 8,
    cache_callback_rows: bool = False,
    analyzed_lru_max: int = 12,
) -> tuple[dict[str, CompactOhlcvArray], dict[str, pd.DataFrame], datetime, datetime]:
    """Chunked 8GB-RAM-safe indicator preprocessing with SSD feather+npz cache and adaptive LRU."""
    _install_fast_custom_data_index()
    candle_def = bt.config["candle_type_def"]
    strat = bt.strategylist[0]
    bt._set_strategy(strat)

    # Adjust timerange identically to Backtesting.load_bt_data() using anchor pair (BTC or first pair)
    anchor = "BTC/USDT:USDT" if "BTC/USDT:USDT" in universe else universe[0]
    anchor_df = history.load_pair_history(
        pair=anchor,
        timeframe=bt.timeframe,
        datadir=cfg["datadir"],
        timerange=bt.timerange,
        startup_candles=bt.required_startup,
        data_format=cfg["dataformat_ohlcv"],
        candle_type=candle_def,
    )
    if not anchor_df.empty:
        min_anchor, _ = history.get_timerange({anchor: anchor_df})
        bt.timerange.adjust_start_if_necessary(
            timeframe_to_seconds(bt.timeframe), bt.required_startup, min_anchor
        )
    del anchor_df

    bt._load_bt_data_detail()
    manifest_path = cache_dir / "_manifest.json"
    prec_path = cache_dir / "_price_prec.pkl"
    cached_map = getattr(bt.dataprovider, "_DataProvider__cached_pairs", {})
    cached_bt_map = getattr(bt.dataprovider, "_DataProvider__cached_pairs_backtesting", {})

    ohclv_lists: dict[str, CompactOhlcvArray] = {}
    analyzed_paths: dict[str, Path] = {}

    if manifest_path.exists() and prec_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("universe") == universe and manifest.get("complete"):
            bt.price_pair_prec = pickle.loads(prec_path.read_bytes())
            bt.available_pairs = list(manifest["available_pairs"])
            min_date = pd.Timestamp(manifest["min_date"]).to_pydatetime()
            max_date = pd.Timestamp(manifest["max_date"]).to_pydatetime()
            for p, fnames in manifest["files"].items():
                ohclv_lists[p] = CompactOhlcvArray.load_npz(cache_dir / fnames["ohlcv"])
                analyzed_paths[p] = cache_dir / fnames["analyzed"]
            logger.info(
                "Loaded cached indicators for %d pairs (%s -> %s) from %s",
                len(ohclv_lists),
                min_date,
                max_date,
                cache_dir,
            )
    if not ohclv_lists:
        t_prep = time.time()
        min_date: datetime | None = None
        max_date: datetime | None = None
        file_index: dict[str, dict[str, str]] = {}
        bt.price_pair_prec = {}
        bt.available_pairs = []

        for idx in range(0, len(universe), max(1, chunk_size)):
            batch = universe[idx : idx + max(1, chunk_size)]
            data_batch = history.load_data(
                datadir=cfg["datadir"],
                pairs=batch,
                timeframe=bt.timeframe,
                timerange=bt.timerange,
                startup_candles=bt.required_startup,
                fail_without_data=False,
                data_format=cfg["dataformat_ohlcv"],
                candle_type=candle_def,
            )
            if not data_batch:
                continue
            for p in batch:
                if p in data_batch:
                    bt.price_pair_prec[p] = get_tick_size_over_time(data_batch[p])
                    bt.available_pairs.append(p)
            prep_batch = strat.advise_all_indicators(data_batch)
            for p, df_prep in prep_batch.items():
                if not df_prep.empty:
                    trimmed_dates = trim_dataframe(
                        df_prep[["date"]], bt.timerange, startup_candles=bt.required_startup
                    )["date"]
                    if not trimmed_dates.empty:
                        d0 = trimmed_dates.iloc[0].to_pydatetime()
                        d1 = trimmed_dates.iloc[-1].to_pydatetime()
                        if min_date is None or d0 < min_date:
                            min_date = d0
                        if max_date is None or d1 > max_date:
                            max_date = d1

            ohl_batch = bt._get_ohlcv_as_lists(dict(prep_batch))
            for p, rows in ohl_batch.items():
                if not rows:
                    continue
                pair_s = p.replace("/", "_").replace(":", "_")
                arr = CompactOhlcvArray.from_rows(rows)
                ohl_name = f"ohlcv_{pair_s}.npz"
                an_name = f"an_{pair_s}.feather"
                arr.save_npz(cache_dir / ohl_name)
                ohclv_lists[p] = arr
                df_an_tuple = cached_map.pop((p, bt.timeframe, candle_def), None)
                if df_an_tuple is not None:
                    df_an_tuple[0].reset_index(drop=True).to_feather(
                        cache_dir / an_name, compression="zstd", compression_level=3
                    )
                    analyzed_paths[p] = cache_dir / an_name
                    file_index[p] = {"ohlcv": ohl_name, "analyzed": an_name}

            # Evict chunk informative pairs from backtesting cache while keeping BTC informative warm
            for k in list(cached_bt_map.keys()):
                if k[0] != "BTC/USDT:USDT":
                    cached_bt_map.pop(k, None)
            del data_batch, prep_batch, ohl_batch
            gc.collect()
            logger.info(
                "Preprocessed batch %d/%d pairs (elapsed %.1fs)",
                min(idx + max(1, chunk_size), len(universe)),
                len(universe),
                time.time() - t_prep,
            )

        cached_bt_map.clear()
        cached_map.clear()
        gc.collect()
        if min_date is None or max_date is None:
            raise OperationalException("No preprocessed data in selected timerange.")
        prec_path.write_bytes(pickle.dumps(bt.price_pair_prec))
        manifest_path.write_text(
            json.dumps(
                {
                    "complete": True,
                    "universe": universe,
                    "available_pairs": bt.available_pairs,
                    "min_date": pd.Timestamp(min_date).isoformat(),
                    "max_date": pd.Timestamp(max_date).isoformat(),
                    "files": file_index,
                }
            )
        )

    # Install fast column-pruned LRU wrapper on dataprovider.get_analyzed_dataframe
    orig_get_analyzed = bt.dataprovider.get_analyzed_dataframe
    needed_callback_cols = _discover_callback_columns()
    fast_table_lru: OrderedDict[str, _FastAnalyzedTable] = OrderedDict()
    analyzed_lru: OrderedDict[str, tuple[pd.DataFrame, datetime]] = OrderedDict()
    callback_snapshots: OrderedDict[tuple, tuple[pd.DataFrame, datetime]] = OrderedDict()

    def _lazy_get_analyzed_dataframe(pair: str, timeframe: str):
        if timeframe == bt.timeframe and pair in analyzed_paths:
            if not cache_callback_rows:
                if pair in fast_table_lru:
                    fast_table_lru.move_to_end(pair)
                    tbl = fast_table_lru[pair]
                else:
                    tbl = _FastAnalyzedTable.load_feather(
                        analyzed_paths[pair], needed_cols=needed_callback_cols
                    )
                    fast_table_lru[pair] = tbl
                    while len(fast_table_lru) > analyzed_lru_max:
                        fast_table_lru.popitem(last=False)
                slice_map = getattr(bt.dataprovider, "_DataProvider__slice_index", {})
                max_idx = slice_map.get(pair)
                length = tbl.n_rows if max_idx is None else min(int(max_idx), tbl.n_rows)
                return _FastAnalyzedFrame(tbl, max(0, length)), tbl.last_date

            cur_map = getattr(bt.dataprovider, "_DataProvider__cached_pairs", {})
            key = (pair, timeframe, candle_def)
            if pair in analyzed_lru:
                analyzed_lru.move_to_end(pair)
                df_full, la = analyzed_lru[pair]
                if key not in cur_map:
                    cur_map[key] = (df_full, la)
            else:
                df_full = pd.read_feather(analyzed_paths[pair])
                la = (
                    df_full["date"].iloc[-1].to_pydatetime()
                    if not df_full.empty
                    else datetime.fromtimestamp(0, tz=UTC)
                )
                analyzed_lru[pair] = (df_full, la)
                cur_map[key] = (df_full, la)
                while len(analyzed_lru) > analyzed_lru_max:
                    old_p, _ = analyzed_lru.popitem(last=False)
                    cur_map.pop((old_p, timeframe, candle_def), None)
                    for snapshot_key in list(callback_snapshots):
                        if snapshot_key[0] == old_p:
                            del callback_snapshots[snapshot_key]
        result = orig_get_analyzed(pair, timeframe)
        if not cache_callback_rows or timeframe != bt.timeframe or result[0].empty:
            return result
        from scripts.prop_callback_snapshot import CallbackSnapshot
        source = getattr(bt.dataprovider, "_DataProvider__cached_pairs", {}).get(
            (pair, timeframe, candle_def)
        )
        boundary = getattr(bt.dataprovider, "_DataProvider__slice_index", {}).get(pair)
        snapshot_key = (pair, timeframe, id(source[0]) if source else None, boundary)
        if snapshot_key not in callback_snapshots:
            callback_snapshots[snapshot_key] = (CallbackSnapshot(result[0]), result[1])
            while len(callback_snapshots) > analyzed_lru_max:
                callback_snapshots.popitem(last=False)
        else:
            callback_snapshots.move_to_end(snapshot_key)
        return callback_snapshots[snapshot_key]

    bt.dataprovider.get_analyzed_dataframe = _lazy_get_analyzed_dataframe
    return ohclv_lists, {}, min_date, max_date


def run_suite(args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    config_path = Path(args.config)
    schedule = None
    schedule_meta_summary: dict[str, Any] = {}
    if args.pairlist_schedule:
        meta = json.loads(Path(args.pairlist_schedule).read_text())
        if meta.get("zero_lookahead") is False and not getattr(args, "allow_research_schedule", False):
            raise OperationalException(
                "Historical schedule is unverified: resolve instrument classification, "
                "market-cap identity and observation-vintage evidence before benchmarking "
                "(or pass --allow-research-schedule for explicit research sweeps)."
            )
        if meta.get("zero_lookahead") is False:
            logger.warning(
                "Running research schedule (%s): limitations=%s",
                meta.get("validation_status"),
                meta.get("limitations"),
            )
        schedule_meta_summary = {
            "path": str(args.pairlist_schedule),
            "zero_lookahead": meta.get("zero_lookahead"),
            "validation_status": meta.get("validation_status"),
            "mcap_mode": meta.get("mcap_mode"),
            "anti_survivorship": meta.get("anti_survivorship"),
            "include_archived_delisted": meta.get("include_archived_delisted"),
            "archived_delisted_included": meta.get("archived_delisted_included", []),
            "coverage_diagnostics": meta.get("coverage_diagnostics"),
            "limitations": meta.get("limitations", []),
        }
        schedule = meta["schedule"]
        if args.start >= "2024-11-15" and "ever_whitelisted_2y" in meta:
            universe = list(meta["ever_whitelisted_2y"])
        elif args.start.startswith("2023-11"):
            universe = list(meta["ever_whitelisted_3y"])
        else:
            universe = list(meta["ever_whitelisted_all"])
        if args.limit_pairs > 0:
            has_btc = "BTC/USDT:USDT" in universe
            universe = universe[: args.limit_pairs]
            if has_btc and "BTC/USDT:USDT" not in universe:
                universe = ["BTC/USDT:USDT"] + universe[:-1]
    elif args.pairs:
        universe = list(args.pairs)
    else:
        raise ValueError("Specify --pairlist-schedule or --pairs")

    timerange_str = f"{args.start.replace('-', '')}-{args.end.replace('-', '')}"
    cfg, profile, policy = build_bt_config(
        config_path,
        universe,
        timerange_str,
        timeframe_detail=args.timeframe_detail,
        start_iso=args.start,
    )
    btc_map = load_btc_close_map(cfg["datadir"])

    bt = PropParityBacktesting(
        cfg,
        universe_pairs=universe,
        pairlist_schedule=schedule,
        prop_profile=profile,
        prop_budget_policy=policy,
        btc_close_map=btc_map,
        extra_slippage_bps_roundtrip=args.slippage_bps,
        stop_on_halt=args.stop_on_halt,
        stop_on_pass=args.stop_on_pass,
        strict_wick_guard=args.strict_wick_guard,
    )

    ram_mode = getattr(args, "ram_profile", "auto")
    total_ram_gb = _detect_total_ram_gb()
    high_ram = ram_mode == "high" or (ram_mode == "auto" and total_ram_gb >= 15.0)
    analyzed_lru_max = 256 if high_ram else 12
    bt._detail_max_cached = 256 if high_ram else 36
    logger.info(
        "RAM profile=%s (detected %.1f GB): analyzed_lru=%d detail_lru=%d",
        "high" if high_ram else "normal",
        total_ram_gb,
        analyzed_lru_max,
        bt._detail_max_cached,
    )

    if getattr(args, "detail_array_cache", ""):
        revision_files = [Path(__file__), REPO / "scripts" / "prop_detail_array_cache.py"]
        revision_files += sorted((REPO / "freqtrade" / "data").rglob("*.py"))
        revision = hashlib.sha256(b"".join(p.read_bytes() for p in revision_files)
                                  + f"{pd.__version__}:{np.__version__}".encode()).hexdigest()
        bt._detail_array_cache = DetailArrayCache(args.detail_array_cache, revision)
    configure_checkpoints(bt, args, REPO, cfg["datadir"])
    cache_dir = _resolve_cache_dir(args, universe)
    ohclv_lists, analyzed_dfs, min_date, max_date = _prepare_universe_cached(
        bt,
        cfg,
        universe,
        cache_dir,
        chunk_size=args.chunk_size,
        cache_callback_rows=getattr(args, "cache_callback_rows", False),
        analyzed_lru_max=analyzed_lru_max,
    )
    logger.info(
        "Ready %d pairs (%s -> %s, %d days) in %.1fs",
        len(ohclv_lists),
        min_date,
        max_date,
        (max_date - min_date).days,
        time.time() - t0,
    )

    delisted_fallbacks = {
        "synthetic_market_pairs": list(bt.prop_synthetic_market_pairs),
        "synthetic_leverage_tier_pairs": list(bt.prop_synthetic_leverage_tier_pairs),
        "synthetic_mark_pairs": list(bt.prop_synthetic_mark_pairs),
        "synthetic_funding_pairs": list(bt.prop_synthetic_funding_pairs),
    }

    if getattr(args, "grid_json", ""):
        return run_grid_sweep(
            bt,
            cfg,
            profile,
            ohclv_lists,
            analyzed_dfs,
            min_date,
            max_date,
            args,
            schedule_meta=schedule_meta_summary,
            delisted_fallbacks=delisted_fallbacks,
        )

    selected_cands = getattr(args, "candidates", None) or []
    applied_candidate = None
    if selected_cands:
        cand_map = {c["id"]: c for c in DEFAULT_MONEY_GRID}
        if len(selected_cands) != 1 or selected_cands[0] not in cand_map:
            raise ValueError(
                f"For --suite-json / single run, --candidates must specify exactly 1 valid candidate ID from DEFAULT_MONEY_GRID (got {selected_cands})."
            )
        applied_candidate = cand_map[selected_cands[0]]
        _apply_candidate_money_params(bt, cfg, profile, applied_candidate)

    if args.suite_json:
        scenarios = [
            {"name": "base_0bps", "slippage_bps": 0.0, "strict_wick_guard": False},
            {"name": "friction_10bps_rt", "slippage_bps": 10.0, "strict_wick_guard": False},
            {"name": "stress_28bps_rt", "slippage_bps": 28.0, "strict_wick_guard": False},
            {"name": "strict_wick_guard", "slippage_bps": 10.0, "strict_wick_guard": True},
            {"name": "stress_28bps_strict_wick", "slippage_bps": 28.0, "strict_wick_guard": True},
        ]
        suite_meta = {
            "schema_version": 3,
            "start": str(min_date),
            "end": str(max_date),
            "days": (max_date - min_date).days,
            "universe_size": len(universe),
            "timeframe_detail": args.timeframe_detail or "5m",
            "cohort_step_days": args.cohort_step_days,
            "applied_candidate": applied_candidate,
            "schedule_meta": schedule_meta_summary,
            "delisted_fallbacks": delisted_fallbacks,
            "preprocess_sec": round(time.time() - t0, 1),
        }
        suite_out: dict[str, Any] = {
            "meta": suite_meta,
            "scenarios": {},
        }
        suite_path = Path(args.suite_json)
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        if suite_path.exists() and not getattr(args, "overwrite_suite", False):
            try:
                prev = json.loads(suite_path.read_text())
                prev_meta = prev.get("meta", {})
                same_run = all(
                    prev_meta.get(k) == suite_meta.get(k)
                    for k in ("schema_version", "start", "end", "universe_size", "timeframe_detail", "cohort_step_days", "applied_candidate")
                )
                if same_run and isinstance(prev.get("scenarios"), dict):
                    suite_out["scenarios"] = prev["scenarios"]
                    logger.info(
                        "Resuming suite %s: %d scenario(s) already completed (%s)",
                        suite_path.name,
                        len(suite_out["scenarios"]),
                        ", ".join(suite_out["scenarios"].keys()),
                    )
            except Exception as exc:
                logger.warning("Could not resume existing suite %s: %s", suite_path, exc)

        for sc in scenarios:
            if sc["name"] in suite_out["scenarios"]:
                print(f"\n=== SKIPPING COMPLETED SCENARIO: {sc['name']} ===", flush=True)
                continue
            print(f"\n=== RUNNING SCENARIO: {sc['name']} ===", flush=True)
            sc_res = _execute_scenario(
                bt,
                cfg,
                profile,
                ohclv_lists,
                analyzed_dfs,
                min_date,
                max_date,
                universe_size=len(universe),
                name=sc["name"],
                slippage_bps=sc["slippage_bps"],
                strict_wick_guard=sc["strict_wick_guard"],
                cohort_step_days=args.cohort_step_days,
            )
            suite_out["scenarios"][sc["name"]] = sc_res
            suite_out["total_runtime_sec"] = round(time.time() - t0, 1)
            tmp_path = suite_path.with_suffix(suite_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(suite_out, indent=2))
            tmp_path.replace(suite_path)
            gc.collect()
        return suite_out

    res = _execute_scenario(
        bt,
        cfg,
        profile,
        ohclv_lists,
        analyzed_dfs,
        min_date,
        max_date,
        universe_size=len(universe),
        name="custom",
        slippage_bps=args.slippage_bps,
        strict_wick_guard=args.strict_wick_guard,
        cohort_step_days=args.cohort_step_days,
    )
    out = {
        "continuous": res["continuous"],
        "cohorts": res["cohorts"],
        "cohorts_summary": res["cohorts_summary"],
        "runtime_sec": round(time.time() - t0, 1),
    }
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(out, indent=2))
    return out


def _summarize_trades(trades: list[LocalTrade]) -> dict[str, Any]:
    by_tag: dict[str, dict[str, Any]] = {}
    by_exit: dict[str, dict[str, Any]] = {}
    by_pair: dict[str, dict[str, Any]] = {}
    by_month: dict[str, dict[str, Any]] = {}
    durations_h: list[float] = []
    trades_with_dca = 0
    total_dca_fills = 0
    dca_counts: list[int] = []
    dca_sizes: list[float] = []
    for t in trades:
        pnl = float(t.close_profit_abs or 0.0)
        win = 1 if pnl > 0 else 0
        entries = int(getattr(t, "nr_of_successful_entries", 1) or 1)
        dca_counts.append(max(0, entries - 1))
        filled_entries = t.select_filled_orders(t.entry_side)
        dca_sizes.extend(float(o.safe_filled) * float(o.safe_price) for o in filled_entries[1:])
        if entries > 1:
            trades_with_dca += 1
            total_dca_fills += entries - 1
        o_dt = _to_utc(getattr(t, "open_date_utc", None) or getattr(t, "open_date", None))
        c_dt = _to_utc(getattr(t, "close_date_utc", None) or getattr(t, "close_date", None))
        if o_dt and c_dt and c_dt >= o_dt:
            durations_h.append((c_dt - o_dt).total_seconds() / 3600.0)
        month_key = c_dt.strftime("%Y-%m") if c_dt else "open"
        for bucket, key in (
            (by_tag, str(t.enter_tag or "unknown")),
            (by_exit, str(t.exit_reason or "unknown")),
            (by_pair, str(t.pair)),
            (by_month, month_key),
        ):
            rec = bucket.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
            rec["trades"] += 1
            rec["wins"] += win
            rec["pnl"] = round(rec["pnl"] + pnl, 2)
    top_pairs = dict(sorted(by_pair.items(), key=lambda kv: kv[1]["pnl"], reverse=True)[:15])
    worst_pairs = dict(sorted(by_pair.items(), key=lambda kv: kv[1]["pnl"])[:15])
    return {
        "avg_duration_hours": round(sum(durations_h) / len(durations_h), 2) if durations_h else 0.0,
        "median_duration_hours": round(statistics.median(durations_h), 2) if durations_h else 0.0,
        "max_duration_hours": round(max(durations_h), 2) if durations_h else 0.0,
        "trades_with_dca": trades_with_dca,
        "total_dca_fills": total_dca_fills,
        "max_dca_per_trade": max(dca_counts, default=0),
        "mean_dca_per_trade": statistics.mean(dca_counts) if dca_counts else 0.0,
        "dca_count_distribution": {str(n): dca_counts.count(n) for n in sorted(set(dca_counts))},
        "dca_notional_sizes_usdt": sorted(dca_sizes),
        "max_single_dca_notional_usdt": max(dca_sizes, default=0.0),
        "by_month": dict(sorted(by_month.items())),
        "by_enter_tag": dict(sorted(by_tag.items(), key=lambda kv: kv[1]["pnl"], reverse=True)),
        "by_exit_reason": dict(sorted(by_exit.items(), key=lambda kv: kv[1]["trades"], reverse=True)),
        "by_pair": dict(sorted(by_pair.items())),
        "top_15_pairs": top_pairs,
        "worst_15_pairs": worst_pairs,
        "unique_pairs_traded": len(by_pair),
    }


@phase_checkpoint
def _run_continuous_pass(
    bt: PropParityBacktesting,
    cfg: dict[str, Any],
    profile: dict[str, Any],
    ohclv_lists: dict[str, list[tuple]],
    analyzed_dfs: dict[str, pd.DataFrame],
    min_date: datetime,
    max_date: datetime,
    *,
    universe_size: int,
    name: str,
    slippage_bps: float,
    strict_wick_guard: bool,
    ignore_target_pause: bool,
) -> dict[str, Any]:
    """Run full-window continuous parity simulation (Evaluation or Non-Compounding Funded)."""
    bt._prop_profile = copy.deepcopy(profile)
    bt._stop_on_halt = False
    bt._stop_on_pass = False
    bt._ignore_target_pause = bool(ignore_target_pause)
    bt.reset_backtest(bt.enable_protections)
    bt._reset_prop_state(min_date)
    bt._hist_pairlists._last_bucket = None
    bt.wallets.update()
    for p, df_an in analyzed_dfs.items():
        bt.dataprovider._set_cached_df(p, bt.timeframe, df_an, bt.config["candle_type_def"])
    for ct, pair, row, is_last, tdir in bt.time_pair_generator(
        min_date, max_date, list(ohclv_lists.keys()), ohclv_lists
    ):
        bt.backtest_loop(row, pair, ct, tdir, not is_last)
        if bt.prop_halted:
            break
    if not bt.prop_halted:
        bt.handle_left_open(LocalTrade.bt_trades_open_pp, data=ohclv_lists)
        bt.wallets.update()
        bt._recompute_ledger(max_date)

    cont_eq = float(bt.wallets.get_total(cfg["stake_currency"]))
    # Include the final (possibly halted or partial) UTC day in withdrawal risk analysis.
    if not bt.prop_daily_curve or bt.prop_daily_curve[-1]["date"] != bt.prop_current_day:
        bt.prop_daily_curve.append({
            "date": bt.prop_current_day,
            "midnight_equity": cont_eq,
            "min_day_equity_wick": min(bt.prop_min_day_equity_wick, cont_eq),
            "partial_day": True,
            "open_trades": len(LocalTrade.bt_trades_open),
        })
    trades_sample = [
        {
            "pair": t.pair,
            "open_date": str(t.open_date_utc),
            "close_date": str(t.close_date_utc),
            "stake_amount": round(float(t.stake_amount or 0), 2),
            "max_stake": round(float(t.max_stake_amount or 0), 2),
            "profit_abs": round(float(t.close_profit_abs or 0), 2),
            "profit_pct": round(float(t.close_profit or 0) * 100.0, 2),
            "funding_fees": round(float(t.funding_fees or 0), 3),
            "enter_tag": t.enter_tag,
            "exit_reason": t.exit_reason,
            "entries": int(t.nr_of_successful_entries),
        }
        for t in LocalTrade.bt_trades
    ]
    return {
        "scenario": name,
        "mode": "funded_noncompounding" if ignore_target_pause else "evaluation_strict",
        "start": str(min_date),
        "end": str(max_date),
        "universe_size": universe_size,
        "slippage_bps_roundtrip": slippage_bps,
        "strict_wick_guard": strict_wick_guard,
        "effective_fee_per_side": round(float(bt.fee), 6),
        "halted": bt.prop_halted,
        "halt_reason": bt.prop_halt_reason,
        "halt_dt": str(bt.prop_halt_dt) if bt.prop_halt_dt else None,
        "passed_target": bt.prop_passed,
        "pass_dt": str(bt.prop_pass_dt) if bt.prop_pass_dt else None,
        "milestone_pass_dt": str(bt.prop_milestone_pass_dt) if bt.prop_milestone_pass_dt else None,
        "consistency_pause_days": len(bt.prop_consistency_pause_days),
        "final_equity": round(cont_eq, 2),
        "net_pnl": round(cont_eq - float(profile["initial_balance"]), 2),
        "counted_profit": round(bt._cached_ledger["counted_profit"], 2),
        "qualifying_days": len(bt._cached_ledger["qualifying_days"]),
        "total_trades": len(LocalTrade.bt_trades),
        "win_trades": sum(1 for t in LocalTrade.bt_trades if (t.close_profit_abs or 0) > 0),
        "total_funding_fees": round(sum(float(t.funding_fees or 0) for t in LocalTrade.bt_trades), 2),
        "max_daily_dd_close": round(bt.prop_max_daily_dd_close, 2),
        "max_daily_dd_wick": round(bt.prop_max_daily_dd_wick, 2),
        "max_overall_dd_close": round(bt.prop_max_overall_dd_close, 2),
        "max_overall_dd_wick": round(bt.prop_max_overall_dd_wick, 2),
        "max_month_dd_close": round(bt.prop_max_month_dd_close, 2),
        "max_month_dd_wick": round(bt.prop_max_month_dd_wick, 2),
        "month_max_dd_close_by_month": {
            k: round(v, 2) for k, v in sorted(bt.prop_month_max_dd_close_by_month.items())
        },
        "month_max_dd_wick_by_month": {
            k: round(v, 2) for k, v in sorted(bt.prop_month_max_dd_wick_by_month.items())
        },
        "official_breach_close": bt.prop_official_breach_close,
        "official_breach_wick": bt.prop_official_breach_wick,
        "official_daily_breach_wick_bars": bt.prop_official_daily_breach_wick_bars,
        "official_overall_breach_wick_bars": bt.prop_official_overall_breach_wick_bars,
        "funded_margin_gt_25pct_bars": bt.prop_funded_margin_gt_25pct_bars,
        "funded_notional_gt_2x_bars": bt.prop_funded_notional_gt_2x_bars,
        "lowcap_combined_gt_250_bars": None,
        "lowcap_evidence_status": "unverified_historical_classification_and_ticker_mapping",
        "max_slot_notional_observed": round(bt.prop_max_slot_notional_observed, 2),
        "max_aggregate_notional_observed": round(bt.prop_max_aggregate_notional_observed, 2),
        "shock_1coin_35_breach_bars": bt.prop_shock_1coin_35_breach_bars,
        "shock_1coin_50_breach_bars": bt.prop_shock_1coin_50_breach_bars,
        "shock_2coin_25_breach_bars": bt.prop_shock_2coin_25_breach_bars,
        "min_post_shock_50_headroom_usd": round(bt.prop_min_post_shock_50_headroom_usd, 2),
        "internal_wick_touch": bt.prop_internal_wick_touch,
        "keepalive_count": len(bt.prop_keepalive_events),
        "inactivity_30d_breach": bt.prop_inactivity_breach,
        "macro_pause_bars": bt.prop_macro_pause_bars,
        "rules_pause_bars": bt.prop_rules_pause_bars,
        "dynamic_shield_bars": bt.prop_dynamic_shield_bars,
        "budget_denials": dict(bt.prop_budget_denials),
        "funded_withdrawal_policies": (
            simulate_funded_withdrawals(
                bt.prop_daily_curve,
                initial_balance=float(profile["initial_balance"]),
                official_max_loss_pct=float(profile["official_max_loss_percent"]),
            )
            if ignore_target_pause
            else None
        ),
        "trade_analytics": _summarize_trades(LocalTrade.bt_trades),
        "daily_curve": bt.prop_daily_curve,
        "trades_head": trades_sample[:10],
        "trades_tail": trades_sample[-10:],
    }


def simulate_funded_withdrawals(
    daily_curve: list[dict[str, Any]],
    *,
    initial_balance: float = 5000.0,
    official_max_loss_pct: float = 0.08,
    payout_amount: float = 250.0,
    split_ratio: float = 0.80,
) -> dict[str, Any]:
    """Compare Funded continuous no-withdrawal vs Reset ($5,250->$5,000) vs Buffer ($5,450->$5,200)."""
    static_floor = initial_balance * (1.0 - official_max_loss_pct)

    def _run_policy(trigger_eq: float) -> dict[str, Any]:
        withdrawn_gross = 0.0
        payouts = 0
        first_payout_day: int | None = None
        min_eq = initial_balance
        breached_floor = False
        final_adj = initial_balance
        for idx, row in enumerate(daily_curve, start=1):
            raw_eq = float(row["midnight_equity"])
            adj_eq = raw_eq - withdrawn_gross
            wick_eq = float(row.get("min_day_equity_wick", raw_eq)) - withdrawn_gross
            min_eq = min(min_eq, adj_eq, wick_eq)
            final_adj = adj_eq
            if min(adj_eq, wick_eq) <= static_floor:
                breached_floor = True
                break
            while adj_eq >= trigger_eq and not row.get("partial_day", False):
                withdrawn_gross += payout_amount
                payouts += 1
                if first_payout_day is None:
                    first_payout_day = idx
                adj_eq = raw_eq - withdrawn_gross
                min_eq = min(min_eq, adj_eq)
                final_adj = adj_eq
        return {
            "trigger_equity_usdt": trigger_eq,
            "post_withdrawal_base_usdt": round(trigger_eq - payout_amount, 2),
            "payout_count": payouts,
            "withdrawn_gross_usdt": round(withdrawn_gross, 2),
            "withdrawn_net_trader_80pct_usdt": round(withdrawn_gross * split_ratio, 2),
            "days_to_first_payout": first_payout_day,
            "final_account_equity_usdt": round(final_adj, 2),
            "min_account_equity_usdt": round(min_eq, 2),
            "min_distance_to_static_floor_usdt": round(min_eq - static_floor, 2),
            "survived_static_floor": not breached_floor,
        }

    return {
        "method": "counterfactual_daily_equity_offset_not_execution_replay",
        "wick_coverage_complete": all("min_day_equity_wick" in r for r in daily_curve) and bool(daily_curve),
        "static_overall_floor_usdt": round(static_floor, 2),
        "reset_5250_to_5000": _run_policy(initial_balance + payout_amount),
        "buffer_5450_to_5200": _run_policy(initial_balance + 200.0 + payout_amount),
        "buffer_5650_to_5400": _run_policy(initial_balance + 400.0 + payout_amount),
    }


def _percentile(vals: list[float], pct: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(math.ceil((pct / 100.0) * len(s)) - 1)))
    return round(float(s[idx]), 1)


def run_sequential_program_cohorts(
    bt: PropParityBacktesting,
    ohclv_lists: dict[str, list[tuple]],
    analyzed_dfs: dict[str, pd.DataFrame],
    start_dates: list[datetime],
    max_date: datetime,
    *,
    initial_balance: float = 5000.0,
    one_step_daily_guard: float = 0.036,
    one_step_max_guard: float = 0.065,
    two_step_daily_guard: float = 0.043,
    two_step_max_guard: float = 0.080,
) -> dict[str, Any]:
    """Compare One-Step vs Two-Step sequential cohorts (Evaluation -> Funded -> 1st Payout +$250)."""
    saved_profile = copy.deepcopy(bt._prop_profile)
    saved_stop_halt = bt._stop_on_halt
    saved_stop_pass = bt._stop_on_pass
    saved_ignore = bt._ignore_target_pause
    bt._stop_on_halt = True
    bt._stop_on_pass = True
    bt._ignore_target_pause = False

    profs = build_program_profiles(
        initial_balance=initial_balance,
        one_step_daily_guard=one_step_daily_guard,
        one_step_max_guard=one_step_max_guard,
        two_step_daily_guard=two_step_daily_guard,
        two_step_max_guard=two_step_max_guard,
    )
    one_step_cohorts: list[dict[str, Any]] = []
    two_step_cohorts: list[dict[str, Any]] = []

    try:
        prev_os_anchor_dt: datetime | None = None
        prev_os_first_enter_dt: datetime | None = None
        prev_os_unpaused_days: set[str] = set()
        prev_os_eval: dict[str, Any] | None = None
        prev_os_funded: dict[str, Any] | None = None
        prev_os_row: dict[str, Any] | None = None

        prev_ts_anchor_dt: datetime | None = None
        prev_ts_first_enter_dt: datetime | None = None
        prev_ts_unpaused_days: set[str] = set()
        prev_ts_p1: dict[str, Any] | None = None
        prev_ts_row: dict[str, Any] | None = None

        for cohort_index, s_dt in enumerate(start_dates, 1):
            cohort_started = time.monotonic()
            s_utc = _to_utc(s_dt)
            s_day_str = s_utc.strftime("%Y-%m-%d")
            os_eval: dict[str, Any] | None = None
            logger.info("Sequential cohort %d/%d starting %s", cohort_index, len(start_dates), s_dt)
            # --- A. ONE-STEP SEQUENTIAL: Eval (+10%, 3d) -> Funded (+5% 1st Payout) ---
            if (
                prev_os_row is not None
                and prev_os_eval is not None
                and prev_os_anchor_dt is not None
                and prev_os_first_enter_dt is not None
                and s_utc <= prev_os_first_enter_dt
                and (prev_os_first_enter_dt - prev_os_anchor_dt) < timedelta(days=30)
                and s_day_str in prev_os_unpaused_days
                and prev_os_eval["outcome"] in ("PASS", "HALT_INTERNAL_GUARD", "FAIL_OFFICIAL")
            ):
                delta_days = (s_utc - prev_os_anchor_dt).total_seconds() / 86400.0
                os_q_days = (
                    round(max(0.0, float(prev_os_row["days_to_qualify"]) - delta_days), 2)
                    if prev_os_row["days_to_qualify"] is not None
                    else None
                )
                os_p_days = (
                    round(
                        os_q_days + float(prev_os_funded["days_elapsed"]),
                        2,
                    )
                    if (os_q_days is not None and prev_os_funded and prev_os_funded["outcome"] == "PASS")
                    else None
                )
                os_row = dict(prev_os_row)
                os_row["cohort_start"] = s_day_str
                os_row["days_to_qualify"] = os_q_days
                os_row["days_to_first_payout"] = os_p_days
                one_step_cohorts.append(os_row)
                os_eval_outcome = os_row["eval_outcome"]
            else:
                bt._prop_profile = copy.deepcopy(profs["one_step_eval"])
                os_eval = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, s_dt, max_date)
                os_funded = None
                os_days_to_payout = None
                if os_eval["outcome"] == "PASS" and bt.prop_pass_dt is not None:
                    f_start = _to_utc(bt.prop_pass_dt) + timedelta(minutes=5)
                    if f_start < _to_utc(max_date):
                        bt._prop_profile = copy.deepcopy(profs["one_step_funded"])
                        os_funded = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, f_start, max_date)
                        if os_funded["outcome"] == "PASS":
                            os_days_to_payout = round(os_eval["days_elapsed"] + os_funded["days_elapsed"], 2)
                os_row = {
                    "cohort_start": s_day_str,
                    "eval_outcome": os_eval["outcome"],
                    "days_to_qualify": os_eval["days_elapsed"] if os_eval["outcome"] == "PASS" else None,
                    "funded_1st_payout_outcome": os_funded["outcome"] if os_funded else None,
                    "days_to_first_payout": os_days_to_payout,
                    "max_daily_dd_wick": os_eval["max_daily_dd_wick"],
                    "max_overall_dd_wick": os_eval["max_overall_dd_wick"],
                    "official_daily_breach_wick_bars": os_eval["official_daily_breach_wick_bars"],
                    "official_overall_breach_wick_bars": os_eval["official_overall_breach_wick_bars"],
                    "shock_1coin_50_breach_bars": os_eval["shock_1coin_50_breach_bars"],
                }
                one_step_cohorts.append(os_row)
                os_eval_outcome = os_eval["outcome"]
                fe_iso = os_eval.get("first_enter_attempt_ts")
                prev_os_anchor_dt = s_utc
                prev_os_first_enter_dt = datetime.fromisoformat(fe_iso) if fe_iso else None
                prev_os_unpaused_days = set(os_eval.get("flat_unpaused_midnights") or [])
                prev_os_eval = os_eval
                prev_os_funded = os_funded
                prev_os_row = os_row

            # --- B. TWO-STEP SEQUENTIAL: Phase 1 (+10%, 4d, $200/d) -> Phase 2 (+5%, 4d, $100/d) -> Funded (+5%) ---
            if (
                prev_ts_row is not None
                and prev_ts_p1 is not None
                and prev_ts_anchor_dt is not None
                and prev_ts_first_enter_dt is not None
                and s_utc <= prev_ts_first_enter_dt
                and (prev_ts_first_enter_dt - prev_ts_anchor_dt) < timedelta(days=30)
                and s_day_str in prev_ts_unpaused_days
                and prev_ts_p1["outcome"] in ("PASS", "HALT_INTERNAL_GUARD", "FAIL_OFFICIAL")
            ):
                delta_days = (s_utc - prev_ts_anchor_dt).total_seconds() / 86400.0
                ts_row = dict(prev_ts_row)
                ts_row["cohort_start"] = s_day_str
                if ts_row["p1_days"] is not None:
                    ts_row["p1_days"] = round(max(0.0, float(prev_ts_row["p1_days"]) - delta_days), 2)
                if ts_row["days_to_qualify"] is not None and ts_row["p2_days"] is not None:
                    ts_row["days_to_qualify"] = round(float(ts_row["p1_days"]) + float(ts_row["p2_days"]), 2)
                if ts_row["days_to_first_payout"] is not None and prev_ts_row["days_to_qualify"] is not None:
                    funded_d = float(prev_ts_row["days_to_first_payout"]) - float(prev_ts_row["days_to_qualify"])
                    ts_row["days_to_first_payout"] = round(float(ts_row["days_to_qualify"]) + funded_d, 2)
                two_step_cohorts.append(ts_row)
                ts_outcome = ts_row["eval_outcome"]
            else:
                os_denials = (os_eval.get("budget_denials") or {}) if os_eval is not None else {}
                has_headroom_denial = any(
                    k.endswith(("operating_headroom_exhausted", "insufficient_operating_headroom_for_stress_bound"))
                    and v > 0
                    for k, v in os_denials.items()
                )
                can_reuse_os_as_p1 = (
                    os_eval is not None
                    and not has_headroom_denial
                    and os_eval["official_daily_breach_wick_bars"] == 0
                    and os_eval["official_overall_breach_wick_bars"] == 0
                    and os_eval["shock_1coin_50_breach_bars"] == 0
                    and (
                        os_eval["outcome"] in ("OPEN_END", "FAIL_INACTIVITY_30D")
                        or (
                            os_eval["outcome"] == "PASS"
                            and int(os_eval.get("qualifying_days", 0))
                            >= int(profs["two_step_phase1"]["min_trading_days"])
                        )
                    )
                )
                if can_reuse_os_as_p1:
                    ts_p1 = dict(os_eval)
                    bt.prop_pass_dt = (
                        datetime.fromisoformat(ts_p1["pass_dt"]) if ts_p1.get("pass_dt") else None
                    )
                else:
                    bt._prop_profile = copy.deepcopy(profs["two_step_phase1"])
                    ts_p1 = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, s_dt, max_date)
                ts_p2 = None
                ts_funded = None
                ts_days_to_qualify = None
                ts_days_to_payout = None
                ts_outcome = ts_p1["outcome"]
                if ts_p1["outcome"] == "PASS" and bt.prop_pass_dt is not None:
                    p2_start = _to_utc(bt.prop_pass_dt) + timedelta(minutes=5)
                    if p2_start < _to_utc(max_date):
                        bt._prop_profile = copy.deepcopy(profs["two_step_phase2"])
                        ts_p2 = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, p2_start, max_date)
                        ts_outcome = ts_p2["outcome"]
                        if ts_p2["outcome"] == "PASS" and bt.prop_pass_dt is not None:
                            ts_days_to_qualify = round(ts_p1["days_elapsed"] + ts_p2["days_elapsed"], 2)
                            f_start = _to_utc(bt.prop_pass_dt) + timedelta(minutes=5)
                            if f_start < _to_utc(max_date):
                                bt._prop_profile = copy.deepcopy(profs["two_step_funded"])
                                ts_funded = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, f_start, max_date)
                                if ts_funded["outcome"] == "PASS":
                                    ts_days_to_payout = round(ts_days_to_qualify + ts_funded["days_elapsed"], 2)
                    else:
                        ts_outcome = "OPEN_END"
                ts_row = {
                    "cohort_start": s_day_str,
                    "p1_outcome": ts_p1["outcome"],
                    "p1_days": ts_p1["days_elapsed"] if ts_p1["outcome"] == "PASS" else None,
                    "p2_outcome": ts_p2["outcome"] if ts_p2 else None,
                    "p2_days": ts_p2["days_elapsed"] if (ts_p2 and ts_p2["outcome"] == "PASS") else None,
                    "eval_outcome": ts_outcome,
                    "days_to_qualify": ts_days_to_qualify,
                    "funded_1st_payout_outcome": ts_funded["outcome"] if ts_funded else None,
                    "days_to_first_payout": ts_days_to_payout,
                    "max_daily_dd_wick": max(ts_p1["max_daily_dd_wick"], ts_p2["max_daily_dd_wick"] if ts_p2 else 0.0),
                    "max_overall_dd_wick": max(ts_p1["max_overall_dd_wick"], ts_p2["max_overall_dd_wick"] if ts_p2 else 0.0),
                    "official_daily_breach_wick_bars": ts_p1["official_daily_breach_wick_bars"] + (ts_p2["official_daily_breach_wick_bars"] if ts_p2 else 0),
                    "official_overall_breach_wick_bars": ts_p1["official_overall_breach_wick_bars"] + (ts_p2["official_overall_breach_wick_bars"] if ts_p2 else 0),
                    "shock_1coin_50_breach_bars": ts_p1["shock_1coin_50_breach_bars"] + (ts_p2["shock_1coin_50_breach_bars"] if ts_p2 else 0),
                }
                two_step_cohorts.append(ts_row)
                fe_ts_iso = ts_p1.get("first_enter_attempt_ts")
                prev_ts_anchor_dt = s_utc
                prev_ts_first_enter_dt = datetime.fromisoformat(fe_ts_iso) if fe_ts_iso else None
                prev_ts_unpaused_days = set(ts_p1.get("flat_unpaused_midnights") or [])
                prev_ts_p1 = ts_p1
                prev_ts_row = ts_row
            logger.info(
                "Sequential cohort %d/%d completed in %.1fs: one-step=%s two-step=%s",
                cohort_index, len(start_dates), time.monotonic() - cohort_started,
                os_eval_outcome, ts_outcome,
            )
    finally:
        bt._prop_profile = saved_profile
        bt._stop_on_halt = saved_stop_halt
        bt._stop_on_pass = saved_stop_pass
        bt._ignore_target_pause = saved_ignore

    def _summarize_seq(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        q_days = [float(r["days_to_qualify"]) for r in rows if r["days_to_qualify"] is not None]
        p_days = [float(r["days_to_first_payout"]) for r in rows if r["days_to_first_payout"] is not None]
        return {
            "total_cohorts": n,
            "qualified_count": len(q_days),
            "qualify_rate_pct": round(100.0 * len(q_days) / n, 1) if n else 0.0,
            "first_payout_count": len(p_days),
            "first_payout_rate_pct": round(100.0 * len(p_days) / n, 1) if n else 0.0,
            "fail_official": sum(1 for r in rows if r["eval_outcome"] == "FAIL_OFFICIAL"),
            "fail_inactivity_30d": sum(1 for r in rows if r["eval_outcome"] == "FAIL_INACTIVITY_30D"),
            "halt_internal_guard": sum(1 for r in rows if r["eval_outcome"] == "HALT_INTERNAL_GUARD"),
            "open_end": sum(1 for r in rows if r["eval_outcome"] == "OPEN_END"),
            "median_days_to_qualify": round(statistics.median(q_days), 1) if q_days else None,
            "p95_days_to_qualify": _percentile(q_days, 95.0),
            "median_days_to_first_payout": round(statistics.median(p_days), 1) if p_days else None,
            "p95_days_to_first_payout": _percentile(p_days, 95.0),
            "worst_daily_dd_wick": round(max((r["max_daily_dd_wick"] for r in rows), default=0.0), 2),
            "worst_overall_dd_wick": round(max((r["max_overall_dd_wick"] for r in rows), default=0.0), 2),
            "total_official_daily_breach_wick_bars": sum(r["official_daily_breach_wick_bars"] for r in rows),
            "total_official_overall_breach_wick_bars": sum(r["official_overall_breach_wick_bars"] for r in rows),
            "total_shock_1coin_50_breach_bars": sum(r["shock_1coin_50_breach_bars"] for r in rows),
        }

    return {
        "one_step_summary": _summarize_seq(one_step_cohorts),
        "two_step_summary": _summarize_seq(two_step_cohorts),
        "one_step_cohorts": one_step_cohorts,
        "two_step_cohorts": two_step_cohorts,
    }


def _can_derive_two_step_funded_from_one_step(res: dict[str, Any]) -> bool:
    """True when one_step_funded continuous pass had zero halts, headroom denials, or floor touches."""
    if not isinstance(res, dict) or "daily_curve" not in res or "budget_denials" not in res:
        return False
    if res.get("halted") or res.get("internal_wick_touch") is not None:
        return False
    if (
        res.get("official_daily_breach_wick_bars", 0) != 0
        or res.get("official_overall_breach_wick_bars", 0) != 0
        or res.get("shock_1coin_35_breach_bars", 0) != 0
        or res.get("shock_1coin_50_breach_bars", 0) != 0
        or res.get("shock_2coin_25_breach_bars", 0) != 0
        or res.get("official_breach_close") is not None
        or res.get("official_breach_wick") is not None
    ):
        return False
    denials = res.get("budget_denials") or {}
    return not any(
        k.endswith(("operating_headroom_exhausted", "insufficient_operating_headroom_for_stress_bound"))
        and v > 0
        for k, v in denials.items()
    )


def _derive_two_step_funded_summary(
    one_step_res: dict[str, Any],
    one_step_profile: dict[str, Any],
    two_step_profile: dict[str, Any],
    *,
    scenario_name: str | None = None,
) -> dict[str, Any]:
    out = copy.deepcopy(one_step_res)
    if scenario_name is not None:
        out["scenario"] = scenario_name
    init_bal = float(two_step_profile["initial_balance"])
    out["funded_withdrawal_policies"] = simulate_funded_withdrawals(
        one_step_res["daily_curve"],
        initial_balance=init_bal,
        official_max_loss_pct=float(two_step_profile["official_max_loss_percent"]),
    )
    daily_floor_diff = init_bal * (
        float(two_step_profile["official_daily_limit_percent"])
        - float(one_step_profile["official_daily_limit_percent"])
    )
    if "min_post_shock_50_headroom_usd" in out and out["min_post_shock_50_headroom_usd"] < init_bal:
        out["min_post_shock_50_headroom_usd"] = round(
            float(out["min_post_shock_50_headroom_usd"]) + daily_floor_diff, 2
        )
    return out


def _execute_scenario(
    bt: PropParityBacktesting,
    cfg: dict[str, Any],
    profile: dict[str, Any],
    ohclv_lists: dict[str, list[tuple]],
    analyzed_dfs: dict[str, pd.DataFrame],
    min_date: datetime,
    max_date: datetime,
    *,
    universe_size: int,
    name: str,
    slippage_bps: float,
    strict_wick_guard: bool,
    cohort_step_days: int,
    skip_standalone_cohorts: bool = False,
    skip_crash_sweep: bool = False,
) -> dict[str, Any]:
    t_sc = time.time()
    bt._extra_slippage_bps = float(slippage_bps)
    bt._strict_wick_guard = bool(strict_wick_guard)
    bt.set_fee()

    funded_profiles = build_program_profiles(
        initial_balance=float(profile["initial_balance"]),
        start_utc=_to_utc(min_date).isoformat(),
        one_step_daily_guard=float(profile["daily_limit_percent"]),
        one_step_max_guard=float(profile["max_loss_percent"]),
        two_step_daily_guard=float(bt._prop_policy.get("_two_step_daily_guard", 0.043)),
        two_step_max_guard=float(bt._prop_policy.get("_two_step_max_guard", 0.080)),
    )

    # 1. Continuous runs: (a) strict Evaluation pause at +10%, (b) 24-month Non-Compounding Funded projection
    continuous_summary = (
        None
        if skip_standalone_cohorts
        else _run_continuous_pass(
            bt,
            cfg,
            profile,
            ohclv_lists,
            analyzed_dfs,
            min_date,
            max_date,
            universe_size=universe_size,
            name=name,
            slippage_bps=slippage_bps,
            strict_wick_guard=strict_wick_guard,
            ignore_target_pause=False,
        )
    )
    continuous_funded_summary = _run_continuous_pass(
        bt,
        cfg,
        funded_profiles["one_step_funded"],
        ohclv_lists,
        analyzed_dfs,
        min_date,
        max_date,
        universe_size=universe_size,
        name=name,
        slippage_bps=slippage_bps,
        strict_wick_guard=strict_wick_guard,
        ignore_target_pause=True,
    )
    if _can_derive_two_step_funded_from_one_step(continuous_funded_summary):
        continuous_funded_two_step_summary = _derive_two_step_funded_summary(
            continuous_funded_summary,
            funded_profiles["one_step_funded"],
            funded_profiles["two_step_funded"],
            scenario_name=name,
        )
    else:
        continuous_funded_two_step_summary = _run_continuous_pass(
            bt, cfg, funded_profiles["two_step_funded"], ohclv_lists, analyzed_dfs,
            min_date, max_date, universe_size=universe_size, name=name,
            slippage_bps=slippage_bps, strict_wick_guard=strict_wick_guard,
            ignore_target_pause=True,
        )
    bt._prop_profile = copy.deepcopy(profile)
    print(
        f"  [{name}] Funded 2y Continuous: Eq=${continuous_funded_summary['final_equity']} "
        f"(Net ${continuous_funded_summary['net_pnl']:+}) | Trades={continuous_funded_summary['total_trades']} "
        f"| Halted={continuous_funded_summary['halted']} | MaxMonthDD=${continuous_funded_summary['max_month_dd_close']}/{continuous_funded_summary['max_month_dd_wick']}",
        flush=True,
    )

    # 2. Rolling Independent One-Step Challenge Cohorts + Sequential One-Step vs Two-Step Comparison
    bt._ignore_target_pause = False
    cohorts = []
    cohort_starts: list[datetime] = []
    if cohort_step_days > 0:
        bt._stop_on_halt = True
        bt._stop_on_pass = True
        cur_start = _to_utc(min_date)
        last_cohort_start = _to_utc(max_date) - timedelta(days=30)
        while cur_start <= last_cohort_start:
            cohort_starts.append(cur_start)
            if not skip_standalone_cohorts:
                c_res = bt.run_cached_cohort(ohclv_lists, analyzed_dfs, cur_start, max_date)
                cohorts.append(c_res)
                print(
                    f"  [{name}] Cohort {c_res['cohort_start']}: {c_res['outcome']} in {c_res['days_elapsed']}d "
                    f"| Eq=${c_res['final_equity']} | Counted=${c_res['counted_profit']} "
                    f"| QDays={c_res['qualifying_days']} | MaxDayDD=${c_res['max_daily_dd_close']}/{c_res['max_daily_dd_wick']}",
                    flush=True,
                )
            cur_start += timedelta(days=cohort_step_days)

    program_comparison = None
    autumn_crash_sweep = None
    if cohort_starts:
        os_d_guard = float(profile.get("daily_limit_percent", 0.036))
        os_m_guard = float(profile.get("max_loss_percent", 0.065))
        ts_d_guard = float(bt._prop_policy.get("_two_step_daily_guard", 0.043))
        ts_m_guard = float(bt._prop_policy.get("_two_step_max_guard", 0.080))
        program_comparison = run_sequential_program_cohorts(
            bt, ohclv_lists, analyzed_dfs, cohort_starts, max_date,
            initial_balance=float(profile["initial_balance"]),
            one_step_daily_guard=os_d_guard,
            one_step_max_guard=os_m_guard,
            two_step_daily_guard=ts_d_guard,
            two_step_max_guard=ts_m_guard,
        )
        os_s = program_comparison["one_step_summary"]
        ts_s = program_comparison["two_step_summary"]
        print(
            f"  [{name}] 1-Step vs 2-Step: "
            f"1-Step Qual={os_s['qualify_rate_pct']}% (Med {os_s['median_days_to_qualify']}d, 1stPay Med {os_s['median_days_to_first_payout']}d) | "
            f"2-Step Qual={ts_s['qualify_rate_pct']}% (Med {ts_s['median_days_to_qualify']}d, 1stPay Med {ts_s['median_days_to_first_payout']}d)",
            flush=True,
        )
        crash_w_start = datetime(2026, 9, 15, tzinfo=UTC)
        crash_w_end = datetime(2026, 11, 10, tzinfo=UTC)
        if (not skip_crash_sweep) and _to_utc(min_date) <= crash_w_start and _to_utc(max_date) >= crash_w_end:
            daily_starts: list[datetime] = []
            d_cur = crash_w_start
            while d_cur <= crash_w_end:
                daily_starts.append(d_cur)
                d_cur += timedelta(days=1)
            autumn_crash_sweep = run_sequential_program_cohorts(
                bt, ohclv_lists, analyzed_dfs, daily_starts, max_date,
                initial_balance=float(profile["initial_balance"]),
                one_step_daily_guard=os_d_guard,
                one_step_max_guard=os_m_guard,
                two_step_daily_guard=ts_d_guard,
                two_step_max_guard=ts_m_guard,
            )

    pass_days = [c["days_elapsed"] for c in cohorts if c["outcome"] == "PASS"]
    n_tot = len(cohorts)
    n_pass = len(pass_days)
    cohorts_summary = {
        "total": n_tot,
        "passed": n_pass,
        "pass_rate_pct": round((100.0 * n_pass / n_tot), 1) if n_tot else 0.0,
        "fail_official": sum(1 for c in cohorts if c["outcome"] == "FAIL_OFFICIAL"),
        "fail_inactivity_30d": sum(1 for c in cohorts if c["outcome"] == "FAIL_INACTIVITY_30D"),
        "halt_internal_guard": sum(1 for c in cohorts if c["outcome"] == "HALT_INTERNAL_GUARD"),
        "open_end": sum(1 for c in cohorts if c["outcome"] == "OPEN_END"),
        "median_days_to_pass": round(statistics.median(pass_days), 1) if pass_days else None,
        "p95_days_to_pass": _percentile(pass_days, 95.0),
        "min_days_to_pass": round(min(pass_days), 1) if pass_days else None,
        "max_days_to_pass": round(max(pass_days), 1) if pass_days else None,
        "worst_daily_dd_close": round(max((c["max_daily_dd_close"] for c in cohorts), default=0.0), 2),
        "worst_daily_dd_wick": round(max((c["max_daily_dd_wick"] for c in cohorts), default=0.0), 2),
        "worst_overall_dd_close": round(max((c["max_overall_dd_close"] for c in cohorts), default=0.0), 2),
        "worst_overall_dd_wick": round(max((c["max_overall_dd_wick"] for c in cohorts), default=0.0), 2),
    }
    return {
        "continuous": continuous_summary,
        "continuous_funded": continuous_funded_summary,
        "continuous_funded_two_step": continuous_funded_two_step_summary,
        "cohorts": cohorts,
        "cohorts_summary": cohorts_summary,
        "program_comparison": program_comparison,
        "autumn_2026_daily_crash_sweep": autumn_crash_sweep,
        "runtime_sec": round(time.time() - t_sc, 1),
    }


DEFAULT_MONEY_GRID: list[dict[str, Any]] = [
    # --- A. 2 CRYPTO SIMULTANEE (da ultra-prudente $750 fino a $1.200 sotto limite Funded $1.250) ---
    {"id": "S2_E255_SLOT375_AGG750", "max_open_trades": 2, "stake_amount": 255.0, "max_slot_notional_usdt": 375.0, "max_aggregate_notional_usdt": 750.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S2_E275_SLOT400_AGG850", "max_open_trades": 2, "stake_amount": 275.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 850.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S2_E275_SLOT475_AGG950", "max_open_trades": 2, "stake_amount": 275.0, "max_slot_notional_usdt": 475.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S2_E300_SLOT550_AGG1100", "max_open_trades": 2, "stake_amount": 300.0, "max_slot_notional_usdt": 550.0, "max_aggregate_notional_usdt": 1100.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S2_E350_SLOT600_AGG1200", "max_open_trades": 2, "stake_amount": 350.0, "max_slot_notional_usdt": 600.0, "max_aggregate_notional_usdt": 1200.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    # --- B. 3 CRYPTO SIMULTANEE (da entry $155<$250 fino a $275/$1.200 + baseline unlimited) ---
    {"id": "S3_E155_SLOT350_AGG850", "max_open_trades": 3, "stake_amount": 155.0, "max_slot_notional_usdt": 350.0, "max_aggregate_notional_usdt": 850.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E200_SLOT350_AGG850", "max_open_trades": 3, "stake_amount": 200.0, "max_slot_notional_usdt": 350.0, "max_aggregate_notional_usdt": 850.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E200_SLOT400_AGG950", "max_open_trades": 3, "stake_amount": 200.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT350_AGG950", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 350.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT400_AGG950", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT450_AGG1100", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 450.0, "max_aggregate_notional_usdt": 1100.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E275_SLOT500_AGG1200", "max_open_trades": 3, "stake_amount": 275.0, "max_slot_notional_usdt": 500.0, "max_aggregate_notional_usdt": 1200.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_UNLIM_SLOT500_AGG950", "max_open_trades": 3, "stake_amount": "unlimited", "max_slot_notional_usdt": 500.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    # --- C. 4 CRYPTO SIMULTANEE (massima diversificazione dentro cap Funded $1.200) ---
    {"id": "S4_E155_SLOT350_AGG950", "max_open_trades": 4, "stake_amount": 155.0, "max_slot_notional_usdt": 350.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S4_E200_SLOT350_AGG1100", "max_open_trades": 4, "stake_amount": 200.0, "max_slot_notional_usdt": 350.0, "max_aggregate_notional_usdt": 1100.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S4_E255_SLOT400_AGG1200", "max_open_trades": 4, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 1200.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    # --- D. ABLAZIONI SOGLIE STOP-LOSS GIORNALIERO/TOTALE (conservative vs standard vs near_official) ---
    {"id": "S3_E255_SLOT400_AGG950_CONSERVATIVE", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "conservative", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT400_AGG950_NEARGUARD", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "near_official", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E275_SLOT500_AGG1200_NEARGUARD", "max_open_trades": 3, "stake_amount": 275.0, "max_slot_notional_usdt": 500.0, "max_aggregate_notional_usdt": 1200.0, "guard": "near_official", "macro": "standard", "dynamic_shield": False, "headroom_mode": "standard"},
    # --- E. ABLAZIONI PAUSA CRASH BTC (OFF vs tight -3%/-6.5% vs wide -5%/-10%) ---
    {"id": "S3_E255_SLOT400_AGG950_MACRO_OFF", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "off", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT400_AGG950_MACRO_TIGHT", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "tight", "dynamic_shield": False, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT400_AGG950_MACRO_WIDE", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "wide", "dynamic_shield": False, "headroom_mode": "standard"},
    # --- F. SCUDO DINAMICO EQUITY (riduce slot se eq < $4.900) & HEADROOM BUFFER RIDOTTO ---
    {"id": "S3_E275_SLOT500_AGG1200_SHIELD", "max_open_trades": 3, "stake_amount": 275.0, "max_slot_notional_usdt": 500.0, "max_aggregate_notional_usdt": 1200.0, "guard": "standard", "macro": "standard", "dynamic_shield": True, "headroom_mode": "standard"},
    {"id": "S3_E255_SLOT400_AGG950_TIGHT_HEADROOM", "max_open_trades": 3, "stake_amount": 255.0, "max_slot_notional_usdt": 400.0, "max_aggregate_notional_usdt": 950.0, "guard": "standard", "macro": "standard", "dynamic_shield": False, "headroom_mode": "tight"},
]


def _apply_candidate_money_params(
    bt: PropParityBacktesting,
    cfg: dict[str, Any],
    profile: dict[str, Any],
    cand: dict[str, Any],
) -> None:
    init_bal = float(bt._prop_profile["initial_balance"])
    bt._checkpoint_candidate = copy.deepcopy(cand)
    max_open = int(cand["max_open_trades"])
    stake_amt = cand["stake_amount"]
    max_slot = float(cand["max_slot_notional_usdt"])
    max_agg = float(cand["max_aggregate_notional_usdt"])
    ratio = round(max_agg / init_bal, 4)

    guard_mode = cand.get("guard", "standard")
    if guard_mode == "conservative":
        os_d, os_m = 0.034, 0.060
        ts_d, ts_m = 0.042, 0.075
    elif guard_mode == "near_official":
        # Stop interno vicinissimo ai limiti ufficiali (margine tecnico $10 daily / $25 overall)
        os_d, os_m = 0.038, 0.075
        ts_d, ts_m = 0.048, 0.095
    else:
        os_d, os_m = 0.036, 0.065
        ts_d, ts_m = 0.044, 0.082

    macro_mode = cand.get("macro", "standard")
    if macro_mode == "off":
        bt._macro_enabled = False
    elif macro_mode == "tight":
        bt._macro_enabled = True
        bt._macro_1h_trigger, bt._macro_24h_trigger = -3.0, -6.5
        bt._macro_1h_recover, bt._macro_24h_recover = -2.0, -4.5
    elif macro_mode == "wide":
        bt._macro_enabled = True
        bt._macro_1h_trigger, bt._macro_24h_trigger = -5.0, -10.0
        bt._macro_1h_recover, bt._macro_24h_recover = -3.0, -7.0
    else:
        bt._macro_enabled = True
        bt._macro_1h_trigger, bt._macro_24h_trigger = -4.0, -8.0
        bt._macro_1h_recover, bt._macro_24h_recover = -2.5, -6.0

    bt._dynamic_shield_enabled = bool(cand.get("dynamic_shield", False))

    headroom_mode = cand.get("headroom_mode", "standard")
    if headroom_mode == "tight":
        bt._prop_policy["latency_gap_stress_bps"] = 100.0
        bt._prop_policy["min_headroom_buffer_usdt"] = 15.0
    else:
        bt._prop_policy["latency_gap_stress_bps"] = 250.0
        bt._prop_policy["min_headroom_buffer_usdt"] = 30.0

    profile["daily_limit_percent"] = os_d
    profile["max_loss_percent"] = os_m
    bt._prop_profile["daily_limit_percent"] = os_d
    bt._prop_profile["max_loss_percent"] = os_m
    bt._prop_policy["_two_step_daily_guard"] = ts_d
    bt._prop_policy["_two_step_max_guard"] = ts_m

    cfg["max_open_trades"] = max_open
    cfg["stake_amount"] = stake_amt
    cfg["tradable_balance_ratio"] = ratio
    cfg["amend_last_stake_amount"] = stake_amt == "unlimited"
    bt.config["max_open_trades"] = max_open
    bt.config["stake_amount"] = stake_amt
    bt.config["tradable_balance_ratio"] = ratio
    bt.config["amend_last_stake_amount"] = stake_amt == "unlimited"
    bt.strategy.max_open_trades = max_open
    bt.strategy.stake_amount = stake_amt
    bt.wallets._config["max_open_trades"] = max_open
    bt.wallets._config["stake_amount"] = stake_amt
    bt.wallets._config["tradable_balance_ratio"] = ratio
    bt.wallets._config["amend_last_stake_amount"] = stake_amt == "unlimited"
    bt._prop_policy["max_slot_notional_usdt"] = max_slot
    bt._prop_policy["max_aggregate_notional_usdt"] = max_agg


def run_grid_sweep(
    bt: PropParityBacktesting,
    cfg: dict[str, Any],
    profile: dict[str, Any],
    ohclv_lists: dict[str, list[tuple]],
    analyzed_dfs: dict[str, pd.DataFrame],
    min_date: datetime,
    max_date: datetime,
    args: argparse.Namespace,
    *,
    schedule_meta: dict[str, Any] | None = None,
    delisted_fallbacks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate money-management & risk-control candidates on Train, OOS, Full 2Y, and 1-Step vs 2-Step."""
    t0 = time.time()
    mid_date = _to_utc(min_date) + (_to_utc(max_date) - _to_utc(min_date)) / 2
    mid_date = mid_date.replace(hour=0, minute=0, second=0, microsecond=0)

    selected_ids = getattr(args, "candidates", None) or []
    if selected_ids:
        cand_map = {c["id"]: c for c in DEFAULT_MONEY_GRID}
        unknown = [cid for cid in selected_ids if cid not in cand_map]
        if unknown:
            raise ValueError(f"Unknown candidate ID(s): {unknown}. Valid IDs: {sorted(cand_map)}")
        grid_to_run = [cand_map[cid] for cid in selected_ids]
    else:
        grid_to_run = list(DEFAULT_MONEY_GRID)

    out_path = Path(args.grid_json) if getattr(args, "grid_json", "") else None
    completed_by_id: dict[str, dict[str, Any]] = {}
    if out_path and out_path.exists() and not getattr(args, "overwrite_suite", False):
        try:
            prev = json.loads(out_path.read_text())
            same_run = (
                prev.get("schema_version") == 4
                and prev.get("train_window") == [str(_to_utc(min_date)), str(mid_date)]
                and prev.get("oos_window") == [str(mid_date), str(_to_utc(max_date))]
                and prev.get("slippage_bps_roundtrip") == args.slippage_bps
                and prev.get("strict_wick_guard") == args.strict_wick_guard
                and prev.get("cohort_step_days") == args.cohort_step_days
                and (prev.get("schedule_meta") or {}).get("path") == (schedule_meta or {}).get("path")
            )
            if same_run and isinstance(prev.get("candidates"), list):
                for row in prev["candidates"]:
                    cid = (row.get("candidate") or {}).get("id")
                    if cid:
                        completed_by_id[cid] = row
                if completed_by_id:
                    logger.info(
                        "Resuming grid %s: %d candidate(s) already completed",
                        out_path.name,
                        len(completed_by_id),
                    )
        except Exception as exc:
            logger.warning("Could not resume grid %s: %s", out_path, exc)

    results: list[dict[str, Any]] = []

    def _build_payload() -> dict[str, Any]:
        return {
            "schema_version": 4,
            "train_window": [str(_to_utc(min_date)), str(mid_date)],
            "oos_window": [str(mid_date), str(_to_utc(max_date))],
            "slippage_bps_roundtrip": args.slippage_bps,
            "strict_wick_guard": args.strict_wick_guard,
            "cohort_step_days": args.cohort_step_days,
            "schedule_meta": schedule_meta or {},
            "delisted_fallbacks": delisted_fallbacks or {},
            "runtime_sec": round(time.time() - t0, 1),
            "candidates": results,
        }

    for cand in grid_to_run:
        c_id = cand["id"]
        if c_id in completed_by_id:
            print(f"\n=== SKIPPING COMPLETED GRID CANDIDATE {c_id} ===", flush=True)
            results.append(completed_by_id[c_id])
            continue

        print(f"\n=== GRID CANDIDATE {c_id}: {cand} ===", flush=True)
        _apply_candidate_money_params(bt, cfg, profile, cand)
        full_res = _execute_scenario(
            bt, cfg, profile, ohclv_lists, analyzed_dfs, min_date, max_date,
            universe_size=len(ohclv_lists), name=f"{c_id}_full",
            slippage_bps=args.slippage_bps,
            strict_wick_guard=args.strict_wick_guard,
            cohort_step_days=args.cohort_step_days,
            skip_standalone_cohorts=True,
            skip_crash_sweep=bool(getattr(args, "skip_crash_sweep", False)),
        )
        profs = build_program_profiles(
            initial_balance=float(profile["initial_balance"]),
            start_utc=_to_utc(min_date).isoformat(),
            one_step_daily_guard=float(profile["daily_limit_percent"]),
            one_step_max_guard=float(profile["max_loss_percent"]),
            two_step_daily_guard=float(bt._prop_policy["_two_step_daily_guard"]),
            two_step_max_guard=float(bt._prop_policy["_two_step_max_guard"]),
        )
        one_step_funded_profile = profs["one_step_funded"]
        two_step_funded_profile = profs["two_step_funded"]

        train_funded = _run_continuous_pass(
            bt, cfg, one_step_funded_profile, ohclv_lists, analyzed_dfs, _to_utc(min_date), mid_date,
            universe_size=len(ohclv_lists), name=f"{c_id}_train",
            slippage_bps=args.slippage_bps,
            strict_wick_guard=args.strict_wick_guard,
            ignore_target_pause=True,
        )
        oos_funded = _run_continuous_pass(
            bt, cfg, one_step_funded_profile, ohclv_lists, analyzed_dfs, mid_date, _to_utc(max_date),
            universe_size=len(ohclv_lists), name=f"{c_id}_oos",
            slippage_bps=args.slippage_bps,
            strict_wick_guard=args.strict_wick_guard,
            ignore_target_pause=True,
        )
        two_step_windows = {}
        for window_name, window_start, window_end, one_step_win in (
            ("train_1y_funded_two_step", _to_utc(min_date), mid_date, train_funded),
            ("oos_1y_funded_two_step", mid_date, _to_utc(max_date), oos_funded),
        ):
            if _can_derive_two_step_funded_from_one_step(one_step_win):
                result = _derive_two_step_funded_summary(
                    one_step_win,
                    one_step_funded_profile,
                    two_step_funded_profile,
                    scenario_name=f"{c_id}_{window_name}",
                )
            else:
                result = _run_continuous_pass(
                    bt, cfg, two_step_funded_profile, ohclv_lists, analyzed_dfs, window_start, window_end,
                    universe_size=len(ohclv_lists), name=f"{c_id}_{window_name}",
                    slippage_bps=args.slippage_bps, strict_wick_guard=args.strict_wick_guard,
                    ignore_target_pause=True,
                )
            two_step_windows[window_name] = {k: v for k, v in result.items()
                                             if k not in ("daily_curve", "trades_head", "trades_tail")}
        bt._prop_profile = copy.deepcopy(profile)
        results.append({
            "candidate": cand,
            **two_step_windows,
            "full_2y_continuous_funded_two_step": {
                k: v for k, v in full_res["continuous_funded_two_step"].items()
                if k not in ("daily_curve", "trades_head", "trades_tail")
            },
            "train_1y_funded": {
                k: v for k, v in train_funded.items() if k not in ("daily_curve", "trades_head", "trades_tail")
            },
            "oos_1y_funded": {
                k: v for k, v in oos_funded.items() if k not in ("daily_curve", "trades_head", "trades_tail")
            },
            "full_2y_continuous_funded": {
                k: v for k, v in full_res["continuous_funded"].items() if k not in ("daily_curve", "trades_head", "trades_tail")
            },
            "program_comparison": full_res["program_comparison"],
            "autumn_2026_daily_crash_sweep": full_res["autumn_2026_daily_crash_sweep"],
        })
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(_build_payload(), indent=2))
            tmp_path.replace(out_path)
        gc.collect()

    out = _build_payload()
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(out, indent=2))
        tmp_path.replace(out_path)
    return out


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.setLevel(logging.INFO)
    logging.getLogger("NostalgiaForInfinityX8_Prop").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(REPO / "user_data" / "config_prop_nfix8.json"))
    parser.add_argument("--pairlist-schedule", default="")
    parser.add_argument("--allow-research-schedule", action="store_true", default=False,
                        help="Allow running schedules marked zero_lookahead=False for research sweeps (limitations recorded in JSON).")
    parser.add_argument("--pairs", nargs="*", default=None)
    parser.add_argument("--start", default="2023-11-15")
    parser.add_argument("--end", default="2026-11-15")
    parser.add_argument("--timeframe-detail", default="")
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--cohort-step-days", type=int, default=7)
    parser.add_argument("--skip-crash-sweep", action="store_true", default=False,
                        help="Skip the 57-day daily Sep-Nov 2026 crash sweep during fast 24-candidate grid screening.")
    parser.add_argument("--limit-pairs", type=int, default=0)
    parser.add_argument("--stop-on-halt", action="store_true", default=True)
    parser.add_argument("--stop-on-pass", action="store_true", default=False)
    parser.add_argument("--strict-wick-guard", action="store_true", default=False)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--suite-json", default="")
    parser.add_argument("--grid-json", default="")
    parser.add_argument("--candidates", nargs="*", default=None,
                        help="Optional subset of candidate IDs from DEFAULT_MONEY_GRID to run.")
    parser.add_argument("--overwrite-suite", action="store_true", default=False)
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--keep-deepcopy", action="store_true", default=False)
    parser.add_argument("--cache-callback-rows", action="store_true", default=False,
                        help="Cache rows of immutable offline callback snapshots (opt-in).")
    parser.add_argument("--phase-checkpoint-dir", default="",
                        help="Opt-in atomic resume checkpoints for completed phases and cohorts.")
    parser.add_argument("--detail-array-cache", default="",
                        help="Opt-in disk cache of normalized minute arrays, read through memory maps.")
    parser.add_argument("--ram-profile", choices=["auto", "normal", "high"], default="auto",
                        help="Memory profile for analyzed and 1m detail LRU caches (auto detects >=15GB RAM for high).")
    args = parser.parse_args()
    if not args.keep_deepcopy:
        import freqtrade.strategy.strategy_wrapper as _sw
        _orig_sw_deepcopy = _sw.deepcopy
        _sw.deepcopy = lambda obj, *a, **kw: obj if isinstance(obj, LocalTrade) else _orig_sw_deepcopy(obj, *a, **kw)
    res = run_suite(args)
    if args.grid_json:
        for row in res.get("candidates", []):
            c = row["candidate"]
            tr = row["train_1y_funded"]
            oos = row["oos_1y_funded"]
            f2 = row["full_2y_continuous_funded"]
            pc = row.get("program_comparison") or {}
            os_s = pc.get("one_step_summary") or {}
            ts_s = pc.get("two_step_summary") or {}
            print(
                f"CANDIDATE {c['id']}: TrainNet=${tr['net_pnl']:+} OOSNet=${oos['net_pnl']:+} "
                f"2YNet=${f2['net_pnl']:+} (MaxDayWick=${f2['max_daily_dd_wick']}) | "
                f"1-Step Qual={os_s.get('qualify_rate_pct')}% (1stPay Med={os_s.get('median_days_to_first_payout')}d) | "
                f"2-Step Qual={ts_s.get('qualify_rate_pct')}% (1stPay Med={ts_s.get('median_days_to_first_payout')}d)"
            )
        return
    if args.suite_json:
        for sc_name, sc_res in res["scenarios"].items():
            c_s = sc_res["cohorts_summary"]
            cf = sc_res.get("continuous_funded", {})
            print(
                f"SCENARIO {sc_name}: EvalEq=${sc_res['continuous']['final_equity']} "
                f"FundedEq=${cf.get('final_equity')} (Net=${cf.get('net_pnl')}) "
                f"PASS={c_s['passed']}/{c_s['total']} ({c_s['pass_rate_pct']}%) "
                f"HALT={c_s['halt_internal_guard']} FAIL={c_s['fail_official']} "
                f"OPEN={c_s['open_end']} ({sc_res['runtime_sec']}s)"
            )
        return
    print(json.dumps({k: v for k, v in res["continuous"].items() if k not in ("daily_curve", "trades_head", "trades_tail")}, indent=2))
    if res["cohorts"]:
        passed = sum(1 for c in res["cohorts"] if c["outcome"] == "PASS")
        halted = sum(1 for c in res["cohorts"] if c["outcome"].startswith("FAIL") or c["outcome"].startswith("HALT"))
        open_end = sum(1 for c in res["cohorts"] if c["outcome"] == "OPEN_END")
        print(f"COHORTS SUMMARY: total={len(res['cohorts'])} PASS={passed} FAIL/HALT={halted} OPEN_END={open_end}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    main()
