"""Reproducible historical comparisons and explicitly synthetic execution stress.

No private endpoints or trading commands. Run with the repository virtualenv.
Backtests require public market metadata and downloaded Bybit futures OHLCV.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PERIODS = {
    "bull_q1": "20240101-20240401",
    "bear_jun_jul": "20240601-20240801",
    "crash_april": "20240410-20240417",
    "crash_august": "20240801-20240808",
}
PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]


def candle_file(data_dir, pair, tf="5m", kind="futures"):
    return (
        Path(data_dir)
        / "futures"
        / f"{pair.replace('/', '_').replace(':', '_')}-{tf}-{kind}.feather"
    )


def inventory(data_dir):
    result = []
    for pair in PAIRS:
        for tf in ("5m", "15m", "1h", "4h", "1d"):
            path = candle_file(data_dir, pair, tf)
            if not path.exists():
                result.append({"pair": pair, "tf": tf, "missing": True})
                continue
            frame = pd.read_feather(path)
            dates = pd.to_datetime(frame.date, utc=True)
            delta = pd.Timedelta(tf.replace("m", "min").replace("d", "D"))
            gaps = dates.diff().dropna() != delta
            result.append(
                {
                    "pair": pair,
                    "tf": tf,
                    "rows": len(frame),
                    "start": str(dates.min()),
                    "end": str(dates.max()),
                    "gaps": int(gaps.sum()),
                    "duplicate_dates": int(dates.duplicated().sum()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    return result


def load_result(path):
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith(".json") and not name.endswith("_config.json"):
                content = json.loads(archive.read(name))
                if "strategy" in content:
                    return next(iter(content["strategy"].values()))
    raise ValueError(f"No strategy result in {path}")


def equity_metrics(summary, data_dir):
    """Reprice filled order inventory at 5m closes, including partial fills/DCA.

    For 1x longs, cashflows plus inventory reproduce futures equity. Closing
    adjustments reconcile to Freqtrade's net PnL, including its funding fees.
    Funding is allocated at trade close, not its actual payment time. This and
    candle sampling mean these metrics cannot certify tick-level compliance.
    """
    initial = float(summary["starting_balance"])
    start = pd.to_datetime(summary["backtest_start_ts"], unit="ms", utc=True)
    end = pd.to_datetime(summary["backtest_end_ts"], unit="ms", utc=True)
    index = pd.date_range(start, end, freq="5min", inclusive="both")
    cash_events = np.zeros(len(index))
    quantities = {pair: np.zeros(len(index)) for pair in PAIRS}
    slippage_cost = np.zeros(len(index))
    trades = summary["trades"]
    for trade in trades:
        if trade.get("is_short") or trade.get("leverage", 1) != 1:
            raise ValueError("Equity reconstruction only supports the audited 1x long profile")
        flow = 0.0
        remaining = 0.0
        for order in trade["orders"]:
            stamp = pd.to_datetime(order["order_filled_timestamp"], unit="ms", utc=True)
            loc = int(index.searchsorted(stamp))
            if not 0 <= loc < len(index):
                raise ValueError("Fill lies outside the backtest window")
            qty = float(order["amount"])
            price = float(order["safe_price"])
            entry = order["ft_is_entry"]
            signed_qty = qty if entry else -qty
            fee = float(trade["fee_open"] if entry else trade["fee_close"])
            cash = -signed_qty * price - qty * price * fee
            cash_events[loc] += cash
            quantities[trade["pair"]][loc] += signed_qty
            slippage_cost[loc] += qty * price
            remaining += signed_qty
            flow += cash
        if abs(remaining) > max(1e-6, float(trade["amount"]) * 1e-6):
            raise ValueError("Incomplete order inventory cannot be certified")
        close_loc = int(
            index.searchsorted(pd.to_datetime(trade["close_timestamp"], unit="ms", utc=True))
        )
        cash_events[close_loc] += float(trade["profit_abs"]) - flow

    equity = initial + np.cumsum(cash_events)
    for pair, events in quantities.items():
        if not np.any(events):
            continue
        frame = pd.read_feather(candle_file(data_dir, pair)).set_index("date")
        frame.index = pd.to_datetime(frame.index, utc=True)
        prices = frame["close"].reindex(index)
        holdings = np.cumsum(events)
        if prices[abs(holdings) > 1e-8].isna().any():
            raise ValueError(f"Missing candle while exposed: {pair}")
        equity += holdings * prices.fillna(0).to_numpy()
    if abs(equity[-1] - (initial + sum(t["profit_abs"] for t in trades))) > 0.02:
        raise ValueError("Reconstructed terminal equity does not reconcile")

    def drawdowns(values):
        curve = pd.Series(values, index=index)
        # Previous close is the observable approximation of UTC day opening equity.
        day_open = curve.shift(1).fillna(initial).groupby(curve.index.date).transform("first")
        peak = np.maximum.accumulate(np.r_[initial, values])[1:]
        return {
            "max_daily_dd_pct": float(max(0, ((day_open - curve) / initial).max()) * 100),
            "max_trailing_dd_pct": float(((peak - values) / initial).max() * 100),
            "net_profit_pct": float((values[-1] / initial - 1) * 100),
        }

    profits = np.array([t["profit_abs"] for t in trades], dtype=float)
    wins = profits[profits > 0]
    losses = profits[profits < 0]
    result = drawdowns(equity)
    result.update(
        trades=len(trades),
        win_rate_pct=float(len(wins) / len(trades) * 100) if trades else None,
        profit_factor=float(wins.sum() / -losses.sum()) if len(losses) else None,
        average_win_loss_ratio=float(wins.mean() / -losses.mean())
        if len(wins) and len(losses)
        else None,
        pnl_over_max_dd=float(profits.sum() / (result["max_trailing_dd_pct"] * initial / 100))
        if result["max_trailing_dd_pct"]
        else None,
        equity_basis="5m close inventory reconstruction, funding reconciled at trade close",
        account_sentinel_simulated=False,
    )
    result["slippage_sensitivity"] = {
        str(slip): drawdowns(equity - np.cumsum(slippage_cost) * slip)
        for slip in (0.001, 0.002, 0.003)
    }
    return result


def stress(seed=20261102, count=10000):
    """Execution counterexamples, not market forecasts or strategy backtests."""
    rng = np.random.default_rng(seed)
    shocks = rng.uniform(0, 0.40, size=count)
    slip = rng.uniform(0.001, 0.003, size=count)
    fee = 0.00055
    before = 0.24 * (shocks + 2 * fee + slip)
    after_smooth = 0.24 * (np.minimum(shocks, 0.06) + 2 * fee + slip)
    after_gap = 0.24 * (shocks + 2 * fee + slip)
    return {
        "seed": seed,
        "scenarios": count,
        "model": "three correlated $400 positions; loss and execution costs / $5000",
        "note": (
            "Uniform shocks are adversarial inputs, not estimated event probabilities. "
            "No simulated return target."
        ),
        "before_daily_breaches": int((before >= 0.025).sum()),
        "after_continuous_stop_daily_breaches": int((after_smooth >= 0.025).sum()),
        "after_gap_through_stop_daily_breaches": int((after_gap >= 0.025).sum()),
        "after_gap_through_stop_trailing_breaches": int((after_gap >= 0.038).sum()),
        "continuous_stop_max_loss_pct": float(after_smooth.max() * 100),
        "gap_max_loss_pct": float(after_gap.max() * 100),
        "three_positions_minus_10pct_with_03pct_slippage_loss_pct": 0.24
        * (0.10 + 0.003 + 2 * fee)
        * 100,
        "three_positions_minus_20pct_with_03pct_slippage_loss_pct": 0.24
        * (0.20 + 0.003 + 2 * fee)
        * 100,
    }


def run_backtests(args):
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    baseline = work / "baseline"
    baseline.mkdir(exist_ok=True)
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline_ref}:user_data/strategies/NostalgiaForInfinityX8_Prop.py"],
        cwd=ROOT,
    )
    (baseline / "NostalgiaForInfinityX8_Prop.py").write_bytes(source)
    before_config = json.loads(
        subprocess.check_output(
            ["git", "show", f"{args.baseline_ref}:user_data/config_prop_nfix8.json"], cwd=ROOT
        )
    )
    after_config = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
    runs = []
    for variant, config, strategy_path in (
        ("before", before_config, baseline),
        ("after", after_config, ROOT / "user_data/strategies"),
    ):
        config["exchange"].update(key="", secret="", demo_trading=False, pair_whitelist=PAIRS)
        config.pop("telegram", None)
        config.pop("api_server", None)
        config.update(
            dry_run=True,
            dry_run_wallet=5000,
            fee=0.00055,
            pairlists=[{"method": "StaticPairList"}],
            user_data_dir=str(work),
        )
        config_file = work / f"{variant}_config.json"
        config_file.write_text(json.dumps(config, indent=2))
        for name, timerange in PERIODS.items():
            dest = work / f"{variant}_{name}"
            dest.mkdir(exist_ok=True)
            command = [
                sys.executable,
                "-m",
                "freqtrade",
                "backtesting",
                "--config",
                str(config_file),
                "--strategy-path",
                str(strategy_path),
                "--datadir",
                str(args.data_dir.resolve()),
                "--timerange",
                timerange,
                "--cache",
                "none",
                "--export",
                "trades",
                "--backtest-directory",
                str(dest),
            ]
            print(f"Running {variant} {name}", flush=True)
            previous_archives = set(dest.glob("*.zip"))
            # Explicitly isolate simulations from any private/live config overrides.
            environment = {k: v for k, v in os.environ.items() if not k.startswith("FREQTRADE__")}
            with (dest / "run.log").open("w", encoding="utf-8") as output:
                process = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    env=environment,
                )
            archives = sorted(set(dest.glob("*.zip")) - previous_archives)
            row = {
                "variant": variant,
                "period": name,
                "timerange": timerange,
                "returncode": process.returncode,
            }
            if process.returncode != 0 or not archives:
                row["error"] = "No result archive; inspect run.log"
            else:
                row["archive"] = str(archives[-1].relative_to(ROOT))
                try:
                    row.update(equity_metrics(load_result(archives[-1]), args.data_dir))
                except (ValueError, KeyError, OSError, IndexError) as exc:
                    row["error"] = str(exc)
            runs.append(row)
            (work / "matrix.json").write_text(json.dumps(runs, indent=2, allow_nan=False))
            print(json.dumps(row), flush=True)
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "user_data/forensic/data/bybit")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "user_data/forensic")
    parser.add_argument("--run-backtests", action="store_true")
    parser.add_argument("--baseline-ref", default="192a933fca224e6ae0189db95a42eaf4bca2e7f1")
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "stress": stress(),
        "data_inventory": inventory(args.data_dir),
        "baseline_ref": args.baseline_ref,
        "full_stack_certified": False,
    }
    if args.run_backtests:
        if any(
            row.get("missing") or row.get("gaps") or row.get("duplicate_dates")
            for row in result["data_inventory"]
        ):
            raise ValueError("Missing or discontinuous OHLCV; inspect inventory before backtesting")
        result["backtests"] = run_backtests(args)
    output = args.work_dir / "validation.json"
    output.write_text(json.dumps(result, indent=2, allow_nan=False))
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
