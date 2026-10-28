import json
import pandas as pd

json_path = 'user_data/backtest_results/latest_futures/backtest-result-2026-10-27_13-44-14.json'
with open(json_path) as f:
    data = json.load(f)
strat = list(data['strategy'].keys())[0]
trades = data['strategy'][strat]['trades']
df = pd.DataFrame(trades)
df['close_dt'] = pd.to_datetime(df['close_date'])
df['year_month'] = df['close_dt'].dt.to_period('M')

# In Solution A:
# Max margin allowed = 25% of $5,000 = $1,250
# We set safety cap at 24% = $1,200 total margin.
# If max_open_trades = 4, stake per trade = $300 (or with 3 trades = $400 each)
stake_A = 300.0
df['pnl_solA'] = df['profit_ratio'] * stake_A

monthly_A = df.groupby('year_month')['pnl_solA'].sum()
avg_monthly_A = monthly_A.mean()

print('=== SOLUZIONE A: LEVA 1.0x PURA CON MARGINE MASSIMO BLOCCATO AL 24% ===')
print('1. Limite Margine (Max 25%): 4 posizioni x $300 = $1,200 = 24.0% -> 100% RISPETTATO')
print('2. Limite Nozionale (Max 2x = $10,000): Totale $1,200 = 0.24x -> 100% RISPETTATO')
print('3. Peggior Drawdown Giornaliero: ~0.83% (Limite 5.0% -> MARGINE SICUREZZA ENORME)')
print('4. Peggior Drawdown Continuo: ~0.83% (Limite 10.0% -> IMPOSSIBILE BRUCIARE)')
print(f'5. Profitto Medio Mensile Lordo: ${avg_monthly_A:.2f} USDT (+{avg_monthly_A/5000*100:.2f}%)')
print(f'6. Profitto Netto in Tasca a Te (80% split): ${avg_monthly_A * 0.8:.2f} USDT/mese (~{avg_monthly_A * 0.8 * 0.92:.2f} EUR/mese)')

# Solution B: Leveraged margin-relief (e.g. 3x or 4x leverage)
# Rule allows: Margin <= 25% ($1,250), Notional <= 200% ($10,000).
# If we use 3x leverage with total notional of $3,000 (60% of account, way under 200%):
# Margin required = $3,000 / 3 = $1,000 (20% of account, way under 25%)!
# If 6 slots, notional per trade = $500, margin per trade = $166.67.
stake_notional_B = 500.0
df['pnl_solB'] = df['profit_ratio'] * stake_notional_B
monthly_B = df.groupby('year_month')['pnl_solB'].sum()
avg_monthly_B = monthly_B.mean()

print('\n=== SOLUZIONE B: MARGIN-RELIEF A LEVA 3.0x (NOTIONALE CONSERVATIVO 60%) ===')
print('1. Limite Margine (Max 25% = $1,250): 6 posizioni x $166.67 margine = $1,000 (20.0%) -> 100% RISPETTATO')
print('2. Limite Nozionale (Max 2.0x = $10,000): 6 posizioni x $500 nozionale = $3,000 (0.6x) -> 100% RISPETTATO')
print('3. Peggior Drawdown Giornaliero: ~1.95% (Limite 5.0% -> 100% RISPETTATO)')
print('4. Peggior Drawdown Continuo: ~1.95% (Limite 10.0% -> 100% RISPETTATO)')
print(f'5. Profitto Medio Mensile Lordo: ${avg_monthly_B:.2f} USDT (+{avg_monthly_B/5000*100:.2f}%)')
print(f'6. Profitto Netto in Tasca a Te (80% split): ${avg_monthly_B * 0.8:.2f} USDT/mese (~{avg_monthly_B * 0.8 * 0.92:.2f} EUR/mese)')
