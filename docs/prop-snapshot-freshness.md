# Patch 7: freschezza delle componenti del journal

Prima della patch il core acquisiva ordini aperti, posizioni (con mark price),
storico ed esecuzioni in sequenza. Il journal assegnava `checked_ms` alla fine:
questo valore non descriveva l'età delle componenti. Le pagine del core non
conservavano né validavano il timestamp della risposta V5. Il supervisore
verificava già l'età delle singole risposte, ma non conservava tutti gli intervalli.

Ogni pagina ora registra inizio locale, ricezione locale, durata monotonic e
timestamp server della risposta. I limiti restano 15 secondi di età/finestra e
2 secondi di skew; non vengono aumentati. Una risposta server precedente
all'inizio della richiesta oltre lo skew ammesso non certifica una lettura corrente.
Salti dell'orologio incompatibili con la durata monotonic invalidano il dato.
`checked_ms` rimane il completamento della riconciliazione, non la data dei dati.

Le pagine conservano la componente di origine: ordini aperti, posizioni, realtime,
storico ed esecuzioni per client ID. Sono richieste tutte le pagine. I timestamp
storici di esecuzione e aggiornamento degli ordini non sono timestamp di lettura:
un fill vecchio è ancora una prova valida se recuperato con una query completa
e coerente. Nessuna modifica alle somme degli eseguiti o alla deduplicazione.

Ordini e quantità delle posizioni vengono riletti dopo lo storico. Differenze
rispetto alle letture iniziali rendono lo snapshot inconcludente. È una verifica
di stabilità osservata entro una finestra limitata, non un'operazione atomica:
REST non esclude cambiamenti transitori o successivi all'ultima risposta.

Il journal verifica gli intervalli prima di applicare le osservazioni e prima
di completare la transazione. Se scadono durante la riconciliazione, i nuovi
settlement vengono annullati; prenotazioni incerte restano trattenute. Uno snapshot
incompleto non può rendere il journal pronto. Il restart richiede nuove letture.

P0.6 rivalida le componenti al momento dell'autorizzazione, includendo le letture
indipendenti di identità, modalità account ed equity. Il supervisore conserva
questi metadati nello stato senza modificarne formule o baseline. Lo stato senza
metadati è insufficiente per nuove autorizzazioni; il timestamp di salvataggio
non li sostituisce. La decisione persistita conserva anche gli intervalli usati.

## Limite mark price

Il `markPrice` della risposta posizioni non contiene, nel percorso implementato,
un timestamp verificabile della sua valorizzazione. `updatedTime` della posizione
e `time` della risposta non dimostrano quando il mark sia stato osservato.
L'adapter non inventa `mark_price_observed_ms`. Pertanto P0.6 rifiuta nuovo rischio
con posizioni aperte prive di tale evidenza, anche se una revisione della politica
fosse presente. Una fonte pertinente deve essere dimostrata prima di abilitare
questa operatività; i test positivi usano esclusivamente metadati sintetici dichiarati.

## Emergenza e limiti

Il gate di ammissione non è invocato per reduce-only né per l'autorità indipendente
di emergenza. Il supervisore conserva i propri controlli, deadline e handoff;
non dipende dalla freschezza del journal core per effettuare nuove letture.
Uno snapshot indipendente incompleto o temporalmente incoerente resta inutilizzabile.
La mancanza del timestamp del mark per l'ammissione non aggiunge un blocco alle
riduzioni già autorizzate con le evidenze indipendenti previste.

Nessuna lettura live effettuata. La semantica e la latenza effettive delle fonti
remote non sono certificate dai test. Restano aperti baseline UTC e overnight;
nessuna approvazione operativa viene creata. Nessuna garanzia di perdita massima.
