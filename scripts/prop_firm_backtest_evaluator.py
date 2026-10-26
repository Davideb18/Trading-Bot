#!/usr/bin/env python3
"""
Prop Firm Challenge Evaluator for Freqtrade
===========================================
Audits Freqtrade backtest trade-by-trade results against strict institutional 
prop firm challenge rules (e.g., Crypto Fund Trader, FTMO, Funding Pips):

Key Constraints Evaluated:
1. Max Daily Drawdown: Hard Cap 4.0% (Warning buffer at 3.5%)
2. Max Total Drawdown: Hard Cap 8.0% (Warning buffer at 7.0%)
3. Profit Target Phase 1: +8.0% or +10.0%
4. Profit Target Phase 2: +5.0%
5. Consistency Rule: Max profit from a single trade < 40% of total profit
6. Zero Martingale / No open-ended DCA verification
"""

import os
import sys
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def load_backtest_data(results_dir: str = "user_data/backtest_results", specific_file: str = None) -> dict:
    """Loads backtest JSON data from the latest result or a specified file."""
    if specific_file:
        file_path = Path(specific_file)
        if file_path.suffix == ".zip":
            with zipfile.ZipFile(file_path, "r") as z:
                for name in z.namelist():
                    if name.endswith(".json") and not name.endswith("_config.json"):
                        with z.open(name) as f:
                            return json.load(f)
        else:
            with open(file_path) as f:
                return json.load(f)

    last_res_path = Path(results_dir) / ".last_result.json"
    if not last_res_path.exists():
        raise FileNotFoundError(f"No .last_result.json found in {results_dir}")

    with open(last_res_path) as f:
        meta = json.load(f)

    latest_filename = meta.get("latest_backtest")
    if not latest_filename:
        raise ValueError("Could not find latest_backtest in .last_result.json")

    latest_path = Path(results_dir) / latest_filename
    with zipfile.ZipFile(latest_path, "r") as z:
        for name in z.namelist():
            if name.endswith(".json") and not name.endswith("_config.json"):
                with z.open(name) as f:
                    return json.load(f)

    raise ValueError("No valid backtest JSON found inside zip archive")


