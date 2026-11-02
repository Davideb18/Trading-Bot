# 🚨 ZERO-TRUST RED-TEAM AUDIT & MASTER PROMPT (CARTE BIANCA TOTALE)
### Mandato Operativo per Claude Code Ultra — Verifica Forense e Ottimizzazione da Zero a Zeta

> **POSTURA OPERATIVA**: ZERO-TRUST ASSOLUTO & ADVERSARIAL AUDITING  
> **AUTORITÀ**: CARTE BIANCA TOTALE (Totale libertà di vivisezione codice, download dati, esecuzione migliaia di simulazioni, stress-test e refactoring)  
> **TARGET**: Repository locale `Trading-Bot` (branch: `feature/prop-firm-futures-validation`)  
> **AMBIENTE**: Locale isolato (senza necessità di accessi AWS o chiavi di produzione)

---

## 1. IL TUO MANDATO: AUDITOR AVVERSARIO & CARTE BIANCA

Non assumere **MAI** che qualcosa sia corretto solo perché è stato scritto o configurato nei mesi scorsi.  
**Il tuo presupposto iniziale deve essere che ogni singolo file, algoritmo, parametro di trading, formula di drawdown, filtro di pairlist e script di sentinella contenga bug latenti, presupposti fragili o trappole silenti pronte a liquidare il conto o causare la squalifica immediata dalla Prop Firm.**

Hai **carta bianca totale** per:
1. **Ripartire idealmente da zero** e ricontrollare ogni singolo componente dalla A alla Z, analizzando ogni possibile variazione ed edge-case in migliaia di scenari di mercato.
2. **Scaricare qualsiasi dato storico** (giorni, mesi, anni su qualsiasi timeframe e coppia) per sottoporre le strategie a backtest massivi e stress-test estremi.
3. **Scrivere qualsiasi script di verifica ad-hoc**, fuzzer, simulatore Monte Carlo o test di robustezza matematica per toglierti qualsiasi dubbio.
4. **Modificare, rifattorizzare o riscrivere** qualsiasi parte di codice della strategia (`NostalgiaForInfinityX8_Prop.py`), della configurazione (`config_prop_nfix8.json`) o dei supervisori di rischio (`llm_risk_manager_prop.py`) per garantire che l'ecosistema sia **matematicamente inattaccabile e sicuro al 100%**.

---

## 2. L'OBIETTIVO SUPREMO: HYROTRADER 5K TWO-STEP (ZERO VIOLAZIONI)

La sfida Prop Firm HyroTrader Futures 5.000$ ha regole inflessibili. Una sola violazione di un singolo tick equivale alla squalifica immediata:

| Parametro Regola | Limite HyroTrader (Hard Breach) | Tuo Target di Sicurezza Ingegneristica |
|---|---|---|
| **Max Daily Drawdown** | **5.00% ($250 su $5.000)** | **Max Daily DD < 2.0% - 2.5% ($100 - $125)** |
| **Max Overall Trailing DD** | **8.0% ($400 su $5.000)** | **Max Trailing DD < 3.5% ($175)** |
| **Profit Target Step 1** | **+8.0% ($400)** | Conseguibile in 15-45 giorni con Win Rate > 70% |
| **Profit Target Step 2** | **+5.0% ($250)** | Conseguibile con massima stabilità |
| **Leva Finanziaria** | Non vietata, ma ad altissimo rischio | **Hard-clamped a 1.0x (Nessuna leva/liquidazione)** |
| **Margine Posizioni** | Isolato obbligatorio | **`margin_mode: "isolated"` (Zero cross-contagion)** |
| **Rischio per Trade** | - | **Max ~8% capitale per slot (3 trade max = 24%)** |

---

## 3. CHECKLIST FORENSE DALLA A ALLA Z: COSA DEVI VIVISEZIONARE

### A. STRATEGIA (`user_data/strategies/NostalgiaForInfinityX8_Prop.py` & spot `X8`)
- [ ] **Lookahead Bias & Data Leakage**: Ispeziona riga per riga il caricamento delle candele informative (`15m`, `1h`, `4h`, `1d`). C'è anche solo una riga in cui un indicatore su timeframe superiore guarda al futuro (`close` invece di `shift(1)`)?
- [ ] **Matematica delle Safety Orders (DCA / Grind Mode)**:
  - Quanti ordini di acquisto successivi può fare una posizione?
  - Qual è l'assorbimento cumulativo di capitale nel caso peggiore (worst-case scenario)?
  - **Scenario Apocalittico**: Cosa succede se tutti e 3 i trade aperti scendono contemporaneamente al livello massimo di DCA durante un crash sistemico? Il capitale residuo copre gli ordini o si va in margin call / violazione del daily drawdown?
