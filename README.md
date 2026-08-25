# Orchestratore Azure DevOps + agenti di coding

Due script Python che orchestrano Azure DevOps (via SDK ufficiale `azure-devops`)
e delegano l'implementazione dei ticket e la correzione delle PR a un agente
configurabile. Claude Code via `claude-agent-sdk` resta il provider predefinito.

- `ingest_loop.py`: prende i ticket assegnati nell'iterazione corrente, scompone
  le Epic in PBI figli, crea un branch per ogni PBI/Task e chiede a Claude Code
  di implementarlo, commitare, pushare e aprire la PR.
- `review_loop.py`: guarda le PR aperte dal bot, legge i thread di commenti non
  risolti e chiede a Claude Code di classificare ogni commento nuovo: se e' un
  fix meccanico lo applica e risponde al thread; se richiede giudizio umano,
  risponde spiegandolo, blocca il ticket e si ferma.

I nuovi branch seguono la convenzione `feature/<id_pbi>__<titolo_in_snake_case>`;
per i work item Azure DevOps di tipo `Bug` usano invece il prefisso `bugfix/`.

Prima di creare una PR dalla dashboard, esegui le **verifiche tecniche**: la
dashboard rileva automaticamente gli script dichiarati nel repository
(`test`, `lint`, `type-check` e `build`, privilegiando le varianti `:affected`)
e li esegue senza usare token IA. La creazione della PR, normale o con
auto-completamento Azure DevOps, rimane bloccata finché le verifiche del commit
corrente non sono superate.

Lo stato (branch creato, PR aperta, bloccato, epic scomposta) vive interamente
su Azure Boards come tag custom sul work item (`agent:branch-created`,
`agent:pr-open`, `agent:blocked`, `agent:decomposed`), gestiti da `state.py`.
Questo rende i due loop idempotenti tra run diversi: rilanciarli non duplica
branch, PR o PBI.

## File

| File               | Ruolo                                                              |
|--------------------|---------------------------------------------------------------------|
| `config.py`         | Legge la configurazione dall'ambiente e crea la connessione ADO    |
| `state.py`          | Tag custom sui work item (stato idempotente)                       |
| `claude_runner.py`  | Runner per Claude SDK o un agente CLI configurabile                 |
| `retry.py`          | Singolo retry automatico per le chiamate API fallite                |
| `autofix.py`        | Autofix deterministico (prettier + lint --fix), senza Claude         |
| `history.py`        | Storico persistente (SQLite) di run e decisioni, per la dashboard   |
| `graphify_context.py` | Contesto Graphify opzionale per i piani, con fallback ai file      |
| `dashboard_server.py` | Server locale della dashboard (FastAPI)                           |
| `ingest_loop.py`    | Loop 1: ingest ticket -> branch -> implementazione -> PR             |
| `review_loop.py`    | Loop 2: review commenti PR -> fix meccanico o blocco                |

## Requisiti

- Python 3.10+
- Un repository git locale del progetto Azure DevOps, gia' clonato, il cui
  path va indicato in `REPO_PATH` (i due script operano su quella working
  copy: creano branch, fanno checkout, e Claude Code vi legge/scrive file).
