"""Download official archived trades and derive observed 1m bars, retaining provenance.

Archive presence is not proof of instrument classification, funding or mark-price coverage.
No price is forward-filled across missing trade minutes.
"""

import argparse
import concurrent.futures
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1] / "user_data/forensic/strict_validation"
BASE = "https://public.bybit.com/trading/"


def session():
    client = requests.Session()
    client.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    return client


def inventory(symbol):
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        raise ValueError("Invalid USDT symbol")
    with session() as client:
        response = client.get(BASE + symbol + "/", timeout=60)
        response.raise_for_status()
    files = sorted(set(re.findall(r'href="(' + re.escape(symbol)
                                 + r'\d{4}-\d{2}-\d{2}\.csv\.gz)"', response.text)))
    return dict(symbol=symbol, source=BASE + symbol + "/", files=files,
                observed_at=datetime.now(UTC).isoformat())


def download(job):
    symbol, filename = job
    directory = ROOT / "archive" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / filename
    result_path = directory / (filename + ".manifest.json")
    if result_path.exists():
        return json.loads(result_path.read_text())
    url = BASE + symbol + "/" + filename
    if not raw.exists():
        with session() as client, client.get(url, timeout=120, stream=True) as response:
            response.raise_for_status()
            temporary = raw.with_suffix(raw.suffix + ".part")
            with temporary.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
            temporary.replace(raw)
    parts = []
    trade_count = 0
    previous_end = None
    for chunk in pd.read_csv(raw, compression="gzip", chunksize=200_000,
                             usecols=["timestamp", "price", "size", "foreignNotional"]):
        chunk["date"] = pd.to_datetime(chunk.timestamp, unit="s", utc=True)
        chunk = chunk.sort_values("date", kind="stable").set_index("date")
        if previous_end is not None and chunk.index.min() < previous_end:
            raise ValueError(f"Archive chunk order is not chronological: {url}")
        previous_end = chunk.index.max()
        grouped = chunk.resample("1min")
        frame = grouped.agg(open=("price", "first"), high=("price", "max"),
                            low=("price", "min"), close=("price", "last"),
                            volume=("size", "sum"), turnover=("foreignNotional", "sum"))
        parts.append(frame.dropna())
        trade_count += len(chunk)
    if not parts:
        raise ValueError(f"No trades in {url}")
    bars = pd.concat(parts).groupby(level=0, sort=True).agg(
        dict(open="first", high="max", low="min", close="last", volume="sum", turnover="sum"))
    target = directory / (filename.removesuffix(".csv.gz") + "-1m.feather")
    bars.reset_index().to_feather(target)
    with raw.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    result = dict(symbol=symbol, source=url, raw=str(raw), output=str(target),
                  sha256=digest, bytes=raw.stat().st_size, trades=trade_count, bars=len(bars),
                  start=str(bars.index.min()), end=str(bars.index.max()),
                  missing_internal_minutes=int((bars.index.max()-bars.index.min()).total_seconds()/60)
                      + 1 - len(bars),
                  downloaded_at=datetime.now(UTC).isoformat(),
                  caveat="Observed trade bars only; no mark, funding or classification evidence")
    result_path.write_text(json.dumps(result, indent=2))
    print(f"{symbol} {filename}: {len(bars)} minute bars", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default="OMUSDT ALPACAUSDT FTMUSDT MKRUSDT UXLINKUSDT NEIROETHUSDT VOXELUSDT ACTUSDT".split())
    parser.add_argument("--start", default="2024-11-01")
    parser.add_argument("--end", default="2026-11-18", help="Exclusive UTC date")
    parser.add_argument("--sample", action="store_true", help="First and last archive day plus OM crash; not a complete dataset")
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        inventories = list(pool.map(inventory, args.symbols))
        (ROOT / "archive_inventory.json").write_text(json.dumps(inventories, indent=2))
        jobs = []
        for item in inventories:
            symbol = item["symbol"]
            files = [f for f in item["files"] if args.start <= f[len(symbol):len(symbol)+10] < args.end]
            if args.sample and files:
                selected = {files[0], files[-1]}
                selected.update(f for f in files if symbol == "OMUSDT"
                                and "2025-04-12" <= f[len(symbol):len(symbol)+10] <= "2025-04-14")
                files = sorted(selected)
            jobs.extend((symbol, f) for f in files)
            print(symbol, "available files", len(item["files"]), "selected", len(files), flush=True)
        if not args.inventory_only:
            results = list(pool.map(download, jobs))
            (ROOT / "archive_download_manifest.json").write_text(json.dumps(
                dict(sample_only=args.sample, start=args.start, end=args.end, results=results), indent=2))


if __name__ == "__main__":
    main()
