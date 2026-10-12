#!/usr/bin/env python3
"""
Comprehensive Strategy Benchmark Engine
Executes standardized Freqtrade backtests across multiple candidate strategies
with 100 USDT starting capital and realistic Binance Spot fees.
"""

import os
import sys
import json
import zipfile
import subprocess
from datetime import datetime

STRATEGIES = [
    "CombinedBinHAndCluc",
    "SMAOffsetProtectOptV1",
    "E0V1E",
    "HighFreqScalper",
    "NostalgiaForInfinityX7"
]

WINDOWS = [
    {"name": "Jan_2026_Stress_Crash", "timerange": "20260101-20260201"},
    {"name": "Full_Year_2025", "timerange": "20250101-20260101"},
    {"name": "Recent_8_Months_2026", "timerange": "20260101-20260831"}
]

CONFIGS = ["user_data/config.json", "user_data/backtest_config.json"]
RESULTS_DIR = "user_data/backtest_results"
SUMMARY_OUTPUT = "user_data/backtest_results/benchmark_summary.json"


def get_latest_backtest_stats():
    last_file_path = os.path.join(RESULTS_DIR, ".last_result.json")
    if not os.path.exists(last_file_path):
        return None
    with open(last_file_path, "r") as f:
        meta = json.load(f)
    latest_zip = os.path.join(RESULTS_DIR, meta.get("latest_backtest", ""))
    if not os.path.exists(latest_zip):
        return None

    with zipfile.ZipFile(latest_zip, "r") as z:
        for name in z.namelist():
            if name.endswith(".json") and not name.endswith("_config.json"):
                data = json.load(z.open(name))
                for strat_name, stats in data.get("strategy", {}).items():
                    total_trades = stats.get("total_trades", 0)
                    wins = stats.get("wins", 0)
                    losses = stats.get("losses", 0)
                    draws = stats.get("draws", 0)
                    profit_abs = stats.get("profit_total_abs", 0.0)
                    profit_pct = stats.get("profit_total", 0.0) * 100.0
                    drawdown_pct = stats.get("max_drawdown_account", 0.0) * 100.0
                    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
                    profit_factor = stats.get("profit_factor", 0.0)
                    cagr = stats.get("cagr", 0.0) * 100.0 if stats.get("cagr") is not None else 0.0

                    return {
                        "strategy": strat_name,
                        "total_trades": total_trades,
                        "wins": wins,
                        "losses": losses,
                        "draws": draws,
                        "win_rate_pct": round(win_rate, 2),
                        "profit_total_abs": round(profit_abs, 2),
                        "profit_total_pct": round(profit_pct, 2),
                        "max_drawdown_pct": round(drawdown_pct, 2),
                        "profit_factor": round(profit_factor, 2),
                        "cagr_pct": round(cagr, 2)
                    }
    return None


def run_benchmark():
    all_results = []
    print("=" * 70)
    print("🚀 STARTING COMPREHENSIVE STRATEGY BENCHMARK")
    print(f"Candidate Strategies: {STRATEGIES}")
    print(f"Starting Capital: 100 USDT | Fee: 0.075% (Spot)")
    print("=" * 70)

    for win in WINDOWS:
        win_name = win["name"]
        trange = win["timerange"]
        print(f"\n==================================================")
        print(f"📅 TESTING WINDOW: {win_name} ({trange})")
        print(f"==================================================")

        for strat in STRATEGIES:
            print(f"\n▶ Running backtest: Strategy={strat} | Timerange={trange}...")
            cmd = [
                ".venv/bin/python3", "-m", "freqtrade", "backtesting",
                "--strategy", strat,
                "-c", CONFIGS[0],
                "-c", CONFIGS[1],
                "--timerange", trange,
                "--dry-run-wallet", "100",
                "--fee", "0.00075",
                "--no-color"
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1200)
                if proc.returncode != 0:
                    print(f"❌ Error running {strat}: {proc.stderr[:300]}")
                    continue

                stats = get_latest_backtest_stats()
                if stats:
                    stats["window"] = win_name
                    stats["timerange"] = trange
                    all_results.append(stats)
                    print(f"✅ Finished {strat}: Trades={stats['total_trades']}, "
                          f"WinRate={stats['win_rate_pct']}%, "
                          f"Profit={stats['profit_total_pct']}%, "
                          f"MaxDD={stats['max_drawdown_pct']}%")
                else:
                    print(f"⚠️ Could not read stats for {strat}")

            except subprocess.TimeoutExpired:
                print(f"⏰ Timeout reached for {strat} on window {win_name}")
            except Exception as e:
                print(f"❌ Exception on {strat}: {e}")

    with open(SUMMARY_OUTPUT, "w") as f:
        json.dump(all_results, f, indent=4)

    print("\n" + "=" * 70)
    print("🏁 BENCHMARK COMPLETE! Summary saved to:", SUMMARY_OUTPUT)
    print("=" * 70)


if __name__ == "__main__":
    run_benchmark()