- [ ] **Hard-Clamping della Leva**:
  - Verifica che la funzione `leverage()` restituisca **sempre e in ogni condizione `1.0`**, ignorando qualsiasi parametro proposto o ereditato.
  - Verifica che `correct_min_stake()` non generi mai stake sovradimensionate.
- [ ] **Logica di Uscita e Stoploss**:
  - La strategia usa soft-stop o ordini a mercato?
  - Se il prezzo crolla del 20% in una singola candela da 5 minuti senza ritracciare, la strategia quando e come taglia la perdita?
  - Verifica che i parametri di uscita prematura (es. `bad_trade_controller_structure_break_exit_enable`) siano disattivati per non svendere sui minimi.

### B. CONFIGURAZIONE & MERCATO (`user_data/config_prop_nfix8.json` e `config.json`)
- [ ] **Esecuzione Ordini e Slippage**:
  - `entry_pricing`: Gli ordini `limit` con `use_order_book: true` e `order_book_top: 1` vengono fillati realisticamente nei backtest o soffrono di un fill rate illusorio (unmodeled fill latency)?
  - `unfilledtimeout`: Se un ordine di acquisto o di vendita resta appeso oltre 2-3 minuti, il bot lo cancella correttamente senza rimanere in stati di locking?
- [ ] **Filtri di Liquidità e Whitelist Dinamica**:
  - `VolumePairList`: Il floor di volume a **$15.000.000 - $25.000.000** esclude al 100% monete a rischio pump-and-dump come il caso storico di OGN?
  - `AgeFilter`: Il filtro a 60 giorni impedisce l'acquisto di coin appena listate prive di storico?
  - `blacklist`: Sono escluse tutte le stablecoin, le commodity sintetiche, i token a leva (`*UP`, `*DOWN`, `*BULL`, `*BEAR`)?
- [ ] **Sizing del Capitale**:
  - Con `tradable_balance_ratio: 0.24` e `max_open_trades: 3`, se il wallet subisce una fluttuazione, come si comporta la stake size? È proporzionale o fissa?

### C. RISK SENTINEL & ACCOUNTING (`scripts/llm_risk_manager_prop.py`)
- [ ] **Formula di `get_equity()` (Audit Matematico al Centesimo)**:
  - Ispeziona il codice che legge i saldi da `/balance` e `/status`.
  - Assicurati che **NON esista alcun doppio conteggio** di PnL non realizzato.
  - Verifica la gestione di `NaN`, `None`, chiavi mancanti e divisione per zero quando il saldo di picco è nullo.
- [ ] **Calcolo del Daily Drawdown (Regola Fuso Orario 00:00 UTC)**:
  - Il reset giornaliero avviene esattamente alle 00:00 UTC?
  - Il broker di HyroTrader calcola il Daily Drawdown basandosi su UTC o sul fuso orario della piattaforma? C'è rischio di sfasamento temporale?
- [ ] **Tracciamento dell'High-Water Mark (Trailing Drawdown)**:
  - Il peak balance tiene conto del floating equity massimo intraday o solo dei trade chiusi?
  - Lo stato persistente su disco (`risk_manager_prop_state.json`) sopravvive a crash, riavvii o interruzioni di rete senza corrompersi o innescare loop infiniti?
- [ ] **Disaccoppiamento LLM e Circuit Breakers**:
  - La sentinella AI (Gemini) è completamente isolata dal thread deterministico di salvaguardia del capitale (0ms di latenza sul blocco drawdown)?
  - Se Gemini restituisce errore 429 (quota esaurita), 500 o timeout, la sentinella si appoggia a fallback deterministici (Fear & Greed Index, variazione prezzo BTC) senza mai bloccarsi?
  - Verifica che la condizione `is_market_crash` (calo BTC > 8% in 24h o > 4% in 1h) sia matematicamente robusta e non bypassabile da titoli di news.

---

## 4. IL REGISTRO DEI 10 ERRORI STORICI (DA NON REINTRODURRE MAI)

Prima di effettuare qualsiasi modifica, memorizza i 10 errori già affrontati e risolti nei mesi passati:

