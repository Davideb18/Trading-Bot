# 🎯 PROMPT AUDIT FORENSE FASE 2: REDDITIVITÀ & VALIDAZIONE STATISTICA ESTESA
### Da eseguire sul branch `feature/prop-firm-forensic-hardening` (dopo i commit 7c9394141 e bc78e86f2)

> **CONTESTO PER CODEX / CLAUDE CODE**:  
> Hai svolto un eccellente lavoro di hardening difensivo nella Fase 1: l'equity nativa Bybit, l'hard-clamping a 1.0x, la sentinella con latch permanente e i 203 test passati rappresentano una base solida.  
> Tuttavia, come evidenziato dal report forense stesso, **il bot è attualmente in stato NO-GO per carenza di redditività e campione statistico insufficiente (soli 8 trade totali, 0 nel Q1)**:
> 1. Disabilitando completamente il DCA (`adjust_trade_position = return None`), hai rimosso il motore centrale di NostalgiaForInfinityX8, trasformandola in una strategia che non apre posizioni e non raggiungerà mai il target di profitto (+8% Step 1, +5% Step 2 in 15-45 giorni), rischiando la squalifica per inattività (30 giorni).
> 2. Limitare il backtest a sole 3 coppie (BTC, ETH, SOL) non rappresenta l'operatività reale della `VolumePairList` istituzionale.
> 
> **OBIETTIVO FASE 2**: Ripristinare il potenziale di profitto necessario a superare la challenge HyroTrader 5K, **mantenendo rigidamente invariate le protezioni di rischio già implementate (cap 400 USDT per slot, 1.0x leva, Daily DD < 2.5%, Trailing DD < 3.5%)**.

---

## 1. RIPRENDI DAI COMMIT ESISTENTI (ZERO LAVORO DUPLICATO)
- Lavora direttamente sul branch: `feature/prop-firm-forensic-hardening` (a partire da `bc78e86f2`).
- Mantieni integre le correzioni di sicurezza della Fase 1:
  - `freqtrade/exchange/bybit.py` (equity nativa e gestione balance).
  - Leva rigorosamente hard-clamped a 1.0x.
  - La sentinella `scripts/prop_risk_guard.py` e la persistenza dello stato.
  - I test di regressione in `scripts/tests/test_prop_hardening.py`.

---

## 2. MODIFICHE CRUCIALI ALLA STRATEGIA: DCA CONTROLLATO & CAP RIGIDO PER SLOT

Il problema dello spot era il DCA infinito (fino a 8-10 livelli) che prosciugava il wallet.  
La soluzione non è eliminare il DCA (`return None`), ma **capparlo matematicamente a micro-livelli dentro il budget massimo di 400 USDT per posizione**:

### A. Architettura del "Micro-DCA Controllato" (Max 2 Rebuy, Max 400 USDT Totali):
Modifica `NostalgiaForInfinityX8_Prop.py`:
1. **Riabilita `position_adjustment_enable = True`**, ma con vincolo ferreo:
   - `max_entry_position_adjustment = 2` (massimo 2 safety orders in totale).
2. **Micro-Sizing della Stake**:
   - **Ordine Base (Entry iniziale)**: ~120 USDT (30% dello slot).
   - **Safety Order 1**: ~140 USDT (a -2.5% dal prezzo medio).
   - **Safety Order 2**: ~140 USDT (a -5.0% dal prezzo medio).
   - **Cap Assoluto per Slot**: **400 USDT TASSATIVI** (incluso qualsiasi arrotondamento o fee).
   - La funzione `adjust_trade_position()` deve restituire `None` non appena il cumulato investito nel trade raggiunge i 400 USDT o il trade ha già effettuato 2 acquisti.
3. **Stoploss Calibrato**:
   - Stoploss hard posizionato a **-6.0%** dal *prezzo medio di carico consolidato* (dopo i 2 rebuy), garantendo che l'uscita in stoploss su una posizione piena costi al massimo:  
     $$400 \text{ USDT} \times 0.06 = 24 \text{ USDT} \ (0.48\% \text{ del conto})$$
   - Anche nel caso peggiore in cui **tutte e 3 le posizioni piene prendano stoploss contemporaneamente**, la perdita totale è:  
     $$3 \times 24 = 72 \text{ USDT} \ (1.44\% \text{ del conto})$$  
     **ampiamente inferiore al limite giornaliero del 5.0% (250 USDT) e al target di sicurezza del 2.5% (125 USDT)!**

