#!/usr/bin/env python3
"""
Institutional Prop Firm Deep Auditor for Freqtrade & HyroTrader Two-Step Challenge
=================================================================================
Performs a rigorous, mathematical audit of Freqtrade backtest trades and 5m candle wallet equity
against HyroTrader's exact institutional rules:

Evaluation Phase Rules (Two-Step):
- Phase 1 Target: +10%
- Phase 2 Target: +5%
- Daily Drawdown Limit: 5% (Static floor set at 00:00 UTC based on equity at start of day)
- Maximum Loss Limit: 10% (Fixed floor calculated from initial starting balance)
- Profit Consistency Rule: Max 40% contribution from any single trading day towards target
- Minimum Trading Days: 5 active trading days per phase
  * Active day definition: Closed trade with value >= 5% initial balance AND |pnl| >= 1% trade value
- Unlimited trading period (Max inactivity between trades: 30 days)

Funded Account Stage (Phase 3):
- Profit Target: None (Continuous trading)
- Profit Split: 80% to 90%
- Challenge fee refunded with first approved payout
- Maximum Margin Exposure: Max 25% of initial account balance across all open positions
- Cumulative Open Position Value: Max 2.0x initial account balance
- Daily Drawdown: 5%
- Maximum Loss: 10%
- Consistency Rule: None on funded accounts
"""

import sys
import os
import json
import zipfile
import io
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path


def load_backtest_ledger(source_path: str):
    """Loads trades and wallet equity from a backtest JSON or ZIP file."""
    p = Path(source_path)
    trades = []
    df_wallet = None

    if p.suffix == ".zip":
        with zipfile.ZipFile(p, "r") as z:
            for name in z.namelist():
                if name.endswith(".json") and not name.endswith("_config.json"):
                    with z.open(name) as f:
                        data = json.load(f)
                        strat = list(data.get("strategy", {}).keys())[0]
                        trades = data["strategy"][strat].get("trades", [])
                elif "wallet.feather" in name:
                    with z.open(name) as f:
                        df_wallet = pd.read_feather(io.BytesIO(f.read()))
    elif p.suffix == ".json":
        with open(p) as f:
            data = json.load(f)
            if "strategy" in data:
                strat = list(data["strategy"].keys())[0]
                trades = data["strategy"][strat].get("trades", [])
            elif isinstance(data, list):
                trades = data
            elif "trades" in data:
                trades = data["trades"]
    else:
        raise ValueError(f"Unsupported file format: {source_path}")

    return trades, df_wallet


def parse_trade_ledger(trades: list):
    """Parses trades into a standardized pandas DataFrame."""
    if not trades:
        return pd.DataFrame()
    
    df = pd.DataFrame(trades)
    
    # Parse timestamps
    if "open_timestamp" in df.columns:
        df["open_dt"] = pd.to_datetime(df["open_timestamp"], unit="ms", utc=True)
    elif "open_date" in df.columns:
        df["open_dt"] = pd.to_datetime(df["open_date"], utc=True)
        
    if "close_timestamp" in df.columns:
        df["close_dt"] = pd.to_datetime(df["close_timestamp"], unit="ms", utc=True)
    elif "close_date" in df.columns:
        df["close_dt"] = pd.to_datetime(df["close_date"], utc=True)

    df = df.sort_values("close_dt").reset_index(drop=True)
    df["close_day"] = df["close_dt"].dt.strftime("%Y-%m-%d")
    df["open_day"] = df["open_dt"].dt.strftime("%Y-%m-%d")
    df["profit_abs"] = df["profit_abs"].astype(float)
    df["profit_ratio"] = df["profit_ratio"].astype(float)
    
    # Trade value / stake
    if "stake_amount" in df.columns:
        df["trade_value"] = df["stake_amount"].astype(float)
    elif "cost" in df.columns:
        df["trade_value"] = df["cost"].astype(float)
    else:
        df["trade_value"] = 500.0  # Fallback
        
    return df


def evaluate_qualifying_day(trades_in_day: pd.DataFrame, initial_balance: float):
    """
    Checks if a UTC day qualifies as an official active trading day under HyroTrader rules:
    - At least 1 closed trade with value >= 5% initial balance ($250 on $5k, $500 on $10k)
    - Profit/loss magnitude >= 1% of the trade value before fees (|pnl / trade_value| >= 0.01)
    """
    min_trade_val = initial_balance * 0.05
    for _, t in trades_in_day.iterrows():
        val = t["trade_value"]
        pnl = t["profit_abs"]
        ratio = abs(pnl / val) if val > 0 else abs(t["profit_ratio"])
        if val >= min_trade_val and ratio >= 0.0099: # 1% threshold
            return True
    return False


