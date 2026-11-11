# Patch 6: approvazione P0.6 vincolata al contenuto

Nessuna approvazione operativa viene generata. La configurazione distribuita resta
priva di approvazione. Non cambiano modello numerico, capitale, soglie o strategia.

Il contratto schema 1 usa SHA-256 della rappresentazione JSON canonica (chiavi
ordinate, separatori fissi, ASCII, numeri non finiti vietati). Il contenuto include:

- tutti i parametri della politica, versione del modello inclusa; sono esclusi
  soltanto `approval` e `status`;
- il profilo validato completo: account, exchange, challenge, fase, ambiente,
  valuta, capitale iniziale, date di inizio e soglie;
- configurazione operativa selezionata esplicitamente in `OPERATING_KEYS`,
  inclusi i parametri NFIX8 di leva, modalità futures/isolated, sizing e ordini;
- leva, tipo ordine e time-in-force effettivamente richiesti.

L'ordine delle chiavi non cambia il fingerprint. Una diversa rappresentazione
numerica può invalidarlo conservativamente; non normalizzare o riscrivere il
documento dopo la revisione. Quantità, prezzo e classificazione ingresso/DCA non
appartengono all'identità della politica: sono valutati ad ogni richiesta dal
budget, con gli stessi limiti precedenti.

L'approvazione richiede `kind=risk_budget_approval`, `reviewed=true`,
`schema_version=1`, `policy_fingerprint` corrispondente, account e challenge
coerenti, `approved_by`, `reference` e `reviewed_at` UTC con timezone, non futuro
e non precedente alla fase. Anche `status=approved` è obbligatorio. Un hash da solo
non soddisfa il contratto. Versioni modello non supportate restano rifiutate.

La revisione è un confine di fiducia esterno: l'operatore deve verificare il
documento referenziato e l'identità del revisore. Questi controlli non sono una
firma digitale, non autenticano una persona e non dimostrano empiricamente
slippage, latenza o perdita massima. Un amministratore capace di falsificare
configurazione e attestazione non è autenticato da questo meccanismo.

Ogni richiesta non reduce-only acquisisce una copia della configurazione e
ricalcola il fingerprint; nessuna cache di approvazione sopravvive al restart.
Assenza o incoerenza della revisione blocca ingressi, DCA e replacement. Restano
obbligatori baseline, identità remota, stato recente e journal riconciliato.

La decisione include `policy_canonical`, `policy_fingerprint`, versione schema
ed evidenza della revisione accettata. Il journal esistente salva questi campi
insieme alla prenotazione, nella stessa transazione dell'intento. Nessuna modifica
allo schema del database o migrazione è necessaria. Le prenotazioni precedenti
mantengono valori, provenienza e stato; non vengono approvate retroattivamente.
Il rilascio resta subordinato alla riconciliazione completa. `loss_bound` resta
la somma precedente di costi e perdite modellate.

Le riduzioni bypassano soltanto questo gate, conservando i controlli di execution;
handoff e autorità d'emergenza restano invariati. Il commissioning non crea
approvazioni. Prima di nuovi ingressi servono revisione indipendente della politica
esatta, evidenze sui parametri e baseline attendibile; i limiti overnight della
Patch 5 restano aperti. Il bot non è dichiarato pronto al deployment.
