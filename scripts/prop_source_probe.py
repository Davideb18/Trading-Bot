"""Record public source availability without claiming historical universe completeness."""

import concurrent.futures
import json
from datetime import UTC, datetime
from pathlib import Path

import requests


def probe(symbol):
    params = dict(category="linear", symbol=symbol, interval="1", limit=1,
                  start=1730419200000, end=1761955199999)
    try:
        response = requests.get("https://api.bybit.com/v5/market/kline",
                                params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return dict(symbol=symbol, http=response.status_code, params=params,
                    retCode=payload.get("retCode"), retMsg=payload.get("retMsg"),
                    rows=payload.get("result", {}).get("list", []))
    except requests.RequestException as exc:
        return dict(symbol=symbol, error=type(exc).__name__)


def main():
    symbols = "BTCUSDT OMUSDT ALPACAUSDT FTMUSDT MKRUSDT UXLINKUSDT NEIROETHUSDT VOXELUSDT ACTUSDT".split()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(probe, symbols))
    output = Path("user_data/forensic/strict_validation/source_probe.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(observed_at=datetime.now(UTC).isoformat(),
                                     note="Availability probe, not a complete dataset", results=results), indent=2))
    for result in results:
        print(result["symbol"], result.get("retCode", result.get("error")),
              "rows=", len(result.get("rows", [])))


if __name__ == "__main__":
    main()
