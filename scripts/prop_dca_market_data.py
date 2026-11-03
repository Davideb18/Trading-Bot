"""Public Bybit history only; resumable files, no credentials or trading endpoints."""

import argparse
import concurrent.futures
import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "user_data/forensic/dca_validation"
DATA = WORK / "data/bybit/futures"
END = int(pd.Timestamp("2025-01-01", tz="UTC").timestamp() * 1000)
BASE_COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK DOT LTC BCH TRX ATOM NEAR UNI AAVE ETC FIL ARB OP"
EXTRA_COINS = "SUI APT INJ ICP ALGO RUNE CRV GALA SAND IMX"


def prepare_configs():
    WORK.mkdir(parents=True, exist_ok=True)
    for filename, coins in (
        ("research.json", BASE_COINS),
        ("research30.json", BASE_COINS + " " + EXTRA_COINS),
    ):
        path = WORK / filename
        if path.exists():
            continue
        config = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
        config["exchange"].update(
            key="",
            secret="",
            demo_trading=False,
            pair_blacklist=[],
            pair_whitelist=[c + "/USDT:USDT" for c in coins.split()],
        )
        config.pop("api_server", None)
        config.pop("telegram", None)
        config.update(
            dry_run=True,
            dry_run_wallet=5000,
            stake_amount="unlimited",
            fee=0.00055,
            pairlists=[{"method": "StaticPairList"}],
            user_data_dir=str(WORK),
        )
        path.write_text(json.dumps(config, indent=2))


def history(session, symbol, endpoint, start, interval=None):
    cursor = END - 1
    rows = []
    while cursor >= start:
        params = {"category": "linear", "symbol": symbol, "end": cursor, "limit": 1000}
        if interval:
            params.update(interval=interval, start=start)
        else:
            params = {"category": "linear", "symbol": symbol, "endTime": cursor, "limit": 200}
        for attempt in range(6):
            try:
                response = session.get(
                    "https://api.bybit.com/v5/market/" + endpoint, params=params, timeout=30
                )
                response.raise_for_status()
                payload = response.json()
                if payload["retCode"] != 0:
                    raise ValueError(payload)
                batch = payload["result"]["list"]
                break
            except (requests.RequestException, ValueError):
                if attempt == 5:
                    raise
                time.sleep(2**attempt)
        if not batch:
            break
        if interval:
            stamps = [int(r[0]) for r in batch]
            rows.extend(
                [
                    [int(r[0]), *map(float, r[1:6])]
                    if len(r) >= 6
                    else [int(r[0]), *map(float, r[1:5]), 0.0]
                    for r in batch
                ]
            )
        else:
            stamps = [int(r["fundingRateTimestamp"]) for r in batch]
            rows.extend(
                [
                    [int(r["fundingRateTimestamp"]), float(r["fundingRate"]), 0.0, 0.0, 0.0, 0.0]
                    for r in batch
                ]
            )
        previous = cursor
        cursor = min(stamps) - 1
        if cursor >= previous:
            raise ValueError("Pagination made no progress")
        time.sleep(0.12)
    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    frame = frame.loc[(frame.date >= start) & (frame.date < END)]
    frame["date"] = pd.to_datetime(frame.date, unit="ms", utc=True)
    return frame.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def download(pair):
    symbol = pair.split("/")[0] + "USDT"
    stem = pair.replace("/", "_").replace(":", "_")
    session = requests.Session()
    specs = [
        ("5m", "futures", "kline", "5", "2023-08-01"),
        ("1d", "futures", "kline", "D", "2021-01-01"),
        ("1h", "mark", "mark-price-kline", "60", "2023-12-01"),
        ("1h", "funding_rate", "funding/history", None, "2023-12-01"),
    ]
    for tf, kind, endpoint, interval, start in specs:
        path = DATA / f"{stem}-{tf}-{kind}.feather"
        if not path.exists():
            start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
            frame = history(session, symbol, endpoint, start_ms, interval)
            if frame.empty:
                raise ValueError(f"No history: {pair} {tf} {kind}")
            frame.to_feather(path)
    base = pd.read_feather(DATA / f"{stem}-5m-futures.feather").set_index("date")
    for tf in ("15m", "1h", "4h"):
        target = DATA / f"{stem}-{tf}-futures.feather"
        if target.exists():
            continue
        frame = base.resample(tf.replace("m", "min"), label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
        frame.dropna().reset_index().to_feather(target)
    print(f"Completed {pair}: {len(base)} 5m bars", flush=True)


def liquidity_snapshot(pairs, filename="liquidity_snapshot.json"):
    session = requests.Session()
    response = session.get(
        "https://api.bybit.com/v5/market/tickers", params={"category": "linear"}, timeout=30
    )
    response.raise_for_status()
    tickers = {t["symbol"]: t for t in response.json()["result"]["list"]}
    rows = []
    for pair in pairs:
        symbol = pair.split("/")[0] + "USDT"
        response = session.get(
            "https://api.bybit.com/v5/market/orderbook",
            params={"category": "linear", "symbol": symbol, "limit": 50},
            timeout=30,
        )
        response.raise_for_status()
        book = response.json()["result"]
        bid, ask = float(book["b"][0][0]), float(book["a"][0][0])
        mid = (bid + ask) / 2
        rows.append(
            {
                "pair": pair,
                "timestamp_ms": book["ts"],
                "turnover_24h_usdt": float(tickers[symbol]["turnover24h"]),
                "spread_pct": (ask - bid) / mid * 100,
                "bid_depth_within_01pct_usdt": sum(
                    float(p) * float(q) for p, q in book["b"] if float(p) >= mid * 0.999
                ),
                "ask_depth_within_01pct_usdt": sum(
                    float(p) * float(q) for p, q in book["a"] if float(p) <= mid * 1.001
                ),
            }
        )
        time.sleep(0.12)
    (WORK / filename).write_text(json.dumps(rows, indent=2))


def main():
    prepare_configs()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="research.json")
    parser.add_argument("--manifest", default="data_manifest.json")
    parser.add_argument("--liquidity", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    pairs = json.loads((WORK / args.config).read_text())["exchange"]["pair_whitelist"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, pairs))
    manifest = []
    stems = {p.replace("/", "_").replace(":", "_") for p in pairs}
    for path in sorted(DATA.glob("*.feather")):
        if path.name.split("-")[0] not in stems:
            continue
        frame = pd.read_feather(path)
        gaps = 0
        tf = path.name.split("-")[1]
        if "funding_rate" not in path.name:
            gaps = int((frame.date.diff().dropna() != pd.Timedelta(tf.replace("m", "min"))).sum())
        manifest.append(
            {
                "file": path.name,
                "rows": len(frame),
                "start": str(frame.date.min()),
                "end": str(frame.date.max()),
                "gaps": gaps,
                "duplicates": int(frame.date.duplicated().sum()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (WORK / args.manifest).write_text(json.dumps(manifest, indent=2))
    if args.liquidity:
        liquidity_snapshot(pairs)
    print("Data manifest complete", flush=True)


if __name__ == "__main__":
    main()
