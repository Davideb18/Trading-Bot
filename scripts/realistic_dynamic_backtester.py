import os
import json
import glob
import re
import subprocess
import zipfile
import warnings
import pandas as pd
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pyarrow.feather as feather

# Suppress warnings
warnings.filterwarnings('ignore')

DATA_DIR = "user_data/data/binance/"
RESULTS_DIR = "user_data/backtest_results/"
BASE_CONFIG = "user_data/config.json"
TEMP_CONFIG = "user_data/temp_dynamic_window_config.json"
STRATEGY = "NostalgiaForInfinityX8Dynamic"
TOP_N_PAIRS = 40
LOOKAHEAD_DAYS = 35

def load_blacklist():
    with open(BASE_CONFIG) as f:
        cfg = json.load(f)
    patterns = cfg.get("exchange", {}).get("pair_blacklist", [])
    compiled = [re.compile(p) for p in patterns]
    return compiled

def is_pair_blacklisted(pair: str, blacklist_patterns) -> bool:
    for pattern in blacklist_patterns:
        if pattern.search(pair):
            return True
    return False

def get_top_pairs_for_month(prev_start: datetime, prev_end: datetime, current_start: datetime, blacklist):
    volumes = {}
    files = glob.glob(os.path.join(DATA_DIR, "*-1d.feather"))
    
    # Check if this is the very first month (Jan 2021) where prev_month has no data
    is_initial_month = current_start <= datetime(2021, 1, 1)
    
    for f in files:
        pair_base = os.path.basename(f).replace("-1d.feather", "")
        parts = pair_base.split("_")
        if len(parts) >= 3:
            pair_name = f"{parts[0]}/{parts[1]}:{parts[2]}"
        else:
            pair_name = pair_base.replace("_", "/")
            
        # Check blacklist
        if is_pair_blacklisted(pair_name, blacklist):
            continue
            
        # Ensure all 5 timeframes exist
        tf_5m = f.replace("-1d.feather", "-5m.feather")
        tf_15m = f.replace("-1d.feather", "-15m.feather")
        tf_1h = f.replace("-1d.feather", "-1h.feather")
        tf_4h = f.replace("-1d.feather", "-4h.feather")
        if not (os.path.exists(tf_5m) and os.path.exists(tf_15m) and os.path.exists(tf_1h) and os.path.exists(tf_4h)):
            continue
            
        try:
            df = feather.read_table(f).to_pandas()
            if df.empty or 'date' not in df.columns:
                continue
                
            first_date = pd.to_datetime(df['date'].iloc[0], utc=True)
            
            # AgeFilter: Legacy coins present at start of 2021 are valid. New coins must be >= 60 days old
            is_legacy_2021 = first_date <= pd.to_datetime('2021-01-02', utc=True)
            if not is_legacy_2021:
                req_age = pd.to_datetime(current_start, utc=True) - timedelta(days=60)
                if first_date > req_age:
                    continue  # Coin too young (< 60 days)
                    
            if is_initial_month:
                # For Jan 2021, calculate volume over first 15 days of Jan 2021
                mask = (df['date'] >= pd.to_datetime('2021-01-01', utc=True)) & (df['date'] < pd.to_datetime('2021-01-15', utc=True))
                df_filtered = df[mask]
                if not df_filtered.empty:
                    quote_vol = float((df_filtered['close'] * df_filtered['volume']).sum())
                    volumes[pair_name] = quote_vol
            else:
                # Normal months: calculate volume in previous month
                mask = (df['date'] >= pd.to_datetime(prev_start, utc=True)) & (df['date'] < pd.to_datetime(prev_end, utc=True))
                df_filtered = df[mask]
                if not df_filtered.empty:
                    quote_vol = float((df_filtered['close'] * df_filtered['volume']).sum())
                    if quote_vol >= 5000000:
                        volumes[pair_name] = quote_vol
        except Exception:
            continue
            
    # Sort by quote volume descending
    sorted_pairs = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
    top_pairs = [p[0] for p in sorted_pairs[:TOP_N_PAIRS]]
    return top_pairs

def build_month_config(pairs, max_entry_date_str, wallet_balance):
    with open(BASE_CONFIG) as f:
        cfg = json.load(f)
        
    cfg["strategy"] = STRATEGY
    cfg["max_open_trades"] = 6
    cfg["stake_currency"] = "USDT"
    cfg["stake_amount"] = "unlimited"
    cfg["tradable_balance_ratio"] = 0.99
    cfg["amend_last_stake_amount"] = True
    cfg["max_stake_amount"] = 50000
    cfg["dry_run"] = True
    cfg["dry_run_wallet"] = wallet_balance
    cfg["timeframe"] = "5m"
    cfg["trading_mode"] = "spot"
    cfg["exchange"]["pair_whitelist"] = pairs
    cfg["pairlists"] = [{"method": "StaticPairList"}]
    cfg["max_entry_date"] = max_entry_date_str
    
    with open(TEMP_CONFIG, "w") as f:
        json.dump(cfg, f, indent=4)

