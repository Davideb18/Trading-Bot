# PROMPT COMPLETO E DEFINITIVO PER CODEX (AUDIT MODIFICHE + ESECUZIONE BENCHMARK ONE-STEP VS TWO-STEP)

> Istruzioni d'uso: copia e incolla integralmente tutto il blocco dentro il riquadro di testo qui sotto in una nuova sessione Codex (zero contesto necessario: contiene tutte le regole, la cronologia esatta, l'elenco dettagliato di ogni modifica da controllare prima dei test e tutti i comandi per eseguire i benchmark e generare il report finale).

---

```text
Agisci come un Quantitative Systems Engineer e Quant Developer senior specializzato in Freqtrade, futures perpetui USDT su Bybit e prop firm HyroTrader.

Lavora sulla repository GitHub `https://github.com/Davideb18/Trading-Bot.git`, branch `bot/prop-one-step`.
Prima di fare qualsiasi operazione, esegui `git status` e `git pull --ff-only` per assicurarti di avere gli ultimi commit aggiornati sul branch `bot/prop-one-step`.

Questo prompt ha **DUE FASI OBBLIGATORIE E SEQUENZIALI**:
- **FASE 0 (PRIMA DI TUTTO — CODE AUDIT RIGOROSO):** devi controllare riga per riga TUTTE le modifiche recentemente applicate al codice dopo il commit `a1a99af32` (descritte in dettaglio nella Sezione 3 qui sotto), verificare che non ci siano bug, incongruenze tra live e backtest, lookahead o falle di sicurezza, correggere eventuali difetti reali che individui (SENZA MAI toccare la logica nativa della strategia) e confermare tutti i test unitari verdi.
- **FASI 1–4 (BENCHMARK REALE E REPORT FINALE):** solo dopo aver superato la Fase 0, devi completare/preparare i 2 anni di dati reali (incluse le coin delistate), eseguire fisicamente tutta la suite di backtest 1:1 (One-Step vs Two-Step, griglia 14 configurazioni di money management + filtri crypto + stress crolli + Train vs Out-of-Sample + prelievi Funded) e consegnare il **Report Finale Numerico Completo in italiano semplice**.

**DIVIETO ASSOLUTO:** NON fermarti a scrivere un "report di stato senza backtest". Dopo aver chiuso la Fase 0, DEVI lanciare fisicamente i benchmark e produrre tutti i numeri reali. Non eseguire deploy AWS, non inviare ordini live/demo su exchange e non committare dati grezzi in `user_data/data/` o `user_data/forensic/`.

====================================================================
1. CONTESTO COMPLETO DEL PROGETTO E CRONOLOGIA
====================================================================
- Strategia: `user_data/strategies/NostalgiaForInfinityX8_Prop.py` (derivata dalla strategia originale GitHub `user_data/strategies/NostalgiaForInfinityX8.py`, `system_v4`, timeframe `5m`, futures perpetui USDT su Bybit, `margin_mode: "isolated"`, leva bloccata a `1.0x`, solo Long `can_short: false`, `prop_non_compounding: true` su base fissa `$5.000`).
- Configurazione principale: `user_data/config_prop_nfix8.json`.
- Motore backtest 1:1: `scripts/prop_backtest_1to1.py`.
- Preparazione dataset & schedule storico point-in-time: `scripts/prop_prepare_benchmark_data.py` (affiancato da `scripts/prop_archive_data.py`, `scripts/prop_marketcap_archive.py`, `scripts/prop_shock_bounds.py`, `scripts/prop_source_probe.py`).
- Supervisore live e guardie di sicurezza: `scripts/llm_risk_manager_prop.py`, `scripts/prop_risk_guard.py`, `freqtrade/prop_profile.py`, `freqtrade/prop_risk_budget.py`, `freqtrade/prop_qualification.py`, `freqtrade/prop_controls.py`, `freqtrade/prop_security.py`, `freqtrade/plugins/pairlist/PropInstrumentFilter.py`, `freqtrade/plugins/pairlist/MarketCapPairList.py`.

