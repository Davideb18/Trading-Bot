# P0.1: configurazione e riconciliazione One-Step

Questa patch non rende il bot pronto per una challenge. Non modifica la strategia.

## Contratto di configurazione

`user_data/config_prop_nfix8.json` è la configurazione attesa dal supervisore.
Freqtrade espone su `/show_config` il campo non segreto `prop_profile`, costruito
dalla propria configurazione risolta. Il supervisore confronta i due contratti
prima di aggiornare l'autorizzazione del capitale. Override Freqtrade divergenti,
contratto assente e variabili legacy discordanti bloccano l'autorizzazione.

Compilare esplicitamente `account_id`, `challenge_id`, `challenge_start_utc` e
`phase_start_utc`. I valori null distribuiti sono intenzionalmente bloccanti.
Usare identificativi non segreti; non inserire API key. Account ID è un'identità
dichiarata dall'operatore, non una verifica remota della proprietà delle credenziali.
Per One-Step la fase è `evaluation` e le due date di inizio coincidono.
Il profilo supporta esclusivamente Bybit demo, futures isolated USDT.

Il capitale rimane `prop_initial_balance`, alla radice della configurazione.
Le percentuali ufficiali sono 4% daily e 6% overall; quelle operative rimangono
3,6% e 5,2%. Entrambi i budget monetari usano il capitale iniziale:

```
official_daily_floor = midnight_equity - initial_balance * 0.04
official_overall_floor = initial_balance * 0.94
operating_daily_floor = midnight_equity - initial_balance * 0.036
operating_overall_floor = initial_balance * 0.948
```

Le uguaglianze alle soglie operative attivano l'halt. I profitti intraday non
alzano i floor. Target, cap e giorni minimi provengono dallo stesso contratto;
la logica di contabilizzazione del monitor non è stata corretta in P0.1.

## Stato e baseline

### Patch 4: rollover UTC senza baseline attendibile

Il giorno valutato segue la ricezione UTC dell'equity, non l'avvio del ciclo.
Lo stato conserva separatamente `cycle_started_at`, `received_at` e, quando
disponibile, `response_at` del server Bybit. Quest'ultimo è il timestamp della
risposta: non certifica l'istante esatto di misurazione dell'equity. Se risposta
e ricezione appartengono a giorni diversi, il campione non abilita il controllo
daily del nuovo giorno. Nessun campione viene retrodatato.

Una baseline accettata deve soddisfare `validate_baseline`: oltre ai campi
descritti sotto, richiede `status: verified` ed evidenza revisionata di tipo
`historical_account_equity`, con account, valore, istante e riferimento coerenti.
Il riferimento è un confine di fiducia esterno; compilare questi campi non
autentica automaticamente una fonte o un valore manuale.

Senza baseline valida rimangono nulli i floor daily. Lo stato dichiara
`daily_monitoring_available: false`, `overnight_policy_required: true` e
`monitoring_status: overall_only` soltanto se l'equity permette il controllo
overall; altrimenti il monitoraggio è `unavailable`. Il fault resta persistente
e blocca ingressi, DCA e replacement. Il ritorno della baseline non cancella il
fault né riavvia automaticamente il trading.

Dopo il controllo overall, il supervisore tenta una fotografia indipendente
dell'account entro la deadline già esistente. `overnight_exposure.status` distingue
`present` (posizioni o ordini aperti), `flat_observed` e `unknown`. Una fotografia
flat non prova la baseline storica né garantisce che l'account resti flat.
La query non precede un'emergenza overall già rilevata e non usa fallback RPC.
Il gap daily è segnalato nei log, nello stato e nei fault del journal. Con stato
corrotto si preserva il file originale e si usa il journal per la segnalazione
persistente; con storage indisponibile restano soltanto log, latch in memoria e
blocco per heartbeat, senza promessa di durabilità.

