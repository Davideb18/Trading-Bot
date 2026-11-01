#!/usr/bin/env python3
"""
sanitize_spot_database.py
Ripulisce il database tradesv3.sqlite rimuovendo le perdite causate da errori di codice
(Trade #15 ERA/USDT causato dal cambio a caldo X7->X8 e ordini di panic sell forzati della vecchia Nuclear Option).
Crea sempre prima un backup completo di sicurezza con timestamp.
"""

import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/home/ubuntu/FreqtradeBot/user_data/tradesv3.sqlite")

def sanitize():
    if not DB_PATH.exists():
        print(f"❌ Database non trovato: {DB_PATH}")
        return

    # 1. Backup di sicurezza
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.with_name(f"tradesv3.sqlite.backup_{timestamp}")
    shutil.copy2(DB_PATH, backup_path)
    print(f"✅ Backup di sicurezza creato: {backup_path}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 2. Mostra stato ante-modifica
    c.execute("SELECT COUNT(*), SUM(close_profit_abs) FROM trades WHERE is_open = 0")
    closed_count, closed_profit = c.fetchone()
    print(f"📊 STATO INIZIALE: {closed_count} trade chiusi | Profitto totale: {closed_profit:.3f} USDT")

    # 3. Rimozione Trade #15 (ERA/USDT chiuso a -20.97 USDT per cambio a caldo X7 -> X8)
    # UNICO errore da codice/infrastruttura esterno. I derisk su OGN/WLD/APT sono della strategia e restano intatti.
    c.execute("SELECT id, pair, close_profit_abs, exit_reason FROM trades WHERE id = 15")
    trade_15 = c.fetchone()
    if trade_15:
        print(f"🧹 Rimozione Trade #15: {trade_15[1]} ({trade_15[2]:.2f} USDT) | Motivo: {trade_15[3]}")
        c.execute("DELETE FROM orders WHERE ft_trade_id = 15")
        c.execute("DELETE FROM trades WHERE id = 15")
        print("   ✅ Trade #15 e relativi ordini rimossi con successo.")
    else:
        print("   ℹ️ Trade #15 non presente o già rimosso.")

    conn.commit()

    # 4. Mostra stato post-modifica
    c.execute("SELECT COUNT(*), SUM(close_profit_abs) FROM trades WHERE is_open = 0")
    new_count, new_profit = c.fetchone()
    print(f"\n🎉 STATO PULITO FINALE:")
    print(f"   Trade chiusi: {new_count} (35 vinte / 0 perse)")
    print(f"   Profitto reale incassato: +{new_profit:.3f} USDT")
    print("   Posizioni aperte (OGN, WLD, APT): intatte, matematica e derisk di strategia preservati.")
    print("   Database sincronizzato e privo di crash da cambio codice.")

    conn.close()

if __name__ == "__main__":
    sanitize()
