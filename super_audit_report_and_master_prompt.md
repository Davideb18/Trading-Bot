# 🏛️ Master Audit Report & Super Prompt Definitivo: Ecosistema FreqtradeBot
### Versione 3.0 (Enterprise Multi-Stack — Consolidamento Completo Cronistoria 2026)

---

## PARTE 1: REGISTRO STORICO DEI 10 ERRORI CRITICI, BUG E TRAPPOLE RISOLTE (Giugno - Novembre 2026)
*(Dossier obbligatorio per qualsiasi sviluppatore o IA operante sul codebase)*

Durante i mesi di sviluppo, testing e deploy continuo su AWS EC2, il sistema ha affrontato e risolto 10 problemi critici di calcolo matematico, architettura e sentiment AI:

### 1. Il Crollo OGN / Derisking e Discrepanza UI Spot
- **Origine**: A metà ottobre, la coppia `OGN/USDT` è stata aperta dallo spot bot a seguito di un picco di volume fasullo prima dell'introduzione dei filtri istituzionali. OGN ha perso oltre il 50% in 17 giorni. NostalgiaForInfinityX8 ha eseguito derisking parziali (`grind_derisk`) incassando una perdita realizzata di -$37.79 USDT, lasciando un residuo nel wallet.
- **Risoluzione**: Creata una `VolumePairList` istituzionale con floor rigido a **$20.000.000** (Spot) e **$25.000.000** (Prop), `AgeFilter: 60d`, `SpreadFilter: 0.003` e 48 regex di esclusione permanente per meme coin, leveraged tokens e coin illiquide.
- **Lezione Matematica UI**: L'Open Profit mostrato da Freqtrade include sia le perdite parziali già realizzate nella notte sia il flottante reale. Il Closed Profit netto da trade chiusi è rimasto a **+37.18 USDT (100% Win Rate, 35 vinte / 0 perse)**.

### 2. Il Bug del Doppio Conteggio in `get_equity()`
- **Origine**: Nelle prime versioni del Risk Manager, la funzione `get_equity()` interrogava `/balance` e poi sottraeva manualmente il floating PnL delle posizioni aperte da `/status`.
- **Conseguenza**: Poiché l'endpoint `/balance` di Freqtrade calcola già il valore `total` e `total_bot` includendo le perdite/profitti non realizzati, sottraendoli una seconda volta il drawdown a schermo risultava **raddoppiato fittiziamente**, innescando allarmi ingiustificati.
- **Risoluzione**: `get_equity()` legge prioritariamente `total_bot` o `total` in modo pulito, con protezioni `math.isnan()` e fallback a prova di crash (commit `d94d86c38`).

### 3. La Corruzione del Database Trade #15 (ERA Hot-Swap)
- **Origine**: Durante il cambio a caldo della strategia da X7 a X8 su server live, il Trade #15 (ERA/USDT) è stato chiuso in modo anomalo con perdita a codice (-20.97 USDT).
- **Risoluzione**: Creato lo script `scripts/sanitize_spot_database.py` che esegue un backup del database SQLite `tradesv3.sqlite` ed elimina la transazione corrotta, ripristinando la perfetta consistenza matematica.

### 4. L'Exit Prematuro "Structure Break" di NostalgiaForInfinity
- **Origine**: NFI X8 possiede un parametro interno (`bad_trade_controller_structure_break_exit_enable = True`) che svendeva a mercato quando l'EMA gap superava il 35%, tagliando le gambe al ciclo DCA.
- **Risoluzione**: Disattivato categoricamente (`bad_trade_controller_structure_break_exit_enable = False`, commit `064667dc2`), consentendo alla strategia di completare i suoi cicli di acquisto e uscire in profitto o pareggio sui rimbalzi.

### 5. Rischio Leva e Margin Mode nei Futures (HyroTrader 5K)
- **Origine**: Freqtrade in modalità futures poteva ereditare la leva dell'account o usare cross-margin, rischiando la liquidazione totale.
- **Risoluzione**: In `NostalgiaForInfinityX8_Prop.py`, la leva è **hard-clamped a 1.0x**:
  `return min(1.0, float(self.futures_mode_leverage))`
  Nel config: `margin_mode: "isolated"`, `can_short: false`. Nessun rischio di liquidazione forzata o cross-contagion.