CRONOLOGIA DEI COMMIT RECENTI SU `bot/prop-one-step`:
1. `f4d6df68f` (17 Nov 2026): introdotti filtri anti-pump (`VolatilityFilter <= 0.50`, `RangeStabilityFilter <= 0.85`, blacklist `INIT`) e cap `500/950 USDT` (`tradable_balance_ratio: 0.19`).
2. `8687bd12b` (18 Nov 2026): aggiornati i limiti One-Step a `8%` max loss statico e `3` giorni qualificanti (mantenendo retrocompatibilità legacy `6% / 5d`).
3. `a8b903788` (18 Nov 2026): migliorata persistenza ledger ordini in `llm_risk_manager_prop.py`.
4. `a1a99af32` (18 Nov 2026, "Fix fatti dall'audit"):
   - Introdotto `freqtrade/prop_qualification.py` (giorni qualificanti solo su trade 100% chiusi, nozionale >= $250, |gross_pnl| >= 1% pre-fee).
   - Rimosso il keep-alive artificiale (`/forceenter` + `force_exit` e micro-trade fittizio `$15` nel backtest).
   - Introdotto `PropInstrumentFilter.py` e aggiornato `MarketCapPairList.py` (`min_market_cap: 100M`).
   - Corretto un bug di lookahead in `HistoricalPairListAdapter` (prima del primo snapshot ora restituisce `[]` invece dello snapshot futuro).
   - Aggiunti `prop_archive_data.py` (eseguito solo con `--sample` su 19 giorni), `prop_marketcap_archive.py`, `prop_shock_bounds.py`, `prop_source_probe.py`.
   - **MA** in `a1a99af32` era stato anche aggiunto un wrapper (`freqtrade/prop_dca.py`) dentro `NostalgiaForInfinityX8_Prop.py` che tagliava i DCA a max 2 aggiunte, e NON era stato eseguito nessun backtest.
5. **ULTIMO COMMIT DOPO `a1a99af32` (OGGETTO DELL'AUDIT IN FASE 0):**
   - Ha ripristinato `NostalgiaForInfinityX8_Prop.py` 100% identica a `NostalgiaForInfinityX8.py` sui DCA (rimuovendo `prop_dca.py`), perché l'utente impone come **VINCOLO TASSATIVO** di NON toccare la logica interna della strategia.
   - Ha risolto il problema della gestione esterna dei soldi ("se ci sono 3 crypto aperte non può più mediare") separando i 4 numeri del denaro (`max_open_trades`, `stake_amount`, `max_slot_notional_usdt`, `max_aggregate_notional_usdt`) e bloccando l'apertura di nuove prime entry rimpicciolite.
   - Ha migliorato `MarketCapPairList.py` con cache su disco e preservazione ordine volume.
   - Ha esteso `prop_profile.py` e `prop_backtest_1to1.py` per supportare **Two-Step sequenziale + Funded + prelievi Reset vs Cuscinetto + shock su posizioni aperte + griglia automatica `--grid-json` (14 combinazioni fino a $1.200 totali e stop interni vicini al limite ufficiale)** e creato `scripts/prop_prepare_benchmark_data.py`.

====================================================================
2. REGOLE UFFICIALI HYROTRADER & EMAIL SUPPORTO (VINCOLANTI AL 100%)
====================================================================
Conto testato: **$5.000 USDT** (Bybit Linear USDT Perpetual, leva `1.0x`, Isolated, Long-Only).

A) HYROTRADER ONE-STEP ($5.000):
- Target Evaluation: **10%** (`+$500`)
- Max Loss Statico: **8%** (`-$400` -> floor `$4.600`)
- Daily Loss (reset 00:00 UTC su equity): **4%** (`-$200/giorno`)
- Min. Trading Days: **3 giorni qualificanti**
- Consistency Rule 40%: max **40%** del target in un singolo giorno UTC (**`$200/giorno`** conteggiati; le giornate in perdita contano per intero)

B) HYROTRADER TWO-STEP ($5.000):
- Target Fase 1: **10%** (`+$500`) -> reset -> Target Fase 2: **5%** (`+$250`) -> Funded
- Max Loss Statico (Fase 1, Fase 2 e Funded): **10%** (`-$500` -> floor `$4.500`)
- Daily Loss (reset 00:00 UTC su equity): **5%** (`-$250/giorno`)
- Min. Trading Days: **4 giorni qualificanti** per Fase 1 e **4 giorni qualificanti** separati per Fase 2
- Consistency Rule 40% in ENTRAMBE le fasi:
  * Fase 1: max **`$200/giorno`** conteggiati verso `+$500`
  * Fase 2: max **`$100/giorno`** conteggiati verso `+$250` (molto vincolante!)

