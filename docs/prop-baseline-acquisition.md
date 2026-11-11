# Patch 5: baseline UTC, evidenze disponibili e limite operativo

Analisi dei componenti locali; nessuna verifica live delle capacità del servizio.
Non è stato introdotto un acquisitore automatico: i dati attualmente acquisiti
non dimostrano l'equity delle esatte 00:00 UTC.

| Caso | Evidenza disponibile | Esito |
|---|---|---|
| Snapshot esatto a mezzanotte | Wallet corrente e timestamp della risposta server; nessuna attestazione dell'istante effettivo di valorizzazione | Non dimostrato, anche con risposta marcata 00:00:00 |
| Ricostruzione storica | Ordini/esecuzioni, quantità e snapshot correnti; nessun ledger completo di movimenti di capitale e valorizzazioni storiche | Non dimostrata |
| Stima con intervallo | Campioni prima/dopo con provenienza e ricezione UTC | Campioni conservati; non definiscono un intervallo numerico garantito per il valore intermedio |
| Dato mancante | Baseline assente, scaduta, incoerente o sorgente indisponibile | Fail-closed |

`SupervisorBybit.equity()` usa l'equity USDT restituita dal wallet, comprensiva
del flottante: non aggiunge nuovamente il P&L. `IndependentRiskAPI` trasporta il
valore e il timestamp della risposta; non li trasforma in un dato storico.
`RiskGuard` conserva ricezione e risposta separatamente, usa il giorno della
ricezione e rifiuta il daily quando risposta e ricezione attraversano giorni UTC
diversi. Il journal conserva la provenienza degli ordini, non un estratto conto
completo o la valorizzazione dell'account a mezzanotte.

Per una ricostruzione servirebbero almeno un riferimento contabile attendibile,
copertura completa e temporalmente coerente di depositi, prelievi, trasferimenti,
rettifiche, fee, funding, realizzi, quantità aperte e prezzi di valorizzazione
pertinenti all'istante richiesto. Ordini assenti e conto flat non escludono questi
movimenti. Il capitale iniziale non sostituisce nessuna di queste evidenze.

## Contratto esterno e persistenza

`validate_baseline` controlla struttura, identità, timestamp, valore e attestazione
di revisione del record esterno. Non autentica il riferimento, non legge una fonte
storica e non prova che il revisore abbia verificato i dati. Un JSON compilato
manualmente, anche con `reviewed: true`, non costituisce da solo evidenza.
L'accettazione strutturale nei test non certifica uno snapshot exchange.

Il commissioning senza baseline rifiuta l'inizializzazione. Non sono introdotti
nuovi percorsi di scrittura o importazione: restano i controlli precedenti e la
persistenza dello stato tramite file temporaneo, fsync e replace. Un errore di
scrittura blocca l'autorizzazione; non è stato validato un processo automatico
end-to-end di acquisizione e pubblicazione della baseline, perché manca la fonte.

## Overnight e decisioni prima del deployment

Rimane la gestione Patch 4: baseline non verificata blocca ingressi, DCA e
replacement; daily dichiarato non verificabile; esposizione indipendente presente,
flat osservata oppure sconosciuta; fault persistente e overall valutabile quando
l'equity è attendibile. Non si liquidano posizioni sulla sola base di una stima.
Le riduzioni e l'emergenza esistenti conservano journal e handoff esclusivo.

Prima del deployment occorre scegliere e approvare una fonte di baseline con
semantica temporale dimostrata e una procedura di revisione/provenienza, oppure
una politica alternativa esplicita per posizioni e ordini overnight. Tale politica
deve definire interventi, prova flat, gestione di cancellazioni incerte, restart,
indisponibilità del servizio e autorità competente. Non vengono scelti margini
numerici o tempi di chiusura senza evidenze. Una chiusura software non garantisce
la perdita massima né l'assenza di esposizione a un istante futuro.

Non è dimostrata l'idoneità al deployment con esposizione overnight. Il blocco
esistente resta necessario; la sola conformità del record al validatore non basta.
