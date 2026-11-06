# P0.3 — Journal ordini e riconciliazione

Implementazione limitata a V4, successiva a P0.1 e P0.2. Non certifica il bot per una
challenge. Strategia, segnali, dimensionamento e parametri di rischio restano invariati.

## Persistenza e identità

Con `prop_guard_enabled`, il bot apre `user_data/order_journal/orders.sqlite` prima
dell'inizializzazione exchange e dell'avvio RPC. Il percorso usa il `user_data_dir`
risolto da Freqtrade. Il volume `user_data` già presente nel compose Prop conserva
anche il journal; non occorrono nuovi servizi o dipendenze.

SQLite usa transazioni e `synchronous=FULL`. Ogni intento conserva richiesta,
client ID, timestamp, trade ID se disponibile, ID exchange, stato, evidenza e flag
di collegamento/riconciliazione. `findings` conserva anomalie e `checks` l'ultimo
esito. Il binding schema 1 comprende identità P0.1, exchange/ambiente/valuta,
fingerprint SHA-256 della chiave API e URL del database Freqtrade. Non salva segreti.
Identità diversa, credenziali CCXT discordanti o journal corrotto impediscono l'avvio.
Rotazione chiave, spostamento DB o cambio fase richiedono una migrazione verificata:
non cancellare il journal per aggirare il controllo.

Il fingerprint rileva scambi accidentali delle credenziali, ma non certifica il UID
remoto né la proprietà dell'account. Resta necessario l'account dedicato previsto
da P0.1. Non viene adottato automaticamente alcun ordine esterno.

## Invio e crash

L'unico percorso d'invio resta `exchange.create_order()`. Il wrapper comune a entry,
DCA, replacement e uscite registra prima l'intento, poi lo stato `sending`, entrambi
con commit. Passa a CCXT `clientOrderId=ftj<uuid32>` (35 caratteri); l'adapter Bybit
CCXT lo traduce in `orderLinkId`. La richiesta di esecuzione non viene ritentata
automaticamente dal journal. Un secondo dispatch dello stesso ID viene respinto.

| Punto di arresto | Comportamento al riavvio |
|---|---|
| Prima della persistenza | Nessuna chiamata di creazione ordine è stata effettuata. |
| Intento `prepared`, prima dell'invio | Prenotazione conservata; nessun reinvio automatico. |
| Stato `sending`, durante invio o prima del salvataggio risposta | Ricerca tramite lo stesso client ID; assenza non significa rifiuto. |
| Risposta persa o non verificabile | Stato `uncertain`; ingressi bloccati. |
| Risposta accettata, prima del commit Freqtrade | Ordine classificato dal journal; senza record locale resta bloccante. |
| Commit Freqtrade completato, prima del binding | Il recovery riconosce ID, coppia, lato, quantità e trade persistito e ripara il riferimento. |

Gli stati distinguono `prepared`, `sending`, `accepted`, `uncertain`,
`partially_filled`, `filled`, `canceled`/`cancelled`, `expired`, `rejected`.
Un errore o una risposta di creazione apparentemente negativa non libera la
prenotazione. Un rifiuto/cancellazione senza fill è risolto soltanto con evidenza
di lettura coerente ed effettive quantità riconciliate.

Il journal e il DB ORM non hanno una transazione distribuita. Il commit del trade
precede il binding; il recovery copre l'intervallo. Non vengono fabbricati trade
NFIX8 dopo un crash: mancherebbero metadati strategici affidabili. Ordini accettati
senza record locale richiedono intervento verificato, non un nuovo ingresso.

## Riconciliazione e blocchi

Parte all'avvio, all'inizio del ciclo e immediatamente prima di un invio. L'adapter
Bybit usa endpoint V5 attraverso il client CCXT esistente, esclusivamente in lettura:

- tutti gli ordini aperti del conto linear USDT;
- realtime e storico per ogni client ID non risolto;
- esecuzioni per ID exchange, con controllo origine e somma dei fill;
- posizioni linear USDT in modalità one-way, convertite in quantità base.

La paginazione segue tutti i cursori e fallisce se incompleta, ciclica, malformata
o oltre 1.000 pagine. Disaccordo realtime/storico, fill regressivi, quantità diverse,
evidenze mancanti o identità discordanti mantengono il blocco. Lo storico non è
illimitato: un intento fuori dalla retention rimane irrisolto, senza presumere
che non sia mai stato eseguito. Le letture exchange non costituiscono uno snapshot
atomico; una discordanza transitoria viene rivalutata al ciclo successivo.