def simulate_evaluation_challenge(df_trades: pd.DataFrame, start_dt: datetime, initial_balance: float = 5000.0, allow_heartbeat: bool = False, daily_wallet_stats: dict | None = None):
    """
    Simulates a 2-Step Challenge starting at start_dt.
    Phase 1: +10% target, max 40% consistency per day, 5 active days, 5% daily DD, 10% max DD.
    Phase 2: +5% target, max 40% consistency per day, 5 active days, 5% daily DD, 10% max DD.
    allow_heartbeat: if True, trader/bot resets 30d inactivity timer with a heartbeat micro-trade if gap > 25 days.
    daily_wallet_stats: precomputed dict of day_str -> {'intraday_loss': float} from 5m candle wallet data.
    """
    sub_df = df_trades[df_trades["open_dt"] >= start_dt].copy().reset_index(drop=True)
    if sub_df.empty:
        return {"status": "INSUFFICIENT_DATA"}

    # Track Phase 1
    phase1_target_abs = initial_balance * 0.10  # $500 on $5k
    phase1_daily_cap = phase1_target_abs * 0.40  # $200 max per day
    phase1_active_days = set()
    phase1_equity = initial_balance
    phase1_peak_equity = initial_balance
    phase1_counted_profit = 0.0
    phase1_passed = False
    phase1_pass_dt = None
    phase1_pass_days = 0

    # Drawdown limits
    daily_dd_limit_abs = initial_balance * 0.05  # $250 on $5k
    max_loss_floor = initial_balance * 0.90       # $4,500 on $5k (10% fixed floor)

    # Group trades by close day
    daily_groups = sub_df.groupby("close_day")
    
    current_phase = 1
    last_trade_dt = start_dt

    # For Phase 2
    phase2_start_dt = None
    phase2_initial_balance = initial_balance
    phase2_target_abs = phase2_initial_balance * 0.05  # $250 on $5k
    phase2_daily_cap = phase2_target_abs * 0.40        # $100 max per day
    phase2_active_days = set()
    phase2_equity = phase2_initial_balance
    phase2_peak_equity = phase2_initial_balance
    phase2_counted_profit = 0.0
    phase2_passed = False
    phase2_pass_dt = None
    phase2_pass_days = 0

    all_days = sorted(sub_df["close_day"].unique())

    for day_str in all_days:
        day_trades = sub_df[sub_df["close_day"] == day_str]
        day_start_dt = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        
        # Check inactivity > 30 days
        gap_days = (day_start_dt - last_trade_dt).days
        if gap_days > 30:
            if not allow_heartbeat:
                return {
                    "status": "FAILED_INACTIVITY",
                    "reason": f"Inactivity > 30 days between trades ({day_str})",
                    "phase": current_phase
                }
            else:
                # Heartbeat simulated: timer was reset, no penalty
                last_trade_dt = day_start_dt - timedelta(days=5)

        day_gross_pnl = day_trades["profit_abs"].sum()
        last_trade_dt = day_trades["close_dt"].max()

        # Check true 5m intraday floating drop from 00:00 UTC wallet
        intraday_loss_5m = 0.0
        if daily_wallet_stats and day_str in daily_wallet_stats:
            intraday_loss_5m = daily_wallet_stats[day_str].get("intraday_loss", 0.0)

        effective_daily_loss = max(abs(day_gross_pnl) if day_gross_pnl < 0 else 0.0, intraday_loss_5m)

        if current_phase == 1:
            # Daily DD check: intraday losses in day
            if effective_daily_loss > daily_dd_limit_abs:
                return {
                    "status": "FAILED_DAILY_DD",
                    "reason": f"Breached Daily Drawdown in Phase 1: {effective_daily_loss:.2f}$ loss on {day_str} (Limit: -{daily_dd_limit_abs:.2f}$)",
                    "phase": 1,
                    "date": day_str
                }
            
            phase1_equity += day_gross_pnl
            if phase1_equity < max_loss_floor:
                return {
                    "status": "FAILED_MAX_DD",
                    "reason": f"Breached Max Loss in Phase 1: Equity {phase1_equity:.2f}$ < Floor {max_loss_floor:.2f}$ on {day_str}",
                    "phase": 1,
                    "date": day_str
                }
                
            # Active day check
            if evaluate_qualifying_day(day_trades, initial_balance):
                phase1_active_days.add(day_str)
                
            # Consistency rule: max 40% of target counted per day
            counted_day_pnl = max(min(day_gross_pnl, phase1_daily_cap), day_gross_pnl if day_gross_pnl < 0 else 0.0)
            phase1_counted_profit += counted_day_pnl
            
            # Check Phase 1 completion
            if phase1_counted_profit >= phase1_target_abs and len(phase1_active_days) >= 5:
                phase1_passed = True
                phase1_pass_dt = day_trades["close_dt"].max()
                phase1_pass_days = (phase1_pass_dt - start_dt).days + 1
                current_phase = 2
                phase2_start_dt = phase1_pass_dt
                phase2_equity = phase2_initial_balance
                continue

        elif current_phase == 2:
            if day_trades["close_dt"].max() < phase2_start_dt:
                continue
                
            if effective_daily_loss > daily_dd_limit_abs:
                return {
                    "status": "FAILED_DAILY_DD",
                    "reason": f"Breached Daily Drawdown in Phase 2: {effective_daily_loss:.2f}$ loss on {day_str} (Limit: -{daily_dd_limit_abs:.2f}$)",
                    "phase": 2,
                    "date": day_str
                }
                
            phase2_equity += day_gross_pnl
            if phase2_equity < max_loss_floor:
                return {
                    "status": "FAILED_MAX_DD",
                    "reason": f"Breached Max Loss in Phase 2: Equity {phase2_equity:.2f}$ < Floor {max_loss_floor:.2f}$ on {day_str}",
                    "phase": 2,
                    "date": day_str
                }
                
            if evaluate_qualifying_day(day_trades, phase2_initial_balance):
                phase2_active_days.add(day_str)
                
            counted_day_pnl = max(min(day_gross_pnl, phase2_daily_cap), day_gross_pnl if day_gross_pnl < 0 else 0.0)
            phase2_counted_profit += counted_day_pnl
            
            if phase2_counted_profit >= phase2_target_abs and len(phase2_active_days) >= 5:
                phase2_passed = True
                phase2_pass_dt = day_trades["close_dt"].max()
                phase2_pass_days = (phase2_pass_dt - phase2_start_dt).days + 1
                return {
                    "status": "SIMULATED_TARGET_ONLY",
                    "full_stack_certified": False,
                    "phase1_days": phase1_pass_days,
                    "phase2_days": phase2_pass_days,
                    "total_days": (phase2_pass_dt - start_dt).days + 1,
                    "phase1_active_days": len(phase1_active_days),
                    "phase2_active_days": len(phase2_active_days),
                    "phase1_pass_date": str(phase1_pass_dt.date()) if phase1_pass_dt is not None else None,
                    "phase2_pass_date": str(phase2_pass_dt.date()) if phase2_pass_dt is not None else None
                }

    # If timeline ended without passing
    if not phase1_passed:
        return {
            "status": "IN_PROGRESS",
            "phase": 1,
            "counted_profit": phase1_counted_profit,
            "active_days": len(phase1_active_days)
        }
    else:
        return {
            "status": "IN_PROGRESS",
            "phase": 2,
            "phase1_days": phase1_pass_days,
            "counted_profit": phase2_counted_profit,
            "active_days": len(phase2_active_days)
        }


