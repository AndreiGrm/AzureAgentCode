# Azure DevOps Agent Dashboard

Applicazione desktop locale per gestire ticket Azure DevOps con workflow
controllato: legge PBI assegnati, crea branch, pianifica, implementa, verifica
e gestisce pull request. La dashboard mostra stato, storico, costo/token,
notifiche e mappa interattiva di tutte le azioni.

La configurazione operativa avviene dalla pagina **Settings** della dashboard.
Credenziali e impostazioni restano nel profilo Windows dell'utente; non serve
creare o modificare file `.env` manualmente.

## Come funziona oggi

### 1. Ticket e Azure DevOps: zero IA

Quando viene eseguito Ingest, applicazione:

1. legge ticket assegnati nell'iterazione corrente tramite Azure DevOps SDK;
2. mostra PBI/Task nella pagina **Ticket**;
3. mantiene stato tramite tag Azure Boards (`agent:plan-ready`,
   `agent:implemented`, `agent:blocked`, ecc.);
4. crea o riusa branch locali `feature/<id>__<titolo>` oppure
   `bugfix/<id>__<titolo>`;
5. salva eventi, piani, notifiche e qualità in SQLite locale.

Queste fasi usano Azure DevOps SDK, Git e SQLite. Nessun modello IA, nessun
token.

### 2. Analisi: Copilot + Headroom + Graphify

Con provider **Automatic: Copilot analysis / Claude execution**:

- Copilot prepara piani read-only, scompone Epic e pianifica correzioni PR;
- Headroom instrada Copilot tramite proxy e riduce contesto;
- Graphify interroga `graphify-out/graph.json` prima delle ricerche estese;
- se Graphify non è disponibile, Copilot usa Read, Grep e Glob.

Graphify viene usato solo nella fase di analisi. Dopo piano approvato, non
viene interrogato di nuovo inutilmente.

### 3. Implementazione: Claude

Claude resta provider per attività che modificano repository:

- implementazione ticket;
- fix richiesti dopo review;
- classificazione e applicazione commenti PR;
- commit, push e synthetic review.

Usa piano approvato, file repository e tool consentiti. Risposte naturali sono
richieste in stile Caveman Ultra: concise, ma con decisioni, rischi, file e
verifiche. Output macchina, come JSON e marker di stato, restano invariati.

### 4. Qualità e pull request: zero IA

Dopo implementazione:

1. vengono eseguiti formatter e lint deterministici;
2. dashboard rileva ed esegue script repository: test, lint, type-check,
   build, privilegiando comandi `:affected`;
3. PR viene bloccata finché verifiche del commit corrente non passano;
4. Azure DevOps SDK crea PR o abilita auto-complete solo secondo policy scelta.

### Mappa Workflow

Pagina **Workflow** contiene:

- pipeline sintetica con frecce colorate: strumenti deterministici, Copilot +
  Headroom + Graphify, contesto già approvato, Claude;
- mappa zoomabile/pannabile con ogni azione applicativa;
- riga `Uses:` su ogni card: mostra strumenti reali coinvolti;
- dettaglio e feedback persistente per ogni nodo.

## Prima configurazione

Apri applicazione, vai su **Settings**, completa:

- organizzazione, progetto, team e repository Azure DevOps;
- path repository Git locale;
- branch base;
- PAT Azure DevOps;
- provider agente e modello.

Configurazione consigliata:

- **Agent provider:** `Automatic: Copilot analysis / Claude execution`
- **Optimize agent context with Headroom:** `Enabled`

Servono:

- Git;
- Python 3.10+;
- repository target già clonato;
- Azure DevOps PAT con permessi Work Items e Code read/write;
- GitHub Copilot CLI autenticato;
- Claude Code autenticato per fasi di scrittura;
- Headroom proxy attivo per misurare/comprimere traffico Copilot;
- Graphify e un grafo esistente, opzionali ma consigliati.

## Avvio sviluppo

Da repository applicativo:

```powershell
pip install -r requirements.txt
python desktop_app.py
```

App apre finestra desktop e dashboard locale su `http://127.0.0.1:8765`.

Per debug browser:

```powershell
python dashboard_server.py
```

Per avviare singoli loop dopo configurazione dashboard:

```powershell
python ingest_loop.py
python review_loop.py
```

Non eseguire Ingest e Review contemporaneamente sulla stessa copia repository:
condividono working tree Git.

## Struttura

| File | Ruolo |
|---|---|
| `desktop_app.py` | Finestra desktop e server locale |
| `dashboard_server.py` | API FastAPI dashboard |
| `static/` | Interfaccia dashboard e mappa workflow |
| `ingest_loop.py` | Ticket, piano, branch, implementazione |
| `review_loop.py` | Commenti PR, review e fix |
| `claude_runner.py` | Routing Claude, Copilot e Headroom |
| `workflow_context.py` | Contesto Graphify e output Caveman Ultra |
| `graphify_context.py` | Query Graphify con fallback esplicito |
| `autofix.py` | Formatter/lint deterministici |
| `quality_checks.py` | Rilevamento/esecuzione verifiche repository |
| `history.py` | SQLite: run, eventi, qualità, feedback |
| `state.py` | Tag e stato Azure Boards |
| `config.py` | Configurazione locale dashboard |

## Sicurezza operativa

- PAT non viene restituito in chiaro alla UI.
- Dashboard ascolta solo su `127.0.0.1`.
- Nessuna PR prima delle verifiche tecniche richieste.
- Piano richiede approvazione prima dell'implementazione.
- Batch fix PR richiede approvazione prima di commit/push.
- Headroom e Graphify non sostituiscono verifica dei file sorgente.