Ordini aperti sconosciuti e posizioni senza origine locale/journal vengono
registrati come anomalie esterne persistenti. Non vengono cancellati, chiusi o
attribuiti al bot. La loro semplice scomparsa non sblocca gli ingressi. Una
differenza di quantità su una posizione già nota può risolversi dopo che il
normale recovery P0.2 ha aggiornato il DB e una nuova lettura conferma l'accordo.

Tutti gli ingressi, inclusi DCA e replacement, rispettano sia journal sia blocchi
P0.2. Un invio fallito con trade esistente aggiunge `journal:<client_id>` ai marker
P0.2. Il recovery rimuove solo i propri marker risolti: non rimuove
`force_exit_submission_unknown` né i marker degli altri ordini.

Le riduzioni `reduceOnly` non dipendono dal blocco globale degli ingressi, ma
restano vietate sulla stessa coppia con intento irrisolto o quantità/origine non
verificata. Una verifica precedente non vale dopo il fallimento della successiva.
Un rifiuto pre-invio del journal restituisce `False` alle uscite, evitando di
trasformarlo artificialmente in una submission sconosciuta. La logica RPC force-exit
e la semantica di cancellazione/replacement P0.2 restano invariate.

## Coordinamento e limiti operativi

Un lock OS esclusivo su `orders.lock` resta aperto per la vita del bot. Un mutex
coordina i thread locali. Un secondo processo sullo stesso volume viene respinto;
la morte del processo libera il lock OS, non le prenotazioni SQLite. La garanzia
è un solo dispatch locale per intento, non exactly-once exchange assoluto.

Tutte le istanze dello stesso account devono condividere **lo stesso directory
del journal su un filesystem con lock affidabili**. Volumi separati, altri host
non coordinati, vecchie versioni del bot e altri sistemi di trading non sono
intercettati dal lock. Non avviarli contemporaneamente. Nessuna promessa di
coordinamento distribuito o resistenza alla perdita fisica del volume.

Lo scope supportato è quello attuale: derivati Bybit linear USDT, one-way,
`stoploss_on_exchange=false`. L'avvio rifiuta stop exchange nativi, che avrebbero
un percorso d'invio separato non journalizzato. Nessun parametro è stato cambiato.
Spot, inverse, altri collateral, hedge mode e ordini condizionali non sono
abilitati da questa patch. La verifica di account remoto e la sincronizzazione
completa di tutti i servizi rimangono limiti dichiarati.

Le anomalie esterne e gli intenti non attribuibili richiedono riconciliazione
manuale documentata; questa patch non introduce un comando di reset o adozione.
Conservare insieme journal, database trade e stato P0.1 nei backup a bot fermo.
Un rollback del codice richiede prima arresto e risoluzione degli intenti pendenti:
una versione precedente ignorerebbe il journal. Il DB Freqtrade non viene migrato
né modificato strutturalmente; nessun dato P0.1/P0.2 viene eliminato.

## Verifica offline

Suite: `scripts/tests/test_order_journal.py`, `test_prop_profile.py`,
`test_order_recovery.py`. Coprono timeout, rifiuti, diversi punti di crash,
morte reale di subprocess, lock fra processi, restart, fill parziali, storico,
paginazione, origine, quantità residue, assenza di reinvio, blocchi P0.2, force-exit
e replacement. Il socket è bloccato nei test; gli endpoint sono mock.

I test usano SQLite reale e metodi core/adapter estratti dall'AST, non una copia
della loro implementazione. Il test del mapping ID esegue CCXT reale senza rete.
CCXT locale: **4.5.63**, mentre `requirements.txt` richiede **4.5.68**. La verifica
deve essere ripetuta nell'ambiente target con la versione prevista; nessuna
dipendenza è stata installata. Mancano `sqlalchemy`, `schedule` e `xdist`: non è
stata eseguita la suite completa Freqtrade né dimostrata l'integrazione ORM reale.

## Dipendenze successive e riuso Two-Step

P0.4–P0.7 non sono implementate. Dovranno rispettare journal, lock, identità e
marker persistenti; eventuali nuovi percorsi d'invio o stop nativi dovranno usare
lo stesso protocollo pre-send. Il journal non sostituisce risk guard o baseline UTC.

`order_journal.py`, le primitive Bybit/Exchange e gli hook core sono riutilizzabili:
non contengono percentuali One-Step. `journal_execution.open_journal` usa ancora
il validatore P0.1 `profile_from_config`, attualmente limitato a One-Step. Per
Two-Step servirà un contratto validato per ciascuna fase e un binding coerente,
con configurazioni separate; non basta copiare il file e cambiare il nome profilo.
Ripetere tutte e tre le suite, test di cambio fase/identità e integrazione offline
con ORM e CCXT target. Nessun merge, cherry-pick o modifica del branch Two-Step.
