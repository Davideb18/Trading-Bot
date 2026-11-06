# P0.5 — Supervisore indipendente e autorità di emergenza

Scope: V2 soltanto, branch `bot/prop-one-step`, dopo i checkpoint P0.1–P0.4.
Strategia, formule, baseline UTC, leva e limiti HyroTrader restano invariati.
Questa implementazione non certifica il bot per la challenge.

## File

- `freqtrade/supervisor_bybit.py`: adapter CCXT indipendente, identità, equity,
  ordini, esecuzioni, posizioni, precisione e sole riduzioni di emergenza.
- `freqtrade/emergency_supervisor.py`: macchina di emergenza persistente, lease
  del supervisore, prova di origine, cancellazioni, liquidazione e conferma flat.
- `freqtrade/emergency_authority.py`: controllo dell'autorità e arresto cooperativo
  del core mediante `EmergencyTakeover`, senza dipendenze ORM.
- `freqtrade/order_journal.py`: tabelle additive `emergency` e `emergency_orders`,
  intenti market reduce-only autorizzati dall'owner esclusivo.
- `freqtrade/journal_execution.py`: blocco startup e invio quando esiste handoff.
- `freqtrade/fault_recovery.py`: rispetto dell'autorità prima delle cancellazioni.
- `freqtrade/freqtradebot.py`: uscita dal worker anche se STOPPED; cleanup di
  emergenza senza cancel-all né rilascio esplicito anticipato del journal.
- `freqtrade/rpc/rpc.py`: force-exit rispetta la stessa autorità prima di agire.
- `scripts/prop_independent.py`: wiring del client diretto e delle credenziali esplicite.
- `scripts/prop_risk_guard.py`: letture tramite facade e procedura indipendente di
  emergenza, mantenendo il percorso precedente per i test di regressione.
- `scripts/llm_risk_manager_prop.py`: attiva il percorso indipendente in produzione,
  mantenendo separati macro, contabilità e LLM.
- `docker/Dockerfile.riskmanager-prop`, `docker-compose.prop.yml`: ricetta specifica
  del supervisore con CCXT e nuove variabili; nessun build o installazione eseguito.
- `scripts/tests/test_prop_emergency.py`: test offline P0.5.

## Configurazione e identità

Prima dell'avvio, sono necessari un journal P0.3 esistente e coerente, profilo P0.1
valido e stato/baseline verificati. Non viene inizializzata automaticamente una
nuova storia ordini quando il journal manca. Il database dei trade può invece
essere completamente indisponibile: il suo URL nel binding è solo un riferimento.

Il supervisore legge `PROP_BYBIT_API_KEY` e `PROP_BYBIT_API_SECRET` dall'ambiente.
Il fingerprint della chiave deve concordare col binding P0.3: una seconda chiave,
anche sul medesimo UID, richiede una migrazione esplicita del binding. Nessuna
credenziale operativa è usata nei test o registrata nei log.

`account_id` del profilo deve essere il **UID Bybit effettivo**, confrontato con
`userID` di V5 query-api. Un'etichetta locale non basta. Sono verificati URL HTTPS
effettivi dopo l'espansione CCXT, ambiente demo/live e modalità account. Per il
profilo One-Step attuale continua a valere il vincolo P0.1 sull'ambiente demo.
Le credenziali uguali, da sole, non costituiscono prova d'identità.

Sono riconosciuti i codici Classic/UTA1/UTA2 previsti dall'adapter, wallet
CONTRACT/UNIFIED, isolamento e modalità one-way. Dati mancanti, UID diverso,
cross/portfolio o hedge mode fanno fallire la verifica. Non vengono cambiati
margin mode, position mode o leva per forzare la compatibilità.

Per autorizzare la presa di controllo deve essere configurato esplicitamente:

```text
PROP_EMERGENCY_AUTHORITY=dedicated-account-single-host
```

Questa è un'attestazione operativa, non una prova distribuita: tutti i writer devono
usare il volume condiviso e il protocollo aggiornato, senza altri bot/host che
operino sul conto. In assenza dell'attestazione l'equity viene comunque monitorata,
ma il supervisore non prende autorità di invio. Non viene chiesto né simulato un
consenso implicito dall'esistenza del lock locale.

