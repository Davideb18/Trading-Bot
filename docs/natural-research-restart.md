# Ripartenza NFIX8 Natural in modalità ricerca

Questa procedura esegue 24 combinazioni (3 universi × 8 candidati), sweep settimanali,
due finestre crash giornaliere e stress test sui candidati selezionati. Non modifica
il bot live. La ripartenza non riutilizza risultati con firma mancante o incompatibile.
Le cache indicatori sono separate per revisione degli input.

## Installare l'aggiornamento

Chiudere prima i vecchi processi di benchmark. Aprire PowerShell nella cartella
del repository `Trading-Bot`, con l'ambiente Python già usato per i backtest.
Salvare `natural-research-update.zip` nella cartella Download dell'utente.
Il pacchetto contiene solo script, test e questa guida: nessuna credenziale o dato di mercato.

```powershell
$ErrorActionPreference = 'Stop'
if (!(Test-Path 'scripts/prop_backtest_1to1.py')) { throw 'Aprire PowerShell nella cartella Trading-Bot' }
$backup = Join-Path $env:TEMP ('natural-before-update-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
Compress-Archive -Path 'scripts/run_natural_benchmark_auto.py','scripts/prop_prepare_benchmark_data.py','scripts/prop_backtest_1to1.py','scripts/tests/test_natural_benchmark_auto.py','scripts/tests/test_prop_prepare_benchmark_data.py' -DestinationPath $backup
Expand-Archive -LiteralPath "$HOME/Downloads/natural-research-update.zip" -DestinationPath . -Force
Write-Host "Backup precedente: $backup"

$python = @('.venv-prop/Scripts/python.exe', '.venv/Scripts/python.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
if (!$python) { throw 'Ambiente Python non trovato: attivare quello del progetto e impostare $python al suo eseguibile' }
$python = (Resolve-Path $python).Path
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

## Controllare l'installazione, senza backtest

```powershell
$testTemp = Join-Path $env:TEMP ('natural-tests-' + [guid]::NewGuid().ToString())
& $python -m pytest scripts/tests/test_natural_research_mode.py scripts/tests/test_natural_audit_regressions.py scripts/tests/test_natural_benchmark_auto.py scripts/tests/test_prop_prepare_benchmark_data.py -q -p no:cacheprovider --basetemp $testTemp
if ($LASTEXITCODE -ne 0) { throw 'Test falliti: fermarsi qui' }
```

## Preparare dati e whitelist, senza backtest

Il comando seguente abilita esplicitamente tre approssimazioni di ricerca:

- metadati e classificazione storica degli strumenti non certificati;
- market cap `hybrid`, che può usare snapshot attuali o osservazioni vecchie;
- mark price/funding sintetici solo dove mancano i relativi file.

Queste opzioni sono registrate nei report. Non rendono il risultato zero-lookahead
né identico al live. Per vietare i dati sintetici, togliere
`--allow-synthetic-market-data` da **tutti** i comandi. Per usare solo le market cap
storiche disponibili, sostituire `hybrid` con `strict`: le lacune possono bloccare
la preparazione. Non impostare manualmente `zero_lookahead=true`.

```powershell
$researchArgs = @('--research', '--mcap-mode', 'hybrid', '--allow-synthetic-market-data')
& $python -u scripts/run_natural_benchmark_auto.py @researchArgs --prepare-only
if ($LASTEXITCODE -ne 0) { throw 'Preparazione/controlli falliti: leggere natural_preflight.json prima di avviare' }
```

La preparazione può scaricare molto storico: parte dal 1 luglio 2024 per il warmup,
ricostruisce le whitelist con l'ordine corretto e verifica i file. Non avvia worker
di backtest. Non viene fornita una durata stimata: dipende da dati già presenti,
disco, rete e limiti delle API.

Per ripetere soltanto i controlli offline, senza download:

```powershell
& $python -u scripts/run_natural_benchmark_auto.py @researchArgs --check-only
if ($LASTEXITCODE -ne 0) { throw 'Controlli falliti: non avviare il benchmark' }
```

I controlli verificano: manifest degli archivi, versione/ordine delle schedule,
ancore giornaliere e whitelist orarie, universo delle coppie, presenza dei sei
timeframe, timestamp ordinati e univoci, valori OHLCV finiti e coerenti, copertura
senza buchi tra prima e ultima inclusione in whitelist, 120 giorni di warmup BTC,
mark price e funding. Il funding è una serie di eventi: si usa una tolleranza
massima di 24 ore, non si inventano eventi orari. File presenti ma corrotti o
parziali restano bloccanti anche quando sono permessi dati sintetici.

Questi controlli non certificano l'origine dei dati né la ricostruzione delle
posizioni dopo un delisting. I limiti storici rimangono dichiarati nel report.

## Avviare o riprendere il benchmark

Nella stessa finestra PowerShell, dopo il successo dei controlli:

```powershell
& $python -u scripts/run_natural_benchmark_auto.py @researchArgs --workers 5
if ($LASTEXITCODE -ne 0) { throw 'Benchmark interrotto: leggere i log dei worker' }
```

Usare `--workers 0` per scegliere automaticamente in base a RAM e CPU. Se cinque
worker esauriscono la RAM, interrompere e ripetere con meno worker. Non eliminare
i checkpoint: lo stesso comando riprende i candidati compatibili già completati.
Non eseguire due orchestratori contemporaneamente sulla stessa cartella.

I report si trovano in `user_data/forensic/strict_validation/`:

- `natural_preflight.json`: controlli, errori e fallback autorizzati;
- `natural_benchmark_report.txt`: risultati leggibili e limiti della ricerca;
- `natural_benchmark_summary.json`: classifica e metadati della ricerca;
- `suite_natural_winner_stress.json` ed eventualmente `suite_natural_winner_2step_stress.json`;
- `_workers/*.log`: log dei singoli candidati.

Il superamento dei vincoli nella simulazione non è una garanzia di sicurezza live.
Verificare anche gli stress test: il vincitore della griglia può fallire sotto stress.

## Linux/macOS

Dalla radice del repository, estrarre lo stesso archivio conservando i percorsi,
attivare l'ambiente Python esistente e usare:

```bash
python -m pytest scripts/tests/test_natural_research_mode.py scripts/tests/test_natural_audit_regressions.py scripts/tests/test_natural_benchmark_auto.py scripts/tests/test_prop_prepare_benchmark_data.py -q -p no:cacheprovider
python -u scripts/run_natural_benchmark_auto.py --research --mcap-mode hybrid --allow-synthetic-market-data --prepare-only
python -u scripts/run_natural_benchmark_auto.py --research --mcap-mode hybrid --allow-synthetic-market-data --check-only
python -u scripts/run_natural_benchmark_auto.py --research --mcap-mode hybrid --allow-synthetic-market-data --workers 5
```

Eseguire ogni riga solo se la precedente termina con codice zero.
