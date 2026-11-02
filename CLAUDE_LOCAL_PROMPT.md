# MASTER PROMPT DEFINITIVO & DOSSIER STORICO ARCHITETTURALE
### Per Claude Code Ultra, Ingegneri Quantitativi & Collaboratori Distribuiti

> **VERSIONE**: 3.0 (Enterprise Institutional Edition — Consolidata su tutta la cronologia del progetto da Giugno a Novembre 2026)  
> **APPLICAZIONE**: Esecuzione Locale su Repository GitHub (`Trading-Bot`) senza necessità di credenziali AWS di produzione.

---

## 1. VISIONE GENERALE E FILOSOFIA ZERO-BURN
Questo repository (`FreqtradeBot`) non è un semplice bot di trading open-source: è un **sistema quantitativo istituzionale resiliente a eventi Black Swan**, progettato per operare su due stack paralleli e rigorosamente disaccoppiati:

1. **STACK SPOT BINANCE (Accumulazione & Long-Only DCA)**:
   - **Obiettivo**: Crescita patrimoniale continua a rischio di rovina zero (Zero-Burn).
   - **Configurazione**: `user_data/config.json` (Binance Spot, live trading).
   - **Strategia**: `NostalgiaForInfinityX8` (DCA multi-livello ad alta frequenza).
   - **Safe Box**: 20% del capitale totale bloccato come riserva intoccabile (`tradable_balance_ratio: 0.80`).
   - **Gestione del Rischio**: Non si vende MAI in perdita a mercato per fluttuazioni ordinarie. L'accumulazione abbassa il prezzo medio di carico per uscire in profitto o pareggio sui rimbalzi.
   - **Supervisore**: `scripts/llm_risk_manager.py` (interviene *esclusivamente* con `/stopbuy` in caso di allarme).

2. **STACK FUTURES BYBIT (Prop Firm Challenge — HyroTrader 5K Two-Step)**:
   - **Obiettivo**: Superare le sfide di valutazione proprietarie rispettando rigidamente i vincoli di drawdown giornaliero e trailing.
   - **Configurazione**: `user_data/config_prop_nfix8.json` (Bybit Futures, Demo/Live).
   - **Strategia**: `NostalgiaForInfinityX8_Prop` (leva hard-clamped a 1.0x).
   - **Allocazione Capitale**: `tradable_balance_ratio: 0.24`, `max_open_trades: 3` (massimo ~8% di esposizione per singolo trade).
   - **Supervisore**: `scripts/llm_risk_manager_prop.py` (sentinella tarata su soglie di Daily DD < 2.5% e Trailing DD < 3.8%).

---

## 2. REGISTRO STORICO DEGLI ERRORI CRITICI, BUG E TRAPPOLE RISOLTE
*(Chiunque lavori su questo codebase — che sia umano o IA — DEVE conoscere questi 10 errori storici per non reintrodurli mai più)*

### 🔴 TRAPPOLA 1: La "Nuclear Option" con Panic-Sell Forzato a Mercato
- **Il Problema Storico**: Nelle prime versioni del Risk Manager, un drawdown dell'8% o un allarme di panic news innescava la vendita forzata a mercato (`/forceexit`) di tutte le posizioni aperte.
- **La Conseguenza**: Nello spot, dove la strategia NFI stava accumulando sui minimi per mediare (DCA), il bot svendeva sui minimi locali trasformando un drawdown temporaneo non realizzato in una perdita reale devastante.
- **La Soluzione Adottata**: 
  - Nello Spot, la Nuclear Option è stata **disattivata**: il supervisore invia unicamente `/stopbuy` (mette in pausa i nuovi ingressi), lasciando che la strategia gestisca autonomamente gli exit in rimbalzo.
  - Nei Futures, l'uscita forzata è consentita solo ed esclusivamente se confermata da crollo reale di prezzo (vedi Trappola 7) a protezione del conto prop.

### 🔴 TRAPPOLA 2: Il Bug del Doppio Conteggio in `get_equity()`
- **Il Problema Storico**: La funzione `get_equity()` interrogava `/balance` e poi sottraeva manualmente il floating PnL delle posizioni aperte da `/status`.
- **La Conseguenza**: L'endpoint `/balance` di Freqtrade calcola già il valore `total` e `total_bot` includendo le perdite/profitti non realizzati. Sottraendoli una seconda volta, il drawdown a schermo risultava **raddoppiato fittiziamente**, facendo scattare allarmi ingiustificati.
- **La Soluzione Adottata**: 
  - `get_equity()` legge prioritariamente `total_bot` o `total`.
  - Introdotti controlli contro divisione per zero e gestione valori `NaN` o `None` (commit `d94d86c38`).

