"""Reproducible DCA capital study. Does not start a bot or access private APIs."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd
from forensic_prop_validation import equity_metrics, load_result
from prop_dca_market_data import prepare_configs


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "user_data/forensic/dca_validation"
DATA = WORK / "data/bybit"
VARIANTS = {
    "safe_2": (2, 0.02),
    "safe_3": (3, 0.02),
    "safe_4": (4, 0.02),
    "diagnostic_10pct": (3, 0.10),
    "diagnostic_24pct": (3, 0.24),
    "wide_30": (3, 0.02),
    "reserve_30": (3, 0.02),
}


def statistics(values, initial=5000):
    return {
        "net_profit_pct": float((values.iloc[-1] - values.iloc[0]) / initial * 100),
        "max_daily_dd_pct": float(
            max(
                0,
                (
                    (
                        values.shift(1)
                        .fillna(values.iloc[0])
                        .groupby(values.index.date)
                        .transform("first")
                        - values
                    )
                    / initial
                ).max(),
            )
            * 100
        ),
        "max_trailing_dd_pct": float(((values.cummax() - values) / initial).max() * 100),
    }


def evaluate(archive):
    summary = load_result(archive)
    result, index, equity, turnover = equity_metrics(summary, DATA, return_curve=True)
    curve = pd.Series(equity, index=index)
    trades = summary["trades"]
    times = sorted(
        [summary["backtest_start_ts"], summary["backtest_end_ts"]]
        + [t["open_timestamp"] for t in trades]
    )
    result["max_entry_inactivity_days_including_boundaries"] = float(max(np.diff(times)) / 86400000)
    for label, only_entries in (("all_fills", False), ("entry_fills", True)):
        stamps = sorted(
            [summary["backtest_start_ts"], summary["backtest_end_ts"]]
            + [
                o["order_filled_timestamp"]
                for t in trades
                for o in t["orders"]
                if not only_entries or o["ft_is_entry"]
            ]
        )
        gaps = np.diff(stamps)
        longest = int(np.argmax(gaps))
        result[f"max_{label}_inactivity_days"] = float(gaps[longest] / 86400000)
        result[f"max_{label}_inactivity_interval"] = [
            str(pd.to_datetime(stamps[i], unit="ms", utc=True)) for i in (longest, longest + 1)
        ]
    result["dca_trades"] = sum(sum(o["ft_is_entry"] for o in t["orders"]) > 1 for t in trades)
    result["entry_fills"] = sum(o["ft_is_entry"] for t in trades for o in t["orders"])
    result["pairs_traded"] = sorted({t["pair"] for t in trades})
    result["periods"] = {}
    for name, start, end in [
        ("train_h1", "2024-01-01", "2024-07-01"),
        ("validation_h2", "2024-07-01", "2025-01-01"),
        ("april_crash", "2024-04-10", "2024-04-18"),
        ("august_crash", "2024-08-01", "2024-08-09"),
        ("june_july", "2024-06-01", "2024-08-01"),
    ]:
        part = curve.loc[start:end]
        result["periods"][name] = statistics(part)
    result["cost_sensitivity"] = {}
    for slip in (0.0, 0.001, 0.002, 0.003):
        adjusted = curve - turnover * slip
        pnl = np.array(
            [
                t["profit_abs"] - slip * sum(o["amount"] * o["safe_price"] for o in t["orders"])
                for t in trades
            ]
        )
        stats = statistics(adjusted)
        stats["net_profit_pct"] = float((adjusted.iloc[-1] - 5000) / 50)
        stats["win_rate_pct"] = float((pnl > 0).mean() * 100) if len(pnl) else None
        stats["profit_factor"] = (
            float(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum()) if (pnl < 0).any() else None
        )
        stats["first_8pct_day"] = (
            float((adjusted[adjusted >= 5400].index[0] - index[0]).total_seconds() / 86400)
            if (adjusted >= 5400).any()
            else None
        )
        stats["first_5pct_day"] = (
            float((adjusted[adjusted >= 5250].index[0] - index[0]).total_seconds() / 86400)
            if (adjusted >= 5250).any()
            else None
        )
        stats["first_10pct_day"] = (
            float((adjusted[adjusted >= 5500].index[0] - index[0]).total_seconds() / 86400)
            if (adjusted >= 5500).any()
            else None
        )
        daily = adjusted.resample("1D").last()
        for days in (15, 30, 45):
            changes = daily.diff(days).dropna()
            stats[f"best_{days}d_profit_pct"] = float(changes.max() / 50)
            stats[f"{days}d_windows_8pct"] = int((changes >= 400).sum())
            stats[f"{days}d_windows"] = len(changes)
        # Moving block bootstrap of daily PnL: dependence preserved within 7-day blocks.
        # Descriptive interval for mean daily return, NOT probability of challenge success.
        changes = daily.diff().dropna().to_numpy()
        blocks = np.lib.stride_tricks.sliding_window_view(changes, 7)
        rng = np.random.default_rng(20261103)
        means = np.array(
            [
                blocks[rng.integers(0, len(blocks), size=53)].ravel()[: len(changes)].mean()
                for _ in range(2000)
            ]
        )
        stats["daily_pnl_7day_block_bootstrap_95pct_interval_usd"] = np.quantile(
            means, [0.025, 0.975]
        ).tolist()
        result["cost_sensitivity"][str(slip)] = stats
    result["archive"] = str(archive.relative_to(ROOT))
    pd.DataFrame({"date": index, "equity": equity, "cumulative_turnover": turnover}).to_feather(
        archive.parent / "equity.feather"
    )
    return result


def run(name):
    prepare_configs()
    wide = name in ("wide_30", "reserve_30")
    manifest_file = "data_manifest_30.json" if wide else "data_manifest.json"
    manifest = json.loads((WORK / manifest_file).read_text())
    if len(manifest) != (210 if wide else 140) or any(
        row["gaps"] or row["duplicates"] for row in manifest
    ):
        raise ValueError("Incomplete/discontinuous data manifest")
    slots, ratio = VARIANTS[name]
    dest = WORK / name
    dest.mkdir(exist_ok=True)
    config = json.loads((WORK / ("research30.json" if wide else "research.json")).read_text())
    # Bybit's bulk tier pagination can omit symbols. Never substitute invented tiers.
    tiers_path = DATA / "futures/leverage_tiers_USDT.json"
    tiers = json.loads(tiers_path.read_text()) if tiers_path.exists() else {"data": {}}
    missing = [p for p in config["exchange"]["pair_whitelist"] if not tiers["data"].get(p)]
    if missing:
        exchange = ccxt.bybit(
            {
                "enableRateLimit": True,
                "timeout": 30000,
                "options": {"fetchMarkets": {"types": ["linear"]}},
            }
        )
        for pair in missing:
            tiers["data"][pair] = exchange.fetch_market_leverage_tiers(pair)
            if not tiers["data"][pair]:
                raise ValueError(f"Missing exchange risk tiers: {pair}")
        tiers["updated"] = datetime.now(UTC).isoformat()
        tiers_path.write_text(json.dumps(tiers))
    config.update(
        max_open_trades=slots, tradable_balance_ratio=ratio, user_data_dir=str(dest.resolve())
    )
    if name == "reserve_30":
        config["stake_amount"] = 10
    # Limit public metadata requests to linear instruments.
    for key in ("ccxt_config", "ccxt_async_config"):
        config["exchange"][key]["options"] = {"fetchMarkets": {"types": ["linear"]}}
    path = dest / "config.json"
    path.write_text(json.dumps(config, indent=2))
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "freqtrade",
        "backtesting",
        "--config",
        str(path),
        "--strategy-path",
        str(ROOT / "user_data/strategies"),
        "--datadir",
        str(DATA),
        "--timerange",
        "20240101-20250101",
        "--cache",
        "none",
        "--export",
        "trades",
        "--backtest-directory",
        str(dest),
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("FREQTRADE__")}
    before = set(dest.glob("*.zip"))
    print(f"Running {name}", flush=True)
    with (dest / "run.log").open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    archives = sorted(set(dest.glob("*.zip")) - before)
    if process.returncode or not archives:
        raise RuntimeError(f"Backtest failed: {dest / 'run.log'}")
    result = evaluate(archives[-1])
    (dest / "metrics.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "trades",
                    "net_profit_pct",
                    "max_daily_dd_pct",
                    "max_trailing_dd_pct",
                    "dca_trades",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("variants", nargs="*", default=list(VARIANTS))
    args = parser.parse_args()
    if args.summarize:
        results = {}
        for variant in args.variants:
            archive = max((WORK / variant).glob("*.zip"))
            results[variant] = evaluate(archive)
            results[variant]["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            results[variant]["slots"], results[variant]["allocation_ratio"] = VARIANTS[variant]
        output = {
            "full_stack_certified": False,
            "variants": results,
            "data_manifest": json.loads((WORK / "data_manifest.json").read_text()),
            "liquidity_snapshot": json.loads((WORK / "liquidity_snapshot.json").read_text()),
        }
        for name in ("data_manifest_30", "liquidity_snapshot30"):
            path = WORK / f"{name}.json"
            if path.exists():
                output[name] = json.loads(path.read_text())
        output["source_sha256"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                ROOT / "scripts/prop_dca_study.py",
                ROOT / "scripts/prop_dca_market_data.py",
                ROOT / "scripts/forensic_prop_validation.py",
                ROOT / "user_data/strategies/NostalgiaForInfinityX8.py",
                ROOT / "user_data/strategies/NostalgiaForInfinityX8_Prop.py",
            )
        }
        (ROOT / "docs/prop_dca_results.json").write_text(
            json.dumps(output, indent=2, allow_nan=False)
        )
    else:
        for variant in args.variants:
            run(variant)
