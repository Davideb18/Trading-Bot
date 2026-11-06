# P0.4 — V6 e B2

Intervento limitato ai fault, agli ingressi pendenti e all'isolamento del monitor
di rischio. P0.5–P0.7 non sono implementate. Non certifica il bot per la challenge.

## File e responsabilità

- `freqtrade/prop_controls.py`: interlock nel journal, eventi fault persistenti,
  verifica del supervisore, stato operativo e autorizzazione del riarmo.
- `freqtrade/fault_recovery.py`: cancellazione verificata dei soli ingressi noti.
- `freqtrade/order_journal.py`: tabelle additive `control`, `control_audit`,
  `cancellations`, `fault_events`; prova recente di riconciliazione e account flat.
- `freqtrade/journal_execution.py`: controllo finale dei fault prima di nuovi ingressi.
- `freqtrade/freqtradebot.py`: sincronizzazione e cancellazioni all'avvio e nel ciclo;
  blocco comune a entry, DCA e replacement.
- `freqtrade/rpc/rpc.py`, `freqtrade/rpc/api_server/api_v1.py`: protezione di `/start`,
  pausa amministrativa persistente, endpoint autenticati per stato e riarmo.
- `scripts/prop_http.py`: sessioni per worker e budget HTTP monotono.
- `scripts/llm_risk_manager_prop.py`: client rischio e contabilità separati; il
  macro-worker pubblica richieste, non invia più direttamente stop/start.
- `scripts/prop_risk_guard.py`: fault persistenti, coordinamento, acknowledgement
  verificato e nessun mutex dello stato rischio mantenuto durante I/O o sleep.
- `scripts/tests/test_prop_faults.py`: regressioni P0.4 offline.
- `scripts/tests/test_prop_profile.py`: fixture P0.1 aggiornata con la prova di
  riconciliazione richiesta per confermare la liquidazione; aspettative sui limiti invariate.

## Stati e persistenza

Il journal P0.3 resta il registro del core. Non vengono introdotti nuovi ID ordini
o percorsi alternativi di invio. L'envelope del supervisore rimane versione 1,
`risk_schema_version=2`; `control_schema_version=1` aggiunge la semantica operativa.

| Stato | Regola |
|---|---|
| `macro_paused` | Pausa del macro-monitor; può cessare dopo normalizzazione macro. |
| `data_fault` | Latch persistente; dati nuovamente validi non lo cancellano. |
| `reconciliation_required` | Derivato da prove P0.3, marker P0.2, orfani e cancellazioni pendenti; si risolve solo con riconciliazione. |
| `risk_halt` | Latch nel journal, coerente con `nuclear_triggered`; non azzerabile dal riarmo ordinario. |
| `manual_lock` | Pausa/stop amministrativo; richiede acknowledgement esplicito. |

I worker segnalano immediatamente il fault in memoria e appendono un evento nel
journal con connessione SQLite propria. Possono scrivere esclusivamente eventi:
non modificano intenti o risultati degli ordini. Il binding verifica profilo,
account, challenge e fase. SQLite usa `synchronous=FULL`, con busy timeout di
50 ms per questa pubblicazione. Il core importa l'evento nel proprio latch anche
se il supervisore muore prima di aggiornare il suo JSON.

Il solo worker rischio scrive il JSON del supervisore. Copia lo stato sotto mutex,
poi rilascia il mutex prima di scrittura, fsync e rename. Il ritorno dell'equity
valida aggiorna `data_valid`, non cancella `data_fault`. Revisioni/epoch impediscono
a un acknowledgement precedente di eliminare un fault successivo. File mancanti,
obsoleti, incongruenti o non verificabili bloccano nuovi ingressi nel core.

Un errore dello storage non permette di promettere persistenza: resta il latch in
memoria, viene tentata la segnalazione RPC e scade l'heartbeat. La durata fino alla
reazione effettiva dipende anche dal fatto che il processo core possa eseguire.

## Ordini pendenti e posizioni

Il ciclo del core registra la richiesta di cancellazione nel journal e conserva
il marker P0.2 prima della chiamata exchange. Valida ID, simbolo, lato, quantità
e `reduceOnly=False`. Per gli intenti senza record locale verifica anche il client ID.
Ordini esterni senza origine verificata non vengono cancellati.

La risposta di cancel non certifica il risultato: segue una lettura dell'ordine.
Timeout, risposta incompleta o stato ancora aperto mantengono la cancellazione
incerta. Un timeout può risolversi solo se la lettura successiva prova lo stato
terminale. I gestori P0.2 aggiornano fill parziali/completi e database; segue la
riconciliazione P0.3. Il record della cancellazione sopravvive al restart.

Ordini di uscita, protettivi o reduce-only non vengono cancellati indiscriminatamente.
Le uscite restano disponibili, salvo i vincoli P0.2/P0.3 sulla coppia incerta. Una
lista Freqtrade vuota non prova che l'account sia flat: il monitor richiede anche
una prova recente P0.3 senza posizioni e ordini aperti.

## Pausa e percorso di riarmo

Gli endpoint sono nel router privato autenticato già usato da Freqtrade:

1. `GET /api/v1/prop/control`: mostra blocchi, epoch, pending entries e prova corrente.
2. Dopo verifica dell'account, inviare `POST /api/v1/prop/control` con, per esempio:

   ```json
   {"action":"acknowledge","reason":"Verificati account e ordini; causa del fault risolta","flags":["data_fault","manual_lock"]}
   ```

