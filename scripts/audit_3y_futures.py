import json
import pandas as pd
import numpy as np

json_path = 'user_data/backtest_results/latest_futures/backtest-result-2026-10-27_13-44-14.json'
with open(json_path) as f:
    data = json.load(f)

strat = list(data['strategy'].keys())[0]
strat_data = data['strategy'][strat]
trades = strat_data['trades']
df = pd.DataFrame(trades)

print('=== 1. GENERAL PERFORMANCE ===')
print(f'Total Trades: {len(df)}')
print(f'Wins: {len(df[df.profit_ratio > 0])} ({len(df[df.profit_ratio > 0])/len(df)*100:.2f}%)')
print(f'Losses: {len(df[df.profit_ratio < 0])} ({len(df[df.profit_ratio < 0])/len(df)*100:.2f}%)')
print(f'Ties: {len(df[df.profit_ratio == 0])}')
print(f'Starting Balance: 5000.00 USDT')
final_balance = 5000.0 + df['profit_abs'].sum()
print(f'Final Balance: {final_balance:.2f} USDT')
print(f'Total Net Profit: {df["profit_abs"].sum():.2f} USDT (+{df["profit_abs"].sum()/5000*100:.2f}%)')

# Check sides & leverage
print('\n=== 2. DIRECTION & LEVERAGE AUDIT ===')
if 'is_short' in df.columns:
    print('is_short values:', df['is_short'].value_counts().to_dict())
if 'leverage' in df.columns:
    print('leverage values:', df['leverage'].value_counts().to_dict())

# 5 losing trades audit
print('\n=== 3. AUDIT OF THE 5 LOSING TRADES ===')
losses = df[df.profit_ratio < 0].copy()
for idx, r in losses.iterrows():
    pair = r['pair']
    open_d = r['open_date']
    close_d = r['close_date']
    loss_pct = r['profit_ratio'] * 100
    loss_usd = r['profit_abs']
    reason = r['exit_reason']
    print(f'Pair: {pair:<16} | Open: {open_d} | Close: {close_d} | Loss%: {loss_pct:6.2f}% | Loss USDT: {loss_usd:8.2f} | Reason: {reason}')

# Daily Drawdown audit using wallet feather
wallet_path = 'user_data/backtest_results/latest_futures/backtest-result-2026-10-27_13-44-14_NostalgiaForInfinityX8_wallet.feather'
df_wallet = pd.read_feather(wallet_path)
print('\n=== 4. WALLET & DRAWDOWN AUDIT (5-MINUTE RESOLUTION) ===')
df_wallet['date_dt'] = pd.to_datetime(df_wallet['date'])
df_wallet['day'] = df_wallet['date_dt'].dt.date

# Calculate daily peak-to-trough drawdown for each calendar day
daily_stats = []
for day, group in df_wallet.groupby('day'):
    start_val = group.iloc[0]['balance']
    min_val = group['balance'].min()
    max_val = group['balance'].max()
    end_val = group.iloc[-1]['balance']
    daily_dd_pct = (min_val - start_val) / start_val * 100
    intraday_dd_pct = (min_val - max_val) / max_val * 100
    daily_stats.append({
        'day': day,
        'start': start_val,
        'min': min_val,
        'max': max_val,
        'end': end_val,
        'daily_dd_from_open': daily_dd_pct,
        'intraday_dd_from_peak': intraday_dd_pct,
        'pnl_usd': end_val - start_val
    })

df_daily = pd.DataFrame(daily_stats)
print('Worst 5 Days by Drawdown from Day Open:')
worst_open_dd = df_daily.sort_values('daily_dd_from_open').head(5)
for idx, r in worst_open_dd.iterrows():
    print(f"  {r['day']} | Start: ${r['start']:8.2f} | Min: ${r['min']:8.2f} | DD from Open: {r['daily_dd_from_open']:5.2f}% | PnL: ${r['pnl_usd']:7.2f}")

print('\nWorst 5 Days by Intraday Peak-to-Trough Drawdown:')
worst_peak_dd = df_daily.sort_values('intraday_dd_from_peak').head(5)
for idx, r in worst_peak_dd.iterrows():
    print(f"  {r['day']} | Max: ${r['max']:8.2f} | Min: ${r['min']:8.2f} | Intraday DD: {r['intraday_dd_from_peak']:5.2f}% | PnL: ${r['pnl_usd']:7.2f}")

# Check prop firm limits
violated_4_5 = df_daily[df_daily['daily_dd_from_open'] <= -4.5]
violated_5_0 = df_daily[df_daily['daily_dd_from_open'] <= -5.0]
print(f'\nDays violating 4.5% Prop Firm Daily Limit: {len(violated_4_5)}')
print(f'Days violating 5.0% Prop Firm Daily Limit: {len(violated_5_0)}')

# Overall peak-to-trough drawdown across all 3.6 years
df_wallet['rolling_peak'] = df_wallet['balance'].cummax()
df_wallet['overall_dd_pct'] = (df_wallet['balance'] - df_wallet['rolling_peak']) / df_wallet['rolling_peak'] * 100
max_overall_dd = df_wallet['overall_dd_pct'].min()
print(f'\nMax Overall Continuous Drawdown across 3.6 years: {max_overall_dd:.2f}%')
print(f'Overall Drawdown limit (8.0% / 10.0%) violated?: {max_overall_dd <= -8.0}')

# Monthly breakdown
df['close_dt'] = pd.to_datetime(df['close_date'])
df['year_month'] = df['close_dt'].dt.to_period('M')
monthly = df.groupby('year_month').agg(
    trades=('profit_abs', 'count'),
    wins=('profit_ratio', lambda s: (s > 0).sum()),
    profit_usdt=('profit_abs', 'sum')
)
monthly['win_rate'] = monthly['wins'] / monthly['trades'] * 100
print('\n=== 5. MONTHLY PERFORMANCE BREAKDOWN (ALL 44 MONTHS) ===')
for ym, r in monthly.iterrows():
    print(f"{ym} | Trades: {int(r['trades']):2d} | WinRate: {r['win_rate']:5.1f}% | Profit: {r['profit_usdt']:8.2f} USDT")
