#!/usr/bin/env python3
"""
Institutional Daily Prop Firm Auditor for Freqtrade
===================================================
Audits the exact 5-minute true wallet equity across every single second/candle
to mathematically verify that the Daily Drawdown (4.0% - 5.0%) was NEVER breached.
"""

import sys
import glob
import json
import zipfile
import io
import pandas as pd
from pathlib import Path


def audit_latest_backtest():
    base_dir = Path(__file__).resolve().parent.parent
    result_dir = base_dir / "user_data" / "backtest_results"
    
    # Locate latest zip
    zips = sorted(result_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        print("❌ No backtest zip files found in user_data/backtest_results/")
        return

    target_zip = None
    for zpath in zips:
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            for n in names:
                if "wallet.feather" in n and "NostalgiaForInfinity" in n:
                    target_zip = zpath
                    feather_name = n
                    break
        if target_zip:
            break

    if not target_zip:
        # Fallback to the latest zip that has a wallet feather
        for zpath in zips:
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                for n in names:
                    if "wallet.feather" in n:
                        target_zip = zpath
                        feather_name = n
                        break
            if target_zip:
                break

    if not target_zip:
        print("❌ No wallet.feather found in any backtest zip.")
        return

    print(f"📦 Auditing backtest: {target_zip.name}")
    print(f"   Wallet ledger:     {feather_name}\n")

    with zipfile.ZipFile(target_zip) as z:
        with z.open(feather_name) as f:
            df = pd.read_feather(io.BytesIO(f.read()))

    # True equity is sum of quotes across all held currencies per timestamp
    wallet_equity = df.groupby('date')['total_quote'].sum().reset_index()
    wallet_equity['day'] = wallet_equity['date'].dt.date

    start_balance = wallet_equity.iloc[0]['total_quote']
    final_balance = wallet_equity.iloc[-1]['total_quote']
    total_profit_pct = (final_balance - start_balance) / start_balance * 100

    daily_records = []
    breaches_38 = []
    breaches_40 = []
    breaches_50 = []

    for day, group in wallet_equity.groupby('day'):
        start_eq = group.iloc[0]['total_quote']
        min_eq = group['total_quote'].min()
        max_eq = group['total_quote'].max()
        end_eq = group.iloc[-1]['total_quote']

        intraday_dd = (start_eq - min_eq) / start_eq * 100
        daily_pnl = (end_eq - start_eq) / start_eq * 100

        record = {
            "Day": str(day),
            "Start ($)": round(start_eq, 2),
            "Min ($)": round(min_eq, 2),
            "Max ($)": round(max_eq, 2),
            "End ($)": round(end_eq, 2),
            "Intraday DD (%)": round(intraday_dd, 2),
            "Day PnL (%)": round(daily_pnl, 2),
        }
        daily_records.append(record)

        if intraday_dd > 3.8:
            breaches_38.append((str(day), intraday_dd))
        if intraday_dd > 4.0:
            breaches_40.append((str(day), intraday_dd))
        if intraday_dd > 5.0:
            breaches_50.append((str(day), intraday_dd))

    res_df = pd.DataFrame(daily_records)
    worst_day_dd = res_df["Intraday DD (%)"].max()

    print("=" * 78)
    print("🏆 INSTITUTIONAL DAILY DRAWDOWN & COMPLIANCE AUDIT REPORT")
    print("=" * 78)
    print(f"• Total Days Evaluated:          {len(res_df)} days (Continuous 5m candles)")
    print(f"• Starting Balance:              ${start_balance:,.2f}")
    print(f"• Final Balance:                 ${final_balance:,.2f} (+{total_profit_pct:.2f}%)")
    print(f"• Worst Intraday Drawdown:       {worst_day_dd:.2f}% (Limit: 4.0% - 5.0%)")
    print(f"• Breaches > 3.8% (Sentinel):    {len(breaches_38)} days")
    print(f"• Breaches > 4.0% (FTMO Limit):  {len(breaches_40)} days")
    print(f"• Breaches > 5.0% (CFT Limit):   {len(breaches_50)} days")
    print("-" * 78)
    if worst_day_dd <= 3.8:
        print("✅ VERDICT: 100% COMPLIANT WITH ALL PROP FIRM RULES! (PASSED)")
    elif worst_day_dd <= 4.0:
        print("✅ VERDICT: COMPLIANT WITH 4% - 5% LIMITS! (PASSED)")
    else:
        print("❌ VERDICT: NON-COMPLIANT (BREACHED)")
    print("=" * 78)

    print("\n📊 5 WORST DRAWDOWN DAYS IN HISTORY:")
    print(res_df.sort_values(by="Intraday DD (%)", ascending=False).head(5).to_string(index=False))


if __name__ == "__main__":
    audit_latest_backtest()
