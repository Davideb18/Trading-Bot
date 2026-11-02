"""Report sampled equity limits without inventing compliance or future profits."""

import argparse
import json
from pathlib import Path

from forensic_prop_validation import equity_metrics, load_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="Freqtrade result ZIP")
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Bybit historical data directory"
    )
    args = parser.parse_args()
    metrics = equity_metrics(load_result(args.result), args.data_dir)
    breached = metrics["max_daily_dd_pct"] >= 2.5 or metrics["max_trailing_dd_pct"] >= 3.8
    metrics["verdict"] = "SAMPLED_LIMIT_EXCEEDED" if breached else "NO_BREACH_AT_SAMPLED_CLOSES"
    metrics["full_stack_certified"] = False
    print(json.dumps(metrics, indent=2, allow_nan=False))
    return int(breached)


if __name__ == "__main__":
    raise SystemExit(main())
