import os
import json
import glob
import subprocess
import pandas as pd
import numpy as np
from datetime import datetime

DATA_DIR = "user_data/data/binance/"
RESULTS_DIR = "user_data/backtest_results/"
BASE_CONFIG = "user_data/config.json"
BACKTEST_CONFIG = "user_data/config_continuous_backtest.json"
STRATEGY = "NostalgiaForInfinityX8"
EXPORT_FILE = os.path.join(RESULTS_DIR, "continuous_5_7_years_nfix8.json")

def get_valid_pairs():
    print("Selecting top liquid pairs (Mega-caps + high-momentum alts) ensuring 100% memory safety...")
    
    curated_candidates = [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT', 
        'ADA/USDT', 'DOT/USDT', 'PEPE/USDT', 'AVAX/USDT', 'TRX/USDT', 
        'LINK/USDT', 'LTC/USDT', 'SUI/USDT', 'NEAR/USDT', 'ARB/USDT', 
        'AAVE/USDT', 'ENA/USDT', 'WLD/USDT', 'APT/USDT', 'FET/USDT', 
        'OP/USDT', 'INJ/USDT', 'TAO/USDT', 'RENDER/USDT', 'PUMP/USDT', 
        'ONDO/USDT', 'SHIB/USDT', 'UNI/USDT'
    ]
    
    # Calculate volume weights from 1d files
    pair_volumes = {}
    for pair in curated_candidates:
        f_1d = os.path.join(DATA_DIR, f"{pair.replace('/', '_')}-1d.feather")
        if os.path.exists(f_1d):
            try:
                df_1d = pd.read_feather(f_1d, columns=['close', 'volume'])
                pair_volumes[pair] = float((df_1d['close'] * df_1d['volume']).sum())
            except Exception:
                pair_volumes[pair] = 1.0
        else:
            pair_volumes[pair] = 1.0
            
    # Sort pairs by quote volume
    sorted_pairs = sorted(pair_volumes.items(), key=lambda x: x[1], reverse=True)
    selected_pairs = [p[0] for p in sorted_pairs]
    print(f"Selected {len(selected_pairs)} pairs: {selected_pairs}")
    return selected_pairs

def build_config(pairs):
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
    cfg["dry_run_wallet"] = 1000
    cfg["timeframe"] = "5m"
    cfg["trading_mode"] = "spot"
    cfg["exchange"]["pair_whitelist"] = pairs
    
    # Backtesting requires StaticPairList
    cfg["pairlists"] = [
        {"method": "StaticPairList"}
    ]
    
    os.makedirs(os.path.dirname(BACKTEST_CONFIG), exist_ok=True)
    with open(BACKTEST_CONFIG, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f"Configuration written to {BACKTEST_CONFIG}")

