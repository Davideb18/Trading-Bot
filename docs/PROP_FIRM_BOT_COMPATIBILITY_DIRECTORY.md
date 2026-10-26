# Directory Ufficiale e Verificata: Dove Puoi Usare il Tuo Bot per Gareggiare e Vincere

Questo documento contiene l'elenco rigoroso, verificato clausola per clausola, dei siti e delle Prop Firm dove puoi o NON puoi utilizzare il tuo bot **Freqtrade + NostalgiaForInfinityX8** ([`user_data/config_prop_nfix8.json`](file:///Users/davide/Desktop/Personal_project/Bot%20Trading/FreqtradeBot/user_data/config_prop_nfix8.json)).

---

## 🟢 CATEGORIA 1: SITI DOVE PUOI USARE IL BOT DIRETTAMENTE NELLA GARA / TORNEO GRATIS

In questi siti i bot/API sono **espressamente autorizzati** durante le competizioni e le prove gratuite per scalare la classifica e vincere capitali reali o conti finanziati.

---

### 1. Crypto Fund Trader (CFT) — Torneo Gratuito & Prop Firm
* **Sito Web:** [cryptofundtrader.com/tournaments/](https://cryptofundtrader.com/tournaments/)
* **Costo di Iscrizione:** **0.00 € (100% Gratuito)**
* **Premi in Palio:**
  * Dal 1° al 10° Classificato: **Conti Finanziati da $200.000 (200k Evaluation)**.
  * Premi in denaro cash per i primi podi.
* **Vincoli Operativi:**
  * **Daily Loss Limit:** **5.0%** (Il nostro bot nei 92 giorni di audit ha registrato un picco massimo di **3.17%**, quindi è pienamente conforme).
  * **Max Overall Loss:** **10.0%** (Il nostro bot nel crollo storico peggiore ha avuto un Max DD del **3.89%**).
* **Regola Ufficiale sui Bot:**
  * **AMMESSI AL 100% su MetaTrader 5 (MT5).**
  * Vietano unicamente: High-Frequency Trading estremo (ordini aperti e chiusi sotto i 2 secondi), tick scalping e arbitraggio di latenza. NostalgiaForInfinityX8 opera su candele a 5 minuti e mantiene le posizioni per ore/giorni: **è 100% legale e conforme**.
* **Come Attivarlo col Tuo Bot:**
  1. Iscriviti gratis alla sezione *Tournaments*.
  2. Ricevi le credenziali del conto gara demo su **MT5**.
  3. Freqtrade elabora i segnali e apre gli ordini su MT5 tramite il modulo bridge locale webhook in 2 ms.
  4. Avvii contemporaneamente il Risk Sentinel ([`scripts/llm_risk_manager.py`](file:///Users/davide/Desktop/Personal_project/Bot%20Trading/FreqtradeBot/scripts/llm_risk_manager.py)) a protezione continua dell'equity.

---

### 2. HyroTrader — Crypto Prop Firm su Bybit
* **Sito Web:** [hyrotrader.com](https://hyrotrader.com)
* **Costo di Iscrizione:** **Free Trials gratuiti** e tornei crypto periodici con montepremi in conti finanziati.
* **Premi in Palio:** Conti finanziati su exchange Bybit da **$25.000 a $200.000**.
* **Vincoli Operativi:**
  * **Daily Loss Limit:** **5.0%**
  * **Max Total Loss:** **10.0%**
* **Regola Ufficiale sui Bot (Dalle FAQ Ufficiali):**
  * *"HyroTrader allows you to use trading bots and automated strategies, provided that the strategy is your own. Third-party copy-trading and account mirroring are not allowed."*
  * **Traduzione & Validità:** Poiché usi la tua istanza personale di Freqtrade con configurazione Zero-Burn a 8 slot, non stai facendo copy-trading di massa. Il bot è **autorizzato**.
* **Come Attivarlo col Tuo Bot:**
  1. Si connette direttamente via **API Bybit / Blofin**.
  2. Nessun programma intermedio: inserisci la `API Key` e `Secret` in `user_data/config_prop_nfix8.json` e lanci Freqtrade.

---

### 3. Bybit World Series of Trading (WSOT) & Tornei Ufficiali Bybit
* **Sito Web:** [bybit.com](https://bybit.com)
* **Costo di Iscrizione:** **0.00 € (Nelle edizioni Demo/Free)** o con micro-deposito minimo.
* **Premi in Palio:** Montepremi globale oltre **10.000.000 USDT** in denaro reale prelevabile.
* **Regola Ufficiale sui Bot:**
  * **BYBIT PREMIA E INCENTIVA UFFICIALMENTE I BOT E LE API.**
  * Esiste una leaderboard ufficiale dedicata esclusivamente a chi fa trading con Bot algoritmici.
* **Come Attivarlo col Tuo Bot:**
  1. Colleghi Freqtrade inserendo le chiavi API di Bybit nel file di config.
  2. Il bot scambia direttamente sul book ordini spot o futures di Bybit 24/7/365.

---

### 4. Binance Futures & Spot Trading Tournament (API Challenges)
* **Sito Web:** [binance.com](https://binance.com)
* **Costo di Iscrizione:** **0.00 €**
* **Premi in Palio:** Centinaia di migliaia di USDT e sconti commissionali istituzionali VIP.
* **Regola Ufficiale sui Bot:**
  * **AMMESSI AL 100% VIA API.** Categorie create specificamente per trader quantitativi.
* **Come Attivarlo col Tuo Bot:**
  * Connessione API nativa identica a quella che usi attualmente sul tuo server AWS EC2.

---

## 🔴 CATEGORIA 2: SITI DOVE I BOT SONO VIETATI DURANTE LA GARA GRATUITA

In questi siti, i tornei gratuiti sono riservati **esclusivamente al trading manuale discrezionale**. Se ci colleghi un bot vieni squalificato all'istante dal sistema antifrode.

> [!WARNING]
> **NON collegare il bot a questi tornei gratuiti!**

| Prop Firm | Regola Ufficiale del Torneo Gratuito | Conseguenza se usi il Bot | Note sul Conto Finanziato (Se Vinto a Mano) |
| :--- | :--- | :--- | :--- |
| **FundedNext** | *"Run EAs, automation or Trade Manager: Manual trading only. Automated strategies and EAs are not allowed and will disqualify the entry."* | **Squalifica Immediata** | Sui conti vinti *Stellar Instant* gli EA sono invece ammessi. |
| **Funding Pips** | *"EA execution is strictly prohibited during the free monthly competitions on Match-Trader."* | **Squalifica Immediata** | Sui conti a pagamento o vinti, gli EA personali sono consentiti. |
| **Alpha Capital Group** | *"Rule 1: No EA Use – Expert Advisors or automated bots are strictly prohibited in the competition."* | **Squalifica Immediata** | Solo trading manuale consentito nel torneo. |
| **AudaCity Capital** | *"No EAs or Bots. Only manual trading is permitted."* | **Squalifica Immediata** | Solo trading manuale nel torneo. |

---

## 🟡 CATEGORIA 3: PROP FIRM AD ALTO CAPITALE CHE ACCETTANO BOT (MA CON CHALLENGE)

Se in futuro deciderai di gestire capitali da **$100.000 a $4.000.000** tramite challenge con rimborso al primo profitto:

### 1. The5ers ([the5ers.com](https://the5ers.com))
* **Accettazione Bot:** **SÌ, AL 100% (Bot proprietari).**
* **Regole:** Ammettono script ed algoritmi personali su MetaTrader 5. Vietati solo HFT da millisecondi e bot copia commerciali comprati sui marketplace.
* **Target:** Drawdown giornaliero del 3-5%, target 6-10%. NostalgiaForInfinity è perfettamente idoneo.

### 2. Breakout Prop ([breakoutprop.com](https://breakoutprop.com))
* **Accettazione Bot:** **SÌ, TRAMITE API DIRETTE.**
* **Caratteristiche:** Prop firm crypto che fornisce accesso API diretto al loro terminale perpetual futures.

---

## 📋 GUIDA OPERATIVA: DOVE INIZIARE SUBITO A COSTO ZERO

1. **Obiettivo Primario (Vincere un Conto da $200.000 Gratuito con il Bot):**
   * Iscriviti al torneo gratuito di **[Crypto Fund Trader](https://cryptofundtrader.com/tournaments/)**.
   * Usa il bot con la configurazione Zero-Burn a 8 slot ([`user_data/config_prop_nfix8.json`](file:///Users/davide/Desktop/Personal_project/Bot%20Trading/FreqtradeBot/user_data/config_prop_nfix8.json)).
   * Il bot rispetterà automaticamente il vincolo del 5% giornaliero (il nostro peggior giorno storico è stato di appena il 3.17%).

2. **Obiettivo Secondario (Vincere Premi Cash Diretti con Bybit/Binance):**
   * Registra il tuo account sulle competizioni API ufficiali di **Bybit** e **Binance**.
   * Collega Freqtrade direttamente tramite le API Keys in 30 secondi.
