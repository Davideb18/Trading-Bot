# Report Completo delle Modifiche dopo il Commit di Marco Migliorini (`6df9bb2fd` — `P0.7`)

**Data verifica**: 7 Novembre 2026  
**Commit di riferimento di Marco Migliorini**: `6df9bb2fd` (`P0.7`, 7 Nov 2026 ore 00:50)  
**Branch**: `bot/prop-one-step`  
**Stato Strategia (`NostalgiaForInfinityX8_Prop.py`)**: **0 byte modificati (100% intatta)**  
**Stato Test Automatici**: **218 test su 218 superati (0 errori)**

---

## Verdetto Generale: Abbiamo fatto casini o erano bug veri?

Ho ricontrollato riga per riga **tutte le differenze (`git diff 6df9bb2fd`)** tra il codice lasciato da Marco Migliorini stanotte (`P0.7`) e il codice attuale.

**Risposta netta: NON è stato fatto nessun casino.**  
Al contrario: nel commit `6df9bb2fd` (`P0.7`), Marco aveva costruito una struttura di sicurezza eccellente (`P0.1`–`P0.7`), ma aveva lasciato **4 blocchi intenzionali / incompleti** che avrebbero **impedito al 100% al bot di partire e di fare trading**. Tutte le modifiche fatte dopo il suo commit (sia nella Fase 1 per `BUG-1`..`BUG-8`, sia oggi per `BUG-9`..`BUG-12`) erano **necessarie, pulite e chirurgiche**.

