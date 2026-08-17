import os
import json
import glob
import subprocess
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pyarrow.feather as feather

DATA_DIR = "user_data/data/binance/futures/"
RESULTS_DIR = "user_data/backtest_results/"
BASE_CONFIG = "user_data/config.json"
TEMP_CONFIG = "user_data/temp_window_config.json"
STRATEGY = "NostalgiaForInfinityX7"
TOP_N_PAIRS = 130

def get_top_pairs_for_month(prev_start: datetime, prev_end: datetime):
    print(f"Calculating Top {TOP_N_PAIRS} pairs by volume for {prev_start.strftime('%Y-%m')}...")
    volumes = {}
    files = glob.glob(os.path.join(DATA_DIR, "*-1d-futures.feather"))
    
    for f in files:
        pair_base = os.path.basename(f).replace("-1d-futures.feather", "")
        parts = pair_base.split("_")
        if len(parts) >= 3:
            pair_name = f"{parts[0]}/{parts[1]}:{parts[2]}"
        else:
            pair_name = pair_base.replace("_", "/")
        try:
            df = feather.read_table(f).to_pandas()
            # Filter dates
            mask = (df['date'] >= pd.to_datetime(prev_start, utc=True)) & (df['date'] < pd.to_datetime(prev_end, utc=True))
            df_filtered = df[mask]
            if not df_filtered.empty:
                # Freqtrade volume is usually base asset. Quote volume ~ close * volume
                quote_vol = (df_filtered['close'] * df_filtered['volume']).sum()
                volumes[pair_name] = quote_vol
        except Exception as e:
            continue
            
    # Sort and pick top N
    sorted_pairs = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
    top_pairs = [p[0] for p in sorted_pairs[:TOP_N_PAIRS]]
    return top_pairs

def create_temp_config(pairs):
    config = {
        "pairlists": [{"method": "StaticPairList"}],
        "exchange": {"pair_whitelist": pairs}
    }
    with open(TEMP_CONFIG, "w") as f:
        json.dump(config, f, indent=4)

def get_latest_backtest_result():
    files = glob.glob(os.path.join(RESULTS_DIR, "backtest-result-*.json"))
    if not files:
        return None
    latest_file = max(files, key=os.path.getctime)
    with open(latest_file, "r") as f:
        return json.load(f)

def run():
    start_date = datetime(2021, 1, 1)
    end_date = datetime(2026, 8, 1) # Oppure datetime.now()
    
    current = start_date
    all_trades = []
    
    starting_balance = 1000.0
    current_balance = starting_balance
    
    monthly_stats = []

    print("=========================================================")
    print("🚀 STARTING DYNAMIC SLIDING WINDOW BACKTEST (2021-2026)")
    print("=========================================================")
    
    while current < end_date:
        next_month = current + relativedelta(months=1)
        prev_month_start = current - relativedelta(months=1)
        prev_month_end = current
        
        timerange_str = f"{current.strftime('%Y%m%d')}-{next_month.strftime('%Y%m%d')}"
        print(f"\n--- Processing Window: {timerange_str} ---")
        
        # 1. Get Top Pairs
        top_pairs = get_top_pairs_for_month(prev_month_start, prev_month_end)
        if not top_pairs:
            print("No data found for previous month. Using default pairs or skipping...")
            current = next_month
            continue
            
        print(f"Selected {len(top_pairs)} pairs. Examples: {top_pairs[:5]}...")
        
        # 2. Create Config
        create_temp_config(top_pairs)
        
        # 3. Run Freqtrade
        cmd = [
            "python3", "-m", "freqtrade", "backtesting",
            "--strategy", STRATEGY,
            "-c", BASE_CONFIG,
            "-c", TEMP_CONFIG,
            "--timerange", timerange_str
        ]
        
        print("Running Freqtrade Backtest...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print("Error running backtest:")
            print(result.stderr[-1000:])
        
        # 4. Parse Results
        res_json = get_latest_backtest_result()
        if res_json and "strategy" in res_json and STRATEGY in res_json["strategy"]:
            strat_results = res_json["strategy"][STRATEGY]
            trades = strat_results.get("trades", [])
            
            all_trades.extend(trades)
            
            month_profit_abs = strat_results.get("profit_total_abs", 0.0)
            month_profit_pct = strat_results.get("profit_total_pct", 0.0) * 100
            
            current_balance += month_profit_abs
            
            monthly_stats.append({
                "Month": current.strftime('%Y-%m'),
                "Trades": len(trades),
                "Profit % (Static)": round(month_profit_pct, 2),
                "Profit USDT (Static)": round(month_profit_abs, 2),
                "Compound Balance": round(current_balance, 2)
            })
            
            print(f"Month {current.strftime('%Y-%m')} finished! Trades: {len(trades)}, Profit: {round(month_profit_pct, 2)}%")
        else:
            print("No results found or error parsing JSON.")
            
        current = next_month
        
    print("\n=========================================================")
    print("✅ BACKTEST COMPLETATO! GENERAZIONE REPORT...")
    print("=========================================================")
    
    # Generate Report
    report_path = os.path.join(RESULTS_DIR, "master_dynamic_report.txt")
    with open(report_path, "w") as f:
        f.write("MASTER SLIDING WINDOW BACKTEST REPORT (2021-2026)\n")
        f.write("Strategy: NostalgiaForInfinityX7\n")
        f.write("Mode: Dynamic Pairlist (Top 130 by Volume updated Monthly)\n")
        f.write("=========================================================\n\n")
        
        f.write("MONTHLY BREAKDOWN:\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'Month':<10} | {'Trades':<8} | {'Profit %':<10} | {'Profit USDT':<12} | {'Simulated Bal':<15}\n")
        f.write("-" * 80 + "\n")
        for stat in monthly_stats:
            f.write(f"{stat['Month']:<10} | {stat['Trades']:<8} | {stat['Profit % (Static)']}%       | {stat['Profit USDT (Static)']}$       | {stat['Compound Balance']}$\n")
            
        total_trades = len(all_trades)
        winning_trades = len([t for t in all_trades if t.get('profit_abs', 0) > 0])
        winrate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        f.write("\n=========================================================\n")
        f.write("GLOBAL STATS:\n")
        f.write(f"Total Trades: {total_trades}\n")
        f.write(f"Global Winrate: {winrate:.2f}%\n")
        f.write(f"Starting Balance: {starting_balance}$\n")
        f.write(f"Ending Balance (Compound): {current_balance:.2f}$\n")
        f.write(f"Total ROI (Compound): {((current_balance - starting_balance) / starting_balance * 100):.2f}%\n")
        
    print(f"Report saved to {report_path}")

if __name__ == '__main__':
    run()