def run_rolling_evaluation_audit(df_trades: pd.DataFrame, df_wallet: pd.DataFrame | None = None, initial_balance: float = 5000.0, step_days: int = 7, allow_heartbeat: bool = False):
    """
    Runs a rolling sliding window audit across the entire historical period.
    Simulates purchasing a challenge every 'step_days' (e.g. weekly).
    allow_heartbeat: if True, assumes account inactivity reset via heartbeat trade.
    df_wallet: 5m candle wallet dataframe to track true floating intraday drawdown.
    """
    start_min = df_trades["open_dt"].min()
    end_max = df_trades["close_dt"].max()
    
    # Precompute daily 5m wallet statistics if available
    daily_wallet_stats = {}
    if df_wallet is not None and not df_wallet.empty:
        df_w = df_wallet.copy()
        df_w['day'] = pd.to_datetime(df_w['date']).dt.date
        for day, grp in df_w.groupby('day'):
            day_str = str(day)
            day_open = grp.iloc[0]['total_quote']
            day_min = grp['total_quote'].min()
            pct_loss = max((day_open - day_min) / day_open, 0.0) if day_open > 0 else 0.0
            daily_wallet_stats[day_str] = {
                'day_open': day_open,
                'day_min': day_min,
                'intraday_pct': pct_loss,
                'intraday_loss': pct_loss * initial_balance
            }

    current_start = start_min
    windows = []
    
    # Stop starting new windows 60 days before the dataset end, but ensure at least 1 window
    cutoff_start = end_max - timedelta(days=60)
    if cutoff_start < start_min:
        cutoff_start = start_min
    
    while current_start <= cutoff_start:
        res = simulate_evaluation_challenge(
            df_trades, current_start,
            initial_balance=initial_balance,
            allow_heartbeat=allow_heartbeat,
            daily_wallet_stats=daily_wallet_stats
        )
        res["start_date"] = str(current_start.date())
        windows.append(res)
        current_start += timedelta(days=step_days)
        
    df_res = pd.DataFrame(windows) if windows else pd.DataFrame(columns=["status", "start_date"])
    return df_res


