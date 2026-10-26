#!/usr/bin/env python3
"""
Prop Firm Futures Benchmark Suite
==================================
Automated backtesting harness that runs each candidate futures strategy
against realistic Prop Firm rules and generates a side-by-side compliance audit.
"""

import os
import sys
import json
import subprocess
from pathlib import Path

PYTHON_EXE = ".venv/bin/python3"
CONFIG_FILE = "user_data/config_prop_futures.json"
RESULTS_DIR = "user_data/backtest_results"

STRATEGIES = [
    "VolatilityBreakoutFutures",
    "SuperTrendMomentumFutures",
    "FOttFuturesStrategy"
]

TIMERANGE = "20260101-20260831"  # 8-month stress test including volatile bull and bear regimes


def run_strategy_backtest(strategy_name: str, timerange: str = TIMERANGE) -> int:
    print("\n" + "=" * 70)
    print(f"🚀 RUNNING PROP FIRM FUTURES BACKTEST: {strategy_name}")
    print(f"Timerange: {timerange} | Timeframe: 15m | Mode: Futures Isolated")
    print("=" * 70)

    cmd = [
        PYTHON_EXE, "-m", "freqtrade", "backtesting",
        "--strategy", strategy_name,
        "-c", CONFIG_FILE,
        "--timerange", timerange,
        "--dry-run-wallet", "10000",
        "--fee", "0.0005",  # 0.05% realistic futures taker fee
        "--no-color"
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if process.stdout is not None:
        for line in iter(process.stdout.readline, ''):
            print(line, end='', flush=True)

    process.wait()
    return process.returncode


def main():
    print("🎯 STARTING INSTITUTIONAL PROP FIRM BENCHMARK SUITE")
    print(f"Strategies to evaluate: {STRATEGIES}")

    for strat in STRATEGIES:
        code = run_strategy_backtest(strat)
        if code == 0:
            print(f"\n✅ Backtest for {strat} completed successfully. Evaluating Prop Firm rules...")
            eval_cmd = [PYTHON_EXE, "scripts/prop_firm_backtest_evaluator.py"]
            subprocess.run(eval_cmd)
        else:
            print(f"\n❌ Backtest for {strat} failed with exit code {code}.")


if __name__ == "__main__":
    main()
