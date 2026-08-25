# Cosa si fa qui

Questa cartella contiene un **orchestratore Python** che collega **Azure
DevOps** e **Claude Code**, automatizzando due fasi del ciclo di sviluppo:
l'implementazione dei ticket e la revisione delle relative pull request.

In pratica sostituisce (o affianca) uno sviluppatore in due compiti
ripetitivi: prendere un ticket assegnato e trasformarlo in codice + PR, e
poi rispondere ai commenti di revisione che arrivano su quella PR, senza
supervisione umana continua.

## I due loop principali

- **`ingest_loop.py`** — Loop 1: ingest
  1. Interroga Azure Boards (via WIQL, `@CurrentIteration`) per i ticket
     assegnati all'utente nell'iterazione corrente.
  2. Se un ticket e' una Epic, la scompone in PBI figli.
  3. Per ogni PBI/Task: crea un branch dedicato, invoca Claude Code (via
     `claude_runner.py`) per implementare il codice, fa girare l'autofix
     deterministico, committa, pusha e apre la PR.

- **`review_loop.py`** — Loop 2: review
  1. Guarda le PR aperte dal bot.
  2. Legge i thread di commenti non risolti.
  3. Chiede a Claude Code di classificare ogni commento nuovo:
     - se e' un **fix meccanico**, lo applica direttamente e risponde al
       thread;
     - se richiede **giudizio umano**, risponde spiegando perche', blocca
       il ticket (tag `agent:blocked`) e si ferma, lasciando la decisione a
       una persona.

## Come viene mantenuto lo stato

Non esiste un datastore di stato "di verita'" separato: lo stato di
avanzamento di ogni work item (branch creato, implementato, PR aperta,
bloccato, epic scomposta) vive come **tag custom su Azure Boards**
(`agent:branch-created`, `agent:implemented`, `agent:pr-open`,
`agent:blocked`, `agent:decomposed`), gestiti da `state.py`. Questo rende
entrambi i loop **idempotenti**: si possono rilanciare piu' volte senza
duplicare branch, PR o PBI, perche' ogni run guarda i tag prima di agire.

`history.py` mantiene invece uno storico locale (SQLite, `history.db`) di
run e decisioni — usato solo per l'osservabilita' (dashboard), non per la
logica di stato.

## Componenti di supporto

| File               | Ruolo                                                              |
|--------------------|---------------------------------------------------------------------|
| `config.py`         | Legge la configurazione da variabili d'ambiente (o `.env`) e crea la connessione ad Azure DevOps. Nessun default per credenziali/organizzazione: se manca qualcosa, si interrompe subito |
| `state.py`          | Tag custom sui work item per lo stato idempotente (vedi sopra)     |
| `claude_runner.py`  | Wrapper sincrono attorno a `claude_agent_sdk.query()`: invoca Claude Code con `permission_mode="dontAsk"` e una lista esplicita di tool pre-approvati, adatto a run non presidiati (cron) |
| `retry.py`          | Singolo retry automatico (con delay) per le chiamate alle API Azure DevOps, che possono fallire per motivi transitori |
| `autofix.py`        | Autofix deterministico (`prettier --write` + `nx affected:lint --fix`) sui soli file cambiati, **senza** invocare Claude: i problemi di puro lint/formattazione si risolvono a costo zero, lasciando a Claude solo cio' che richiede comprensione del codice |
| `history.py`        | Storico persistente (SQLite) di run e decisioni, con motivazione, per la dashboard |
| `dashboard_server.py` | Server FastAPI locale (`127.0.0.1:8765`) che mostra stato corrente, storico e permette di avviare/fermare i due loop da bottone, oltre ad azioni manuali (bloccare un ticket, aprire una PR gia' implementata) |
| `static/`           | Frontend statico servito dalla dashboard |
| `logs/`             | Log per-run scritti dai processi figli lanciati dalla dashboard |

## Perche' e' fatto cosi'

- **Nessun default indovinato**: se manca una variabile d'ambiente
  obbligatoria (`ORG_URL`, `PROJECT`, `REPO_ID`, `AZURE_DEVOPS_PAT`,
  `REPO_PATH`), lo script si ferma con un errore chiaro invece di
  proseguire con credenziali sbagliate.
- **Autofix prima di Claude**: prettier/lint non richiedono comprensione,
  quindi vengono risolti da tool deterministici a costo zero, sia dopo
  l'implementazione (ingest) sia prima della valutazione dei commenti
  (review).
- **Claude Code non decide da solo se bloccare un umano**: quando un
  commento di review richiede giudizio, il loop si ferma e aspetta una
  persona, invece di tentare un fix rischioso.
- **Un solo run alla volta**: ingest e review condividono la stessa
  working copy Git (`REPO_PATH`), quindi la dashboard mantiene un lock in
  memoria per evitare di farli girare insieme; questo lock vale solo per i
  run avviati dalla dashboard, non per lanci manuali da terminale.
- **Dashboard solo locale**: il server ascolta solo su `127.0.0.1`, non e'
  pensato per essere esposto o condiviso in rete.

## Esecuzione

```bash
python ingest_loop.py       # loop 1, manuale
python review_loop.py       # loop 2, manuale
python dashboard_server.py  # dashboard su http://127.0.0.1:8765
```

Per l'esecuzione schedulata (crontab su Linux, Task Scheduler su Windows),
vedi il dettaglio in `README.md`.