### 🔴 TRAPPOLA 3: Il Caso OGN / ERA e la Corruzione del Database (`tradesv3.sqlite`)
- **Il Problema Storico**: Durante il cambio a caldo della strategia da X7 a X8 su server live, il Trade #15 (ERA/USDT) è stato chiuso in modo anomalo con perdita a codice (-20.97 USDT). Inoltre, posizioni come OGN/USDT subirono drawdown prolungati, spingendo a tentativi scorretti di svendita.
- **La Soluzione Adottata**:
  - Creato lo script `scripts/sanitize_spot_database.py` per ripulire con backup il DB SQLite, eliminando le anomalie infrastrutturali e preservando l'integrità matematica delle posizioni reali (35 vinte / 0 perse).
  - Regola ferrea: mai riavviare o cambiare strategia a caldo senza prima svuotare o sincronizzare lo stato.

### 🔴 TRAPPOLA 4: L'Exit Prematuro "Structure Break" di NostalgiaForInfinity
- **Il Problema Storico**: NFI X8 possiede una funzione interna chiamata `bad_trade_controller_structure_break_exit_enable`. Quando il prezzo rompeva la struttura tecnica (EMA gap > 35%), la strategia vendeva la coin a perdita.
- **La Soluzione Adottata**: 
  - Disattivato categoricamente: `bad_trade_controller_structure_break_exit_enable = False` (commit `064667dc2`). La strategia deve completare i suoi cicli di safety orders senza svendere sui breakdown.

### 🔴 TRAPPOLA 5: Il Rischio Leva nei Contratti Futures su Bybit
- **Il Problema Storico**: Freqtrade in modalità futures può ereditare leve superiori o modalità cross-margin dall'exchange, rischiando la liquidazione forzata o la squalifica immediata da HyroTrader.
- **La Soluzione Adottata**:
  - In `user_data/strategies/NostalgiaForInfinityX8_Prop.py`, la leva è **hard-clamped a 1.0x**:
    `return min(1.0, float(self.futures_mode_leverage))`
  - Nel config: `margin_mode: "isolated"`, `can_short: false`. Zero rischio di liquidazione incrociata.

### 🔴 TRAPPOLA 6: Il Crash-Loop da Riavvio Continuo (`nuclear_triggered: true`)
- **Il Problema Storico**: Quando il Risk Manager attivava lo stato di emergenza, scriveva `nuclear_triggered: true` sul file di stato persistente (`risk_manager_state.json`). Al riavvio del container Docker, lo script leggeva il file e si rimetteva istantaneamente in pausa a ciclo infinito.
- **La Soluzione Adottata**:
  - Ricalibrato il meccanismo di recovery con reset controllato dello stato e auto-healing quando i parametri macro tornano nella norma.

### 🔴 TRAPPOLA 7: I Falsi Allarmi LLM basati su News senza Crollo Reale di Prezzo
- **Il Problema Storico**: L'IA (Google Gemini) analizzava feed RSS e leggeva titoli allarmistici (es. vecchi fallimenti, cause SEC o dichiarazioni macro) classificando il mercato come `PANIC`, anche se Bitcoin era stabile o in salita.
- **La Soluzione Adottata**:
  - Introdotto il controllo quantitativo multi-fattoriale **`is_market_crash`**:
    Nessun allarme PANIC viene accettato dall'IA a meno che non sia matematicamente confermato da un crollo effettivo su Binance: **BTC in calo > 8% nelle 24h o > 4% nell'ultima ora** (commit `2ac355b4d`).

### 🔴 TRAPPOLA 8: Esaurimento Quota Gemini Free Tier (Errori 429)
- **Il Problema Storico**: Chiamate individuali per ogni singola coppia della whitelist esaurivano la quota gratuita dell'API di Google in pochi minuti.
- **La Soluzione Adottata**:
  - **Batch Scanning**: Una singola richiesta strutturata ogni 15 minuti per tutta la whitelist.
  - **Cascata Multi-Modello**: `gemini-3.1-flash-lite` → `gemini-3.5-flash-lite` → `gemini-3.8-flash` → fallback deterministico sull'Indice Fear & Greed.
  - **Circuit Breaker**: Cooldown di 30 minuti dopo 3 errori consecutivi per evitare ban dell'endpoint.

### 🔴 TRAPPOLA 9: Incompatibilità Python 3.12 e Fuso Orario UTC
- **Il Problema Storico**: L'uso di `datetime.UTC` generava crash sui runtime Python precedenti al 3.11.
- **La Soluzione Adottata**: Dockerfile aggiornato a Python 3.12 e normalizzazione di tutti i timestamp in UTC esplicito (commit `138a025c9`).

