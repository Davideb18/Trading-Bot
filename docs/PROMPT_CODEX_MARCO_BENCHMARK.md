# PROMPT DEFINITIVO PER CODEX (MARCO) — VERIFICA + BENCHMARK COMPLETO ONE-STEP vs TWO-STEP

Copia e incolla tutto il testo qui sotto dentro Codex sulla repo `https://github.com/Davideb18/Trading-Bot.git` (branch `bot/prop-one-step`).

---

```text
Agisci come un Quantitative Systems Engineer e Quant Developer senior specializzato in Freqtrade, futures perpetui USDT su Bybit e prop firm HyroTrader.

Lavora sulla repository GitHub https://github.com/Davideb18/Trading-Bot.git, branch `bot/prop-one-step`.
Prima di tutto esegui `git pull origin bot/prop-one-step` e verifica di essere sull'ultimo commit.

====================================================================
1. REGOLE NON NEGOZIABILI (LEGGI BENE PRIMA DI INIZIARE)
====================================================================
1) NON MODIFICARE MAI la logica di trading di `user_data/strategies/NostalgiaForInfinityX8_Prop.py` (segnali, entrate, uscite, `adjust_trade_position` / DCA / grinding / rebuy). Deve restare al 100% AST-identica all'originale GitHub `NostalgiaForInfinityX8.py` (verificato da `scripts/tests/test_prop_hardening.py`).
2) NON ri-audire l'intero codebase da zero e NON introdurre nuovi blocchi che impediscano l'esecuzione dei test. I 4 blocker emersi nel commit `486aea22d` sono GIA' STATI RISOLTI in questo branch:
   - `scripts/prop_archive_data.py`: ora supporta `--delete-raw` (attivo di default salvo `--keep-raw`), che elimina ogni `.csv.gz` subito dopo aver scritto `-1m.feather` e salvato `sha256` + `bytes` nel manifest -> spazio occupato **< 150 MB** invece di 220 GB!
   - `scripts/prop_prepare_benchmark_data.py`: ora supporta `--export-download-pairs` (scarica solo perp USDT crypto idonee, ~5-7 GB totali), `--include-archived-delisted` (include i coin delistati archiviati `OM`, `ALPACA`, `FTM`, `MKR`, `UXLINK`, `NEIROETH`, `VOXEL`, `ACT`), `--mcap-mode hybrid --fetch-coingecko` (risolve il buco Coin Metrics post-maggio 2026 mantenendo Coin Metrics autorevole quando presente e usando CoinGecko/carry dopo il cutoff) e `--build-presets-dir` (genera i 3 preset `base`, `strict`, `bluechip`).
   - `scripts/prop_backtest_1to1.py`: ora supporta `--allow-research-schedule` (mantiene il blocco strict di default per i test unitari ma consente l'esecuzione dei benchmark salvando trasparenza e limitazioni nel JSON), fallback in-memory (senza mai fabbricare file falsi su disco) per market/tier/mark/funding dei coin delistati archiviati, fix `maintAmt` nei leverage tiers Bybit, fix Funded senza cap giornaliero fittizio (`daily_profit_cap_percent=1.0`), 3 politiche di prelievo Funded (`reset_5250_to_5000`, `buffer_5450_to_5200`, `buffer_5650_to_5400`), griglia espansa a 24 candidati (2/3/4 slot, entry $155-$350 + unlimited, max/coin $350-$600, max totale $750-$1200, guard `conservative`/`standard`/`near_official`, pausa BTC `off`/`tight`/`standard`/`wide`, Scudo Dinamico Equity ON/OFF, headroom `standard`/`tight`), salvataggio checkpoint atomico dopo ogni candidato e flag `--candidates` / `--skip-crash-sweep`.
3) NON toccare AWS, NON fare deploy, NON modificare credenziali live.
4) Regole ufficiali HyroTrader ($5,000 USDT, leva 1x isolata, long-only) confermate via email dal supporto HyroTrader:
   - One-Step: Target +10% ($500), Max Loss statico 8% (-$400, floor $4,600), Daily Loss 4% (-$200/giorno su equity 00:00 UTC), min 3 giorni qualificanti, Consistency 40% ($200/giorno max contato, pausa live a $175).
   - Two-Step: Fase 1 Target +10% ($500, cap $200/g, 4 giorni qualificanti) -> reset $5,000 -> Fase 2 Target +5% ($250, cap $100/g, pausa live $75, 4 giorni qualificanti) -> Funded. Max Loss statico 10% (-$500, floor $4,500), Daily Loss 5% (-$250/giorno).
   - Giorno Qualificante: trade chiuso al 100% (`remaining_qty == 0`), nozionale aggregato di ingresso (entry + DCA) >= 5% ($250), PnL lordo realizzato prima delle commissioni >= ±1% del nozionale.
   - Inattività: almeno 1 trade reale ogni 30 giorni (nessun micro-trade artificiale).
   - Low-Cap Rule: MCap < $100M o vol 24h $500K-$5M o Bybit Innovation Zone -> max $250 combinati. Politica del bot: esclusione alla radice (Vol >= $20M, PropInstrumentFilter, MCap >= $100M).
   - Limiti Funded ($5,000): margine totale <= 25% ($1,250), nozionale totale <= 2x ($10,000). A leva 1x teniamo il tetto <= $1,200 (24%) per margine tecnico. Profitto massimo per payout = 5% ($250, split 80% = $200 netti al trader).

====================================================================
2. STEP 0 — CONTROLLO RAPIDO PRE-VOLO (MAX 2 MINUTI)
====================================================================
1) Esegui i test automatici per verificare che l'ambiente e tutte le modifiche siano verdi:
   pytest scripts/tests/ -q
   pytest tests/plugins/test_pairlist.py -k MarketCapPairList -q
2) Verifica rapidamente che i file modificati (`scripts/prop_archive_data.py`, `scripts/prop_prepare_benchmark_data.py`, `scripts/prop_backtest_1to1.py`) siano coerenti con le regole HyroTrader/Bybit sopra elencate.
   - Se trovi un bug reale bloccante (crash/errore matematico), correggilo in modo minimale senza introdurre nuovi blocchi e procedi subito ai download e ai test.
   - NON fermarti allo step di verifica: l'obiettivo principale è ESEGUIRE TUTTI I TEST e produrre il report finale.

====================================================================
3. STEP 1 — DOWNLOAD DATI LEGGERO (< 8 GB TOTALI SU DISCO!)
====================================================================
Verifica lo spazio libero su disco (servono ~10-15 GB liberi tra dati e cache backtest, NON 220 GB!).
Esegui nell'ordine:

1) Esporta la lista delle sole perp USDT crypto idonee su Bybit (esclude automaticamente azioni, ETF, forex, commodity, Innovation Zone, ST):
   python scripts/prop_prepare_benchmark_data.py --anti-survivorship --export-download-pairs user_data/forensic/strict_validation/eligible_bybit_pairs.json

2) Scarica 2 anni completi di dati futures Bybit (`1m 5m 15m 1h 4h 1d` + `mark` + `funding_rate` scaricati automaticamente da Freqtrade in modalità futures) SOLO per le coppie crypto idonee (~5-7 GB totali):
   freqtrade download-data --config user_data/config_prop_nfix8.json --pairs-file user_data/forensic/strict_validation/eligible_bybit_pairs.json --trading-mode futures --timeframes 1m 5m 15m 1h 4h 1d --timerange 20241101-20261118 --prepend

3) Scarica gli 8 coin storici delistati/crash (`OMUSDT ALPACAUSDT FTMUSDT MKRUSDT UXLINKUSDT NEIROETHUSDT VOXELUSDT ACTUSDT`) eliminando subito i `.csv.gz` dopo la conversione a `-1m.feather` (< 150 MB totali, con SHA-256 registrato nei manifest):
   python scripts/prop_archive_data.py --start 2024-11-01 --end 2026-11-18 --delete-raw

4) Scarica lo storico market cap Coin Metrics (~100 MB):
   python scripts/prop_marketcap_archive.py

5) Converti i delistati in feather Freqtrade e genera TUTTI E 3 i preset di filtro cripto (`base`, `strict`, `bluechip`) per 2024-11-15 -> 2026-11-15:
   python scripts/prop_prepare_benchmark_data.py --start 2024-11-15 --end 2026-11-15 --anti-survivorship --include-archived-delisted --mcap-mode hybrid --fetch-coingecko --build-presets-dir user_data/forensic/strict_validation

   Controlla l'output stampato: verifica che per tutti e 3 i preset (`base`, `strict`, `bluechip`) sia `empty=0` e `post-May26 empty=0` e che `archived_delisted_included` elenchi i coin delistati archiviati.

====================================================================
4. STEP 2 — ESECUZIONE COMPLETA DI TUTTI I TEST (OGNI VARIABILE ESTERNA)
====================================================================
Tutti i comandi salvano checkpoint atomici su disco e supportano ripresa automatica in caso di interruzione.

--- TEST A: Griglia completa 24 candidati (Base pairlist, 2 anni 1m detail, +10 bps RT, strict wick guard) ---
Testa tutte le leve esterne del bot:
- 2 vs 3 vs 4 crypto simultanee
- Ingresso iniziale $155 / $200 / $255 / $275 / $300 / $350 vs "unlimited"
- Max per crypto $350..$600 e Max totale $750..$1,200 (sotto il tetto Funded $1,250)
- Soglie stop interno: `conservative` vs `standard` vs `near_official`
- Pausa crash BTC: `off` vs `tight` (-3%/-6.5%) vs `standard` (-4%/-8%) vs `wide` (-5%/-10%)
- Scudo Dinamico Equity ON vs OFF (riduce gli slot a `max_open_trades - 1` sotto $4,900)
- Headroom stress reserve: `standard` (250 bps + $30) vs `tight` (100 bps + $15)
- Confronto completo One-Step vs Two-Step (Train Anno 1, OOS Anno 2, Full 24m Funded, coorti sequenziali Eval -> Funded -> 1° Payout, 3 politiche prelievo: Reset $5250->$5000, Buffer $5450->$5200, Wide Buffer $5650->$5400).

Comando Test A:
   python scripts/prop_backtest_1to1.py --pairlist-schedule user_data/forensic/strict_validation/pairlist_schedule_base_2y.json --allow-research-schedule --start 2024-11-15 --end 2026-11-15 --timeframe-detail 1m --slippage-bps 10 --strict-wick-guard --cohort-step-days 14 --skip-crash-sweep --grid-json user_data/forensic/strict_validation/grid_base_24cands.json

--- TEST B: Confronto filtri cripto (`base` vs `strict` vs `bluechip`) + Coorti settimanali (7gg) + Stress giornaliero Autunno 2026 (57 partenze giornaliere 15 Set - 10 Nov 2026) ---
Dal Test A seleziona i **4 migliori candidati** (criterio rigoroso: 0% `fail_official` sia in One-Step sia in Two-Step, 0 bar `funded_margin_gt_25pct`, miglior compromesso sicurezza/profitto su OOS Anno 2 e 24 mesi; includi almeno un candidato 2-slot e uno 3-slot, più il miglior candidato near_official o shield se competitivo).
Sostituisci `<TOP_4_IDS>` con i 4 ID esatti separati da spazio ed esegui su tutti e 3 i filtri cripto (SENZA `--skip-crash-sweep`, così esegue tutto lo sweep giornaliero Autunno 2026):

   python scripts/prop_backtest_1to1.py --pairlist-schedule user_data/forensic/strict_validation/pairlist_schedule_base_2y.json --allow-research-schedule --start 2024-11-15 --end 2026-11-15 --timeframe-detail 1m --slippage-bps 10 --strict-wick-guard --cohort-step-days 7 --candidates <TOP_4_IDS> --grid-json user_data/forensic/strict_validation/grid_top4_base_7d_crash.json

   python scripts/prop_backtest_1to1.py --pairlist-schedule user_data/forensic/strict_validation/pairlist_schedule_strict_2y.json --allow-research-schedule --start 2024-11-15 --end 2026-11-15 --timeframe-detail 1m --slippage-bps 10 --strict-wick-guard --cohort-step-days 7 --candidates <TOP_4_IDS> --grid-json user_data/forensic/strict_validation/grid_top4_strict_7d_crash.json

   python scripts/prop_backtest_1to1.py --pairlist-schedule user_data/forensic/strict_validation/pairlist_schedule_bluechip_2y.json --allow-research-schedule --start 2024-11-15 --end 2026-11-15 --timeframe-detail 1m --slippage-bps 10 --strict-wick-guard --cohort-step-days 7 --candidates <TOP_4_IDS> --grid-json user_data/forensic/strict_validation/grid_top4_bluechip_7d_crash.json

--- TEST C: Stress test completo attrito/slippage (`0 bps`, `+10 bps`, `+28 bps`, `strict_wick @ 10 bps`, `strict_wick @ 28 bps`) sul vincitore assoluto ---
Sostituisci `<WINNER_SCHEDULE>` (tra `base`, `strict`, `bluechip`) e `<WINNER_ID>` con il vincitore assoluto del Test B (se One-Step e Two-Step hanno vincitori diversi, esegui il comando per entrambi con nomi file distinti):

   python scripts/prop_backtest_1to1.py --pairlist-schedule <WINNER_SCHEDULE> --allow-research-schedule --start 2024-11-15 --end 2026-11-15 --timeframe-detail 1m --cohort-step-days 7 --candidates <WINNER_ID> --suite-json user_data/forensic/strict_validation/suite_winner_stress.json

--- TEST D: Verifica matematica Shock Bounds istantanei (-35% / -50% su 1 coin, -25% su 2 coin) ---
   python scripts/prop_shock_bounds.py

====================================================================
5. STEP 3 — REPORT FINALE IN ITALIANO SEMPLICE
====================================================================
Scrivi il report definitivo in `docs/REPORT_DEFINITIVO_ONE_STEP_VS_TWO_STEP.md` in **italiano chiaro, diretto e comprensibile anche a chi non è un programmatore** (spiega ogni metrica con parole semplici, senza sigle criptiche non spiegate).

Il report DEVE contenere tabelle con numeri reali estratti direttamente dai JSON generati (mai stime a occhio):
1) **Verdetto Finale Chiaro: Meglio One-Step o Two-Step?**
   - Confronto diretto su: % superamento challenge, numero fallimenti ufficiali (`fail_official`), giorni mediani e caso peggiore (P95) per passare la challenge, giorni mediani al 1° prelievo, guadagno netto in 24 mesi, drawdown peggiore giornaliero e totale (su close e su wick 1m), sopravvivenza nel crollo Autunno 2026 (57 partenze giornaliere).
   - Raccomandazione secca su quale sfida comprare per prima (e perché).
2) **Classifica Completa dei 24 Candidati (Test A) + Analisi di Ogni Singola Variabile:**
   - Tabella completa di tutti i 24 candidati con: Train Anno 1, OOS Anno 2, Full 24 mesi (1-Step e 2-Step), max drawdown wick, % superamento 1-Step e 2-Step, giorni 1° payout.
   - Sezione chiara che risponde a ciascuna domanda:
     * Meglio **2, 3 o 4 crypto contemporanee**?
     * Meglio **ingresso iniziale fisso >= $255** (qualifica subito al 1° ingresso) o **$155/$200** o `"unlimited"`?
     * Conviene spingere fino a **$1,100–$1,200** di esposizione totale oppure **$850–$950** è più sicuro/redditizio?
     * Conviene avvicinare gli stop interni (`near_official`) oppure `standard` / `conservative` evita fallimenti reali?
     * La **Pausa Crash BTC** aiuta davvero (`off` vs `tight` vs `standard` vs `wide`)?
     * Lo **Scudo Dinamico Equity** (ridurre gli slot sotto $4,900) migliora sicurezza e rendimento?
     * Il buffer headroom `tight` vs `standard` cambia qualcosa?
3) **Confronto Filtri Cripto (`Base` vs `Strict` vs `Bluechip`) & Anti-Survivorship (Test B):**
   - Impatto reale su profitti, drawdown, crollo Set–Nov 2026 e coin delistati (`OM`, `ALPACA`, `FTM`, `MKR`, `UXLINK`, `NEIROETH`, `VOXEL`, `ACT`): quante volte sono stati filtrati fuori o tradati, e con quale esito.
4) **Confronto Politiche di Prelievo in Funded (su entrambi 1-Step 8% e 2-Step 10%):**
   - **Reset `$5,250 -> $5,000`** vs **Cuscinetto `$5,450 -> $5,200`** vs **Cuscinetto Ampio `$5,650 -> $5,400`**: quanti prelievi, quanti $ netti in tasca (80%), dopo quanti giorni arriva il 1° prelievo, e se il conto sopravvive al floor statico anche sui wick intraday (`wick_coverage_complete=True`).
5) **Stress Test Attrito & Telemetria DCA & Shock Bounds (Test C + D):**
   - Confronto `0 bps`, `+10 bps`, `+28 bps` (close e strict wick).
   - Telemetria DCA reale: quanti trade usano DCA, media e massimo DCA per trade, dimensione massima singolo DCA (prova di piena conformità anti-Martingala).
   - Margine residuo dopo shock istantaneo `-35%` / `-50%` su 1 coin o `-25%` su 2 coin.
6) **Configurazione Finale Pronta all'Uso:**
   - I valori esatti consigliati per `user_data/config_prop_nfix8.json` (solo parametri esterni, strategia 100% intatta) + eventuale lista chiara delle modifiche esterne opzionali (es. se lo Scudo Dinamico o un nuovo threshold BTC Macro ha vinto nettamente nei test, segnalarlo così lo porteremo in produzione prima di comprare la challenge).
   - Nota trasparente su eventuali limiti metodologici residui dei dati storici (`mcap_mode=hybrid`, fallback mark/funding in-memory sui delistati).

Al termine, fai `git add docs/REPORT_DEFINITIVO_ONE_STEP_VS_TWO_STEP.md` (più eventuali piccoli fix documentati se necessari), committa e pusha su `bot/prop-one-step` (NON committare i file binari/JSON in `user_data/forensic/` o `user_data/data/` che sono gitignored).
```