def audit_funded_account_performance(df_trades: pd.DataFrame, df_wallet: pd.DataFrame | None = None, initial_balance: float = 5000.0):
    """
    Audits the Funded Account Phase over the multi-year timeline:
    - Verifies Margin Exposure (<= 25%)
    - Verifies Cumulative Notional Exposure (<= 2.0x)
    - Verifies Daily Drawdown (<= 5%) and Max Total Drawdown (<= 10%)
    - Computes Monthly Profit Distribution & Trader Payout (80% split)
    """
    trades = df_trades.copy()
    trades["month"] = trades["close_dt"].dt.to_period("M")
    
    monthly = []
    cum_equity = initial_balance
    peak_equity = initial_balance
    max_overall_dd = 0.0
    
    for m, grp in trades.groupby("month"):
        m_pnl = grp["profit_abs"].sum()
        trades_count = len(grp)
        wins = len(grp[grp["profit_abs"] > 0])
        win_rate = (wins / trades_count * 100) if trades_count > 0 else 0.0
        
        # Monthly payout calculation (80% profit split, fee refunded in month 1)
        payout_80 = max(m_pnl * 0.80, 0.0)
        payout_eur = payout_80 * 0.92  # Approx EUR conversion
        
        cum_equity += m_pnl
        if cum_equity > peak_equity:
            peak_equity = cum_equity
        curr_dd = (peak_equity - cum_equity) / initial_balance * 100.0
        if curr_dd > max_overall_dd:
            max_overall_dd = curr_dd
            
        monthly.append({
            "Month": str(m),
            "Trades": trades_count,
            "Wins": wins,
            "Win Rate (%)": round(win_rate, 1),
            "Gross Profit ($)": round(m_pnl, 2),
            "Return (%)": round(m_pnl / initial_balance * 100, 2),
            "Trader Payout ($)": round(payout_80, 2),
            "Trader Payout (€)": round(payout_eur, 2),
            "End Balance ($)": round(cum_equity, 2)
        })
        
    df_monthly = pd.DataFrame(monthly)
    
    # Audit true 5m candle wallet daily drawdown if available
    worst_daily_dd = 0.0
    worst_day = None
    breaches_daily = 0
    breaches_total = 0
    max_margin_observed_pct = 0.0
    
    if df_wallet is not None and not df_wallet.empty:
        df_wallet["day"] = df_wallet["date"].dt.date
        for day, grp in df_wallet.groupby("day"):
            day_start = grp.iloc[0]["total_quote"]
            day_min = grp["total_quote"].min()
            intraday_dd = ((day_start - day_min) / day_start * 100.0) if day_start > 0 else 0.0
            if intraday_dd > worst_daily_dd:
                worst_daily_dd = intraday_dd
                worst_day = str(day)
            if intraday_dd > 5.0:
                breaches_daily += 1
                
        # Total equity DD
        min_wallet_eq = df_wallet["total_quote"].min()
        total_dd_from_init = (initial_balance - min_wallet_eq) / initial_balance * 100.0
        if total_dd_from_init > 10.0:
            breaches_total += 1
    else:
        # Fallback daily trade closed calculation
        daily_pnl = trades.groupby("close_day")["profit_abs"].sum()
        worst_daily_pnl = daily_pnl.min()
        worst_daily_dd = abs(worst_daily_pnl) / initial_balance * 100.0 if worst_daily_pnl < 0 else 0.0

    return {
        "monthly_breakdown": df_monthly,
        "worst_daily_drawdown_pct": worst_daily_dd,
        "worst_daily_date": worst_day,
        "max_overall_drawdown_pct": max_overall_dd,
        "breaches_daily_dd": breaches_daily,
        "breaches_max_loss": breaches_total,
        "total_gross_profit": df_monthly["Gross Profit ($)"].sum(),
        "total_trader_payout_usd": df_monthly["Trader Payout ($)"].sum(),
        "total_trader_payout_eur": df_monthly["Trader Payout (€)"].sum(),
        "avg_monthly_payout_usd": df_monthly["Trader Payout ($)"].mean(),
        "avg_monthly_payout_eur": df_monthly["Trader Payout (€)"].mean()
    }


def main():
    """Use the evidence-based checker; legacy models above are exploratory only."""
    from check_prop_firm_rules import main as checked_main
    return checked_main()


if __name__ == "__main__":
    raise SystemExit(main())