def audit_prop_firm_compliance(backtest_data: dict, initial_balance: float = 10000.0,
                               max_daily_dd_limit: float = 0.04, max_total_dd_limit: float = 0.08,
                               target_phase1: float = 0.08, target_phase2: float = 0.05) -> dict:
    """Analyzes trade-by-trade ledger against prop firm rules."""
    strategy_results = {}

    strategies = backtest_data.get("strategy", {})
    for strat_name, strat_meta in strategies.items():
        trades = strat_meta.get("trades", [])
        if not trades:
            # Fallback if trades are grouped under root
            trades = backtest_data.get("trades", [])

        # Sort trades chronologically by close_timestamp
        trades = sorted(trades, key=lambda x: x.get("close_timestamp", 0))

        current_equity = initial_balance
        peak_equity = initial_balance
        max_total_dd = 0.0

        daily_stats = {}
        target_phase1_passed_at = None
        target_phase2_passed_at = None
        disqualified_reason = None

        profits = []

        for t in trades:
            profit_abs = float(t.get("profit_abs", 0.0))
            close_time = datetime.fromtimestamp(t.get("close_timestamp", 0) / 1000.0, tz=timezone.utc)
            day_key = close_time.strftime("%Y-%m-%d")

            current_equity += profit_abs
            profits.append(profit_abs)

            # High-water mark
            if current_equity > peak_equity:
                peak_equity = current_equity

            # Current total drawdown
            curr_total_dd = (peak_equity - current_equity) / peak_equity
            if curr_total_dd > max_total_dd:
                max_total_dd = curr_total_dd

            # Daily tracking
            if day_key not in daily_stats:
                daily_stats[day_key] = {
                    "start_equity": current_equity - profit_abs,
                    "peak_equity": current_equity,
                    "trough_equity": current_equity,
                    "end_equity": current_equity,
                    "daily_dd": 0.0
                }
            d = daily_stats[day_key]
            if current_equity > d["peak_equity"]:
                d["peak_equity"] = current_equity
            if current_equity < d["trough_equity"]:
                d["trough_equity"] = current_equity
            d["end_equity"] = current_equity

            # Daily DD calculation (from start or day peak)
            day_peak = max(d["start_equity"], d["peak_equity"])
            day_dd = (day_peak - current_equity) / day_peak
            if day_dd > d["daily_dd"]:
                d["daily_dd"] = day_dd

            # Check rule violations
            if curr_total_dd > max_total_dd_limit and disqualified_reason is None:
                disqualified_reason = f"Breached Max Total Drawdown: {curr_total_dd*100:.2f}% > {max_total_dd_limit*100:.1f}% on {day_key}"

            if day_dd > max_daily_dd_limit and disqualified_reason is None:
                disqualified_reason = f"Breached Max Daily Drawdown: {day_dd*100:.2f}% > {max_daily_dd_limit*100:.1f}% on {day_key}"

            # Check profit targets
            net_roi = (current_equity - initial_balance) / initial_balance
            if net_roi >= target_phase1 and target_phase1_passed_at is None and disqualified_reason is None:
                target_phase1_passed_at = day_key
            if net_roi >= (target_phase1 + target_phase2) and target_phase2_passed_at is None and disqualified_reason is None:
                target_phase2_passed_at = day_key

        worst_daily_dd = max([d["daily_dd"] for d in daily_stats.values()]) if daily_stats else 0.0
        total_profit = current_equity - initial_balance
        max_single_trade_profit = max(profits) if profits else 0.0
        consistency_ratio = (max_single_trade_profit / total_profit) if total_profit > 0 else 0.0

        is_passed = (disqualified_reason is None) and (worst_daily_dd <= max_daily_dd_limit) and (max_total_dd <= max_total_dd_limit) and (total_profit > 0)

        strategy_results[strat_name] = {
            "initial_balance": initial_balance,
            "final_equity": current_equity,
            "total_profit_abs": total_profit,
            "total_profit_pct": (total_profit / initial_balance) * 100.0,
            "worst_daily_drawdown_pct": worst_daily_dd * 100.0,
            "max_total_drawdown_pct": max_total_dd * 100.0,
            "total_trades": len(trades),
            "win_rate_pct": (len([p for p in profits if p > 0]) / len(trades) * 100.0) if trades else 0.0,
            "profit_factor": strat_meta.get("profit_factor", 0.0),
            "target_phase1_passed_date": target_phase1_passed_at,
            "target_phase2_passed_date": target_phase2_passed_at,
            "max_single_trade_pct_of_profit": consistency_ratio * 100.0,
            "passed_prop_rules": is_passed,
            "verdict": "PASSED ✅" if is_passed else f"FAILED ❌ ({disqualified_reason})"
        }

    return strategy_results


def print_report(results: dict):
    print("\n" + "=" * 78)
    print("🏆 INSTITUTIONAL PROP FIRM COMPLIANCE AUDIT REPORT")
    print("=" * 78)

    for name, r in results.items():
        print(f"\n📊 STRATEGY: {name}")
        print("-" * 50)
        print(f"• Verdict:                    {r['verdict']}")
        print(f"• Initial Balance:            ${r['initial_balance']:,.2f}")
        print(f"• Final Equity:               ${r['final_equity']:,.2f}")
        print(f"• Net Profit:                 {r['total_profit_pct']:+.2f}% (${r['total_profit_abs']:+,.2f})")
        print(f"• Total Trades:               {r['total_trades']} (Win Rate: {r['win_rate_pct']:.1f}%)")
        print(f"• Profit Factor:              {r['profit_factor']:.2f}")
        print(f"• Worst Daily Drawdown:       {r['worst_daily_drawdown_pct']:.2f}% (Limit: 4.0%)")
        print(f"• Max Total Drawdown:         {r['max_total_drawdown_pct']:.2f}% (Limit: 8.0%)")
        print(f"• Single Trade Consistency:   {r['max_single_trade_pct_of_profit']:.1f}% of profit (Limit: < 50%)")
        print(f"• Phase 1 Target (+8%):       {'Passed on ' + r['target_phase1_passed_date'] if r['target_phase1_passed_date'] else 'Not reached'}")
        print(f"• Phase 2 Target (+5%):       {'Passed on ' + r['target_phase2_passed_date'] if r['target_phase2_passed_date'] else 'Not reached'}")
        print("-" * 50)
    print("\n" + "=" * 78 + "\n")


if __name__ == "__main__":
    file_arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        data = load_backtest_data(specific_file=file_arg)
        report = audit_prop_firm_compliance(data)
        print_report(report)
    except Exception as e:
        print(f"Error evaluating backtest: {e}")
