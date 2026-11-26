"""Reconstruct no-trade minutes only on days backed by parsed daily trade archives."""
import json
import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "user_data/forensic/strict_validation"
FUTURES = ROOT / "user_data/data/bybit/futures"


def verified_days(directory):
    days = set()
    for source in sorted(directory.glob("*-1m.feather")):
        manifest = source.with_name(source.name.replace("-1m.feather", ".csv.gz.manifest.json"))
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        frame = pd.read_feather(source)
        dates = pd.to_datetime(frame["date"], utc=True)
        day = pd.Timestamp(source.name.removeprefix(directory.name).removesuffix("-1m.feather"), tz="UTC")
        assert meta["symbol"] == directory.name and meta["trades"] > 0 and meta["bytes"] > 0
        assert len(meta["sha256"]) == 64 and len(frame) == meta["bars"]
        assert not dates.duplicated().any() and dates.is_monotonic_increasing
        assert (dates.dt.floor("D") == day).all()
        assert dates.min() == pd.Timestamp(meta["start"]) and dates.max() == pd.Timestamp(meta["end"])
        assert int((dates.max() - dates.min()).total_seconds() / 60) + 1 - len(frame) == meta["missing_internal_minutes"]
        days.add(day)
    return days


def reconstruct(frame, days):
    indexed = frame.set_index("date").sort_index()
    full = pd.date_range(indexed.index.min(), indexed.index.max(), freq="min")
    missing = full.difference(indexed.index)
    if any(day not in days for day in missing.floor("D")):
        raise ValueError("Missing minutes outside verified daily archives")
    result = indexed.reindex(full)
    previous = result["close"].ffill()
    for col in ("open", "high", "low", "close"):
        result.loc[missing, col] = previous.loc[missing]
    result.loc[missing, "volume"] = 0.
    result.index.name = "date"
    return result.reset_index(), len(missing)


def main():
    report = {"method": "Previous close and zero volume only for absent minutes on manifested daily trade archives",
              "limitation": "Raw gzip files were deleted; completeness relies on recorded successful full-file parsing and manifests, not a fresh source download.", "pairs": {}}
    backup = BASE / "archive_minutes_backup"
    backup.mkdir(exist_ok=True)
    for symbol in ("MKR", "NEIROETH", "OM", "UXLINK"):
        days = verified_days(BASE / "archive" / (symbol + "USDT"))
        path = FUTURES / f"{symbol}_USDT_USDT-1m-futures.feather"
        frame = pd.read_feather(path)
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        fixed, count = reconstruct(frame, days)
        for tf, rule in (("1m", None), ("5m", "5min"), ("15m", "15min"), ("1h", "1h"), ("4h", "4h"), ("1d", "1D")):
            target = FUTURES / f"{symbol}_USDT_USDT-{tf}-futures.feather"
            saved = backup / target.name
            if not saved.exists():
                shutil.copy2(target, saved)
            output = fixed if rule is None else fixed.set_index("date").resample(rule).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
            temp = target.with_suffix(".repair.tmp")
            output[["date", "open", "high", "low", "close", "volume"]].to_feather(temp)
            temp.replace(target)
        report["pairs"][symbol] = {"verified_days": len(days), "reconstructed_minutes": count}
        print(symbol, report["pairs"][symbol], flush=True)
    (BASE / "archive_minutes_repair.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