1. **Il Crollo OGN / Derisking e Discrepanza UI**: Token a bassa liquidità comprato su volume fasullo; la strategia ha gestito il derisking incassando -$37.79. Risolto con VolumePairList rigida > $20M/$25M.
2. **Il Doppio Conteggio in `get_equity()`**: Sottraeva due volte il PnL non realizzato, raddoppiando fittiziamente il drawdown a schermo. Risolto leggendo direttamente `total_bot`/`total`.
3. **Corruzione DB Trade #15 (ERA)**: Chiuso in perdita per riavvio a caldo durante upgrade X7->X8. Risolto con lo script `sanitize_spot_database.py`.
4. **Structure Break Exit Prematuro**: Parametro NFIX8 che vendeva in perdita a mercato quando EMA gap > 35%. Risolto disattivando `bad_trade_controller_structure_break_exit_enable`.
5. **Rischio Leva Futures**: Rischio di leva ereditata dall'exchange. Risolto con leva hard-clamped a 1.0x in `NostalgiaForInfinityX8_Prop.py` e margine isolato.
6. **Crash-Loop da `nuclear_triggered: true`**: Container in restart continuo per lettura di stato di emergenza non resettato. Risolto con auto-healing e reset controllato.
7. **Falsi Allarmi LLM da News senza Crollo di Prezzo**: Gemini leggeva news FUD e attivava la Nuclear Option con BTC in rialzo. Risolto richiedendo la conferma quantitativa di crollo reale `is_market_crash`.
8. **Esaurimento Quota Gemini (429)**: Richieste singole per ogni moneta della whitelist. Risolto con batch scanning ogni 15 minuti, cascata a 4 modelli e circuit breaker.
9. **Incompatibilità Python 3.12 UTC**: Crash da `datetime.UTC`. Risolto con aggiornamento Dockerfile a Python 3.12 e gestione esplicita fusi orari.
10. **OOM Freeze su AWS EC2**: Memoria saturata su t3.micro (1 GB). Risolto con 2.0 GB di Swap file su SSD EBS.

---

## 5. LA TUA METODOLOGIA DI STRESS-TESTING (MIGLIAIA DI SCENARI)

Non limitarti a un singolo backtest veloce. Con la tua totale libertà d'azione:
1. **Scarica i dati storici**:
   ```bash
   freqtrade download-data --config user_data/config_prop_nfix8.json --timeframe 5m 15m 1h --timerange 20230101-20241101
   ```
2. **Testa i grandi eventi di crash della storia**:
   - **Flash Crash 12-14 Aprile 2024** (Geopolitica e crollo altcoin del 30-40%)
   - **Flash Crash 5 Agosto 2024** (Black Monday del mercato globale)
   - **Periodi di prolungato trend ribassista o chop estivo** (Giugno-Luglio 2024)
   - **Periodi di rally parabolico** (Q1 2024)
3. **Simulazione Monte Carlo & Worst-Case Permutations**:
   - Esegui simulazioni con slippage maggiorato (0.1% - 0.3%) e commissioni di mercato reali Bybit (taker fee 0.055%).
   - Simula l'ingresso contemporaneo su 3 altcoin che perdono il 10% nelle successive 2 ore.
   - Verifica che in **nessun singolo giorno** il Max Daily Drawdown superi il 2.5%, e in **nessun punto della curva** il Trailing Drawdown superi il 3.8%.
4. **Scrivi Script di Validazione Dedicati**:
   - Se gli script esistenti (`check_prop_firm_rules.py`, `prop_firm_deep_auditor.py`) non coprono uno scenario che ritieni critico, **scrivi tu nuovi script di test e audit** nella cartella `scripts/` e documentane l'esito.

---

## 6. DELIVERABLE FINALI RICHIESTI

Al termine della tua indagine ed esecuzione:
1. **Dossier di Audit Forense**:
   - Quali falle, inefficienze o rischi nascosti hai scoperto nel codice o nei parametri?
   - Quali modifiche hai apportato e perché sono matematicamente superiori?
2. **Matrice di Validazione Empirica**:
   - Tabella comparativa Prima vs Dopo su almeno 3 timeframe di backtest differenti (Bull, Bear, Flash Crash).
   - Metriche tassative: Win Rate %, Profit Factor, Max Daily Drawdown %, Max Overall Drawdown %, Trade Totali, Ratio Guadagno/Rischio.
3. **Commit & Branch Git**:
   - Crea un branch dedicato: `git checkout -b feature/prop-firm-forensic-hardening`
   - Committa tutte le migliorie con messaggi semantici puliti (`feat:`, `fix:`, `perf:`, `test:`).
   - Assicurati che il codice sia immediatamente pronto per essere integrato nel server di produzione live.