3. Il core accetta la richiesta solo con dati del supervisore validi, riconciliazione
   recente, nessun ingresso pendente e nessun risk halt. Persiste motivo, autorità RPC,
   ID, epoch e riferimenti agli eventi. Non sblocca immediatamente.
4. Il supervisore verifica nuovamente dati e prova del core e pubblica l'ID accettato.
5. Il core richiede ancora una prova corrente, epoch invariato e acknowledgement
   non più vecchio di 60 secondi. Solo allora libera i flag richiesti. Gli eventi
   restano conservati come riconosciuti, insieme all'audit.
6. Verificare lo stato e usare `/start`. Il riarmo manuale non invia automaticamente start.

`action=lock` con un motivo applica un blocco amministrativo. `action=fault` serve
alla segnalazione conservativa del supervisore. L'autorità è quella delle credenziali
API Freqtrade esistenti, non un nuovo ruolo RBAC; il motivo è audit, non una firma.

`/stop`, `/pause` e `/stopbuy` ordinari applicano `manual_lock` nel profilo Prop.
Il supervisore usa `/api/v1/prop/pause`, che non trasforma una pausa macro in una
pausa amministrativa. `/start` non cancella alcun interlock, anche se il bot risulta
già running. Il macro-monitor può riavviare solo in assenza degli altri blocchi.
`risk_halt`, stato P0.1 corrotto e anomalie esterne P0.3 non vengono cancellati da
questo endpoint: richiedono la rispettiva procedura di riconciliazione amministrativa.

## Isolamento e tempi effettivi

Nessun thread aggiuntivo. Il worker rischio possiede il proprio client/sessione/token;
il worker contabilità ne possiede un altro. La condivisione dello stesso client tra
thread è rifiutata. Macro e LLM non accedono al client critico. Le richieste macro
sono coordinate dal worker rischio; i fault dei worker hanno anche trasporto persistente.

- Budget monotono del ciclo critico HTTP: 4 secondi complessivi, inclusi login,
  refresh, richieste, retry, backoff e verifica dei comandi.
- Connect timeout: massimo 1 secondo; read timeout: massimo 1,5 secondi, ridotti
  al tempo residuo. Risposte limitate a 2 MiB; risultati oltre deadline scartati.
- GET: massimo tre tentativi, backoff 0,1 e 0,2 secondi dentro lo stesso budget.
- POST con esito ambiguo: nessun reinvio automatico. Un 401 può richiedere un refresh
  e un nuovo tentativo, sempre entro la deadline originale.
- Attesa fra cicli: 5 secondi nominali **dopo** il lavoro. Non è un periodo reale
  garantito di 5 secondi; con 4 secondi di lavoro diventa circa 9 secondi, oltre I/O
  locale e scheduling. Heartbeat e prove exchange hanno finestra massima di 15 secondi.

Le deadline sono cooperative: requests non interrompe forzatamente DNS, stalli OS
o un peer che trasmette lentamente. Il controllo del rischio non attende lock HTTP
dei worker secondari; rimangono i limiti del trasporto e del filesystem. Il lock di
esecuzione del core serializza le cancellazioni con force-exit; non è il mutex
dello stato rischio. I timeout dell'adapter exchange esistente non sono modificati.

## Verifica e limiti

Test offline con API/exchange mock, SQLite reale, thread controllati e metodi reali
estratti dall'AST. Sono coperti fault/restart/riarmo, dati obsoleti, cancellazioni e
fill concorrenti, reduce-only, blocco `/start`, conflitti macro/halt/manuale,
worker bloccati, refresh lento/fallito, retry, deadline e conservazione del journal.
Ripetute anche P0.1–P0.3; nessun backtest, paper trading o collegamento exchange.

La suite completa ORM/FastAPI/CCXT target non è stata eseguita: mancano `sqlalchemy`,
`schedule` e `xdist`; CCXT locale è 4.5.63 contro 4.5.68 richiesto. I test delle
route HTTP reali, autenticazione end-to-end, Docker e latenza live restano da fare
nell'ambiente completo. Nessuna dipendenza è stata installata.

P0.5 dovrà affrontare la dipendenza residua dal core Freqtrade/API per osservazioni
e azioni exchange: se il core non esegue, questa patch non garantisce cancellazioni
o liquidazione entro una deadline. Deve conservare interlock, ID, marker e prova
exchange; non deve aprire un secondo percorso di execution incontrollato.

Migrazione additiva: nessuna tabella P0.3 viene cancellata. Stato legacy con fault
rimane bloccato; il primo avvio può richiedere acknowledgement. Non fare rollback
a versioni che ignorano gli interlock mentre esistono fault o cancellazioni pendenti.

## Trasferimento futuro a Two-Step

Trasferibili journal, fault recovery, interlock, client HTTP, coordinamento e hook
RPC/core. Nessun limite One-Step è aggiunto al core generico. Il validatore profilo
usato resta quello P0.1: occorrerà adattarlo alle due fasi con identità e parametri
separati. Ripetere P0.1–P0.4, cambio fase, riarmo con epoch/eventi, migrazione e
integrazione offline ORM/API con CCXT target. Nessuna modifica al branch Two-Step.