def get_latest_backtest_result():
    last_res_path = os.path.join(RESULTS_DIR, ".last_result.json")
    if os.path.exists(last_res_path):
        try:
            with open(last_res_path) as f:
                data = json.load(f)
            latest_filename = data.get("latest_backtest")
            if latest_filename:
                latest_file = os.path.join(RESULTS_DIR, latest_filename)
                if os.path.exists(latest_file):
                    with zipfile.ZipFile(latest_file, 'r') as z:
                        for name in z.namelist():
                            if name.endswith('.json') and not name.endswith('_config.json'):
                                with z.open(name) as zf:
                                    return json.load(zf)
        except Exception as e:
            print(f"Error reading .last_result.json: {e}")

    files = glob.glob(os.path.join(RESULTS_DIR, "backtest-result-*.zip"))
    if not files:
        return None
    latest_file = max(files, key=os.path.getmtime)
    try:
        with zipfile.ZipFile(latest_file, 'r') as z:
            for name in z.namelist():
                if name.endswith('.json') and not name.endswith('_config.json'):
                    with z.open(name) as f:
                        return json.load(f)
    except Exception as e:
        print(f"Error reading zip: {e}")
    return None

def run():
    blacklist = load_blacklist()
    start_date = datetime(2021, 1, 1)
    end_date = datetime(2026, 9, 1)
    
    current = start_date
    starting_balance = 1000.0
    current_balance = starting_balance
    
    all_trades = []
    monthly_stats = []
    
    print("=" * 80)
    print("🚀 REALISTIC DYNAMIC 5-YEAR SLIDING WINDOW BACKTEST (2021 - 2026)")
    print("Strategy: NostalgiaForInfinityX8Dynamic")
    print("Universe: Top 40 Volume Coins re-adapted each month with AgeFilter >= 60d")
    print(f"Zero Truncation: Max Entry Cutoff + {LOOKAHEAD_DAYS}d Lookahead (NO FORCE EXITS)")
    print("Live Config: Max Open Trades = 6 | Tradable Balance = 0.99 | Amend Stake = True")
    print("=" * 80)
    
    while current < end_date:
        next_month = current + relativedelta(months=1)
        prev_month_start = current - relativedelta(months=1)
        prev_month_end = current
        
        # Buffer end date for natural exits
        buffer_end = next_month + timedelta(days=LOOKAHEAD_DAYS)
        
        timerange_str = f"{current.strftime('%Y%m%d')}-{buffer_end.strftime('%Y%m%d')}"
        max_entry_str = f"{next_month.strftime('%Y-%m-%d')} 00:00:00+00:00"
        month_label = current.strftime('%Y-%m')
        
        print(f"\n[{month_label}] Selecting Top 40 volume pairs from previous month...")
        top_pairs = get_top_pairs_for_month(prev_month_start, prev_month_end, current, blacklist)
        if not top_pairs or len(top_pairs) < 5:
            print(f"[{month_label}] Fallback to liquid pairs...")
            top_pairs = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT', 'ADA/USDT', 'XRP/USDT', 'DOT/USDT', 'AVAX/USDT']
            
        print(f"[{month_label}] Active Universe ({len(top_pairs)} pairs). Top 5: {top_pairs[:5]}")
        
        build_month_config(top_pairs, max_entry_str, current_balance)
        
        cmd = [
            ".venv/bin/python3", "-m", "freqtrade", "backtesting",
            "--strategy", STRATEGY,
            "-c", BASE_CONFIG,
            "-c", TEMP_CONFIG,
            "--timerange", timerange_str,
            "--dry-run-wallet", str(round(current_balance, 2)),
            "--fee", "0.00075",
            "--no-color"
        ]
        
        print(f"[{month_label}] Running backtest (Wallet: ${current_balance:.2f})...")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[{month_label}] Error in backtest:")
            print(res.stderr[-500:])
            current = next_month
            continue
            
        result_json = get_latest_backtest_result()
        if not result_json or "strategy" not in result_json or STRATEGY not in result_json["strategy"]:
            print(f"[{month_label}] Failed to parse result json")
            current = next_month
            continue
            
        strat_res = result_json["strategy"][STRATEGY]
        raw_trades = strat_res.get("trades", [])
        
        # Filter trades: only those OPENED in month M (open_date < next_month)
        next_month_utc = pd.to_datetime(next_month, utc=True)
        month_trades = []
        force_exit_count = 0
        
        for t in raw_trades:
            t_open = pd.to_datetime(t.get("open_date"), utc=True)
            if t_open < next_month_utc:
                month_trades.append(t)
                if t.get("exit_reason") == "force_exit":
                    force_exit_count += 1
                    
        month_profit_abs = sum(t.get("profit_abs", 0.0) for t in month_trades)
        month_wins = len([t for t in month_trades if t.get("profit_abs", 0.0) > 0])
        month_winrate = (month_wins / len(month_trades) * 100) if month_trades else 0.0
        
        current_balance += month_profit_abs
        
        all_trades.extend(month_trades)
        monthly_stats.append({
            "Month": month_label,
            "Pairs": len(top_pairs),
            "Trades": len(month_trades),
            "Wins": month_wins,
            "WinRate": round(month_winrate, 2),
            "ForceExits": force_exit_count,
            "ProfitUSDT": round(month_profit_abs, 2),
            "CompBalance": round(current_balance, 2)
        })
        
        print(f"[{month_label}] COMPLETED: {len(month_trades)} trades (Wins: {month_wins}, WinRate: {month_winrate:.1f}%) | Force-exits: {force_exit_count} | Profit: ${month_profit_abs:+.2f} | Balance: ${current_balance:.2f}")
        current = next_month
        
    print("\n" + "=" * 80)
    print("🎯 5-YEAR BACKTEST FINISHED! GENERATING CONSOLIDATED MASTER REPORT...")
    print("=" * 80)
    
    total_trades = len(all_trades)
    total_wins = len([t for t in all_trades if t.get("profit_abs", 0.0) > 0])
    total_losses = len([t for t in all_trades if t.get("profit_abs", 0.0) < 0])
    global_winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    total_force_exits = sum(m["ForceExits"] for m in monthly_stats)
    total_roi = (current_balance - starting_balance) / starting_balance * 100
    
    report_file = os.path.join(RESULTS_DIR, "master_realistic_dynamic_5_years_report.txt")
    with open(report_file, "w") as f:
        f.write("=" * 85 + "\n")
        f.write("MASTER REALISTIC DYNAMIC 5-YEAR BACKTEST REPORT (2021 - 2026)\n")
        f.write("Strategy: NostalgiaForInfinityX8Dynamic\n")
        f.write("Dynamic Universe: Top 40 Volume USDT pairs re-ranked monthly with AgeFilter >= 60d\n")
        f.write("Zero-Truncation Engine: Entry cutoff at month-end + 35d natural exit buffer\n")
        f.write("Exact Live Configuration: max_open_trades=6, tradable_balance_ratio=0.99, fee=0.00075\n")
        f.write("=" * 85 + "\n\n")
        
        f.write("MONTHLY BREAKDOWN:\n")
        f.write("-" * 85 + "\n")
        f.write(f"{'Month':<8} | {'Pairs':<5} | {'Trades':<6} | {'Wins':<5} | {'Win Rate':<8} | {'Force Exit':<10} | {'Profit USDT':<12} | {'Compound Bal':<14}\n")
        f.write("-" * 85 + "\n")
        for m in monthly_stats:
            f.write(f"{m['Month']:<8} | {m['Pairs']:<5} | {m['Trades']:<6} | {m['Wins']:<5} | {m['WinRate']:>6.2f}% | {m['ForceExits']:<10} | {m['ProfitUSDT']:>+10.2f}$ | {m['CompBalance']:>12.2f}$\n")
        f.write("-" * 85 + "\n\n")
        
        f.write("GLOBAL SUMMARY PERFORMANCE:\n")
        f.write("=" * 50 + "\n")
        f.write(f"Starting Wallet Balance:     ${starting_balance:,.2f}\n")
        f.write(f"Final Compounded Balance:    ${current_balance:,.2f}\n")
        f.write(f"Absolute Net Profit:         ${current_balance - starting_balance:+,.2f}\n")
        f.write(f"Total Compounded Net ROI:    {total_roi:+,.2f}%\n")
        f.write(f"Total Closed Trades:         {total_trades}\n")
        f.write(f"Winning Trades:              {total_wins}\n")
        f.write(f"Losing Trades:               {total_losses}\n")
        f.write(f"Global Win Rate:             {global_winrate:.2f}%\n")
        f.write(f"Artificial Force Exits:      {total_force_exits}\n")
        f.write("=" * 50 + "\n")
        
    print(f"\nReport written to {report_file}")
    
    trades_file = os.path.join(RESULTS_DIR, "master_realistic_dynamic_5_years_trades.json")
    with open(trades_file, "w") as f:
        json.dump(all_trades, f, indent=2, default=str)
    print(f"Saved {len(all_trades)} trades to {trades_file}")

if __name__ == "__main__":
    run()