C) RISPOSTA UFFICIALE RICEVUTA VIA EMAIL DAL SUPPORTO HYROTRADER (TESTO VINCOLANTE):
1. **Custom Trading Bot & Freqtrade:** Bot proprietario via Bybit Demo API + Freqtrade **CONSENTITI** sia in Evaluation che in Funded (vietati copy trading, segnali terzi, strategie comprate, exploit di esecuzione).
2. **DCA vs Martingale:** DCA pre-pianificato dentro un'allocazione fissa per singola posizione (esempio citato da HyroTrader: *"opening an initial $150 position and adding further predetermined entries within your fixed $500 allocation"*) è generalmente consentito; aumentare progressivamente la size per recuperare perdite o sistemi di recovery aggressivi rischiano classificazione Martingale.
   - **NOTA STRATEGIA:** In `NostalgiaForInfinityX8.py` (`system_v4`, attivo), `system_v4_grind_1_stakes_futures = [0.20, 0.20, 0.20, 0.20, 0.20]` e `system_v4_rebuy_mode_stakes_futures = [1.0, 1.0, 1.0, 1.0]` sono già piatti/predeterminati dentro il budget dello slot. **REGOLA TASSATIVA DELL'UTENTE: NON MODIFICARE `NostalgiaForInfinityX8_Prop.py` (ingressi, uscite e `adjust_trade_position` devono restare 100% AST-identici a `NostalgiaForInfinityX8.py`).** Regoliamo SOLO i limiti esterni (soldi, filtri crypto, guardie drawdown) e registriamo nei report le statistiche esatte dei DCA eseguiti (numero DCA per trade, size max per singolo DCA, nozionale max per coin).