- [Azure CLI](https://learn.microsoft.com/cli/azure/) con l'estensione
  `azure-devops` installata (`az extension add --name azure-devops`), usata
  da Claude Code per `az repos pr create`. Quando ingest/review vengono
  avviati dalla dashboard, l'autenticazione e' automatica: `AZURE_DEVOPS_PAT`
  viene passato ad az cli anche come `AZURE_DEVOPS_EXT_PAT`, senza bisogno di
  un `az devops login` separato. Serve solo se si lanciano gli script a mano
  fuori dalla dashboard (es. da crontab/Task Scheduler) con un ambiente che
  non include gia' `AZURE_DEVOPS_PAT`.
- Claude Code CLI installato e autenticato (richiesto da `claude-agent-sdk`
  per eseguire gli agenti).

```bash
pip install -r requirements.txt
```

## Variabili d'ambiente

Tutte tranne `TEAM` sono obbligatorie: se ne manca una lo script si
interrompe subito con un errore, invece di indovinare un default.

| Variabile           | Descrizione                                                        |
|----------------------|---------------------------------------------------------------------|
| `ORG_URL`            | URL dell'organizzazione, es. `https://dev.azure.com/mia-org` (senza il progetto) |
| `PROJECT`            | Nome del progetto Azure DevOps                                     |
| `TEAM`               | *(opzionale)* Nome del team. Necessario se il progetto ha piu' team: `@CurrentIteration` nella WIQL si risolve sull'iterazione corrente di UN team, non del progetto — senza `TEAM` viene usato il team di default, che spesso non e' quello giusto |
| `REPO_ID`            | Nome (o GUID) della repository Git                                 |
| `AZURE_DEVOPS_PAT`   | Personal Access Token con permessi Work Items (Read & Write) e Code (Read & Write) |
| `REPO_PATH`          | Path locale del repository git clonato                             |
| `BASE_BRANCH`        | *(opzionale, default `main`)* Branch da cui creare i feature branch e verso cui aprire le PR. Va impostato a `develop` (o altro) se il repo non usa `main` come branch di default |
| `AGENT_PROVIDER`     | *(opzionale, default `claude_sdk`)* `claude_sdk` per Claude Code oppure `command` per un agente CLI esterno |
| `AGENT_MODEL`        | *(opzionale)* Modello predefinito da usare con l'agente selezionato |
| `AGENT_COMMAND`      | Obbligatorio con `AGENT_PROVIDER=command`; comando che legge il prompt da standard input |
| `AGENT_MAX_OUTPUT_TOKENS` | *(opzionale)* Limite positivo per i token di output di un run; è passato all'agente CLI come variabile d'ambiente e diventa un vincolo esplicito per Claude |
| `AGENT_TOKEN_BUDGET` | *(opzionale)* Budget positivo totale: impedisce nuovi run quando i token registrati lo raggiungono |

### Agenti e budget token

Le stesse opzioni sono disponibili nella pagina **Impostazioni** della
dashboard e vengono applicate senza riavvio ai run successivi. Per un agente
CLI esterno impostare `AGENT_PROVIDER=command` e un comando non interattivo
che riceva il prompt su standard input. Il runner esporta inoltre
`AGENT_MODEL`, `AGENT_MAX_OUTPUT_TOKENS` e `AGENT_ALLOWED_TOOLS`, così il
comando wrapper può passarli al proprio provider.

Il budget e il consumo sono mostrati nelle impostazioni. Claude SDK restituisce
le metriche token effettive e quindi alimenta il budget; un comando CLI generico
deve applicare `AGENT_MAX_OUTPUT_TOKENS` e non può fornire metriche token al
runner senza un wrapper che le esponga.

### Opzione A — file `.env`

`config.py` carica automaticamente un file `.env` nella cartella corrente
(via `python-dotenv`), senza sovrascrivere variabili già impostate
nell'ambiente. E' presente un `.env` con placeholder da compilare:

```
ORG_URL=https://dev.azure.com/tua-org
PROJECT=TuoProgetto
REPO_ID=nome-o-guid-repo
AZURE_DEVOPS_PAT=inserisci-qui-il-tuo-pat
REPO_PATH=C:\path\to\repo-locale
```

Il file `.env` e' incluso in `.gitignore`: non va mai committato, contiene
il PAT in chiaro.

### Opzione B — variabili d'ambiente della shell

```bash
export ORG_URL="https://dev.azure.com/mia-org"
export PROJECT="MioProgetto"
export REPO_ID="mio-repo"
export AZURE_DEVOPS_PAT="********"
export REPO_PATH="/home/utente/progetti/mio-repo"
```

In entrambi i casi: non committare mai il PAT e non scriverlo dentro
`config.py` o altri file tracciati dal repository.

## Esecuzione manuale

```bash
python ingest_loop.py
python review_loop.py
```

Entrambi gli script loggano su stdout ogni decisione presa (ticket, branch,
esito) e non si interrompono se un singolo ticket va in errore: lo loggano
e passano al successivo. Lo stesso storico viene anche scritto in
`history.db` (SQLite locale), leggibile dalla dashboard (sotto) anche a
posteriori.

### Autofix deterministico

Prima che Claude Code tocchi codice per un fix di review, e dopo che ha
finito di implementare un ticket, entrambi gli script lanciano `prettier
--write` e `nx affected:lint --fix` (via `autofix.py`) sul branch corrente,
senza invocare Claude: i problemi di lint/formattazione puro si risolvono
con gli strumenti del progetto, a costo zero, lasciando a Claude solo cio'
che richiede comprensione del codice o del commento.

## Dashboard locale

```bash
python dashboard_server.py
```

Apri `http://127.0.0.1:8765` nel browser. Mostra: stato corrente di ingest
e review (ticket, step, aggiornato ogni 2s), storico persistente delle
decisioni con motivazione (perche' un fix e' meccanico, perche' un ticket e'
bloccato), e due bottoni per avviare i due script senza usare il terminale.

Il server e' solo locale (`127.0.0.1`), non pensato per essere condiviso.
Il lock che impedisce di far girare ingest e review insieme (condividono lo
stesso `REPO_PATH`) e' gestito in memoria dal server: vale solo per i run
avviati dai suoi bottoni, non per lanci manuali da un altro terminale mentre
la dashboard e' aperta — evita di farli in contemporanea.

### Correzione commenti PR dalla dashboard

In **Review PR → Leggi commenti** puoi selezionare piu' thread e creare un
piano unico. La correzione richiede due approvazioni esplicite:

1. **Approva piano e applica modifiche** modifica solo la working copy, senza
   commit o push.
2. Dopo aver controllato le modifiche, **Approva commit e push** esegue test
   mirati, crea un unico commit e pubblica il branch.

Solo al termine del secondo passaggio riuscito l'agente risponde ai thread
selezionati e li marca come risolti su Azure DevOps.

### PR abbandonate e chat posteriore

Quando il loop rileva una PR Azure DevOps in stato `abandoned`, rimuove
`agent:pr-open`, aggiunge `agent:completed` e `agent:abandoned`, e il ticket
compare nella sezione Completed. Dal dettaglio di ogni ticket e' disponibile
la **Chat sul ticket**: ogni richiesta viene salvata e l'agente prepara subito
un piano in sola lettura. La chat non modifica file, non crea commit e non fa
push.

Se PR e branch sono stati eliminati, usa **Riparti da zero** nel pannello chat:
con conferma esplicita rimuove i tag del ciclo precedente, elimina il branch
locale del ticket e avvia ingest per generare un nuovo piano.

### Contesto Graphify nei piani

Prima di generare un piano dalla chat, dalla correzione batch di commenti PR o
da Ingest, il sistema prova a interrogare Graphify sul repository. Usa solo un
grafo esistente (`graphify-out/graph.json`) e il comando `graphify query`, senza
generare o aggiornare il grafo. Se Graphify o il grafo non sono disponibili, il
piano prosegue automaticamente con `Read`, `Grep` e `Glob`.

## Esecuzione schedulata (crontab)

```cron
# ingest: ogni ora
0 * * * * cd /path/to/orchestrator && ORG_URL=... PROJECT=... REPO_ID=... AZURE_DEVOPS_PAT=... REPO_PATH=... /usr/bin/python3 ingest_loop.py >> /var/log/ado-agent/ingest.log 2>&1

# review: ogni 15 minuti
*/15 * * * * cd /path/to/orchestrator && ORG_URL=... PROJECT=... REPO_ID=... AZURE_DEVOPS_PAT=... REPO_PATH=... /usr/bin/python3 review_loop.py >> /var/log/ado-agent/review.log 2>&1
```

In pratica e' preferibile impostare le variabili d'ambiente in un file
caricato dalla shell di cron (es. `. /path/to/env.sh &&`) piuttosto che
scriverle inline nella riga di crontab, per non esporre il PAT in `crontab -l`.

Su Windows si puo' ottenere lo stesso comportamento con due attivita' di Task
Scheduler (`schtasks`) che eseguono `python ingest_loop.py` / `python review_loop.py`
con la stessa periodicita'.
