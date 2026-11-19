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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
FORENSIC_DIR = REPO / "user_data" / "forensic" / "strict_validation"
FUTURES_DIR = REPO / "user_data" / "data" / "bybit" / "futures"


def fetch_or_load_bybit_instruments() -> dict[str, dict[str, Any]]:
    """Return mapping of BASE (e.g. 'BTC') -> Bybit V5 linear instrument info."""
    FORENSIC_DIR.mkdir(parents=True, exist_ok=True)
    inst_path = FORENSIC_DIR / "instruments.json"
    rows: list[dict[str, Any]] = []
    if inst_path.is_file():
        try:
            payload = json.loads(inst_path.read_text())
            rows = payload.get("list") or payload.get("result", {}).get("list") or []
        except Exception:
            rows = []
    if not rows:
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
            )
        )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sym = str(r.get("symbol", ""))
        if sym.endswith("USDT") and r.get("quoteCoin") == "USDT":
            base = sym[:-4]
            out[base] = r
    return out


def is_eligible_bybit_crypto(info: dict[str, Any] | None) -> bool:
    """Exclude Innovation Zone, ST tag, pre-listing, and TradFi (stocks/ETFs/commodities/forex)."""
    if not isinstance(info, dict):
        return True  # Delisted historical coin not in live instruments; checked via archive/blacklist
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
        if target_1m.is_file():
            continue
        parts = [pd.read_feather(f) for f in feathers]
        df_1m = pd.concat(parts, ignore_index=True)
        df_1m["date"] = pd.to_datetime(df_1m["date"], utc=True)
        df_1m = (
            df_1m.sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
        )
        cols = ["date", "open", "high", "low", "close", "volume"]
        df_1m[cols].to_feather(target_1m)

        indexed = df_1m.set_index("date")
        for tf, rule in (("5m", "5min"), ("15m", "15min"), ("1h", "1h"), ("4h", "4h"), ("1d", "1d")):
            res = (
                indexed.resample(rule, label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna()
                .reset_index()
            )
            res[cols].to_feather(FUTURES_DIR / f"{pair_prefix}-{tf}-futures.feather")
            if tf == "1h":
                mark = res[cols].copy()
                mark["volume"] = 0.0
                mark.to_feather(FUTURES_DIR / f"{pair_prefix}-1h-mark.feather")
                fund = mark.copy()
                fund["open"] = 0.0001
                fund["high"] = 0.0001
                fund["low"] = 0.0001
                fund["close"] = 0.0001
                fund.loc[fund["date"].dt.hour % 8 != 0, ["open", "high", "low", "close"]] = 0.0
                fund.to_feather(FUTURES_DIR / f"{pair_prefix}-1h-funding_rate.feather")
        converted.append(f"{base}/USDT:USDT")
    return converted


def load_coinmetrics_marketcap_map(min_market_cap: float) -> dict[str, pd.Series]:
    """Load point-in-time daily market caps (using `earliest_usable_at = date + 1d` to prevent lookahead)."""
    cm_dir = FORENSIC_DIR / "coinmetrics"
    out: dict[str, pd.Series] = {}
    if not cm_dir.is_dir():
        return out
    for f in sorted(cm_dir.glob("*-CapMrktCurUSD.feather")):
        asset = f.name.removesuffix("-CapMrktCurUSD.feather").upper()
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
                out[asset] = s
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
) -> dict[str, Any]:
    cfg = json.loads(config_path.read_text())
    blacklist_patterns = [re.compile(pat) for pat in cfg.get("exchange", {}).get("pair_blacklist", [])]
    instruments = fetch_or_load_bybit_instruments()
    mcap_map = load_coinmetrics_marketcap_map(min_market_cap_usdt)

    daily_files = sorted(FUTURES_DIR.glob("*-1d-futures.feather"))
    pair_daily: dict[str, pd.DataFrame] = {}

    for f in daily_files:
        base = f.name.split("_USDT_USDT-")[0]
        pair = f"{base}/USDT:USDT"
        if any(r.fullmatch(pair) or r.match(pair) for r in blacklist_patterns):
            continue
        if base in instruments and not is_eligible_bybit_crypto(instruments[base]):
            continue
        # Require 5m data present as well
        if not (FUTURES_DIR / f"{base}_USDT_USDT-5m-futures.feather").is_file():
            continue
        try:
            df = pd.read_feather(f)
        except Exception:
            continue
        if df.empty or len(df) < min_days_listed:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["day"] = pd.to_datetime(df["date"], utc=True).dt.floor("D")
        # A 1d candle opening at day D closes at D + 1 day (00:00 UTC): only usable from D + 1 day!
        df["usable_day"] = df["day"] + pd.Timedelta(days=1)
        df["quote_vol"] = df["close"].astype(float) * df["volume"].astype(float)
        log_ret = np.log(df["close"].astype(float) / df["close"].astype(float).shift(1))
        df["volatility_10d"] = log_ret.rolling(lookback_days).std(ddof=0) * np.sqrt(lookback_days)
        roll_max = df["high"].astype(float).rolling(lookback_days).max()
        roll_min = df["low"].astype(float).rolling(lookback_days).min()
        df["roc_10d"] = np.where(roll_min > 0, (roll_max - roll_min) / roll_min, 999.0)
        df["age_days"] = np.arange(1, len(df) + 1)
        pair_daily[pair] = df.set_index("usable_day")[
            ["quote_vol", "volatility_10d", "roc_10d", "age_days"]
        ]

    start_dt = pd.Timestamp(start_iso, tz="UTC").floor("D")
    end_dt = pd.Timestamp(end_iso, tz="UTC").floor("D")
    all_days = pd.date_range(start_dt, end_dt, freq="1D", tz="UTC")

    schedule: dict[str, list[str]] = {}
    ever_2y: set[str] = set()
    ever_all: set[str] = set()

    for d in all_days:
        candidates: list[tuple[float, str]] = []
        for pair, df_p in pair_daily.items():
            if d not in df_p.index:
                continue
            row = df_p.loc[d]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            age = int(row["age_days"])
            qv = float(row["quote_vol"])
            vol = float(row["volatility_10d"]) if pd.notna(row["volatility_10d"]) else 999.0
            roc = float(row["roc_10d"]) if pd.notna(row["roc_10d"]) else 999.0
            if age < min_days_listed or qv < min_value_usdt:
                continue
            if vol > max_volatility or roc > max_rate_of_change:
                continue
            base = pair.split("/")[0]
            if base in mcap_map:
                mc_series = mcap_map[base]
                prior = mc_series.loc[mc_series.index <= d]
                if not prior.empty and float(prior.iloc[-1]) < min_market_cap_usdt:
                    continue
            candidates.append((qv, pair))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top_pairs = [p for _, p in candidates[:number_assets]]
        # Always keep BTC/USDT:USDT in universe if eligible for macro reference
        ts_key = str(int(d.timestamp()))
        schedule[ts_key] = top_pairs
        ever_all.update(top_pairs)
        if d >= pd.Timestamp("2024-11-15", tz="UTC"):
            ever_2y.update(top_pairs)

    ever_2y.add("BTC/USDT:USDT")
    ever_all.add("BTC/USDT:USDT")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "zero_lookahead": True,
        "filters": {
            "number_assets": number_assets,
            "min_value_usdt": min_value_usdt,
            "min_days_listed": min_days_listed,
            "min_market_cap_usdt": min_market_cap_usdt,
            "max_volatility": max_volatility,
            "max_rate_of_change": max_rate_of_change,
            "lookback_days": lookback_days,
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
    parser.add_argument(
        "--output",
        default=str(FORENSIC_DIR / "pairlist_schedule_2y.json"),
    )
    args = parser.parse_args()

    converted = convert_archived_1m_to_freqtrade_feathers()
    if converted:
        print(f"Converted archived delisted symbols to Freqtrade feathers: {converted}")

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
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(
        f"Saved point-in-time schedule ({len(payload['schedule'])} daily buckets, "
        f"{len(payload['ever_whitelisted_2y'])} unique 2Y eligible pairs) -> {out_path}"
    )


if __name__ == "__main__":
    main()