3. **Low-Cap Altcoin Rule:** Una coin è low-cap se: Market Cap `< $100M`, OPPURE volume 24h `$500K–$5M`, OPPURE Bybit Innovation Zone (`symbolType == "innovation"`). Su conto `$5.000` il limite è **MAX `$250` COMBINATI TOTALI** su tutte le posizioni low-cap aperte + relativi DCA (NON `$250` a coin, ma `$250` totali sommati!). Poiché il nostro slot per coin è `$350–$600` (`> $250`), la politica obbligatoria è **ZERO LOW-CAP** (escludere al 100% Innovation Zone, tag `ST`, azioni/ETF/commodity/forex, volume 24h `< $20M`, e Market Cap `< $100M`).
4. **Minimum Trading Days & Inactivity:**
   - Su `$5.000`, un trade qualifica il giorno UTC di chiusura finale SOLO se:
     * È **completamente chiuso** (`remaining_qty == 0`),
     * Ha nozionale combinato (Prima Entry + eventuali DCA nello stesso trade) **`>= $250`** (`5%` del bilancio iniziale),
     * Genera profitto **o perdita** realizzata lorda **prima delle fee `>= 1%`** del nozionale (es. `>= $2,50` su `$250`, `>= $3,00` su `$300`).
   - Inattività: almeno 1 trade genuino ogni **30 giorni** (vietati micro-trade artificiali per manipolare la regola; resta solo l'alert al giorno 24 e il controllo `inactivity_30d_breach` nei backtest).
5. **Funded Exposure Limits ($5.000):**
   - **Max Combined Margin = `$1.250` (`25%`)**
   - **Max Combined Notional = `$10.000` (`2x`)**
   - A leva `1x`, il limite vincolante è `$1.250` (`$950` = `19%` già confermato conforme; testiamo fino a `$1.200` = `24%` lasciando `$50` di margine tecnico per oscillazioni positive del mark price a posizione aperta).
   - Max profit per singolo payout = **`5%` (`$250`**, split trader `80%` = `$200` netti).

====================================================================
3. FASE 0 (OBBLIGATORIA PRIMA DEI TEST) — AUDIT RIGA PER RIGA DELLE MODIFICHE RECENTI
====================================================================
Prima di lanciare qualsiasi simulazione, devi ispezionare (`git diff a1a99af32` + lettura diretta dei file) TUTTI questi 13 file toccati dopo `a1a99af32`, verificare la correttezza tecnica di ogni riga e correggere eventuali bug reali che trovi:

1. `user_data/strategies/NostalgiaForInfinityX8_Prop.py` (rimosso wrapper `clamp_addition` in `adjust_trade_position`), `scripts/tests/test_prop_hardening.py` (ripristinata verifica AST 1:1 con `NostalgiaForInfinityX8.py`), rimozione di `freqtrade/prop_dca.py` e `scripts/tests/test_prop_dca_flat.py`:
   - **Controlla:** `adjust_trade_position`, `custom_stake_amount`, `custom_exit`, `populate_indicators`, `populate_entry_trend`, `populate_exit_trend` devono essere 100% AST-identiche a `NostalgiaForInfinityX8.py`. Non reintrodurre alcun blocco artificiale sui DCA nella strategia.

2. `freqtrade/freqtradebot.py` (+14 righe intorno a riga 1366) & `user_data/config_prop_nfix8.json`:
   - **Cosa è stato modificato:**
     * In `config_prop_nfix8.json`: `"stake_amount": 255.0` (Prima Entry fissa `>= $250`, al posto di `"unlimited"`), `"amend_last_stake_amount": false`, rimosso `"prop_max_dca_additions": 2`, `"max_slot_notional_usdt": 400.0`, `"max_aggregate_notional_usdt": 950.0` (`tradable_balance_ratio: 0.19`), aggiunti `"preserve_order": true` e `"cache_file": "user_data/forensic/coingecko_marketcap_cache.json"` in `MarketCapPairList`.
     * In `freqtradebot.py` (e in `prop_backtest_1to1.py`): quando `trade is None` (nuova Prima Entry), se `prop_guard_enabled` è attivo, `amend_last_stake_amount` è `false`, e `stake_amount > prop_headroom`, il bot salta la nuova apertura (`return enter_limit_requested, 0.0, leverage`) invece di rimpicciolire la Prima Entry a pochi dollari e rubare la riserva DCA delle crypto già aperte.
   - **CHECK CRITICI DA FARE IN FASE 0:**
     * Controlla l'interazione tra `wallets.get_trade_stake_amount()` (`freqtrade/wallets.py`), `stake_amount: 255.0`, `amend_last_stake_amount: false`, e `custom_stake_amount()` di NFIX8 (`regular 1.0x`, `rapid 0.75x`, `rebuy 0.25x`, `grind 0.20x`):
       - Verifica cosa succede in live e in backtest quando hai 2 o 3 trade aperti: es. se `available_amount` è `$185` (riserva DCA) e `stake_amount` è `$255` con `amend_last_stake_amount: false`, `wallets._check_available_stake_amount` alza `DependencyException` (correttamente bloccando una nuova 3ª/4ª entry per preservare la riserva DCA), mentre `check_and_call_adjust_trade_position` per i DCA usa `wallets.get_available_stake_amount()` (`$185`) e quindi **permette regolarmente i DCA**! Conferma che non ci siano crash né blocchi involontari sui DCA.
       - Verifica che `prop_risk_approval.py` (`policy_identity`, `reviewed_policy`) e `prop_risk_budget.py` funzionino senza errori sia con `stake_amount` numerico (`255.0`) sia con `max_aggregate_notional_usdt` fino a `$1.200` (`0.24 * $5.000`).

3. `freqtrade/plugins/pairlist/MarketCapPairList.py` & `scripts/tests/test_prop_marketcap_fallback.py`:
   - **Cosa è stato modificato:** aggiunto check `self._last_good_at <= 0` (fix bug `time.monotonic() < fallback_max_age` su sistemi con uptime < 1h), salvataggio atomico su disco (`cache_file`) + ricaricamento da disco dopo riavvio se CoinGecko è offline entro `fallback_max_age`, e `preserve_order: true` per mantenere l'ordinamento per volume di `VolumePairList`.
   - **CHECK CRITICI DA FARE IN FASE 0:**
     * Controlla che il reload da disco non possa mai aggirare `fallback_max_age` (anche in caso di clock skew `age < 0` o JSON corrotto/vuoto), che `OperationalException` resti fail-closed se non c'è prova valida, e che tutti i 27 test di `tests/plugins/test_pairlist.py -k MarketCapPairList` passino.

4. `freqtrade/prop_profile.py`:
   - **Cosa è stato modificato:** aggiunto supporto a `hyro_two_step` (`phase_1`, `phase_2`, `funded`), `hyro_one_step_funded`, e `build_program_profiles(...)` con soglie guard configurabili (`one_step_daily_guard`, `one_step_max_guard`, `two_step_daily_guard`, `two_step_max_guard`), lasciando invariata la validazione strict su `hyro_one_step`.
   - **CHECK CRITICI DA FARE IN FASE 0:**
     * Verifica che nessun controllo di sicurezza del profilo operativo live (`hyro_one_step`) sia stato indebolito e che `test_prop_profile.py` passi al 100%.
     * Verifica che `two_step_phase2` imponga esattamente `profit_target_percent == 0.05` (`$250`), `daily_profit_cap_percent == 0.40` (`$100/giorno`), `official_daily == 0.05` (`$250`), `official_max_loss == 0.10` (`$500`), `min_trading_days in (4, 5)`.

5. `scripts/prop_backtest_1to1.py` & `scripts/tests/test_prop_strict_ledger.py`:
   - **Cosa è stato modificato:**
     * Aggiunti contatori hard zero-violazioni (`official_daily_breach_wick_bars`, `official_overall_breach_wick_bars`, `funded_margin_gt_25pct_bars`, `funded_notional_gt_2x_bars`, `max_slot_notional_observed`, `max_aggregate_notional_observed`).
     * Aggiunto controllo shock live su posizioni aperte (`-35%` e `-50%` su 1 coin, `-25%` su 2 coin con 39 bps frizione).
     * Aggiunto `simulate_funded_withdrawals(...)` (Reset `$5.250 -> $5.000` vs Cuscinetto `$5.450 -> $5.200`).
     * Aggiunto `run_sequential_program_cohorts(...)` (One-Step Eval -> Funded -> 1° Payout vs Two-Step Fase 1 -> Fase 2 -> Funded -> 1° Payout + sweep giornaliero 15 Set – 10 Nov 2026).
     * Aggiunto `run_grid_sweep(...)` (`--grid-json`) con `DEFAULT_MONEY_GRID` (14 combinazioni complete: 2 vs 3 crypto, da `$850` fino a `$1.200` totali, Prima Entry `$200/$255/$275/$300/$350`, max per coin `$350..$600`, e confronto `guard: "standard"` vs `guard: "near_official"` cioè `-3.8%/-7.5%` su One-Step e `-4.8%/-9.5%` su Two-Step).
   - **CHECK CRITICI DA FARE IN FASE 0 (SISTEMALI SE SERVE PRIMA DI LANCIARE I TEST!):**
     * **Bug potenziale #1 — Continuous Funded Two-Step:** in `_execute_scenario` e `run_grid_sweep`, `continuous_funded` gira attualmente solo sul profilo One-Step (`4%/8%`). Verifica e aggiungi (se mancante) anche `continuous_funded_two_step` con limiti Two-Step (`5%/10%`, floor `$4.500`) così il confronto dei prelievi Funded (Reset `$5.250->$5.000` vs Cuscinetto `$5.450->$5.200`) e del rendimento annuo netto su 2 anni sia calcolato in modo esatto sia per One-Step che per Two-Step!
     * **Bug potenziale #2 — Intraday Wick nei Prelievi Funded (`simulate_funded_withdrawals`):** attualmente `simulate_funded_withdrawals` legge `midnight_equity` da `prop_daily_curve`. Verifica che tracci e verifichi anche il minimo intraday **wick** della giornata (es. registrando `min_day_equity_wick` in `prop_daily_curve`) così non si sottostimi mai un drawdown intraday dopo un prelievo Reset!
     * **Bug potenziale #3 — Residuo stato tra candidati in `run_grid_sweep`:** verifica che `_apply_candidate_money_params` non lasci sporcizia tra un candidato e l'altro (es. `profile`, `bt._prop_profile`, `bt._prop_policy`, `bt.wallets`, `bt.config`, e che sul candidato `S3_UNLIM_SLOT500_AGG950` `amend_last_stake_amount` sia coerente per il confronto baseline).
     * **Bug potenziale #4 — Daily Consistency Pause su Two-Step Fase 2:** verifica in `compute_hyrotrader_ledger` che su Fase 2 (`daily_cap = $100`) `daily_pause_threshold = min(100*0.875, 100-25) = $75.00` sia sensato e che le coorti Fase 1 -> Fase 2 -> Funded resettino completamente stato, equity (`$5.000`), `qualifying_days` e `phase_start` senza portare dietro trade/ordini aperti dalla fase precedente!
     * **Bug potenziale #5 — Telemetria DCA e Low-Cap:** aggiungi (se non già nel summary) il conteggio dei DCA per trade (max DCA sullo stesso trade, distribuzione size DCA per verificare la compatibilità con la regola HyroTrader) e il contatore `lowcap_combined_gt_250_bars` (verificando contro i metadati mcap/innovation).

6. `scripts/prop_prepare_benchmark_data.py` (nuovo file):
   - **Cosa fa:** converte gli archivi 1m di `prop_archive_data.py` in feather Freqtrade (`1m, 5m, 15m, 1h, 4h, 1d`, `mark`, `funding_rate`) e genera il calendario storico point-in-time (`usable_day = day + 1d`, zero lookahead).
   - **CHECK CRITICI DA FARE IN FASE 0 (SISTEMALI PRIMA DI GENERARE GLI SCHEDULE!):**
     * **Bug potenziale #1 — Hindsight Blacklist Bias (CRITICO!):** `user_data/config_prop_nfix8.json` oggi contiene nella `pair_blacklist` statica decine di coin crypto che sono state aggiunte DOPO essere crollate nel 2025–2026 (es. `OM`, `ALPACA`, `FTM`, `MKR`, `UXLINK`, `NEIROETH`, `VOXEL`, `ACT`, `INIT`). Se `prop_prepare_benchmark_data.py` applica quella `pair_blacklist` retroattivamente al 2024–2025, cancella dallo storico proprio le coin delistate/crollate introducendo survivorship/lookahead bias! Aggiungi/usa una modalità `--anti-survivorship` che esclude SOLO categorie oggettive note ex-ante (stablecoin, fiat, token a leva `UP/DOWN/3L/3S`, azioni/ETF/commodity/forex/TradFi, fan-token) ma **LASCIA ENTRARE** le altcoin crypto storiche come `OM`, `ALPACA`, `FTM`, `MKR`, `UXLINK`, `NEIROETH`, `VOXEL`, `ACT`, `INIT` finché rispettavano i filtri oggettivi (`Age`, `Volume`, `Volatility`, `Range`, `MarketCap >= $100M`) in quel giorno!
     * **Bug potenziale #2 — Copertura Market Cap (`base not in mcap_map`):** attualmente in `build_point_in_time_schedule` se `base not in mcap_map` (non presente nei CSV gratuiti Coin Metrics), la coin passa senza prova di mcap `>= $100M`! Correggi gestendo esplicitamente la provenienza mcap (integrando anche snapshot CoinGecko / gestendo fail-closed o tracciando esplicitamente quali coppie avevano mcap verificato vs sconosciuto, e gestendo le collisioni di ticker).
     * **Bug potenziale #3 — Resampling `funding_rate` / `mark` su archivi delistati e filtri `VolatilityFilter` / `RangeStabilityFilter`:** verifica che le formule di `volatility_10d` e `roc_10d` in `prop_prepare_benchmark_data.py` replichino esattamente quelle dei plugin Freqtrade `VolatilityFilter` e `RangeStabilityFilter` (usando solo candele chiuse fino al giorno precedente, zero lookahead!).

Dopo aver completato l'audit e gli eventuali fix di Fase 0, esegui e verifica verdi al 100%:
  pytest scripts/tests/ -q
  pytest tests/plugins/test_pairlist.py -k MarketCapPairList -q

====================================================================
4. FASE 1 — PREPARAZIONE DATI REALI 2 ANNI (NOV 2024 – NOV 2026)
====================================================================
1. Verifica i dati presenti in `user_data/data/bybit/futures/`. Se mancano coppie o timeframe per coprire `2024-09-01` -> `2026-11-18` (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`, `1h-funding_rate`, `1h-mark`), scaricali da Bybit Futures per l'universo USDT perpetual eleggibile (escludendo TradFi/Innovation/ST già a monte con `instruments-info` per risparmiare spazio e tempo).
2. Scarica gli archivi ufficiali completi (SENZA `--sample`!) delle coin delistate/crollate (`OMUSDT`, `ALPACAUSDT`, `FTMUSDT`, `MKRUSDT`, `UXLINKUSDT`, `NEIROETHUSDT`, `VOXELUSDT`, `ACTUSDT` + eventuali altre top delistate 2024–2026) con `scripts/prop_archive_data.py --start 2024-11-01 --end 2026-11-18` e importa lo storico Market Cap con `scripts/prop_marketcap_archive.py`.
3. Genera due schedule storici point-in-time (zero lookahead, in modalità anti-survivorship senza blacklist retroattiva sulle altcoin crollate):
   - **Schedule Base (`schedule_2y_base.json`):** `Volume >= $20M`, `Age >= 60d`, `Volatility <= 0.50`, `RangeStability <= 0.85`, `MarketCap >= $100M`, no Innovation/ST/TradFi.
   - **Schedule Blindato Anti-Pump (`schedule_2y_strict.json`):** `Volume >= $30M`, `Age >= 90d`, `Volatility <= 0.38`, `RangeStability <= 0.65`, `MarketCap >= $100M`, no Innovation/ST/TradFi.
   - (Opzionale se emerge fragilità sull'autunno 2026: testa anche `Age >= 120d`, `Volatility <= 0.35`, `RangeStability <= 0.55`.)

====================================================================
5. FASE 2 — ESECUZIONE BENCHMARK COMPLETO (14 COMBINAZIONI + STRESS)
====================================================================
Esegui i benchmark a precisione **intrabar `1m`** (`--timeframe-detail 1m`), con `--strict-wick-guard`, commissioni futures reali + funding rate + slippage base **`+10 bps` roundtrip** (e stress **`+28 bps` roundtrip** sui finalisti!):

1. **Grid Sweep completa su `schedule_2y_base.json` e `schedule_2y_strict.json` (`--grid-json`):**
   Le 14 combinazioni in `DEFAULT_MONEY_GRID` testano:
   - **2 Crypto simultanee (`max_open_trades = 2`):**
     * `S2_E275_SLOT400_AGG850` (Prima Entry `$275`, Max crypto `$400`, Max totale `$850`, guard standard)
     * `S2_E275_SLOT475_AGG950` (Prima Entry `$275`, Max crypto `$475`, Max totale `$950`, guard standard)
     * `S2_E300_SLOT550_AGG1100` (Prima Entry `$300`, Max crypto `$550`, Max totale `$1.100`, guard standard)
     * `S2_E350_SLOT600_AGG1200` (Prima Entry `$350`, Max crypto `$600`, Max totale `$1.200`, guard standard)
     * `S2_E350_SLOT600_AGG1200_NEARGUARD` (stessa da `$1.200`, ma stop interno vicinissimo al limite: `-3.8%/-7.5%` su 1-Step, `-4.8%/-9.5%` su 2-Step)
   - **3 Crypto simultanee (`max_open_trades = 3`):**
     * `S3_E200_SLOT350_AGG850`, `S3_E200_SLOT400_AGG950`
     * `S3_E255_SLOT350_AGG950`, `S3_E255_SLOT400_AGG950`, `S3_E255_SLOT400_AGG950_NEARGUARD`
     * `S3_E255_SLOT450_AGG1100`
     * `S3_E275_SLOT500_AGG1200`, `S3_E275_SLOT500_AGG1200_NEARGUARD`
   - **Baseline vecchia per confronto:** `S3_UNLIM_SLOT500_AGG950` (`stake_amount: "unlimited"` senza riserva DCA).

2. **Validazione rigorosa Train / Out-of-Sample:**
   - **Train (Anno 1: `2024-11-15` -> `2025-11-15`)**: selezione dei candidati top.
   - **Out-of-Sample cieco (Anno 2: `2025-11-15` -> `2026-11-15`, incl. crolli Set–Nov 2026)**: validazione senza ritoccare parametri a posteriori.
   - **Full 2 Anni (`2024-11-15` -> `2026-11-15`)**: coorti rolling ogni 7 o 14 giorni sia **One-Step -> Funded -> 1° Payout** sia **Two-Step Fase 1 -> Fase 2 ($100/g cap) -> Funded -> 1° Payout**, più **Daily Crash Sweep** su ogni singolo giorno dal **15 Settembre al 10 Novembre 2026**.

3. **Stress Test Finale (`--suite-json`, `0 bps / 10 bps / 28 bps / strict_wick_guard`)** sulle migliori 1–2 configurazioni vincitrici (una per One-Step e una per Two-Step se diverse), verificando tutti i contatori hard:
   - `official_daily_breach_wick_bars == 0`
   - `official_overall_breach_wick_bars == 0`
   - `funded_margin_gt_25pct_bars == 0` e `funded_notional_gt_2x_bars == 0`
   - `lowcap_combined_gt_250_bars == 0`
   - Resistenza agli shock improvvisi (`-35%`, `-50%` su 1 coin aperta, `-25%` su 2 coin aperte) e tasso `inactivity_30d_breach`.

====================================================================
6. DELIVERABLE FINALE — REPORT COMPLETO IN ITALIANO SEMPLICE
====================================================================
Salva il report in `docs/REPORT_DEFINITIVO_ONE_STEP_VS_TWO_STEP.md` e stampane il riassunto completo in chat, in **italiano semplice, chiaro e comprensibile anche a chi non è programmatore**, includendo:

1. **ESITO AUDIT FASE 0:** cosa hai controllato sulle modifiche recenti, se erano corrette, eventuali bug trovati e corretti prima dei test, e risultati finali di `pytest`.
2. **VERDETTO NETTO: CONVIENE ONE-STEP O TWO-STEP?**
   - Tabella comparativa completa: % superamento challenge, % fallimenti ufficiali (obiettivo `0%`), % stop guardia interna, % inattività 30g, giorni min/mediano/p95/max alla qualificazione, giorni min/mediano/p95/max al **1° PRELIEVO REALE in tasca su Funded**, profitto netto annuo, peggior drawdown su wick `1m`.
3. **I 4 NUMERI DEFINITIVI DEI SOLDI + STOP LOSS + FILTRI CRYPTO (IMPOSTAZIONI FINALI):**
   - Quante crypto aperte insieme (`max_open_trades`), soldi della Prima Entry (`stake_amount`), soldi massimi per singola crypto con DCA (`max_slot_notional_usdt`), soldi massimi totali (`max_aggregate_notional_usdt`), riserva DCA sempre libera quando tutte le crypto sono aperte, soglie stop interno (`standard` vs `near_official`), e filtri crypto (`Base` vs `Strict`).
   - Verifica esplicita di come ha reagito la strategia originale sui DCA (quanti DCA medi/massimi per trade, se restano puliti rispetto all'email HyroTrader) e confronto esplicito con la vecchia configurazione `"unlimited"`.
4. **TABELLA COMPLETA DI TUTTE LE 14 COMBINAZIONI:**
   - Train (Anno 1) vs Out-of-Sample (Anno 2) vs 2 Anni completi vs Crolli Set–Nov 2026 vs Shock `-35% / -50% / 2x -25%` vs Stress `+28 bps`.
5. **CONFRONTO STRATEGIA PRELIEVI SUL CONTO FUNDED:**
   - Senza prelievi vs **Prelievo Reset (`$5.250 -> $5.000`)** vs **Prelievo con Cuscinetto (`$5.450 -> $5.200`)**: numero prelievi incassati in 2 anni, dollari netti (80%) portati a casa, distanza minima dal floor statico su wick `1m`.
6. **RISCHI RESIDUI ONESTI & CHECKLIST PER ANDARE LIVE SU BYBIT DEMO -> CHALLENGE -> FUNDED:**
   - Elenca onestamente i rischi reali residui (es. limiti copertura dati storici, gap improvvisi oltre lo stop, rischio inattività 30g senza keep-alive artificiale) e i passi esatti per applicare la configurazione vincitrice e partire su AWS.
```
