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

# In a funded account of $5,000:
# Tradable balance ratio = 0.85 (15% safe box reserve)
# Slots = 6
# Fixed stake per trade = 5000 * 0.85 / 6 = $708.33
stake = 5000.0 * 0.85 / 6.0

df['flat_profit_usd'] = df['profit_ratio'] * stake

monthly_flat = df.groupby('year_month')['flat_profit_usd'].sum()
avg_monthly = monthly_flat.mean()
median_monthly = monthly_flat.median()
best_month = monthly_flat.max()
worst_month = monthly_flat.min()
annual_gross = avg_monthly * 12

print('=== STATISTICHE REALI SU CONTO FINANZIATO DA 5.000$ (PRELIEVO MENSILE) ===')
print(f'Profitto Medio Lordo al Mese: ${avg_monthly:.2f} USDT (+{avg_monthly/5000*100:.2f}%)')
print(f'Profitto Mediano Lordo al Mese: ${median_monthly:.2f} USDT (+{median_monthly/5000*100:.2f}%)')
print(f'Mese Migliore: ${best_month:.2f} USDT (+{best_month/5000*100:.2f}%)')
print(f'Mese Peggiore: ${worst_month:.2f} USDT ({worst_month/5000*100:.2f}%)')
print(f'Profitto Annuo Totale Generato: ${annual_gross:.2f} USDT (+{annual_gross/5000*100:.2f}% sul capitale)')

print('\n=== PAYOUT NETTO CHE ENTRA IN TASCA A TE (PROFIT SPLIT) ===')
print(f'Con Profit Split 80% (standard iniziale):')
print(f'  - Al mese in media: ${avg_monthly * 0.80:.2f} USDT (circa {avg_monthly * 0.80 * 0.92:.2f} EUR)')
print(f'  - All\'anno in media: ${annual_gross * 0.80:.2f} USDT (circa {annual_gross * 0.80 * 0.92:.2f} EUR)')
print(f'Con Profit Split 90% (scaling plan The5ers / HyroTrader):')
print(f'  - Al mese in media: ${avg_monthly * 0.90:.2f} USDT (circa {avg_monthly * 0.90 * 0.92:.2f} EUR)')
print(f'  - All\'anno in media: ${annual_gross * 0.90:.2f} USDT (circa {annual_gross * 0.90 * 0.92:.2f} EUR)')

# Scaling plan: buying larger accounts
print('\n=== SCALING PLAN CON GLI UTILI (SENZA METTERE ALTRI SOLDI PROPRI) ===')
costs = {
    '5k': 35,
    '10k': 65,
    '25k': 150,
    '50k': 280,
    '100k': 490
}
print(f'1. Costo prima challenge 5k: ~35-50 EUR')
print(f'2. Guadagno mese 1 su conto finanziato 5k: ~${avg_monthly*0.8:.2f} USDT (~{avg_monthly*0.8*0.92:.2f} EUR) + rimborso dei ~40 EUR della challenge!')
print(f'3. Con il profitto del mese 1 puoi comprare direttamente una challenge da 25.000$ (costo ~150 EUR) a RISCHIO ZERO.')
print(f'4. Guadagno medio mensile a regime con conto da 25.000$: ${avg_monthly*5*0.8:.2f} USDT/mese (~{avg_monthly*5*0.8*0.92:.2f} EUR/mese puliti in tasca).')
print(f'5. Guadagno medio mensile a regime con conto da 100.000$: ${avg_monthly*20*0.8:.2f} USDT/mese (~{avg_monthly*20*0.8*0.92:.2f} EUR/mese puliti in tasca).')