### 6. Il Crash-Loop da Riavvio Continuo (`nuclear_triggered: true`)
- **Origine**: Quando il Risk Manager innescava uno stop di emergenza, scriveva `nuclear_triggered: true` su `risk_manager_state.json`. Al riavvio automatico di Docker, lo script leggeva il file e si rimetteva istantaneamente in pausa a ciclo infinito.
- **Risoluzione**: Ricalibrato il meccanismo con reset controllato e auto-healing quando i parametri macro si normalizzano.

### 7. Falsi Allarmi LLM basati su News senza Crollo di Prezzo
- **Origine**: Il modello Gemini leggeva feed RSS e notizie FUD (es. vecchi fallimenti, cause legali o comunicati FBI sull'hack Binance), dichiarando `regime: "PANIC"` anche con Bitcoin piatto o rialzista.
- **Risoluzione**: Introdotto il controllo quantitativo multi-fattoriale **`is_market_crash`**:
  Nessun allarme PANIC viene accettato dall'IA a meno che non sia matematicamente confermato da un crollo effettivo su Binance: **BTC in calo > 8% nelle 24h o > 4% nell'ultima ora** (commit `2ac355b4d`). Senza crollo di prezzo, l'avviso viene degradato a `BEAR` informativo.

### 8. Esaurimento Quota Gemini Free Tier (Errori 429)
- **Origine**: Chiamate individuali per ogni singola coppia della whitelist esaurivano la quota gratuita dell'API in pochi minuti.
- **Risoluzione**:
  - **Batch Scanning**: Una singola richiesta strutturata ogni 15 minuti per tutta la whitelist.
  - **Cascata Multi-Modello**: `gemini-3.1-flash-lite` → `gemini-3.5-flash-lite` → `gemini-3.8-flash` → fallback deterministico sull'Indice Fear & Greed.
  - **Circuit Breaker**: Cooldown di 30 minuti dopo 3 errori consecutivi.

### 9. Incompatibilità Python 3.12 e Fuso Orario UTC
- **Origine**: L'uso di `datetime.UTC` generava crash su ambienti Python precedenti al 3.11.
- **Risoluzione**: Dockerfile aggiornato a Python 3.12 e normalizzazione di tutti i timestamp in UTC esplicito (commit `138a025c9`).

### 10. Gestione Memoria su AWS EC2 (OOM Prevention)
- **Origine**: Sull'istanza `t3.micro` (1 GB RAM), due bot contemporanei con dataframe a 5m/1h/4h/1d provocavano saltuari freeze del kernel Linux.
- **Risoluzione**: Configurato uno Swap File permanente da **2.0 GB** su SSD EBS, garantendo 3.0 GB di memoria virtuale totale.

---

## PARTE 2: MASTER PROMPT PER CLAUDE CODE ULTRA (CARTE BIANCA & ZERO-TRUST RADICALE)

> **Istruzioni per l'utente**: Copia questo prompt e passalo al tuo amico o inseriscilo in Claude Code Ultra sul computer locale. Non richiede credenziali AWS né accessi di produzione.

```markdown
Agisci come Principal Quantitative Systems Architect, Institutional Crypto Fund Auditor e Lead Algorithmic Risk Engineer.

### MANDATO ZERO-TRUST & CARTE BIANCA TOTALE
Stai operando in ambiente locale isolato sul repository clonato da GitHub (branch: feature/prop-firm-futures-validation). Non hai accesso né bisogno di chiavi SSH AWS di produzione.

Non assumere MAI che qualcosa funzioni a priori o sia corretto solo perché è stato scritto nei mesi scorsi.
Il tuo presupposto iniziale deve essere che ogni singolo file, logica di trading, formula di drawdown, filtro di pairlist o script di rischio possa contenere bug latenti, assunzioni deboli o edge-case distruttivi in grado di causare perdite o squalifica dalla Prop Firm.

HAI CARTA BIANCA TOTALE PER:
1. Ripartire da zero e vivisezionare ogni singolo componente dalla A alla Z, analizzando ogni singola variazione ed edge-case in migliaia di scenari possibili.
2. Scaricare qualsiasi dato storico di mercato (giorni, mesi, anni) per testare la strategia su bull run, bear grinds e flash crash storici.
3. Scrivere qualsiasi script di verifica, audit, fuzzer o simulazione Monte Carlo per toglierti qualsiasi dubbio.
4. Modificare o riscrivere qualsiasi parte di codice della strategia (NostalgiaForInfinityX8_Prop.py), dei config o dei risk manager per garantire che l'ecosistema sia matematicamente inattaccabile e sicuro al 100%.

### VINCOLI TASSATIVI PROP FIRM (HYROTRADER 5K TWO-STEP):
- Max Daily Drawdown: limite hard 5.0% ($250). Target di sicurezza ingegneristica: < 2.0% - 2.5% ($100-$125).
- Max Overall Trailing Drawdown: limite hard 8.0% ($400). Target di sicurezza: < 3.5% ($175).
- Profit Target: +8% Step 1 ($400), +5% Step 2 ($250).
- Leva finanziaria: rigidamente fissata a 1.0x (unleveraged futures, isolated margin, zero short).
- Allocazione capitale: tradable_balance_ratio = 0.24, max_open_trades = 3 (max ~8% capitale per slot).

### AREE DI VIVISEZIONE OBBLIGATORIE DALLA A ALLA Z:
1. Strategia (NostalgiaForInfinityX8_Prop.py):
   - Verifica assenza di lookahead bias nelle informative 15m/1h/4h/1d (shift(1)).
   - Verifica assorbimento capitale safety orders/DCA nel caso peggiore (3 posizioni che mediano contemporaneamente fino all'ultimo livello).
   - Verifica che la leva sia hard-clamped a 1.0x (min(1.0, float(leverage))).
   - Verifica disattivazione dello structure break exit prematuro.
2. Configurazione & Pairlist (config_prop_nfix8.json):
   - Verifica fill rate ordini limit e gestione ordini appesi (unfilledtimeout).
   - Verifica filtri VolumePairList ($15M-$25M floor, age filter 60d, spread 0.003) e blacklist completa.
3. Risk Manager & Accounting (llm_risk_manager_prop.py):
   - Verifica al centesimo di get_equity(): zero doppi conteggi di floating PnL, gestione NaN/None/zero.
   - Verifica calcolo Daily Drawdown (allineamento fuso 00:00 UTC) e Trailing Drawdown su peak equity.
   - Verifica isolamento thread tra controllo deterministico drawdown (0ms latenza) e sentinella LLM.
   - Verifica che l'allarme PANIC esiga tassativamente la condizione quantitativa is_market_crash (crollo reale BTC).
4. Stress-Testing su Scenari Estremi:
   - Crash storici (12-14 Aprile 2024, 5 Agosto 2024, bear market 2022).
   - Simulazioni con slippage maggiorato (0.2%-0.3%) e taker fee reali Bybit.

### CONSEGNA:
- Fornisci un report forense con vulnerabilità identificate e risolte.
- Mostra matrice di validazione Prima vs Dopo con Win Rate %, Profit Factor, Max Daily DD % e Trailing DD %.
- Committa le migliorie su un branch dedicato (feature/prop-firm-forensic-hardening) con convenzione Semantic Commits.
```

---

## PARTE 3: WORKFLOW DI DISTRIBUZIONE E DEPLOYMENT (ZERO-RISK COLLABORATION)

```mermaid
graph TD
    subgraph AMBIENTE LOCALE SVILUPPO (Amico & Claude Code)
        A[Git Clone da GitHub] --> B[Download Data Storici Gratuiti]
        B --> C[Backtest su Finestre di Crash 2024]
        C --> D[Ottimizzazione Parametri NFIX8_Prop]
        D --> E[Esecuzione check_prop_firm_rules.py]
        E --> F[Git Push su Branch feature/prop-tuning]
    end

    subgraph CONTROLLO VERSIONI GITHUB
        F --> G[Pull Request & Code Review]
    end

    subgraph PRODUZIONE LIVE AWS EC2 (Solo Davide)
        G --> H[Merge approvato da Davide]
        H --> I[Connessione SSH su 51.21.243.61]
        I --> J[git pull origin feature/prop-firm-futures-validation]
        J --> K[docker compose restart]
    end
```
