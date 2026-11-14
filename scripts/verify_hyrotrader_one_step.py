import json
import sys
import numpy as np
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.analyze_noncompounded_multiyear import (
    ROOT, FUT_DATA, SPOT_DATA, load_5m_closes
)

def run_sliding_one_step():
    dyn_path = ROOT / "user_data/backtest_results/master_realistic_dynamic_5_years_trades.json"
    dyn_trades = json.loads(dyn_path.read_text())
    
    curated_cfg = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
    curated_bases = {p.split("/")[0] for p in curated_cfg["exchange"]["pair_whitelist"]}
    curated_dyn_trades = [
        t for t in dyn_trades if t["pair"].split("/")[0] in curated_bases
    ]
    
    # 2024-01-01 to 2026-08-16
    start_ts = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    end_ts = int(pd.Timestamp("2026-08-16", tz="UTC").timestamp() * 1000)
    trades = [t for t in curated_dyn_trades if start_ts <= t["open_timestamp"] <= end_ts]
    trades.sort(key=lambda t: t["open_timestamp"])
    
    target_bal = 5000.0
    r_target = 0.24
    slots = 4
    slot_cap = target_bal * (r_target / slots)  # 300.0
    
    # Pre-scale trades
    scaled_trades = []
    for t in trades:
        orig_entry = t["orders"][0].get("cost", t["stake_amount"])
        scale = slot_cap / orig_entry if orig_entry > 0 else 1.0
        profit_usd = t["profit_abs"] * scale
        open_dt = pd.to_datetime(t["open_timestamp"], unit="ms", utc=True)
        close_dt = pd.to_datetime(t["close_timestamp"], unit="ms", utc=True)
        scaled_trades.append({
            "pair": t["pair"],
            "open_dt": open_dt,
            "close_dt": close_dt,
            "open_ts": t["open_timestamp"],
            "close_ts": t["close_timestamp"],
            "profit_usd": profit_usd,
            "stake_usd": slot_cap,
            "cost_usd": t["orders"][0]["cost"] * scale if "cost" in t["orders"][0] else slot_cap,
            "profit_ratio": t["profit_ratio"],
        })
    
    # Slide start date across all weeks from 2024-01-01 to 2026-01-01
    start_dates = pd.date_range("2024-01-01", "2026-01-01", freq="7D", tz="UTC")
    
    attempts = []
    
    target_profit = 500.0  # 10% on $5,000
    daily_cap = 200.0     # 40% consistency cap per day
    max_loss_usd = 300.0  # 6% Max loss
    daily_loss_usd = 200.0 # 4% Daily loss
    
    for s_dt in start_dates:
        # Evaluate challenge starting at s_dt
        current_equity = target_bal
        high_water = target_bal
        cumulative_target_profit = 0.0
        qualifying_days = set()
        
        passed = False
        failed = False
        fail_reason = ""
        days_taken = 0
        
        # Day tracking
        day_trades = {}
        relevant_trades = [t for t in scaled_trades if t["open_dt"] >= s_dt]
        if not relevant_trades:
            continue
            
        for t in relevant_trades:
            close_day = t["close_dt"].strftime("%Y-%m-%d")
            if close_day not in day_trades:
                day_trades[close_day] = []
            day_trades[close_day].append(t)
            
            # Check qualifying trade: cost >= 5% ($250) and abs(profit_ratio) >= 1%
            if t["stake_usd"] >= 250.0 and abs(t["profit_ratio"]) >= 0.01:
                qualifying_days.add(close_day)
                
            current_equity += t["profit_usd"]
            if current_equity > high_water:
                high_water = current_equity
                
            trailing_dd = high_water - current_equity
            if trailing_dd >= max_loss_usd or (target_bal - current_equity) >= max_loss_usd:
                failed = True
                fail_reason = f"Max Loss 6% Breached (DD: ${trailing_dd:.1f})"
                break
                
            # Add to target profit with consistency cap
            # Day profit capped at $200 for target evaluation
            # Recompute total target profit
            total_counted_profit = 0.0
            for d, d_tr in day_trades.items():
                d_pnl = sum(x["profit_usd"] for x in d_tr)
                # Cap positive days at daily_cap, losing days count in full
                counted_pnl = min(d_pnl, daily_cap) if d_pnl > 0 else d_pnl
                total_counted_profit += counted_pnl
                
            if total_counted_profit >= target_profit and len(qualifying_days) >= 5:
                passed = True
                days_taken = (t["close_dt"] - s_dt).days
                break
                
        attempts.append({
            "start": s_dt.strftime("%Y-%m-%d"),
            "passed": passed,
            "failed": failed,
            "fail_reason": fail_reason,
            "days_taken": days_taken if passed else None,
            "qualifying_days": len(qualifying_days),
        })
        
    df_att = pd.DataFrame(attempts)
    if df_att.empty:
        print("No cohorts tested (insufficient backtest or dataset data).")
        return
    pass_rate = df_att["passed"].mean() * 100.0
    pass_days = df_att[df_att["passed"]]["days_taken"]
    
    print("=== HYROTRADER ONE-STEP SLIDING WINDOW SIMULATION (Weekly Starts 2024-2026) ===")
    print(f"Total Cohorts Tested: {len(df_att)}")
    print(f"Pass Rate: {pass_rate:.1f}%")
    print(f"Fail Rate (Max Loss 6% breached): {(df_att['failed'].mean() * 100.0):.1f}%")
    print(f"Median Days to Pass: {pass_days.median():.0f} days (~{pass_days.median()/30:.1f} months)")
    print(f"Fastest Pass: {pass_days.min():.0f} days (~{pass_days.min()/30:.1f} months)")
    print(f"Slowest Pass: {pass_days.max():.0f} days (~{pass_days.max()/30:.1f} months)")
    print(f"Average Qualifying Days at Pass: {df_att[df_att['passed']]['qualifying_days'].mean():.1f} days (min required: 5)")

if __name__ == "__main__":
    run_sliding_one_step()
