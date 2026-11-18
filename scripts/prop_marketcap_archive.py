"""Import free Coin Metrics history with provenance and explicit coverage limitations."""

import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1] / "user_data/forensic/strict_validation/coinmetrics"
URL = "https://github.com/coinmetrics/data/archive/refs/heads/master.zip"


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "community.zip"
    if not archive.exists():
        with requests.get(URL, timeout=120, stream=True) as response:
            response.raise_for_status()
            temporary = archive.with_suffix(".part")
            with temporary.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
            temporary.replace(archive)
    records = []
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            match = re.fullmatch(r"data-master/csv/([a-zA-Z0-9_.-]+)\.csv", entry.filename)
            if not match:
                continue
            asset = match[1]
            frame = pd.read_csv(io.BytesIO(bundle.read(entry)), low_memory=False)
            columns = [c for c in ("CapMrktEstUSD", "CapMrktCurUSD") if c in frame]
            if not columns:
                continue
            frame["date"] = pd.to_datetime(frame["time"], utc=True)
            frame = frame.loc[frame.date >= "2023-01-01"].copy()
            for metric in columns:
                values = pd.to_numeric(frame[metric], errors="coerce")
                valid = frame.loc[values.notna() & (values > 0), ["date"]].copy()
                valid["market_cap_usd"] = values.loc[valid.index]
                if valid.empty:
                    continue
                # A daily observation must not enter a decision during that same UTC day.
                valid["earliest_usable_at"] = valid.date + pd.Timedelta(days=1)
                valid.reset_index(drop=True).to_feather(ROOT / f"{asset}-{metric}.feather")
                records.append(dict(asset=asset, metric=metric, rows=len(valid),
                                    start=str(valid.date.min()), end=str(valid.date.max())))
        readme = next((n for n in bundle.namelist() if n == "data-master/README.md"), None)
        if readme:
            (ROOT / "SOURCE_README.md").write_bytes(bundle.read(readme))
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    report = dict(source=URL, downloaded_at=datetime.now(UTC).isoformat(), sha256=digest,
                  license="CC BY-NC 4.0; retain attribution; evaluate commercial licensing before production use",
                  caveat="Historical revised daily data, not a certified point-in-time vintage. Asset ticker mapping requires verification. Missing assets remain unknown.",
                  assets_with_cap=len({r["asset"] for r in records}), records=records)
    (ROOT / "manifest.json").write_text(json.dumps(report, indent=2))
    print("Capitalization history imported for", report["assets_with_cap"], "assets", flush=True)


if __name__ == "__main__":
    main()