def run_backtest(timerange="20210101-20260918"):
    print("\n" + "="*70)
    print(f"🚀 RUNNING CONTINUOUS REALISTIC MULTI-YEAR BACKTEST ({timerange})")
    print(f"Strategy: {STRATEGY} | Max Trades: 6 | Tradable Balance Ratio: 0.99")
    print(f"Continuous Trade Timeline: NO Monthly Slicing, NO Premature Force-Exits")
    print("="*70)
    
    os.makedirs(RESULTS_DIR, exist_ok=True)
    
    cmd = [
        ".venv/bin/python3", "-m", "freqtrade", "backtesting",
        "--strategy", STRATEGY,
        "-c", BACKTEST_CONFIG,
        "--timerange", timerange,
        "--dry-run-wallet", "1000",
        "--fee", "0.00075",
        "--no-color"
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if process.stdout is not None:
        for line in iter(process.stdout.readline, ''):
            print(line, end='', flush=True)
            
    process.wait()
    print(f"\nFreqtrade backtest process finished with return code {process.returncode}")
    return process.returncode

import zipfile

def get_latest_backtest_data():
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
    with zipfile.ZipFile(latest_file, 'r') as z:
        for name in z.namelist():
            if name.endswith('.json') and not name.endswith('_config.json'):
                with z.open(name) as f:
                    return json.load(f)
    return None

def analyze_and_report():
    print("\nAnalyzing backtest export data...")
    data = get_latest_backtest_data()
    if not data:
        print("No backtest data found in zip archives!")
        return
        
    strategy_data = data.get("strategy", {}).get(STRATEGY, {})
    trades = strategy_data.get("trades", [])
    
    if not trades:
        print("No trades found in results!")
        return
        
    df = pd.DataFrame(trades)
    print(f"Loaded {len(df)} total trades.")
    
    # Process timestamps
    df['open_date'] = pd.to_datetime(df['open_timestamp'], unit='ms', utc=True)
    df['close_date'] = pd.to_datetime(df['close_timestamp'], unit='ms', utc=True)
    df['close_month'] = df['close_date'].dt.strftime('%Y-%m')
    df['close_year'] = df['close_date'].dt.strftime('%Y')
    
    # Basic metrics
    total_trades = len(df)
    winning_trades = len(df[df['profit_abs'] > 0])
    losing_trades = len(df[df['profit_abs'] < 0])
    scratch_trades = len(df[df['profit_abs'] == 0])
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
    
    total_profit_abs = df['profit_abs'].sum()
    starting_balance = 1000.0
    final_balance = starting_balance + total_profit_abs
    total_roi = (total_profit_abs / starting_balance) * 100
    
    # Compounding simulation
    # In Freqtrade with unlimited stake, profit is compounded. Let's trace wallet evolution trade by trade:
    df = df.sort_values(by='close_date')
    df['cumulative_profit'] = df['profit_abs'].cumsum()
    df['wallet_balance'] = starting_balance + df['cumulative_profit']
    
    # Exit reasons breakdown
    exit_reasons = df['exit_reason'].value_counts().to_dict()
    
    # Annual breakdown
    annual_stats = []
    for year, group in df.groupby('close_year'):
        y_trades = len(group)
        y_wins = len(group[group['profit_abs'] > 0])
        y_losses = len(group[group['profit_abs'] < 0])
        y_winrate = (y_wins / y_trades * 100) if y_trades > 0 else 0
        y_profit = group['profit_abs'].sum()
        annual_stats.append({
            "Year": year,
            "Trades": y_trades,
            "Wins": y_wins,
            "Losses": y_losses,
            "Win Rate %": round(y_winrate, 2),
            "Profit (USDT)": round(y_profit, 2),
            "Ending Balance": round(group['wallet_balance'].iloc[-1], 2)
        })
        
    # Monthly breakdown
    monthly_stats = []
    for month, group in df.groupby('close_month'):
        m_trades = len(group)
        m_wins = len(group[group['profit_abs'] > 0])
        m_winrate = (m_wins / m_trades * 100) if m_trades > 0 else 0
        m_profit = group['profit_abs'].sum()
        monthly_stats.append({
            "Month": month,
            "Trades": m_trades,
            "Wins": m_wins,
            "Win Rate %": round(m_winrate, 2),
            "Profit (USDT)": round(m_profit, 2),
            "Balance": round(group['wallet_balance'].iloc[-1], 2)
        })
        
    # Max Drawdown calculation on closed trades equity curve
    peak = starting_balance
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    for bal in df['wallet_balance']:
        if bal > peak:
            peak = bal
        dd_abs = peak - bal
        dd_pct = (dd_abs / peak) * 100 if peak > 0 else 0
        if dd_abs > max_dd_abs:
            max_dd_abs = dd_abs
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            
    # Calculate 3.5-Year Window (2023-01-01 to 2026-08-31) metrics for direct comparison
    mask_3_5y = (df['close_date'] >= pd.to_datetime('2023-01-01', utc=True)) & (df['close_date'] <= pd.to_datetime('2026-08-31 23:59:59', utc=True))
    df_3_5y = df[mask_3_5y]
    profit_3_5y = df_3_5y['profit_abs'].sum() if not df_3_5y.empty else 0.0
    trades_3_5y = len(df_3_5y)
    wins_3_5y = len(df_3_5y[df_3_5y['profit_abs'] > 0])
    winrate_3_5y = (wins_3_5y / trades_3_5y * 100) if trades_3_5y > 0 else 0.0
            
    # Save Report
    report_path = os.path.join(RESULTS_DIR, "master_continuous_5_7_years_report.txt")
    with open(report_path, "w") as f:
        f.write("="*75 + "\n")
        f.write("CONTINUOUS 5.7-YEAR REALISTIC MASTER BACKTEST REPORT (2021-2026)\n")
        f.write(f"Strategy: {STRATEGY} | Engine: Freqtrade Native Continuous Backtest\n")
        f.write(f"Execution: Continuous Single Run (ZERO artificial monthly cuts)\n")
        f.write(f"Parameters: max_open_trades=6 | tradable_balance_ratio=0.99 | amend_last_stake=True\n")
        f.write(f"Binance Spot BNB Taker Fee: 0.075% (0.00075)\n")
        f.write("="*75 + "\n\n")
        
        f.write("SUMMARY PERFORMANCE METRICS (FULL 5.7-YEAR PERIOD 2021-2026):\n")
        f.write(f"Starting Wallet: ${starting_balance:,.2f} USDT\n")
        f.write(f"Final Wallet:    ${final_balance:,.2f} USDT\n")
        f.write(f"Total Net Profit: ${total_profit_abs:,.2f} USDT\n")
        f.write(f"Total Return (ROI): {total_roi:,.2f}%\n")
        f.write(f"Total Trades: {total_trades}\n")
        f.write(f"Winning Trades: {winning_trades} ({win_rate:.2f}%)\n")
        f.write(f"Losing Trades: {losing_trades} ({(losing_trades/total_trades*100):.2f}%)\n")
        f.write(f"Scratch Trades: {scratch_trades}\n")
        f.write(f"Max Equity Drawdown: {max_dd_pct:.2f}% (${max_dd_abs:,.2f} USDT)\n\n")
        
        f.write("="*75 + "\n")
        f.write("SUB-PERIOD 3.5-YEAR WINDOW (2023-01-01 to 2026-08-31):\n")
        f.write(f"Trades in 3.5y Window: {trades_3_5y}\n")
        f.write(f"Win Rate in 3.5y Window: {winrate_3_5y:.2f}%\n")
        f.write(f"Profit Generated in 3.5y Window: ${profit_3_5y:,.2f} USDT\n")
        f.write("="*75 + "\n\n")
        
        f.write("EXIT REASON DISTRIBUTION:\n")
        for reason, count in exit_reasons.items():
            pct = (count / total_trades) * 100
            f.write(f"  - {reason:<25}: {count:>5} trades ({pct:>5.1f}%)\n")
        f.write("\n")
        
        f.write("ANNUAL PERFORMANCE BREAKDOWN:\n")
        f.write(f"{'Year':<8} | {'Trades':<8} | {'Win Rate':<10} | {'Profit USDT':<15} | {'Ending Balance':<15}\n")
        f.write("-" * 65 + "\n")
        for a in annual_stats:
            f.write(f"{a['Year']:<8} | {a['Trades']:<8} | {a['Win Rate %']:>6.2f}%    | {a['Profit (USDT)']:>10.2f} USDT  | {a['Ending Balance']:>10.2f} USDT\n")
        f.write("\n")
        
        f.write("MONTHLY PERFORMANCE BREAKDOWN (ALL MONTHS):\n")
        f.write(f"{'Month':<10} | {'Trades':<8} | {'Win Rate':<10} | {'Profit USDT':<14} | {'Balance USDT':<15}\n")
        f.write("-" * 65 + "\n")
        for m in monthly_stats:
            f.write(f"{m['Month']:<10} | {m['Trades']:<8} | {m['Win Rate %']:>6.2f}%    | {m['Profit (USDT)']:>10.2f}$  | {m['Balance']:>10.2f}$\n")
            
    print(f"\nMaster report saved to {report_path}")
    return report_path

if __name__ == "__main__":
    pairs = get_valid_pairs()
    build_config(pairs)
    ret = run_backtest("20210101-20260918")
    if ret == 0:
        analyze_and_report()