Questa patch **non rende sicuro mantenere esposizione overnight** senza baseline
attendibile. Non introduce liquidazioni per il solo gap daily o per una stima.
Prima del deployment occorre approvare una fonte affidabile della baseline e/o
una politica esplicita per esposizione e ordini overnight (per esempio riduzione
preventiva con verifica flat). Le eventuali chiusure software restano soggette a
disponibilità exchange, journal e handoff esclusivo: non garantiscono una perdita
massima. Riduzioni ed emergenza esistenti restano invariate.

Il file `risk_manager_prop_state.json` mantiene l'envelope `schema_version: 1`
per il lettore heartbeat della strategia invariata. Il wallet richiede anche
`risk_schema_version: 2`, contratto corrispondente, baseline del giorno UTC e
heartbeat fresco. Vecchi supervisori non possono autorizzare il nuovo wallet.

Il guard non inizializza automaticamente uno stato assente, né converte uno
stato legacy/corrotto. Non sovrascrive questi file. Prima di qualsiasi migrazione:

1. Fermare i processi operativi e conservare copie di configurazione, stato e DB.
2. Riconciliare account, challenge, fase, ordini e posizioni. Verificare anche
   l'eventuale halt precedente; uno stato mancante non dimostra che fosse assente.
3. Recuperare evidenza dell'equity delle esatte 00:00 UTC. Un campione vicino
   alla mezzanotte o il saldo corrente non sono sostituti validi.
4. Costruire un record baseline con i campi seguenti:
   - `profile`: contratto completo restituito da `profile_from_config`;
   - `equity`: equity verificata delle 00:00 UTC;
   - `captured_at`: timestamp timezone-aware delle esatte 00:00 UTC;
   - `verified_at`: timestamp della riconciliazione, non futuro;
   - `source`: riferimento all'evidenza verificata;
   - `confirmed`: `true`.
5. La funzione pura `reconciled_state(profile, baseline, now, halted=...)` in
   `scripts/prop_risk_guard.py` costruisce il nuovo record senza scrivere file
   o contattare servizi. Il parametro `halted` è obbligatorio: conservare `true`
   quando l'halt precedente non è stato esplicitamente risolto. Installare il
   record soltanto dopo la riconciliazione, senza sovrascrivere la copia storica.

Nessuna di queste operazioni è stata effettuata dalla patch. La verifica del
campo `source` è documentale/operativa: il codice non autentica l'evidenza esterna.

Il baseline del giorno corrente è persistito nello stato. Per un nuovo giorno,
installare atomicamente `risk_manager_prop_baseline.json` nella stessa directory,
con il nuovo record verificato. Il guard lo legge al cambio giornata; il wallet
rifiuta immediatamente la baseline del giorno precedente, anche con heartbeat
ancora fresco. In assenza di baseline valida, blocca nuovi rischi e mantiene il
controllo overall quando identità e stato sono validi. Non stima il daily floor.
La normale gestione delle uscite resta attiva. Una nuova baseline non cancella
un halt nucleare. P0.1 non introduce un acquisitore automatico della baseline.

## Compatibilità e rollback

Distribuire insieme core Freqtrade, supervisore e configurazione. Il bind mount
del package `freqtrade` nel supervisore serve a condividere il validatore puro,
senza importare adapter exchange. Il percorso SQLite non viene rinominato e
non vengono cancellati dati; il rifiuto dell'identità divergente impedisce il
riuso silenzioso dello stato. La segregazione fisica del DB e la riconciliazione
account-wide restano attività successive.

Il rollback richiede processi fermi e le copie della versione precedente.
Non basta ripristinare il vecchio JSON di stato e riprendere il trading: può
contenere riferimenti ormai obsoleti e il vecchio codice conserva i bug noti.
Una combinazione di nuovo wallet e vecchio stato resta bloccata intenzionalmente.

## Verifica offline

```
python -B -m unittest discover -s scripts/tests -p test_prop_profile.py -v
```

I test usano esclusivamente la libreria standard, file temporanei e un client
simulato; bloccano le connessioni socket e non importano la strategia o gli adapter.
Le precedenti prove del guard trailing sono sostituite da prove dei floor statici,
senza rimuovere i test separati della strategia e degli altri componenti.
