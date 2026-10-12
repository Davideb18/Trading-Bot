#!/usr/bin/env python3
"""
Tailored Author-Aligned Strategy Benchmark Engine
Executes backtests where EACH strategy is tested strictly against its
specific author-recommended constraints, max_open_trades, pairlists, and risk rules.
"""

import os
import sys
import json
import zipfile
import subprocess

STRATEGY_CONFIGS = [
    {
        "strategy": "CombinedBinHAndCluc",
        "description": "Berlinguyinca optimal setup (max_open_trades=2, exit_profit_only=True)",
        "config": "user_data/author_configs/config_CombinedBinHAndCluc.json",
        "pairlist": "user_data/backtest_config.json"
    },
    {
        "strategy": "E0V1E",
        "description": "Ssssi official spot setup (max_open_trades=2, alts-only whitelist, 10m timeout)",
        "config": "user_data/author_configs/config_E0V1E.json",
        "pairlist": "user_data/author_configs/pairlist_E0V1E.json"
    },
    {
        "strategy": "SMAOffsetProtectOptV1",
        "description": "Stash86 optimal setup (max_open_trades=3, EWO filters, 1h informative)",
        "config": "user_data/author_configs/config_SMAOffsetProtectOptV1.json",
        "pairlist": "user_data/backtest_config.json"
    },
    {
        "strategy": "NostalgiaForInfinityX7",
        "description": "Iterativ flagship setup (max_open_trades=6, full 40-pair macro list)",
        "config": "user_data/config.json",
        "pairlist": "user_data/backtest_config.json"
    }
]

PERIODS = [
    {"name": "Jan_2026_Stress_Crash", "timerange": "20260101-20260201", "desc": "1-Month Market Crash (-12.3%)"},
    {"name": "Full_Year_2025", "timerange": "20250101-20260101", "desc": "1 Full Macro Year (Bull + Consolidations)"},
    {"name": "Recent_8_Months_2026", "timerange": "20260101-20260831", "desc": "8 Months Year-to-Date (Volatile Bear/Chop)"}
]

RESULTS_DIR = "user_data/backtest_results"
OUTPUT_FILE = "user_data/backtest_results/tailored_author_benchmark_results.json"


def get_latest_stats(target_strategy):
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
                strat_dict = data.get("strategy", {})
                if target_strategy in strat_dict:
                    stats = strat_dict[target_strategy]
                    total_trades = stats.get("total_trades", 0)
                    wins = stats.get("wins", 0)
                    losses = stats.get("losses", 0)
                    profit_abs = stats.get("profit_total_abs", 0.0)
                    profit_pct = stats.get("profit_total", 0.0) * 100.0
                    drawdown_pct = stats.get("max_drawdown_account", 0.0) * 100.0
                    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
                    profit_factor = stats.get("profit_factor", 0.0)
                    cagr = stats.get("cagr", 0.0) * 100.0 if stats.get("cagr") is not None else 0.0

                    return {
                        "strategy": target_strategy,
                        "total_trades": total_trades,
                        "wins": wins,
                        "losses": losses,
                        "win_rate_pct": round(win_rate, 2),
                        "profit_total_abs": round(profit_abs, 2),
                        "profit_total_pct": round(profit_pct, 2),
                        "max_drawdown_pct": round(drawdown_pct, 2),
                        "profit_factor": round(profit_factor, 2),
                        "cagr_pct": round(cagr, 2)
                    }
    return None


def run_tailored():
    results = []
    print("=" * 80)
    print("🎯 STARTING TAILORED AUTHOR-ALIGNED BENCHMARK")
    print("Each strategy executes with its EXACT recommended slot sizing, pairlist, and rules")
    print("Capital: 100 USDT | Spot Fee: 0.075%")
    print("=" * 80)

    for period in PERIODS:
        p_name = period["name"]
        trange = period["timerange"]
        desc = period["desc"]
        print(f"\n================================================================================")
        print(f"📅 PERIOD: {p_name} ({trange}) — {desc}")
        print(f"================================================================================")

        for item in STRATEGY_CONFIGS:
            strat = item["strategy"]
            cfg = item["config"]
            plist = item["pairlist"]
            s_desc = item["description"]

            print(f"\n▶ Testing: {strat}")
            print(f"   Architecture: {s_desc}")
            print(f"   Configs: {cfg} + {plist} | Timerange: {trange}")

            cmd = [
                ".venv/bin/python3", "-m", "freqtrade", "backtesting",
                "--strategy", strat,
                "-c", "user_data/config.json",
                "-c", cfg,
                "-c", plist,
                "--timerange", trange,
                "--dry-run-wallet", "100",
                "--fee", "0.00075",
                "--no-color"
            ]

            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1200)
                if proc.returncode != 0:
                    print(f"❌ Error on {strat}: {proc.stderr[:300]}")
                    continue

                stats = get_latest_stats(strat)
                if stats:
                    stats["period"] = p_name
                    stats["timerange"] = trange
                    stats["architecture"] = s_desc
                    results.append(stats)
                    print(f"✅ Result {strat}: Trades={stats['total_trades']}, "
                          f"WinRate={stats['win_rate_pct']}%, "
                          f"Net Profit={stats['profit_total_pct']}%, "
                          f"MaxDD={stats['max_drawdown_pct']}%")
                else:
                    print(f"⚠️ Could not parse stats for {strat}")

            except subprocess.TimeoutExpired:
                print(f"⏰ Timeout on {strat} ({trange})")
            except Exception as e:
                print(f"❌ Exception on {strat}: {e}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=4)

    print("\n" + "=" * 80)
    print("🏁 TAILORED BENCHMARK COMPLETED! Results saved to:", OUTPUT_FILE)
    print("=" * 80)


if __name__ == "__main__":
    run_tailored()