## Letture indipendenti

Equity: V5 wallet-balance, campo coin equity USDT, comprensivo dell'unrealized PnL
una sola volta. Nessun fallback a `/balance`, saldo disponibile o capitale iniziale.
Collateral/debito/equity non-USDT non nulli impediscono la certificazione dello scope.
Le formule restano quelle P0.1: daily basato sull'equity di mezzanotte e sul capitale
iniziale; overall statico. Dato assente/obsoleto produce fault, non una violazione
numerica inventata.

Le letture paginano ordini e posizioni reali; i client ID vengono verificati in
realtime e storico, con somma delle esecuzioni. Contratti convertiti in quantità
base tramite contract size. Cursori mancanti/ciclici, pagine incomplete, dati
discordanti o timestamp oltre 15 secondi sono incertezza; tolleranza futura massima
2 secondi. La retention limitata non diventa prova di assenza di un ordine.

Prima di agire o confermare flat vengono controllate anche categorie fuori scope
(spot, inverse, option e linear USDC). Un endpoint non supportato o non autorizzato
non equivale a una lista vuota: l'emergenza resta irrisolta. Questo può impedire
l'intervento su account per i quali non è disponibile una prova completa.

## Autorità e fasi

1. **Persistenza:** solo un halt di sicurezza già persistito, con profilo e motivo
   coerenti, autorizza la procedura. Il journal registra halt ID, richiesta di
   handoff e riconciliazione DB obbligatoria; conserva gli altri marker P0.2/P0.4.
2. **Isolamento:** il core rileva la richiesta ai confini operativi e deve uscire,
   non restare semplicemente STOPPED. Il cleanup non cancella le protezioni e non
   chiude esplicitamente il journal durante l'handoff. I nuovi processi rifiutano
   l'avvio prima di inizializzare l'exchange.
3. **Esclusività:** il supervisore deve acquisire lo stesso lock OS `orders.lock`
   del journal P0.3. Non ruba lock, non usa scadenze PID/lease e non termina processi
   altrui. Un secondo supervisore è escluso dal lock lifetime `supervisor.lock`.
4. **Riconciliazione:** legge direttamente Bybit. Cancella soltanto entry con origine
   provata dal journal. La richiesta di cancellazione viene persistita prima della
   chiamata; segue lookup terminale con controllo dei fill. Un esito dubbio resta
   pendente. Ordini reduce-only/protettivi sono conservati.
5. **Liquidazione:** nuove letture determinano residui effettivi. L'esposizione deve
   essere spiegata dai fill netti del journal: una posizione assente dal DB ma
   spiegata dal journal è gestibile; una posizione senza origine verificabile no.
   Il nuovo intento è market reduce-only, con lato opposto e positionIdx 0; quantità
   arrotondata per difetto, contract size, minimo e massimo ordine verificati.
6. **Conferma:** due snapshot diretti coerenti, senza posizioni, entry pendenti o
   intenti sconosciuti. Gli ordini di emergenza devono essere riconciliati. Restano
   eventualmente ordini protettivi reduce-only, che non possono aprire esposizione.

Una risposta HTTP di successo non certifica né fill né flat. Journal, intenti e
controlli sono gli stessi P0.3; l'autorità di emergenza sostituisce la prova ORM
indisponibile con prove dirette, senza concedere ingressi ordinari al supervisore.

Il record `emergency` conserva fase, motivo, owner, conferma, timestamp, prova flat
e `db_reconciliation_required=true`; le transizioni sono registrate anche in
`control_audit` e rese visibili nei log senza risposte API o segreti.

## Retry, crash e riavvio

Ogni chiusura usa `ftj<uuid32>` persistito prima del dispatch, tradotto da CCXT in
`orderLinkId`. Stato `sending` precede la chiamata. Timeout/risposta persa conserva
lo stesso intento incerto: niente nuovo ordine finché una lettura attendibile non
ne stabilisce l'esito. Un ordine parziale ancora aperto impedisce un altro invio.
Un terminale parziale può richiedere un nuovo intento, solo dopo nuova verifica
del residuo e dell'origine della posizione.

