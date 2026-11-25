"""Build a zero-lookahead point-in-time pairlist schedule and convert archived delisted coins.

Ensures:
  1. Only ordinary Bybit USDT perpetual crypto (excludes Innovation Zone, ST, stocks, ETFs, forex, commodities).
  2. Strict point-in-time evaluation (only candles fully closed before timestamp `t` are visible at `t`).
  3. Filters: AgeFilter (>= min_days_listed), 24h quoteVolume (>= min_value), MarketCap >= $100M,
     VolatilityFilter (10d), RangeStabilityFilter (10d), and config pair_blacklist.
  4. Converts any archived 1m trade bars from `prop_archive_data.py` into Freqtrade futures feathers.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
FORENSIC_DIR = REPO / "user_data" / "forensic" / "strict_validation"
FUTURES_DIR = REPO / "user_data" / "data" / "bybit" / "futures"

STRUCTURAL_BLACKLIST_PATTERNS = (
    r"(USDC|USDE|USDD|DAI|TUSD|FDUSD|BUSD|USDP|USDT|EUR|GBP|AUD|BRL|TRY|USD|XUSD|USD1|RLUSD|EURI)/.*",
    r".*(_PREMIUM|BEAR|BULL|HALF|HEDGE|[1235][SL])/.*",
    r".*(UP|DOWN|3L|3S)/.*",
)

PAIRLIST_PRESETS: dict[str, dict[str, Any]] = {
    "base": {
        "number_assets": 40,
        "min_value_usdt": 20_000_000.0,
        "min_days_listed": 60,
        "min_market_cap_usdt": 100_000_000.0,
        "max_volatility": 0.50,
        "max_rate_of_change": 0.85,
    },
    "strict": {
        "number_assets": 25,
        "min_value_usdt": 50_000_000.0,
        "min_days_listed": 90,
        "min_market_cap_usdt": 250_000_000.0,
        "max_volatility": 0.40,
        "max_rate_of_change": 0.70,
    },
    "bluechip": {
        "number_assets": 15,
        "min_value_usdt": 100_000_000.0,
        "min_days_listed": 120,
        "min_market_cap_usdt": 500_000_000.0,
        "max_volatility": 0.35,
        "max_rate_of_change": 0.60,
    },
}


def discover_archived_delisted_bases() -> set[str]:
    """Return base symbols that have converted 1m trade archives in FORENSIC_DIR/archive."""
    arch_root = FORENSIC_DIR / "archive"
    if not arch_root.is_dir():
        return set()
    out: set[str] = set()
    for sym_dir in arch_root.iterdir():
        if sym_dir.is_dir() and sym_dir.name.endswith("USDT") and any(sym_dir.glob("*-1m.feather")):
            out.add(sym_dir.name[:-4].upper())
    return out


def fetch_or_load_bybit_instruments(*, allow_offline_empty: bool = False) -> dict[str, dict[str, Any]]:
    """Return mapping of BASE (e.g. 'BTC') -> Bybit V5 linear instrument info."""
    FORENSIC_DIR.mkdir(parents=True, exist_ok=True)
    inst_path = FORENSIC_DIR / "instruments.json"
    rows: list[dict[str, Any]] = []
    if inst_path.is_file():
        try:
            payload = json.loads(inst_path.read_text(encoding="utf-8"))
            rows = payload.get("list") or payload.get("result", {}).get("list") or []
        except Exception:
            rows = []
    if not rows:
        try:
            cursor = ""
            while True:
                params: dict[str, Any] = {"category": "linear", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                resp = requests.get(
                    "https://api.bybit.com/v5/market/instruments-info",
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json().get("result", {})
                rows.extend(data.get("list", []))
                cursor = data.get("nextPageCursor") or ""
                if not cursor:
                    break
            inst_path.write_text(
                json.dumps(
                    {"observed_at": datetime.now(UTC).isoformat(), "count": len(rows), "list": rows},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            if not allow_offline_empty:
                raise
            rows = []
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sym = str(r.get("symbol", ""))
        if sym.endswith("USDT") and r.get("quoteCoin") == "USDT":
            base = sym[:-4]
            out[base] = r
    return out


def load_or_fetch_coingecko_mcap_map(*, fetch_online: bool = False) -> dict[str, float]:
    """Load current CoinGecko market-cap snapshot (USD) from cache, optionally fetching top 1,000 coins."""
    FORENSIC_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = FORENSIC_DIR / "coingecko_mcap_snapshot.json"
    alt_cache = REPO / "user_data" / "forensic" / "coingecko_marketcap_cache.json"
    caps: dict[str, float] = {}

    for candidate in (snap_path, alt_cache):
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("caps"), dict):
                for k, v in raw["caps"].items():
                    if isinstance(v, (int, float)) and np.isfinite(v) and v > 0:
                        caps[str(k).upper()] = max(caps.get(str(k).upper(), 0.0), float(v))
            elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
                for item in raw["data"]:
                    if isinstance(item, dict) and item.get("symbol") and item.get("market_cap"):
                        sym = str(item["symbol"]).upper()
                        mc = float(item["market_cap"])
                        if np.isfinite(mc) and mc > 0:
                            caps[sym] = max(caps.get(sym, 0.0), mc)
        except Exception:
            continue
        if caps:
            return caps

    if not fetch_online:
        return caps

    fetched: dict[str, float] = {}
    for page in range(1, 5):
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false",
                },
                timeout=30,
            )
            if resp.status_code == 429:
                time.sleep(15)
                resp = requests.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                    },
                    timeout=30,
                )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                break
            for item in rows:
                sym = str(item.get("symbol", "")).upper()
                mc = item.get("market_cap")
                if sym and isinstance(mc, (int, float)) and np.isfinite(mc) and mc > 0:
                    # Keep highest market cap when tickers collide (canonical top asset)
                    fetched[sym] = max(fetched.get(sym, 0.0), float(mc))
            if page < 4:
                time.sleep(2.5)
        except Exception as exc:
            print(f"WARNING: CoinGecko page {page} fetch failed ({exc}); continuing with {len(fetched)} caps.", flush=True)
            break

    if fetched:
        snap_path.write_text(
            json.dumps(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "source": "https://api.coingecko.com/api/v3/coins/markets",
                    "count": len(fetched),
                    "caps": fetched,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return fetched


def export_eligible_download_pairs(
    config_path: Path,
    output_path: Path,
    *,
    anti_survivorship: bool = True,
    volume_universe: bool = False,
) -> list[str]:
    """Export eligible pairs, or the wider pre-filter volume universe when requested."""
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    patterns = (
        STRUCTURAL_BLACKLIST_PATTERNS
        if anti_survivorship
        else cfg.get("exchange", {}).get("pair_blacklist", [])
    )
    blacklist_patterns = [re.compile(pat) for pat in patterns]
    instruments = fetch_or_load_bybit_instruments()
    pairs: list[str] = []
    for base, info in sorted(instruments.items()):
        if volume_universe:
            if not (
                info.get("contractType") == "LinearPerpetual"
                and info.get("quoteCoin") == "USDT"
                and info.get("settleCoin") == "USDT"
                and not info.get("isPreListing", False)
            ):
                continue
        elif not is_eligible_bybit_crypto(info):
            continue
        pair = f"{base}/USDT:USDT"
        if any(r.fullmatch(pair) or r.match(pair) for r in blacklist_patterns):
            continue
        pairs.append(pair)
    if "BTC/USDT:USDT" not in pairs:
        pairs.insert(0, "BTC/USDT:USDT")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(pairs, indent=2), encoding="utf-8")
    return pairs


def is_eligible_bybit_crypto(info: dict[str, Any] | None) -> bool:
    """Exclude Innovation Zone, ST tag, pre-listing, and TradFi (stocks/ETFs/commodities/forex)."""
    if not isinstance(info, dict):
        return False  # Missing classification is not evidence of ordinary crypto eligibility.
    if "symbolType" not in info or "tags" not in info:
        return False
    symbol_type = str(info.get("symbolType", "")).lower()
    tags = info.get("tags") or []
    return (
        info.get("contractType") == "LinearPerpetual"
        and info.get("quoteCoin") == "USDT"
        and info.get("settleCoin") == "USDT"
        and symbol_type == ""
        and isinstance(tags, list)
        and len(tags) == 0
        and not info.get("isPreListing", False)
    )


def _replace_atomic(src: Path, dst: Path) -> None:
    for attempt in range(5):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def _feather_readable(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        import pyarrow.ipc as _pa_ipc

        with _pa_ipc.open_file(path) as reader:
            return reader.num_record_batches >= 0
    except Exception:
        return False


def convert_archived_1m_to_freqtrade_feathers() -> list[str]:
    """Convert downloaded 1m archive bars (`user_data/forensic/strict_validation/archive/*`) to Freqtrade format."""
    arch_root = FORENSIC_DIR / "archive"
    if not arch_root.is_dir():
        return []
    FUTURES_DIR.mkdir(parents=True, exist_ok=True)
    converted: list[str] = []
    for sym_dir in sorted(arch_root.iterdir()):
        if not sym_dir.is_dir() or not sym_dir.name.endswith("USDT"):
            continue
        feathers = sorted(sym_dir.glob("*-1m.feather"))
        if not feathers:
            continue
        base = sym_dir.name[:-4]
        pair_prefix = f"{base}_USDT_USDT"
        target_1m = FUTURES_DIR / f"{pair_prefix}-1m-futures.feather"
        all_targets = [
            FUTURES_DIR / f"{pair_prefix}-{tf}-futures.feather"
            for tf in ("1m", "5m", "15m", "1h", "4h", "1d")
        ]
        latest_src_mtime = max(f.stat().st_mtime for f in feathers)
        if all(_feather_readable(t) and t.stat().st_mtime >= latest_src_mtime for t in all_targets):
            converted.append(f"{base}/USDT:USDT")
            continue

        parts = [pd.read_feather(f) for f in feathers]
        if _feather_readable(target_1m):
            parts.insert(0, pd.read_feather(target_1m))
        df_1m = pd.concat(parts, ignore_index=True)
        df_1m["date"] = pd.to_datetime(df_1m["date"], utc=True)
        # Stable sort: on duplicate minutes the archived trade bar (appended last) wins deterministically.
        df_1m = (
            df_1m.sort_values("date", kind="mergesort")
            .drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
        )
        cols = ["date", "open", "high", "low", "close", "volume"]
        tmp_1m = target_1m.with_suffix(".feather.tmp")
        try:
            df_1m[cols].to_feather(tmp_1m, compression="lz4")
            _replace_atomic(tmp_1m, target_1m)
        finally:
            tmp_1m.unlink(missing_ok=True)

        indexed = df_1m.set_index("date")
        for tf, rule in (("5m", "5min"), ("15m", "15min"), ("1h", "1h"), ("4h", "4h"), ("1d", "1D")):
            res = (
                indexed.resample(rule, label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna()
                .reset_index()
            )
            dst_tf = FUTURES_DIR / f"{pair_prefix}-{tf}-futures.feather"
            tmp_tf = dst_tf.with_suffix(".feather.tmp")
            try:
                res[cols].to_feather(tmp_tf, compression="lz4")
                _replace_atomic(tmp_tf, dst_tf)
            finally:
                tmp_tf.unlink(missing_ok=True)
            # Trade prices cannot prove mark prices or historical funding rates.
            # Those series must be downloaded independently from an official source.
        converted.append(f"{base}/USDT:USDT")
    return converted


def load_coinmetrics_marketcap_map(min_market_cap: float) -> dict[str, pd.Series]:
    """Load point-in-time daily market caps (using `earliest_usable_at = date + 1d` to prevent lookahead)."""
    cm_dir = FORENSIC_DIR / "coinmetrics"
    out: dict[str, pd.Series] = {}
    if not cm_dir.is_dir():
        return out
    # Load CapMrktCurUSD first (authoritative), then fill gaps/missing assets from CapMrktEstUSD.
    for suffix in ("-CapMrktCurUSD.feather", "-CapMrktEstUSD.feather"):
        for f in sorted(cm_dir.glob(f"*{suffix}")):
            asset = f.name.removesuffix(suffix).upper()
            try:
                df = pd.read_feather(f)
                if "earliest_usable_at" in df and "market_cap_usd" in df:
                    s = (
                        df.set_index(pd.to_datetime(df["earliest_usable_at"], utc=True).dt.floor("D"))[
                            "market_cap_usd"
                        ]
                        .astype(float)
                        .sort_index()
                    )
                    if asset not in out:
                        out[asset] = s
                    else:
                        out[asset] = out[asset].combine_first(s).sort_index()
            except Exception:
                continue
    return out


def build_point_in_time_schedule(
    *,
    config_path: Path,
    start_iso: str,
    end_iso: str,
    number_assets: int = 40,
    min_value_usdt: float = 20_000_000.0,
    min_days_listed: int = 60,
    min_market_cap_usdt: float = 100_000_000.0,
    max_volatility: float = 0.50,
    max_rate_of_change: float = 0.85,
    lookback_days: int = 10,
    anti_survivorship: bool = False,
    include_archived_delisted: bool = False,
    mcap_mode: str = "strict",
    coingecko_mcap_map: dict[str, float] | None = None,
    fetch_coingecko: bool = False,
    offline_instruments_fallback: bool = False,
    resolution: str = "1d",
) -> dict[str, Any]:
    if mcap_mode not in ("strict", "hybrid", "none"):
        raise ValueError(f"Unsupported mcap_mode: {mcap_mode}")
    if resolution not in ("1d", "1h"):
        raise ValueError(f"Unsupported resolution: {resolution}")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    # In anti-survivorship mode, never apply today's discretionary altcoin blacklist.
    patterns = (
        STRUCTURAL_BLACKLIST_PATTERNS
        if anti_survivorship
        else cfg.get("exchange", {}).get("pair_blacklist", [])
    )
    blacklist_patterns = [re.compile(pat) for pat in patterns]
    if offline_instruments_fallback:
        instruments = fetch_or_load_bybit_instruments(allow_offline_empty=True)
    else:
        instruments = fetch_or_load_bybit_instruments()
    archived_delisted_bases = discover_archived_delisted_bases() if include_archived_delisted else set()
    mcap_map = {} if mcap_mode == "none" else load_coinmetrics_marketcap_map(min_market_cap_usdt)
    if coingecko_mcap_map is not None:
        cg_map = {str(k).upper(): float(v) for k, v in coingecko_mcap_map.items()}
    elif mcap_mode == "hybrid":
        cg_map = load_or_fetch_coingecko_mcap_map(fetch_online=fetch_coingecko)
    else:
        cg_map = {}

    start_dt = pd.Timestamp(start_iso, tz="UTC").floor("D")
    end_dt = pd.Timestamp(end_iso, tz="UTC").floor("D")
    all_days = pd.date_range(start_dt, end_dt, freq="1D", tz="UTC")

    daily_files = sorted(FUTURES_DIR.glob("*-1d-futures.feather"))
    pair_daily: dict[str, pd.DataFrame] = {}
    instrument_eligible: set[str] = set()
    pair_hourly_qv: dict[str, np.ndarray] = {}
    pair_last_usable_s: dict[str, int] = {}
    archived_included: set[str] = set()
    used_offline_inst_fallback = bool(offline_instruments_fallback and not instruments)

    window_start_h = start_dt - pd.Timedelta(hours=23)
    window_end_h = end_dt + pd.Timedelta(hours=23)
    window_start_s = int(window_start_h.timestamp())
    window_end_s = int(window_end_h.timestamp())
    n_ext_hours = ((window_end_s - window_start_s) // 3600) + 1

    for f in daily_files:
        base = f.name.split("_USDT_USDT-")[0].upper()
        pair = f"{base}/USDT:USDT"
        if any(r.fullmatch(pair) or r.match(pair) for r in blacklist_patterns):
            continue
        inst_info = instruments.get(base)
        if not is_eligible_bybit_crypto(inst_info):
            if include_archived_delisted and base in archived_delisted_bases and inst_info is None:
                archived_included.add(pair)
            elif used_offline_inst_fallback:
                # Local offline smoke-test fallback only when explicitly requested and instruments.json is absent.
                pass
            else:
                # Keep its volume in the initial ranking: live filters instruments after Top N.
                inst_info = None
        if is_eligible_bybit_crypto(inst_info) or pair in archived_included or used_offline_inst_fallback:
            instrument_eligible.add(pair)
        # Require 5m data present as well (and 1h when building hourly schedules)
        if not (FUTURES_DIR / f"{base}_USDT_USDT-5m-futures.feather").is_file():
            continue
        f_1h = FUTURES_DIR / f"{base}_USDT_USDT-1h-futures.feather"
        if resolution == "1h" and not f_1h.is_file():
            continue
        try:
            df = pd.read_feather(f)
        except Exception:
            continue
        if df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["day"] = pd.to_datetime(df["date"], utc=True).dt.floor("D")
        # A 1d candle opening at day D closes at D + 1 day (00:00 UTC): only usable from D + 1 day!
        df["usable_day"] = df["day"] + pd.Timedelta(days=1)
        df["quote_vol"] = df["close"].astype(float) * df["volume"].astype(float)
        # Match VolatilityFilter on the last N closed daily candles: first return
        # is zero inside each window, and pandas uses sample standard deviation.
        def window_volatility(closes):
            returns = np.log(closes.shift(1) / closes).fillna(0)
            return returns.std(ddof=1) * np.sqrt(lookback_days)
        df["volatility_10d"] = df["close"].astype(float).rolling(lookback_days).apply(window_volatility)
        roll_max = df["high"].astype(float).rolling(lookback_days).max()
        roll_min = df["low"].astype(float).rolling(lookback_days).min()
        df["roc_10d"] = np.where(roll_min > 0, (roll_max - roll_min) / roll_min, 999.0)
        df["age_days"] = np.arange(1, len(df) + 1)

        if resolution == "1h":
            try:
                df_1h = pd.read_feather(f_1h, columns=["date", "close", "volume"])
            except Exception:
                continue
            if df_1h.empty:
                continue
            ts_open_s = (
                pd.to_datetime(df_1h["date"], utc=True)
                .dt.floor("h")
                .dt.as_unit("s")
                .to_numpy(dtype=np.int64)
            )
            # An hourly candle opening at T closes at T + 3600s: strictly usable only from T + 3600s!
            usable_s = ts_open_s + 3600
            order = np.argsort(usable_s, kind="mergesort")
            usable_s = usable_s[order]
            qv_1h = (
                df_1h["close"].to_numpy(dtype=np.float64)[order]
                * df_1h["volume"].to_numpy(dtype=np.float64)[order]
            )
            last_usable = int(usable_s[-1])
            mask = (usable_s >= window_start_s) & (usable_s <= window_end_s)
            if not np.any(mask):
                continue
            idx_h = ((usable_s[mask] - window_start_s) // 3600).astype(np.int64)
            dense_qv = np.zeros(n_ext_hours, dtype=np.float64)
            # Later duplicate timestamps overwrite earlier ones (matches keep='last').
            dense_qv[idx_h] = np.where(np.isfinite(qv_1h[mask]), qv_1h[mask], 0.0)
            csum = np.empty(n_ext_hours + 1, dtype=np.float64)
            csum[0] = 0.0
            np.cumsum(dense_qv, out=csum[1:])
            pair_hourly_qv[pair] = csum[24:] - csum[:-24]
            pair_last_usable_s[pair] = last_usable

        pair_daily[pair] = df.set_index("usable_day")[
            ["quote_vol", "volatility_10d", "roc_10d", "age_days"]
        ]

    schedule: dict[str, list[str]] = {}
    ever_2y: set[str] = set()
    ever_all: set[str] = set()
    mcap_provenance: dict[str, int] = defaultdict(int)
    delisted_without_mcap: set[str] = set()
    daily_counts: list[int] = []
    hourly_counts: list[int] = []
    empty_days_post_2026_05 = 0

    def _admit_mcap_for_day(pair: str, d: pd.Timestamp) -> bool:
        base = pair.split("/")[0]
        if mcap_mode == "none":
            mcap_provenance["mcap_gate_disabled"] += 1
            return True
        if mcap_mode == "strict":
            if base not in mcap_map:
                return False
            mc_series = mcap_map[base]
            prior = mc_series.loc[
                (mc_series.index <= d) & (mc_series.index >= d - pd.Timedelta(days=1))
            ]
            if prior.empty or not np.isfinite(prior.iloc[-1]) or float(prior.iloc[-1]) < min_market_cap_usdt:
                return False
            mcap_provenance["coinmetrics_fresh"] += 1
            return True
        # hybrid mode: Coin Metrics point-in-time first; post-cutoff carry / CoinGecko fallback;
        # archived delisted stress admission when neither Coin Metrics nor CoinGecko covers it.
        admitted = False
        if base in mcap_map and not mcap_map[base].empty:
            mc_series = mcap_map[base]
            prior = mc_series.loc[
                (mc_series.index <= d) & (mc_series.index >= d - pd.Timedelta(days=1))
            ]
            if not prior.empty:
                val = float(prior.iloc[-1])
                if np.isfinite(val) and val >= min_market_cap_usdt:
                    mcap_provenance["coinmetrics_fresh"] += 1
                    admitted = True
                else:
                    return False
            elif d > mc_series.index.max():
                cg_val = cg_map.get(base)
                if cg_val is not None and np.isfinite(cg_val):
                    if cg_val >= min_market_cap_usdt:
                        mcap_provenance["coingecko_post_cutoff"] += 1
                        admitted = True
                    else:
                        return False
                else:
                    finite_vals = mc_series.dropna()
                    if not finite_vals.empty and float(finite_vals.iloc[-1]) >= min_market_cap_usdt:
                        mcap_provenance["coinmetrics_carried_post_cutoff"] += 1
                        admitted = True
                    else:
                        return False
        if not admitted and base in cg_map:
            cg_val = cg_map[base]
            if np.isfinite(cg_val) and cg_val >= min_market_cap_usdt:
                mcap_provenance["coingecko_proxy"] += 1
                admitted = True
        if not admitted and include_archived_delisted and base in archived_delisted_bases:
            mcap_provenance["archived_delisted_no_mcap_evidence"] += 1
            delisted_without_mcap.add(pair)
            admitted = True
        return admitted

    if resolution == "1d":
        for d in all_days:
            candidates: list[tuple[float, str]] = []
            eligible: set[str] = set()
            for pair, df_p in pair_daily.items():
                if d not in df_p.index:
                    continue
                row = df_p.loc[d]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                age = int(row["age_days"])
                qv = float(row["quote_vol"])
                if np.isfinite(qv) and qv > min_value_usdt:
                    candidates.append((qv, pair))
                vol = float(row["volatility_10d"]) if pd.notna(row["volatility_10d"]) else 999.0
                roc = float(row["roc_10d"]) if pd.notna(row["roc_10d"]) else 999.0
                if pair not in instrument_eligible or age < min_days_listed:
                    continue
                if vol > max_volatility or roc > max_rate_of_change:
                    continue
                if not _admit_mcap_for_day(pair, d):
                    continue
                eligible.add(pair)

            candidates.sort(key=lambda x: x[0], reverse=True)
            top_pairs = [p for _, p in candidates[:number_assets] if p in eligible]
            ts_key = str(int(d.timestamp()))
            schedule[ts_key] = top_pairs
            daily_counts.append(len(top_pairs))
            if len(top_pairs) == 0 and d >= pd.Timestamp("2026-06-01", tz="UTC"):
                empty_days_post_2026_05 += 1
            ever_all.update(top_pairs)
            if d >= pd.Timestamp("2024-11-15", tz="UTC"):
                ever_2y.update(top_pairs)
    else:
        prev_top_pairs: list[str] | None = None
        for d_idx, d in enumerate(all_days):
            day_eligible_pairs: list[str] = []
            for pair, df_p in pair_daily.items():
                if d not in df_p.index:
                    continue
                row = df_p.loc[d]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                age = int(row["age_days"])
                vol = float(row["volatility_10d"]) if pd.notna(row["volatility_10d"]) else 999.0
                roc = float(row["roc_10d"]) if pd.notna(row["roc_10d"]) else 999.0
                if pair not in instrument_eligible or age < min_days_listed or vol > max_volatility or roc > max_rate_of_change:
                    continue
                if not _admit_mcap_for_day(pair, d):
                    continue
                day_eligible_pairs.append(pair)

            day_start_s = int(d.timestamp())
            day_max_pairs = 0
            for hour_offset in range(24):
                h_idx = d_idx * 24 + hour_offset
                h_ts = day_start_s + hour_offset * 3600
                candidates_h: list[tuple[float, str]] = []
                for pair in pair_hourly_qv:
                    if h_ts >= pair_last_usable_s[pair]:
                        continue
                    qv_24h = float(pair_hourly_qv[pair][h_idx])
                    if not np.isfinite(qv_24h) or qv_24h <= min_value_usdt:
                        continue
                    candidates_h.append((qv_24h, pair))
                candidates_h.sort(key=lambda x: x[0], reverse=True)
                top_pairs = [p for _, p in candidates_h[:number_assets] if p in day_eligible_pairs]
                hourly_counts.append(len(top_pairs))
                if len(top_pairs) > day_max_pairs:
                    day_max_pairs = len(top_pairs)
                # Always emit 00:00 UTC daily anchor; emit intraday hours whenever whitelist changes.
                # HistoricalPairListAdapter uses bisect_right, so unchanged hours resolve identically.
                if hour_offset == 0 or top_pairs != prev_top_pairs:
                    schedule[str(h_ts)] = top_pairs
                    prev_top_pairs = top_pairs
                ever_all.update(top_pairs)
                if d >= pd.Timestamp("2024-11-15", tz="UTC"):
                    ever_2y.update(top_pairs)

            daily_counts.append(day_max_pairs)
            if day_max_pairs == 0 and d >= pd.Timestamp("2026-06-01", tz="UTC"):
                empty_days_post_2026_05 += 1

    ever_2y.add("BTC/USDT:USDT")
    ever_all.add("BTC/USDT:USDT")
    limitations = [
        "Current instrument metadata is not historical classification evidence.",
        "Coin Metrics ticker mappings and observation vintages require verification.",
    ]
    if mcap_mode == "hybrid":
        limitations.append(
            "Hybrid market-cap mode uses CoinGecko/carried Coin Metrics after Coin Metrics free CSV cutoff (~May 2026) and for assets absent from Coin Metrics."
        )
    elif mcap_mode == "none":
        limitations.append("Market-cap gate disabled (volume/age/volatility/ROC only).")
    if archived_included:
        limitations.append(
            f"Archived delisted symbols admitted for anti-survivorship stress testing: {sorted(archived_included)}."
        )
    if used_offline_inst_fallback:
        limitations.append("Offline mode: Bybit instruments.json unavailable; instrument-tag gate bypassed.")

    cov_diag: dict[str, Any] = {
        "resolution": resolution,
        "total_days": len(daily_counts),
        "empty_days": sum(1 for c in daily_counts if c == 0),
        "empty_days_post_2026_05": empty_days_post_2026_05,
        "min_daily_pairs": min(daily_counts, default=0),
        "median_daily_pairs": round(statistics.median(daily_counts), 1) if daily_counts else 0.0,
        "max_daily_pairs": max(daily_counts, default=0),
    }
    if resolution == "1h":
        cov_diag.update(
            {
                "total_hours": len(hourly_counts),
                "empty_hours": sum(1 for c in hourly_counts if c == 0),
                "emitted_schedule_keys": len(schedule),
                "median_hourly_pairs": (
                    round(statistics.median(hourly_counts), 1) if hourly_counts else 0.0
                ),
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "resolution": resolution,
        "zero_lookahead": False,
        "pairlist_order": "volume_top_n_then_filters",
        "validation_status": (
            f"research_schedule_mcap_{mcap_mode}"
            if mcap_mode != "strict"
            else "unverified_historical_classification_and_ticker_mapping"
        ),
        "limitations": limitations,
        "anti_survivorship": anti_survivorship,
        "include_archived_delisted": include_archived_delisted,
        "mcap_mode": mcap_mode,
        "mcap_provenance_counts": dict(sorted(mcap_provenance.items())),
        "archived_delisted_included": sorted(archived_included),
        "archived_delisted_without_mcap": sorted(delisted_without_mcap),
        "coverage_diagnostics": cov_diag,
        "filters": {
            "number_assets": number_assets,
            "min_value_usdt": min_value_usdt,
            "min_days_listed": min_days_listed,
            "min_market_cap_usdt": min_market_cap_usdt,
            "max_volatility": max_volatility,
            "max_rate_of_change": max_rate_of_change,
            "lookback_days": lookback_days,
            "resolution": resolution,
        },
        "ever_whitelisted_2y": sorted(ever_2y),
        "ever_whitelisted_3y": sorted(ever_all),
        "ever_whitelisted_all": sorted(ever_all),
        "schedule": schedule,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "user_data" / "config_prop_nfix8.json"))
    parser.add_argument("--start", default="2024-11-15")
    parser.add_argument("--end", default="2026-11-15")
    parser.add_argument("--number-assets", type=int, default=40)
    parser.add_argument("--min-value", type=float, default=20_000_000.0)
    parser.add_argument("--min-days-listed", type=int, default=60)
    parser.add_argument("--min-market-cap", type=float, default=100_000_000.0)
    parser.add_argument("--max-volatility", type=float, default=0.50)
    parser.add_argument("--max-roc", type=float, default=0.85)
    parser.add_argument("--anti-survivorship", action="store_true")
    parser.add_argument("--include-archived-delisted", action="store_true")
    parser.add_argument("--mcap-mode", choices=("strict", "hybrid", "none"), default="strict")
    parser.add_argument("--resolution", choices=("1d", "1h"), default="1d")
    parser.add_argument("--fetch-coingecko", action="store_true")
    parser.add_argument("--offline-instruments-fallback", action="store_true")
    parser.add_argument(
        "--export-volume-universe", action="store_true",
        help="Include instruments rejected by later filters so historical Top N volume is not backfilled.",
    )
    parser.add_argument(
        "--export-download-pairs",
        default="",
        help="Write eligible Bybit USDT crypto perpetual pairs JSON list for `freqtrade download-data --pairs-file` and exit.",
    )
    parser.add_argument(
        "--build-presets-dir",
        default="",
        help="Build all 3 pairlist preset schedules (base, strict, bluechip) into this directory.",
    )
    parser.add_argument(
        "--output",
        default=str(FORENSIC_DIR / "pairlist_schedule_2y.json"),
    )
    args = parser.parse_args()

    if args.export_download_pairs:
        pairs = export_eligible_download_pairs(
            Path(args.config),
            Path(args.export_download_pairs),
            anti_survivorship=args.anti_survivorship,
            volume_universe=args.export_volume_universe,
        )
        print(f"Exported {len(pairs)} eligible Bybit USDT crypto pairs -> {args.export_download_pairs}")
        return

    converted = convert_archived_1m_to_freqtrade_feathers()
    if converted:
        print(f"Converted archived delisted symbols to Freqtrade feathers: {converted}")

    if args.build_presets_dir:
        presets_dir = Path(args.build_presets_dir)
        presets_dir.mkdir(parents=True, exist_ok=True)
        cg_map = load_or_fetch_coingecko_mcap_map(fetch_online=args.fetch_coingecko) if args.mcap_mode == "hybrid" else {}
        suffix = "_1h_2y.json" if args.resolution == "1h" else "_2y.json"
        for preset_name, pcfg in PAIRLIST_PRESETS.items():
            payload = build_point_in_time_schedule(
                config_path=Path(args.config),
                start_iso=args.start,
                end_iso=args.end,
                number_assets=pcfg["number_assets"],
                min_value_usdt=pcfg["min_value_usdt"],
                min_days_listed=pcfg["min_days_listed"],
                min_market_cap_usdt=pcfg["min_market_cap_usdt"],
                max_volatility=pcfg["max_volatility"],
                max_rate_of_change=pcfg["max_rate_of_change"],
                anti_survivorship=args.anti_survivorship,
                include_archived_delisted=args.include_archived_delisted,
                mcap_mode=args.mcap_mode,
                coingecko_mcap_map=cg_map,
                offline_instruments_fallback=args.offline_instruments_fallback,
                resolution=args.resolution,
            )
            out_path = presets_dir / f"pairlist_schedule_{preset_name}{suffix}"
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            diag = payload["coverage_diagnostics"]
            extra = (
                f" | keys={diag['emitted_schedule_keys']}/{diag['total_hours']}h"
                if args.resolution == "1h"
                else ""
            )
            print(
                f"Preset [{preset_name}] ({args.resolution}): {len(payload['ever_whitelisted_2y'])} unique pairs | "
                f"median {diag['median_daily_pairs']}/day{extra} | empty={diag['empty_days']} "
                f"(post-May26 empty={diag['empty_days_post_2026_05']}) -> {out_path}"
            )
        return

    payload = build_point_in_time_schedule(
        config_path=Path(args.config),
        start_iso=args.start,
        end_iso=args.end,
        number_assets=args.number_assets,
        min_value_usdt=args.min_value,
        min_days_listed=args.min_days_listed,
        min_market_cap_usdt=args.min_market_cap,
        max_volatility=args.max_volatility,
        max_rate_of_change=args.max_roc,
        anti_survivorship=args.anti_survivorship,
        include_archived_delisted=args.include_archived_delisted,
        mcap_mode=args.mcap_mode,
        fetch_coingecko=args.fetch_coingecko,
        offline_instruments_fallback=args.offline_instruments_fallback,
        resolution=args.resolution,
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    diag = payload["coverage_diagnostics"]
    print(
        f"Saved point-in-time schedule ({len(payload['schedule'])} {args.resolution} buckets, "
        f"{len(payload['ever_whitelisted_2y'])} unique 2Y eligible pairs, "
        f"median={diag['median_daily_pairs']}/day, empty={diag['empty_days']}, "
        f"post-May26 empty={diag['empty_days_post_2026_05']}) -> {out_path}"
    )


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    main()