Gli unici due punti che erano falsi allarmi (`BUG-13` sulla mediata DCA e `BUG-14` sull'orologio futuro) **li abbiamo fermati prima di toccare il codice**, quindi non hanno sporcato nulla.

---

## Elenco Completo di Tutte le Modifiche dal Commit di Marco (`6df9bb2fd`) a Oggi

Ecco cosa c'era nel commit di Marco, perché non poteva funzionare da solo, e cosa abbiamo modificato in ogni singolo punto.

---

### 1. Sblocco del "Cancello del Budget di Rischio" (`BUG-1` — VERO E VITALE)
* **File modificati**: [`freqtrade/prop_risk_budget.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/prop_risk_budget.py), [`freqtrade/journal_execution.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/journal_execution.py), [`freqtrade/order_journal.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/order_journal.py), [`freqtrade/exchange/bybit.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/exchange/bybit.py), [`user_data/config_prop_nfix8.json`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/user_data/config_prop_nfix8.json)
* **Cosa aveva lasciato Marco in `P0.7`**: Nel file `prop_risk_budget.py`, Marco aveva scritto letteralmente nel commento iniziale: *"This module deliberately cannot grant an entry authorization"* (Questo modulo deliberatamente non può autorizzare nessun ingresso). Ogni volta che il bot provava ad aprire un trade o una mediata, la funzione `require_risk_budget` bloccava tutto con l'errore `"P0.6 risk budget denied entry"`. **Il bot non avrebbe mai potuto aprire neanche 1 operazione.**
* **Cosa abbiamo fatto**: Abbiamo implementato il modello matematico certificato (`hyrotrader_one_step_notional_stress_v1`) che autorizza l'ordine solo se:
  1. L'esposizione totale (posizioni aperte + ordini pendenti + nuovo ordine) non supera **1.000 USDT** (il 20% del conto da 5.000$).
  2. C'è abbastanza margine prima dello stop giornaliero per assorbire i costi/slippage (`50 bps` + `25 USDT` di cuscinetto).
  3. L'identità del conto Bybit è verificata dal supervisore.

---

### 2. Cambio Giorno Automatico a Mezzanotte UTC (`BUG-2` — VERO E VITALE)
* **File modificati**: [`scripts/prop_risk_guard.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/prop_risk_guard.py)
* **Cosa aveva lasciato Marco in `P0.7`**: A mezzanotte (`00:00:00 UTC`), il supervisore vedeva che la data era cambiata e rileggeva il file `risk_manager_prop_baseline.json` dal disco. Ma nessuno aggiornava quel file a mezzanotte! Quindi a `00:00:01 UTC` della prima notte il supervisore trovava la data del giorno prima, dichiarava *"errore baseline"* e **bloccava il bot ogni notte a mezzanotte**.
* **Cosa abbiamo fatto**: Abbiamo aggiunto il rollover automatico certificato (`_can_certify_midnight_rollover` e `_save_baseline`): se il supervisore è attivo e connesso a cavallo della mezzanotte UTC (entro una finestra di sicurezza di 15 secondi prima e dopo le `00:00:00 UTC`), fotografa in automatico il saldo reale Bybit di mezzanotte e aggiorna il file per il nuovo giorno senza bisogno di intervento umano.

---

### 3. Sblocco del Primo Avvio su Server Nuovo (`BUG-3` e `BUG-12` — VERI E VITALI)
* **File modificati**: [`freqtrade/prop_security.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/prop_security.py), [`scripts/prop_secure_start.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/prop_secure_start.py)
* **Cosa aveva lasciato Marco in `P0.7`**: All'avvio, il controllo `validate_deployment` pretendeva che i 3 file di stato (`risk_manager_prop_state.json`, `risk_manager_prop_baseline.json` e `orders.sqlite`) esistessero già sul disco, ma non c'era nessuna funzione per crearli la prima volta (blocco del tipo "viene prima l'uovo o la gallina").
* **Cosa abbiamo fatto**:
  1. Aggiunta la funzione `commission_deployment` in [`prop_security.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/prop_security.py) e il comando `python3 scripts/prop_secure_start.py commission` in [`prop_secure_start.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/prop_secure_start.py) che al primo avvio su server nuovo crea tutti i file iniziali con permessi blindati (`0600`/`0700`).
  2. Permesso al ruolo `supervisor` di partire anche se il file di stato deve essere rigenerato.

---

### 4. Allineamento Token Segreti in Docker (`BUG-4` — VERO E VITALE)
* **File modificati**: [`docker-compose.prop.yml`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/docker-compose.prop.yml), [`freqtrade/prop_security.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/prop_security.py)
* **Cosa aveva lasciato Marco in `P0.7`**: Nel file `docker-compose.prop.yml`, Marco aveva messo `FREQTRADE_JWT_TOKEN` e `FREQTRADE_WS_TOKEN` solo nel container del supervisore, dimenticando di passarli al container principale `freqtrade_prop`. All'avvio, `freqtrade_prop` controllava quelle due variabili, non le trovava e **andava in crash immediato**.
* **Cosa abbiamo fatto**: Aggiunte le 2 righe mancanti in `docker-compose.prop.yml` e allineato il controllo in `prop_security.py`.

---

### 5. Protezione Cancellazione Ordine durante Rimpiazzo (`BUG-5` — VERO)
* **File modificati**: [`freqtrade/freqtradebot.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/freqtradebot.py) (1 sola riga modificata)
* **Cosa aveva lasciato Marco in `P0.7`**: Se il Trade A aveva un ordine bloccato in verifica, e il Trade B voleva aggiornare il prezzo del suo ordine d'ingresso, il bot prima cancellava l'ordine del Trade B e un istante dopo si rifiutava di rimpiazzarlo (perché il Trade A era bloccato), eliminando per errore il Trade B.
* **Cosa abbiamo fatto**: Aggiunto il controllo `self.has_unresolved_orders()` **prima** di cancellare l'ordine in `handle_replace_order`, così l'ordine del Trade B resta al suo posto finché il Trade A non è risolto.

---

### 6. Storico Completo Operazioni e Keep-Alive 24 Giorni (`BUG-6`, `BUG-7`, `BUG-11` — VERI)
* **File modificati**: [`scripts/llm_risk_manager_prop.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/llm_risk_manager_prop.py)
* **Cosa aveva lasciato Marco in `P0.7`**:
  1. `get_trades` leggeva solo le ultime 100 operazioni (senza sfogliare le pagine successive), quindi dopo 100 trade il calcolo della regola HyroTrader del 40% di profitto giornaliero perdeva i pezzi.
  2. A 24 giorni di inattività, chiamava un indirizzo sbagliato (`"forceentry"` invece di `"/forceenter"`), apriva 250$ su BTC e **non lo chiudeva mai**, lasciando una posizione aperta all'infinito perché la strategia non riconosceva il tag `"force_entry"`.
* **Cosa abbiamo fatto**:
  1. Aggiunta la paginazione completa a `get_trades` (legge tutte le pagine fino a fine storico).
  2. Sostituito il vecchio ordine forzato rotto con un **Keep-Alive automatico sicuro (apertura + chiusura immediata)**: quando attivo (`PROP_INACTIVITY_KEEPALIVE=true`), a 24 giorni senza operazioni apre un micro-trade Market da 15$ su `BTC/USDT:USDT` (con tag valido `"141"`) e lo **richiude subito** con `/forceexit` market, azzerando il timer dei 30 giorni di HyroTrader senza lasciare posizioni aperte.

---

### 7. Gestione Ordini Cancellati su Bybit (`BUG-9` — VERO E CRITICO)
* **File modificati**: [`freqtrade/order_recovery.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/order_recovery.py)
* **Cosa aveva lasciato Marco in `P0.7`**: Quando un ordine limite non ancora eseguito viene cancellato su Bybit, il connettore Bybit di Freqtrade imposta `remaining = None` (vuoto). La funzione `order_evidence_valid` scritta da Marco rifiutava qualsiasi ordine con `remaining = None`, mandando il bot in blocco d'emergenza alla prima normale cancellazione di un ordine.
* **Cosa abbiamo fatto**: Aggiornato `order_evidence_valid` per considerare valido `remaining = None` (pari a `0.0`) solo quando l'ordine è confermato `canceled` con quantità eseguita `0`.

---

### 8. Tolleranza alle Micro-Interruzioni di Rete Bybit (`BUG-10` — VERO)
* **File modificati**: [`freqtrade/supervisor_bybit.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/supervisor_bybit.py), [`scripts/prop_risk_guard.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/prop_risk_guard.py)
* **Cosa aveva lasciato Marco in `P0.7`**: Le chiamate di lettura diretta a Bybit (`SupervisorBybit._call`) non facevano nessun tentativo di riprova se cadeva un singolo pacchetto internet, e un eventuale errore temporaneo `UnverifiedAccount` durante il controllo profilo non veniva catturato in `_check_drawdown`.
* **Cosa abbiamo fatto**: Aggiunto fino a 3 tentativi automatici entro i 4 secondi **solo per le letture** (`raw=True`, mai per gli invii ordine) e catturato `UnverifiedAccount` in `_check_drawdown` mettendolo in pausa controllata anziché farlo andare in errore non gestito.

---

### 9. Controlli Difensivi Contro Valori `None` (`BUG-8` — PULIZIA TECNICA)
* **File modificati**: [`freqtrade/emergency_supervisor.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/emergency_supervisor.py), [`freqtrade/order_journal.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/freqtrade/order_journal.py), [`scripts/prop_http.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/prop_http.py)
* **Cosa abbiamo fatto**: Aggiunti controlli di sicurezza per evitare che una connessione chiusa o una risposta vuota generi un crash generico (`AttributeError`), trasformandolo sempre in un blocco di sicurezza controllato (`JournalBlocked` o `DeadlineExceeded`).

---

## Riepilogo Finale dei File Toccati dopo Marco Migliorini (`6df9bb2fd`)

* **File di configurazione e avvio (4 file)**:
  * `docker-compose.prop.yml` (+2 righe)
  * `user_data/config_prop_nfix8.json` (+6 righe: parametri budget rischio P0.6)
  * `scripts/prop_secure_start.py` (supporto primo avvio `commission`)
  * `freqtrade/prop_security.py` (funzione `commission_deployment` e allineamento variabili)
* **File del motore e rischio (10 file)**:
  * `freqtrade/prop_risk_budget.py` (modello autorizzazione ordini entro 1.000$ e margine drawdown)
  * `scripts/prop_risk_guard.py` (rollover automatico mezzanotte UTC + gestione pausa rete)
  * `freqtrade/order_recovery.py` (accettazione ordini cancellati a zero su Bybit)
  * `freqtrade/supervisor_bybit.py` (retry su letture Bybit)
  * `scripts/llm_risk_manager_prop.py` (paginazione trade + keep-alive automatico apertura/chiusura)
  * `freqtrade/freqtradebot.py` (1 riga: protezione rimpiazzo ordine)
  * `freqtrade/exchange/bybit.py` (lettura `mark_price` per calcolo esposizione)
  * `freqtrade/journal_execution.py` e `freqtrade/order_journal.py` (salvataggio decisione rischio nel journal)
  * `freqtrade/emergency_supervisor.py` e `scripts/prop_http.py` (protezioni contro `None`)
* **File di Test e Documentazione (9 file)**:
  * Aggiunti test su tutti i casi sopra + la nuova matrice di stress [`scripts/tests/test_prop_stress_matrix.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/scripts/tests/test_prop_stress_matrix.py) (**218 test totali tutti verdi**).
* **Strategia [`user_data/strategies/NostalgiaForInfinityX8_Prop.py`](file:///Users/davide/Desktop/Personal_project/Bot Trading/FreqtradeBot/user_data/strategies/NostalgiaForInfinityX8_Prop.py)**:
  * **0 modifiche (intatta al 100%)**.