I rifiuti devono essere confermati da evidenza di lettura. Un generico errore
InvalidOrder o un singolo not-found non libera l'intento. Un crash con intento
prepared/sending senza evidenza remota rimane bloccante: non viene dedotta la
mancata esecuzione. Dopo un crash con ordine ritrovato, il recovery continua senza
duplicare l'invio. Il supervisore non ricostruisce trade NFIX8 inventando metadati.

La conferma flat è indipendente dal DB locale e non autorizza la ripresa. Handoff,
risk halt e vincolo di riconciliazione DB rimangono persistenti. P0.5 non aggiunge
un comando di reset dell'halt o di adozione automatica delle posizioni. Un ritorno
all'operatività richiede riconciliazione amministrativa di exchange, journal,
trade DB, identità/fase e stato P0.1–P0.4.

## Tempi, deployment e limiti

Budget complessivo cooperativo: 4 secondi, condiviso con il ciclo rischio P0.4.
Timeout CCXT per richiesta: massimo 2 secondi, ridotto al residuo; retry automatici
del client disabilitati. Il backoff del controllo HTTP resta separato e dentro il
budget originale. DNS, throttling CCXT, filesystem e stalli OS non sono preemptibili:
non esiste garanzia di intervento o liquidazione entro 5 secondi.

Snapshot completi, paginazione, storico ed esecuzioni possono superare il budget;
in questo caso resta il blocco, non una conferma parziale. Tempi e disponibilità
degli endpoint richiedono verifica nell'ambiente target. Non è garantita la
liquidazione di dust sotto precisione/minimi o di posizioni non attribuibili.

Se Freqtrade è offline o il DB trade è assente, l'emergenza può completarsi usando
solo Bybit e il journal. Se un processo Freqtrade bloccato mantiene `orders.lock`,
il supervisore attende: non può dimostrare un'esclusività sicura e non invia ordini.
Lock locali non coordinano altri host, volumi separati o client estranei.

Aggiornare core e supervisore insieme. La migrazione SQLite è additiva; non vengono
cancellati dati precedenti. Non effettuare rollback a un core che ignora l'handoff
mentre l'emergenza è attiva. Conservare journal, stato e trade DB nei backup coerenti.
La nuova ricetta Docker dichiara CCXT 4.5.68; non è stata costruita né eseguita.

## Verifica offline e lavoro successivo

La suite P0.5 usa credenziali fittizie, socket bloccati, CCXT locale senza rete,
exchange deterministico, SQLite e lock OS reali. Comprende equity senza Freqtrade,
DB assente, halt/restart, fill durante cancel, reduce-only, rifiuti, timeout,
parziali, crash, due supervisori/processi, lifecycle del worker/cleanup, identità,
ambiente, scope account, timestamp, precisione e doppia conferma flat.

Ripetute P0.1–P0.4. Mancano `sqlalchemy`, `schedule` e `xdist`: non è stata eseguita
la suite completa del runtime Freqtrade. CCXT locale è 4.5.63, contro 4.5.68 target.
Non sono verificati su Bybit reale UID/query-api, disponibilità/permessi degli
endpoint per ogni modalità account, consistenza temporale, matching e shutdown
Docker end-to-end. Nessuna installazione, rete, backtest o paper trading.

P0.6 e P0.7 restano separati e non implementati. Ogni futuro percorso di invio,
recovery o riarmo dovrà rispettare l'owner esclusivo, gli ID persistenti, i marker
esistenti e la distinzione tra account flat e DB riconciliato.

## Riuso Two-Step

Trasferibili adapter, macchina di emergenza, journal, fencing del core e wiring.
Nessuna percentuale One-Step è inserita nel core generico. Restano da adattare
validatore del profilo, ambiente supportato, identità delle due fasi, credenziali
e binding persistenti. Ripetere P0.1–P0.5, cambio fase e migrazioni, oltre alle
prove ORM/API/Docker e CCXT target. Branch Two-Step non modificato; nessun merge,
cherry-pick, commit o push effettuato.