### 🔴 TRAPPOLA 10: La Whitelist Statica vs Illiquidità Altcoin
- **Il Problema Storico**: Le liste statiche perdevano le migliori opportunità, ma le VolumePairList non filtrate acquistavano token a bassa liquidità soggetti a pump-and-dump.
- **La Soluzione Adottata**:
  - Implementata `VolumePairList` istituzionale con filtro di liquidità minimo a **$15.000.000 di volume 24h** e blacklist rigorosa di token con leva e stablecoin de-peggate (commit `3ce8bcd22`).

---

## 3. MASTER PROMPT OPERATIVO PER CLAUDE CODE ULTRA (AMBIENTE LOCALE)

```text
Agisci come Principal Quantitative Systems Architect, Institutional Crypto Fund Auditor e Senior Algorithmic Risk Engineer.

### CONTESTO AMBIENTE
Stai operando in ambiente locale isolato sul repository clonato da GitHub (branch: feature/prop-firm-futures-validation). Non hai accesso né bisogno di chiavi SSH AWS di produzione. Il tuo compito è l'analisi matematica, lo stress-testing, l'ottimizzazione e la validazione del codice prima che venga promosso in produzione.

### REGOLE TASSATIVE DI SVILUPPO E ZERO-TRUST
1. NON REINTRODURRE NESSUNO DEI 10 ERRORI STORICI DOCUMENTATI NEL FILE CLAUDE_LOCAL_PROMPT.md:
   - Mai forzare vendite a mercato ingiustificate (panic-sell).
   - Mai consentire leva > 1.0x nei contratti futures (hard-clamped a 1.0x).
   - Mai alterare la formula di get_equity() introducendo doppi conteggi di PnL non realizzato.
   - Preservare sempre la disattivazione del structure break exit su NFIX8.
2. VINCOLI HYROTRADER 5K TWO-STEP CHALLENGE:
   - Max Daily Drawdown consentito: 5.0% ($250). Target di sicurezza strategia: < 2.5%.
   - Max Overall Trailing Drawdown consentito: 8.0% ($400). Target di sicurezza strategia: < 3.8%.
   - Profit Target: +8% (Step 1) / +5% (Step 2).
   - Rapporto tradable balance: max 0.24 (3 trade concorrenti da ~8% ciascuno).
3. WORKFLOW OPERATIVO LOCALE:
   - Scarica i dati storici necessari per le coppie attive in config_prop_nfix8.json:
     freqtrade download-data --config user_data/config_prop_nfix8.json --timeframe 5m 15m 1h --timerange 20240101-20241101
   - Esegui backtest e stress test sulle finestre di crash del 2024 (es. 12-14 Aprile 2024, 5 Agosto 2024):
     freqtrade backtesting --config user_data/config_prop_nfix8.json --strategy NostalgiaForInfinityX8_Prop --timerange 20240101-20241101
   - Verifica i risultati con gli auditor istituzionali:
     python3 scripts/check_prop_firm_rules.py
     python3 scripts/prop_firm_deep_auditor.py
4. CONSEGNA:
   - Ogni ottimizzazione deve essere documentata con parametri 'prima vs dopo', Win Rate, Max Drawdown e Profit Factor.
   - Crea un branch git dedicato (es. feature/prop-firm-optimization) e prepara commit puliti secondo Semantic Commits (feat:, perf:, fix:).
```

---

## 4. SCHEMA COLLABORAZIONE DISTRIBUITA (DEV -> STAGING -> PROD)

```mermaid
graph TD
    subgraph AMBIENTE LOCALE (Amico & Claude Code)
        A[Git Clone da GitHub] --> B[Download Data Storici Gratuiti]
        B --> C[Backtest su Crash 2024]
        C --> D[Ottimizzazione Parametri NFIX8_Prop]
        D --> E[Verifica Scripts Audit HyroTrader]
        E --> F[Git Push su Branch dedicato]
    end

    subgraph REPOSITORY GITHUB
        F --> G[Pull Request / Review Modifiche]
    end

    subgraph AMBIENTE LIVE AWS EC2 (Solo Davide)
        G --> H[Merge approvato]
        H --> I[Git Pull sul server 51.21.243.61]
        I --> J[Docker Compose Restart Zero-Downtime]
    end
```

Con questa architettura, la sicurezza è totale: nessuna credenziale critica viene condivisa, il server live non rischia interruzioni e il team di sviluppo quantitativo opera con i più alti standard istituzionali.
