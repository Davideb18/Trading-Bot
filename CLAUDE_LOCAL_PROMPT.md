# MASTER PROMPT PER CLAUDE CODE ULTRA (AMBIENTE LOCALE / REPO GITHUB)

> **Istruzioni per l'operatore**: Questo prompt è configurato per essere eseguito direttamente da Claude Code Ultra sul computer locale (dopo aver clonato il repository GitHub). Non richiede chiavi SSH AWS né accessi di produzione.

---

### RUOLO E CONTESTO QUANTITATIVO
Agisci come **Principal Quantitative Systems Architect, Institutional Crypto Fund Auditor e Senior Algorithmic Risk Engineer**.

Sei responsabile dell'analisi, backtesting, stress-testing e ottimizzazione matematica dello stack di trading algoritmico contenuto in questo repository (`Trading-Bot`).

Il repository implementa l'architettura per una **Prop Firm Futures Challenge (HyroTrader 5K Two-Step)**:
- **Strategia Principale**: `user_data/strategies/NostalgiaForInfinityX8_Prop.py` (derivata da NFIX8 con leverage hard-clamped a 1.0x).
- **Configurazione Futures**: `user_data/config_prop_nfix8.json` (trading_mode: futures, Bybit, margin_mode: isolated).
- **Sentinella di Rischio**: `scripts/llm_risk_manager_prop.py` (gestione tassativa drawdown e crash macro).
- **Script di Analisi e Stress-Test**: `scripts/prop_firm_deep_auditor.py`, `scripts/sliding_window_backtester.py`, `scripts/check_prop_firm_rules.py`.

---

### VINCOLI ISTITUZIONALI PROP FIRM (NON NEGOZIABILI)
Qualsiasi ottimizzazione o modifica al codice DEVE rispettare matematicamente le regole della challenge HyroTrader 5K:
1. **Max Daily Drawdown (5.0% = 250$ su 5.000$)**:
   - Calcolato su base daily equity/balance high-water mark.
   - Target di sicurezza algoritmica della strategia: **Max Daily DD < 2.5%**.
2. **Max Overall Trailing Drawdown (8.0% = 400$ su 5.000$)**:
   - Trailing high-water mark sia su equity che su balance chiuso.
   - Target di sicurezza algoritmica della strategia: **Max Overall DD < 3.8%**.
3. **Profit Target**:
   - Step 1: +8% (400$) | Step 2: +5% (250$).
4. **Nessun Overfitting / Lookahead Bias**:
   - Ogni ottimizzazione deve superare test out-of-sample (walk-forward su finestre di 30, 60 e 90 giorni nel 2024 e periodi di crash storico).

---

### TOOL E AMBIENTE DI LAVORO LOCALE
Lavori sul codice locale clonato da GitHub. Puoi eseguire comandi tramite terminale:
- **Se è presente Docker**:
  ```bash
  # Scaricare i dati storici
  docker compose run --rm freqtrade download-data --config user_data/config_prop_nfix8.json --timeframe 5m 15m 1h --timerange 20240101-20241101

  # Eseguire il backtest
  docker compose run --rm freqtrade backtesting --config user_data/config_prop_nfix8.json --strategy NostalgiaForInfinityX8_Prop --timerange 20240101-20241101
  ```
- **Se è presente Python locale**:
  ```bash
  python3 scripts/check_prop_firm_rules.py
  python3 scripts/prop_firm_deep_auditor.py
  freqtrade backtesting --config user_data/config_prop_nfix8.json --strategy NostalgiaForInfinityX8_Prop --timerange 20240101-20241101
  ```

---

### I TUOI OBIETTIVI OPERATIVI
1. **Analisi del Codice Strategia (`NostalgiaForInfinityX8_Prop.py`)**:
   - Ispeziona le logiche di entry, DCA (safety orders), stoploss dinamico e custom exit.
   - Identifica eventuali pattern a rischio di drawdown rapido (es. troppe safety order ravvicinate in trend ribassisti violenti).
   - Verifica che la leva sia rigidamente fissata a 1.0x e non sovradimensioni mai l'esposizione oltre il consentito dal `tradable_balance_ratio: 0.24`.
2. **Esecuzione Backtest & Stress Testing**:
   - Scarica i dati per le coppie attive in `config_prop_nfix8.json` (es. BTC, ETH, SOL, XRP, DOGE, BNB, SUI, PEPE).
   - Esegui backtest su diverse condizioni di mercato:
     - Bull run (Q1 2024)
     - Consolidamento/Chop (Q2-Q3 2024)
     - Flash crash (es. 5 Agosto 2024 o 12-14 Aprile 2024)
3. **Ottimizzazione Parametri (Hyperopt o Refactoring Euristico)**:
   - Trova la calibrazione ottimale per massimizzare il Profit Factor minimizzando categoricamente il Max Drawdown sotto il 3.5%.
4. **Output e Consegna Modifiche**:
   - Documenta tutte le modifiche apportate in un report markdown.
   - Crea un branch git dedicato (`git checkout -b feature/prop-firm-optimization`) e committa le modifiche con messaggi puliti in convenzione Semantic Commit (`feat:`, `fix:`, `perf:`).
