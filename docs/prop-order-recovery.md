# P0.2: force-exit e recovery degli ordini conosciuti

Queste correzioni sono indipendenti da exchange, percentuali HyroTrader e profilo
One-Step/Two-Step. Non modificano strategia, sizing o configurazione di rischio.

## Force-exit

Il percorso RPC distingue la cancellazione dell'ordine dall'esistenza della
posizione. Dopo un DCA cancellato riconcilia i fill e chiude la quantità residua;
se il primo ingresso non eseguito viene cancellato e il trade viene eliminato,
non invia una vendita. Il booleano di `handle_cancel_enter` rimane invariato.

Per questa operazione la cancellazione usa `cancel_order` seguito da lookup:
non usa il fallback di `cancel_order_with_result` che può sintetizzare un ordine
cancellato. Risposte incomplete, stati sconosciuti, fill decrescenti e lookup
fallite non sono conferme. Anche dopo un timeout di cancellazione una lookup
successiva può confermare lo stato terminale e permettere la chiusura.

Un force-exit già aperto viene monitorato senza cancellarlo e ricrearlo a ogni
retry. Una risposta RPC positiva significa ordine gestito/in corso, non conto
liquidato. La richiesta `all` segnala gli eventuali trade irrisolti invece di
riportare sempre successo.

## Recovery e blocco persistente

All'avvio tutti gli ordini locali aperti vengono marcati prima delle lookup.
L'età superiore a cinque giorni è soltanto diagnostica: non autorizza una
cancellazione presunta. I riferimenti locali rimangono disponibili finché manca
una risposta coerente dell'exchange.

Il marker `core_order_recovery_pending`, nei custom data persistenti del trade,
contiene gli ID ancora da riconciliare. Una lookup valida e l'aggiornamento dello
stato locale rimuovono soltanto l'ID risolto. Il normale monitor degli ordini può
riprovare la lettura; nessun nuovo ordine viene inviato per risolvere il dubbio.

Finché esistono marker o ordini orfani, il gate in `execute_entry` blocca nuove
aperture, DCA e ingressi sostitutivi nella stessa istanza/database. `/start` non
azzera i marker. Il replacement di un trade irrisolto viene rinviato senza
eliminarne i dati. Le uscite di trade con quantità irrisolte attendono la
riconciliazione; i trade non coinvolti continuano a essere gestiti.

Se l'invio RPC di un force-exit solleva un errore exchange, viene mantenuto il
marker `force_exit_submission_unknown`. Non avendo un ID affidabile, P0.2 non
effettua reinvii automatici. Il marker sopravvive al riavvio e blocca altre uscite
software sul trade e nuovi ingressi. Non viene cancellato da lookup di altri ID.
Può richiedere intervento manuale anche per errori che, in seguito, risultino
essere rifiuti certi: la politica privilegia l'assenza di duplicazioni.

## Limiti e dipendenze

- Non è implementato un journal pre-invio, né un `clientOrderId` persistente.
  Un crash prima della persistenza del marker e ordini completamente sconosciuti
  al database restano problemi P0.3.
- I marker coordinano la singola base dati; non garantiscono esclusività tra
  istanze indipendenti sullo stesso account.
- Non viene aggiunta una ricerca account-wide negli ordini storici o nelle
  posizioni. Se la lookup per ID resta inconcludente, il blocco resta attivo.
- La gestione ordinaria della cancellazione mantiene i suoi contratti pregressi;
  il fallback sintetico dell'adapter non è stato rimosso globalmente. Il percorso
  RPC force-exit e il recovery iniziale non lo usano per presumere una cancellazione.
- Una riconciliazione manuale deve verificare ordini aperti, fill, posizioni e DB
  prima di rimuovere marker. Non basta premere `/start` o cancellare custom data.
- Un exchange indisponibile può impedire le chiusure. P0.2 non implementa il
  supervisore indipendente o le altre patch successive.

## Verifica offline e trasferibilità

```
python -B -m unittest discover -s scripts/tests -p test_order_recovery.py -v
python -B -m unittest discover -s scripts/tests -p test_prop_profile.py -v
```

La suite P0.2 esegue i metodi effettivi del core estratti tramite AST, evitando
gli import delle dipendenze runtime non disponibili. Exchange, notifiche e
modello di trade/ordine sono test double; i custom data del test sono conservati
in SQLite in memoria. La suite non sostituisce i test completi con SQLAlchemy.
Le connessioni socket sono bloccate durante i test.

Nel Two-Step si possono trasferire `freqtrade/order_recovery.py`, le modifiche
mirate a `freqtradebot.py` e `rpc.py`, e i relativi test. Non trasferire insieme
le configurazioni o i floor One-Step. Occorre rieseguire le prove RPC, recovery,
cancellazione e replacement con le dipendenze del branch destinatario, oltre
alle regressioni del suo profilo. Nessun trasferimento è stato effettuato qui.