---

## 3. ESTENSIONE DEL CAMPIONE STATISTICO SULLA WHITELIST REALE

Eseguire il test solo su BTC, ETH e SOL distorce la realtà: NFI X8 è concepita per scansionare una watchlist liquida di 20-30 coppie.

1. **Scarica i dati storici per il paniere top-liquidità Bybit Futures** (coppie che rispettano il floor di $25M e l'AgeFilter di 60 giorni):
   - `BTC/USDT:USDT`, `ETH/USDT:USDT`, `SOL/USDT:USDT`, `XRP/USDT:USDT`, `DOGE/USDT:USDT`, `BNB/USDT:USDT`, `SUI/USDT:USDT`, `AVAX/USDT:USDT`, `LINK/USDT:USDT`, `NEAR/USDT:USDT`, `ADA/USDT:USDT`, `DOT/USDT:USDT`, `PEPE/USDT:USDT`.
   - Timeframe: `5m`, `15m`, `1h`, `4h`, `1d`.
   - Timerange: **Intero anno 2024** (`20240101-20241101`) + focus sui periodi di crash (Aprile e Agosto 2024).
2. **Campione Minimo Accettabile**:
   - Il backtest deve produrre **almeno 80 - 200 trade** sull'intero periodo per avere significatività statistica su:
     - Win Rate reale (target: > 70%).
     - Profit Factor (target: > 1.8).
     - Tempo medio di permanenza in trade e trade frequency settimanale.

---

## 4. SIMULAZIONE REALISTICA DI GAP, SLIPPAGE E FEE

1. Esegui il backtest applicando:
   - Fee Bybit reali: **0.055% taker** sia in entrata che in uscita.
   - Slippage stress-test: **0.1% base** e **0.3% estremo**.
2. **Stress-Test Gap di Liquidità**:
   - Simula un gap istantaneo del 10% oltre lo stoploss sulle 3 posizioni e calcola matematicamente l'impatto sul Daily High-Water Mark. Dimostra algebricamente che il conto non viola i 250 USDT di perdita giornaliera.

---

## 5. TEST DI INTEGRAZIONE DOCKER & RUNTIME CHECK

1. Verifica la build del container `Dockerfile.prop` con Docker.
2. Verifica che `docker-compose.prop.yml` si avvii correttamente collegandosi a Bybit Demo con API key configurate tramite variabili d'ambiente (`.env`).
3. Verifica la riconciliazione dell'equity tra `account_equity` esposto da Bybit e l'accounting interno del supervisore `scripts/prop_risk_guard.py`.

---

## 6. CRITERI DI ACCETTAZIONE FINALI (DEFINITION OF DONE)

Il progetto potrà passare da "NO-GO" a **"GO per Paper Trading / Demo Challenge"** SOLO SE:
- [ ] Il backtest sul paniere multi-coppia 2024 genera **almeno 80 trade**.
- [ ] Il Win Rate complessivo è **>= 70%** e il Profit Factor è **>= 1.80**.
- [ ] Il rendimento simulato proietta il raggiungimento dell'8% ($400) entro **15 - 45 giorni di trading**.
- [ ] Il **Max Daily Drawdown** su qualsiasi singola giornata (inclusi 12-14 Aprile e 5 Agosto 2024) rimane **< 2.50% ($125)**.
- [ ] Il **Max Trailing Drawdown** sull'intera curva equity rimane **< 3.50% ($175)**.
- [ ] Non si verificano periodi di inattività > 10 giorni consecutivi (zero rischio squalifica 30 giorni).
- [ ] Tutti i 203 test unitari di hardening della Fase 1 continuano a passare al 100%.

Documenta tutti i risultati empirici in `docs/prop_phase2_matrix.txt` e aggiorna `docs/prop_forensic_audit.txt` con il verdetto finale.
