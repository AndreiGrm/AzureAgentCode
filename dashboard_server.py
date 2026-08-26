"""Dashboard locale di controllo/osservabilità per ingest_loop.py e
review_loop.py: mostra lo storico delle decisioni (con motivazione), lo
stato corrente, e permette di avviare i due script con un bottone.

Solo per uso locale: il server ascolta esclusivamente su 127.0.0.1, non e'
pensato per essere raggiunto da altre macchine. Lancio: `python
dashboard_server.py`, poi apri http://127.0.0.1:8765 nel browser.

Nota: il lock di concorrenza (un solo run attivo tra ingest e review, dato
che condividono la stessa working copy git) e' gestito qui in memoria, ed
e' quindi efficace solo per i run avviati DA questa dashboard. Se lanci
ingest_loop.py o review_loop.py manualmente da un altro terminale mentre la
dashboard e' aperta, il lock non se ne accorge: evita di farlo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from azure.devops.v7_1.git.models import GitPullRequest, GitPullRequestCompletionOptions, ResourceRef
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import history
from app_version import APP_VERSION, GITHUB_REPOSITORY
import quality_checks
import review_loop
import state
from claude_runner import run_claude
from config import ConfigError, get_connection, get_settings, load_config, update_settings, SETTINGS_SCHEMA
from graphify_context import get_graphify_context
from runtime_paths import data_dir, resource_dir

WORKFLOW_DIR = data_dir()
STATIC_DIR = resource_dir() / "static"
LOGS_DIR = data_dir() / "logs"

SCRIPTS = {
    "ingest": "ingest_loop.py",
    "review": "review_loop.py",
}

# Unico run attivo alla volta: {"script": str, "process": Popen, "run_id": int} o vuoto.
_active: dict = {"script": None, "process": None, "run_id": None}

AUTO_INGEST_INTERVAL = timedelta(minutes=5)
_automatic_ingest_schedule: dict = {
    "last_check": None,
    "next_check": None,
    "outcome": "not started",
}

# Endpoint richiamati dal polling automatico della dashboard (ogni POLL_MS in
# static/app.js): loggarli ad ogni chiamata riempie il terminale di rumore
# senza informazione utile. Le richieste non-2xx passano comunque, cosi'
# un errore di polling resta visibile.
_QUIET_POLL_PATHS = {"/api/status", "/api/tickets", "/api/history"}


class _QuietPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client_addr, method, full_path, _http_version, status_code = record.args
        except (TypeError, ValueError):
            return True
        path = full_path.split("?", 1)[0]
        if method == "GET" and path in _QUIET_POLL_PATHS and str(status_code).startswith("2"):
            return False
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("uvicorn.access").addFilter(_QuietPollingFilter())
    LOGS_DIR.mkdir(exist_ok=True)
    history.mark_stale_runs_interrupted()
    _automatic_ingest_schedule.update(
        last_check=None,
        next_check=_schedule_timestamp(datetime.now(timezone.utc) + AUTO_INGEST_INTERVAL),
        outcome="waiting for first scheduled check",
    )
    scheduler_task = asyncio.create_task(
        _automatic_ingest_loop(), name="automatic-ingest-scheduler"
    )
    app.state.automatic_ingest_scheduler_task = scheduler_task
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Azure DevOps Agent Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ConfigError)
async def _config_error_handler(_request: Request, exc: ConfigError) -> JSONResponse:
    """Se manca ancora una variabile obbligatoria (es. prima configurazione
    dalla pagina Impostazioni), risponde con un 400 leggibile invece di far
    esplodere l'endpoint con un 500 generico."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Termina il processo E i suoi figli (es. il claude.exe che
    claude_agent_sdk lancia sotto ingest_loop.py/review_loop.py): un
    .terminate() sul solo processo Python li lascerebbe orfani e in
    esecuzione, continuando a modificare codice/consumare token."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, text=True, check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _reconcile_active_process() -> None:
    """Se il processo che stavamo tracciando e' terminato, libera il lock e,
    se serve, chiude anche la riga 'running' nel DB (caso di crash duro in
    cui lo script non ha potuto eseguire il proprio finally)."""
    proc = _active["process"]
    if proc is None:
        return
    if proc.poll() is None:
        return

    run_id = _active["run_id"]
    if run_id is not None:
        run = history.get_run(run_id)
        if run and run["status"] == "running":
            history.finish_run(run_id, "success" if proc.returncode == 0 else "error")

    _active["script"] = None
    _active["process"] = None
    _active["run_id"] = None


def _schedule_timestamp(value: datetime) -> str:
    return value.isoformat()


def _active_ingest_or_review() -> str | None:
    """Restituisce il run ingest/review attivo, anche se non l'ha avviato il server."""
    _reconcile_active_process()
    if _active["script"] in SCRIPTS and _active["process"] is not None:
        return _active["script"]
    for script in SCRIPTS:
        if history.get_active_run(script) is not None:
            return script
    return None


def _perform_automatic_ingest_check() -> None:
    """Avvia ingest solo quando ingest e review non stanno usando la working copy."""
    checked_at = datetime.now(timezone.utc)
    _automatic_ingest_schedule["last_check"] = _schedule_timestamp(checked_at)

    active_script = _active_ingest_or_review()
    if active_script is not None:
        _automatic_ingest_schedule["outcome"] = f"skipped: {active_script} run is active"
        return

    try:
        run = _start_script_process("ingest")
    except HTTPException as exc:
        # Un avvio manuale puo' vincere la gara fra il controllo e il Popen.
        _automatic_ingest_schedule["outcome"] = f"skipped: {exc.detail}"
    except Exception:
        logging.exception("Automatic ingest check failed")
        _automatic_ingest_schedule["outcome"] = "error: unable to start ingest"
    else:
        _automatic_ingest_schedule["outcome"] = f"started ingest run {run['run_id']}"


async def _automatic_ingest_loop() -> None:
    """Esegue i controlli periodici; il task viene cancellato dal lifespan."""
    while True:
        await asyncio.sleep(AUTO_INGEST_INTERVAL.total_seconds())
        _perform_automatic_ingest_check()
        _automatic_ingest_schedule["next_check"] = _schedule_timestamp(
            datetime.now(timezone.utc) + AUTO_INGEST_INTERVAL
        )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def api_config() -> dict:
    cfg = load_config()
    return {"org_url": cfg.org_url, "project": cfg.project, "repo_id": cfg.repo_id}


class SettingsUpdate(BaseModel):
    values: dict[str, str]


def _optional_positive_int(value: str, key: str) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(400, detail=f"Il campo '{key}' deve essere un numero intero positivo") from exc
    if parsed <= 0:
        raise HTTPException(400, detail=f"Il campo '{key}' deve essere maggiore di zero")
    return parsed


def _token_budget_from_environment() -> int | None:
    return _optional_positive_int(os.environ.get("AGENT_TOKEN_BUDGET", ""), "AGENT_TOKEN_BUDGET")


@app.get("/api/settings")
def api_get_settings() -> list[dict]:
    return get_settings()


def _version_key(version: str) -> tuple[int, ...]:
    normalized = version.strip().lstrip("vV").split("-", 1)[0]
    if not normalized:
        raise ValueError("Versione vuota")
    return tuple(int(part) for part in normalized.split("."))


@app.get("/api/app-update")
def api_app_update() -> dict:
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
    request = UrlRequest(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Azure-DevOps-Agent-Dashboard",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            release = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {
                "current_version": APP_VERSION,
                "latest_version": None,
                "update_available": False,
                "release_url": "",
                "release_name": "",
            }
        raise HTTPException(502, detail=f"GitHub ha risposto con HTTP {exc.code}") from exc
    except URLError as exc:
        raise HTTPException(503, detail=f"Impossibile contattare GitHub: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(502, detail="GitHub ha restituito una release non valida") from exc

    latest_version = str(release.get("tag_name", "")).strip()
    if not latest_version:
        raise HTTPException(502, detail="La release GitHub non contiene un tag versione")
    try:
        update_available = _version_key(latest_version) > _version_key(APP_VERSION)
    except ValueError as exc:
        raise HTTPException(502, detail=f"Tag release non valido: {latest_version}") from exc
    return {
        "current_version": APP_VERSION,
        "latest_version": latest_version,
        "update_available": update_available,
        "release_url": release.get("html_url", ""),
        "release_name": release.get("name") or latest_version,
    }


@app.get("/api/token-budget")
def api_get_token_budget() -> dict:
    return history.get_token_budget_status(_token_budget_from_environment())


@app.post("/api/settings")
def api_update_settings(body: SettingsUpdate) -> list[dict]:
    schema = {key: (required, secret) for key, _label, required, secret in SETTINGS_SCHEMA}

    updates: dict[str, str] = {}
    for key, raw_value in body.values.items():
        if key not in schema:
            continue
        value = raw_value.strip()
        required, secret = schema[key]
        if secret and not value:
            # Campo segreto lasciato vuoto: non toccare il PAT esistente.
            continue
        if required and not value:
            raise HTTPException(400, detail=f"Il campo '{key}' e' obbligatorio")
        if key in {"AGENT_MAX_OUTPUT_TOKENS", "AGENT_TOKEN_BUDGET"}:
            _optional_positive_int(value, key)
        if key == "AGENT_PROVIDER" and value not in {"claude_sdk", "command", "copilot_cli"}:
            raise HTTPException(
                400,
                detail="AGENT_PROVIDER deve essere 'claude_sdk', 'command' oppure 'copilot_cli'",
            )
        updates[key] = value

    if updates:
        update_settings(updates)
    return get_settings()


@app.get("/api/tickets")
def api_tickets(limit: int = 100) -> list[dict]:
    return history.get_tickets(limit=limit)


@app.get("/api/dashboard")
def api_dashboard(
    completed_from: date | None = None,
    completed_to: date | None = None,
    completion_action: str = "all",
    work_item_type: str = "all",
    search: str = "",
) -> dict:
    if completed_from and completed_to and completed_from > completed_to:
        raise HTTPException(400, detail="La data iniziale deve precedere la data finale")

    completed_items = history.get_completed_work_items(
        str(completed_from) if completed_from else None,
        str(completed_to) if completed_to else None,
    )
    if completion_action != "all":
        allowed_actions = {"pr_completed", "closed"}
        if completion_action not in allowed_actions:
            raise HTTPException(400, detail="Filtro stato completamento non valido")
        completed_items = [
            item for item in completed_items if item["completed_action"] == completion_action
        ]
    if not completed_items:
        return {
            "filters": {
                "completed_from": str(completed_from) if completed_from else None,
                "completed_to": str(completed_to) if completed_to else None,
            },
            "summary": {"completed_count": 0, "story_points": 0, "cost_usd": 0.0, "total_tokens": 0},
            "items": [],
        }

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    work_items = wit_client.get_work_items(
        ids=[item["work_item_id"] for item in completed_items],
        project=cfg.project,
        fields=["System.Title", "System.WorkItemType", "Microsoft.VSTS.Scheduling.StoryPoints"],
    )
    fields_by_id = {item.id: item.fields for item in work_items}
    normalized_search = search.strip().lower()
    items = []
    for item in completed_items:
        fields = fields_by_id.get(item["work_item_id"], {})
        story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints")
        item_type = fields.get("System.WorkItemType", "")
        title = fields.get("System.Title", "(titolo non disponibile)")
        if work_item_type != "all" and item_type.lower() != work_item_type.lower():
            continue
        if normalized_search and normalized_search not in f"{item['work_item_id']} {title}".lower():
            continue
        items.append({
            **item,
            "title": title,
            "work_item_type": item_type,
            "story_points": story_points,
        })

    return {
        "filters": {
            "completed_from": str(completed_from) if completed_from else None,
            "completed_to": str(completed_to) if completed_to else None,
        },
        "summary": {
            "completed_count": len(items),
            "story_points": sum(float(item["story_points"] or 0) for item in items),
            "cost_usd": sum(item["cost_usd"] for item in items),
            "total_tokens": sum(item["total_tokens"] for item in items),
        },
        "items": items,
    }


@app.get("/api/history")
def api_history(limit: int = 200, work_item_id: int | None = None) -> list[dict]:
    return history.get_history(limit=limit, work_item_id=work_item_id)


@app.get("/api/notifications")
def api_notifications(limit: int = 50) -> dict:
    notification_actions = {
        "plan_ready": "Piano pronto da approvare",
        "implemented": "PBI pronto per le verifiche tecniche",
        "quality_passed": "Verifiche tecniche superate",
        "quality_failed": "Verifiche tecniche da risolvere",
        "pr_opened": "PR aperta",
        "blocked": "PBI bloccato",
        "error": "Errore nel flusso PBI",
        "pr_completed": "PR completata",
    }
    for event in history.get_history(limit=500):
        label = notification_actions.get(event["action"])
        if label is not None:
            history.add_notification(
                f"event:{event['id']}",
                event["action"],
                f"{label}: {event['message']}",
                event["work_item_id"],
            )
    notifications = history.get_notifications(limit=limit)
    return {
        "items": notifications,
        "unread_count": sum(notification["read_at"] is None for notification in notifications),
    }


@app.get("/api/attention")
def api_attention() -> list[dict]:
    reasons = {
        "plan_ready": "Piano da approvare",
        "implemented": "Esegui le verifiche tecniche",
        "quality_failed": "Verifiche tecniche da risolvere",
        "quality_passed": "Pronto per creare la PR",
        "pr_opened": "PR aperta: controlla review e commenti",
        "blocked": "PBI bloccato",
        "error": "Errore da analizzare",
    }
    items = []
    for ticket in history.get_tickets(limit=200):
        reason = reasons.get(ticket["action"])
        if reason is not None:
            items.append({**ticket, "attention_reason": reason})
    return items


@app.post("/api/notifications/{notification_id}/read")
def api_mark_notification_read(notification_id: int) -> dict:
    history.mark_notification_read(notification_id)
    return {"notification_id": notification_id}


@app.get("/api/runs")
def api_runs(limit: int = 20) -> list[dict]:
    return history.get_runs(limit=limit)


@app.get("/api/status")
def api_status() -> dict:
    _reconcile_active_process()
    status = {}
    for script in SCRIPTS:
        run = history.get_active_run(script)
        latest_event = history.get_latest_event(run["id"]) if run else None
        status[script] = {
            "active": run is not None,
            "run": run,
            "latest_event": latest_event,
        }
    return status


@app.get("/api/automatic-ingest/status")
def api_automatic_ingest_status() -> dict:
    """Espone lo stato del pianificatore server-side di ingest."""
    return {
        "interval_seconds": int(AUTO_INGEST_INTERVAL.total_seconds()),
        **_automatic_ingest_schedule,
    }


@app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def api_run_log(run_id: int) -> str:
    run = history.get_run(run_id)
    if run is None:
        raise HTTPException(404, detail="Run non trovata")
    log_path = LOGS_DIR / f"{run['script']}_{run_id}.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def _start_script_process(script: str, extra_env: dict | None = None) -> dict:
    """Avvia SCRIPTS[script] come sottoprocesso, sotto il lock _active.

    Il run_id lo crea il server, PRIMA di avviare il processo: cosi' il
    nome del file di log e' noto da subito (niente rename di un file che
    il figlio ha ancora aperto, che su Windows fallirebbe) e non serve
    nessun polling per scoprirlo. Lo script figlio, tramite la variabile
    d'ambiente DASHBOARD_RUN_ID, riusa questo id invece di crearne un altro.
    """
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo: {_active['script']}")

    run_id = history.start_run(script)

    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"{script}_{run_id}.log"
    log_file = open(log_path, "w", encoding="utf-8")

    env = {**os.environ, "DASHBOARD_RUN_ID": str(run_id), **(extra_env or {})}
    # Az CLI (usata da Claude Code per `az repos pr create`) si autentica in
    # modo non interattivo con questa variabile: senza, userebbe qualunque
    # identita' sia gia' loggata sulla macchina (az devops login), che puo'
    # non coincidere col PAT impostato nella pagina Impostazioni.
    if "AZURE_DEVOPS_PAT" in env:
        env.setdefault("AZURE_DEVOPS_EXT_PAT", env["AZURE_DEVOPS_PAT"])
    command = (
        [sys.executable, "--worker", script]
        if getattr(sys, "frozen", False)
        else [sys.executable, SCRIPTS[script]]
    )
    process = subprocess.Popen(
        command,
        cwd=WORKFLOW_DIR, stdout=log_file, stderr=subprocess.STDOUT, env=env,
    )
    log_file.close()

    _active["script"] = script
    _active["process"] = process
    _active["run_id"] = run_id
    return {"script": script, "run_id": run_id}


@app.post("/api/run/{script}")
def api_trigger_run(script: str) -> dict:
    if script not in SCRIPTS:
        raise HTTPException(404, detail=f"Script sconosciuto: {script}")
    return _start_script_process(script)


@app.post("/api/stop/{script}")
def api_stop_run(script: str) -> dict:
    if script not in SCRIPTS:
        raise HTTPException(404, detail=f"Script sconosciuto: {script}")

    _reconcile_active_process()
    if _active["script"] != script or _active["process"] is None:
        raise HTTPException(409, detail=f"Nessun run attivo per '{script}'")

    proc = _active["process"]
    run_id = _active["run_id"]
    latest_event = history.get_latest_event(run_id) if run_id else None
    work_item_id = latest_event["work_item_id"] if latest_event else None

    _kill_process_tree(proc)

    if run_id is not None:
        history.finish_run(run_id, "stopped")
        history.log_event(
            run_id, "stopped", f"Run {script} interrotto manualmente dalla dashboard",
            level="warning", work_item_id=work_item_id,
        )

    _active["script"] = None
    _active["process"] = None
    _active["run_id"] = None

    return {"script": script, "run_id": run_id, "work_item_id": work_item_id}


def _log_manual_action(action: str, message: str, *, work_item_id: int, **kwargs) -> None:
    """Le azioni scattate a mano dalla dashboard (blocca, crea PR) non
    fanno parte di un run di ingest/review, ma vanno comunque nello storico
    del ticket: altrimenti il "mini riassunto" avrebbe un buco proprio sui
    passaggi che l'utente ha fatto lui stesso."""
    run_id = history.start_run("dashboard")
    history.log_event(run_id, action, message, work_item_id=work_item_id, **kwargs)
    history.finish_run(run_id, "success")


@app.post("/api/block/{work_item_id}")
def api_block_work_item(work_item_id: int) -> dict:
    """Tagga il work item agent:blocked, cosi' ingest/review lo saltano nei
    run successivi: usato dalla dashboard per fermare la lavorazione
    automatica di un ticket specifico (es. dopo uno stop manuale)."""
    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
    _log_manual_action(
        "blocked", f"Work item #{work_item_id}: bloccato manualmente dalla dashboard",
        work_item_id=work_item_id, level="warning",
    )
    return {"work_item_id": work_item_id, "tags": sorted(state.get_tags(wit_client, work_item_id))}


@app.post("/api/close/{work_item_id}")
def api_close_work_item(work_item_id: int) -> dict:
    """Chiude manualmente un PBI dalla dashboard: lo sposta nella sezione
    "Completed" e lo tagga anche come bloccato, cosi' ingest/review non lo
    riprendono anche se rientrasse per qualche motivo nella WIQL."""
    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_COMPLETED)
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
    _log_manual_action(
        "closed", f"Work item #{work_item_id}: chiuso manualmente dalla dashboard",
        work_item_id=work_item_id, level="warning",
    )
    return {"work_item_id": work_item_id, "tags": sorted(state.get_tags(wit_client, work_item_id))}


@app.post("/api/reopen/{work_item_id}")
def api_reopen_work_item(work_item_id: int) -> dict:
    """Riapre un PBI dalla sezione Completed: rimuove sia il tag di
    completamento sia il blocco che veniva aggiunto in automatico alla
    chiusura, cosi' ingest/review possono ricominciare a seguirlo."""
    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_COMPLETED)
    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_ABANDONED)
    _log_manual_action(
        "reopened", f"Work item #{work_item_id}: riaperto manualmente dalla dashboard",
        work_item_id=work_item_id,
    )
    return {"work_item_id": work_item_id, "tags": sorted(state.get_tags(wit_client, work_item_id))}


@app.post("/api/restart-from-scratch/{work_item_id}")
def api_restart_work_item_from_scratch(work_item_id: int) -> dict:
    """Azzera il ciclo dell'agente per un ticket, ad esempio dopo la
    cancellazione di PR e branch, e avvia una nuova generazione del piano."""
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    branch = history.get_branch_for_work_item(work_item_id)
    deleted_branch = None
    if branch and branch != cfg.base_branch:
        try:
            branch_exists = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=cfg.repo_path, capture_output=True, text=True,
            ).returncode == 0
            if branch_exists:
                subprocess.run(
                    ["git", "fetch", "origin", cfg.base_branch],
                    cwd=cfg.repo_path, check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "checkout", cfg.base_branch],
                    cwd=cfg.repo_path, check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=cfg.repo_path, check=True, capture_output=True, text=True,
                )
                deleted_branch = branch
        except subprocess.CalledProcessError as exc:
            raise HTTPException(
                409,
                detail=f"Impossibile eliminare il branch locale {branch}: {exc.stderr}",
            )

    for tag in (
        state.TAG_BRANCH_CREATED,
        state.TAG_IMPLEMENTED,
        state.TAG_PR_OPEN,
        state.TAG_BLOCKED,
        state.TAG_PLAN_READY,
        state.TAG_PLAN_APPROVED,
        state.TAG_FIX_REQUESTED,
        state.TAG_COMPLETED,
        state.TAG_ABANDONED,
    ):
        state.remove_tag(wit_client, cfg.project, work_item_id, tag)
    _log_manual_action(
        "restart_requested",
        f"Work item #{work_item_id}: ciclo azzerato e nuova pianificazione richiesta",
        work_item_id=work_item_id, branch=branch,
    )
    run = _start_script_process("ingest")
    return {
        "work_item_id": work_item_id,
        "deleted_branch": deleted_branch,
        "tags": sorted(state.get_tags(wit_client, work_item_id)),
        "run": run,
    }


@app.post("/api/create-pr/{work_item_id}")
def api_create_pr(work_item_id: int) -> dict:
    """Apre la PR per un ticket gia' implementato (tag agent:implemented).

    Azione deterministica via SDK (source branch -> target branch, nessun
    ragionamento richiesto): non c'e' motivo di far passare questo passo da
    Claude, e cosi' il codice pushato resta revisionabile dall'utente prima
    che diventi visibile ai reviewer come PR.
    """
    return _create_pr(work_item_id, auto_complete=False)


@app.post("/api/create-pr/{work_item_id}/autocomplete")
def api_create_pr_with_autocomplete(work_item_id: int) -> dict:
    return _create_pr(work_item_id, auto_complete=True)


def _create_pr(work_item_id: int, *, auto_complete: bool) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()

    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_IMPLEMENTED not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non e' nello stato 'implemented'")

    branch = history.get_branch_for_work_item(work_item_id)
    if branch is None:
        raise HTTPException(409, detail=f"Nessun branch noto per il work item #{work_item_id}")

    try:
        review_loop.checkout_pr_branch(cfg, branch)
        commit_sha = quality_checks.current_commit(cfg)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile preparare il branch {branch}: {exc.stderr}")

    quality_run = history.get_latest_quality_run(work_item_id)
    if quality_run is None:
        raise HTTPException(409, detail="Esegui prima le verifiche tecniche obbligatorie")
    if quality_run["status"] != "passed":
        raise HTTPException(409, detail="Le verifiche tecniche non sono tutte superate")
    if quality_run["commit_sha"] != commit_sha:
        raise HTTPException(409, detail="Il branch e' cambiato dopo le verifiche: eseguile di nuovo")

    item = wit_client.get_work_item(work_item_id, fields=["System.Title"])
    title = item.fields.get("System.Title", f"Work item #{work_item_id}")

    pr_to_create = GitPullRequest(
        source_ref_name=f"refs/heads/{branch}",
        target_ref_name=f"refs/heads/{cfg.base_branch}",
        title=f"#{work_item_id}: {title}",
        work_item_refs=[ResourceRef(id=str(work_item_id))],
    )
    created = git_client.create_pull_request(pr_to_create, cfg.repo_id, project=cfg.project)
    if auto_complete:
        created = git_client.update_pull_request(
            GitPullRequest(
                auto_complete_set_by=created.created_by,
                completion_options=GitPullRequestCompletionOptions(),
            ),
            cfg.repo_id,
            created.pull_request_id,
            project=cfg.project,
        )

    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_IMPLEMENTED)
    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_ABANDONED)
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_PR_OPEN)
    state.add_note(wit_client, cfg.project, work_item_id, f"PR #{created.pull_request_id} aperta manualmente dalla dashboard")

    pr_url = f"{cfg.org_url}/{cfg.project}/_git/{cfg.repo_id}/pullrequest/{created.pull_request_id}"
    _log_manual_action(
        "pr_opened",
        f"Work item #{work_item_id}: PR aperta manualmente"
        f"{' con auto-completamento' if auto_complete else ''} ({pr_url})",
        work_item_id=work_item_id, branch=branch, pr_id=created.pull_request_id,
    )
    history.add_notification(
        f"pr-opened:{created.pull_request_id}",
        "pr-opened",
        f"PR #{created.pull_request_id} pronta per il ticket #{work_item_id}",
        work_item_id,
    )
    return {
        "work_item_id": work_item_id,
        "pull_request_id": created.pull_request_id,
        "url": pr_url,
        "auto_complete": auto_complete,
    }


class TextBody(BaseModel):
    text: str


class ThreadBatchRequest(BaseModel):
    thread_ids: list[int]
    planning_notes: dict[int, str] = Field(default_factory=dict)


@app.get("/api/work-item/{work_item_id}")
def api_get_work_item(work_item_id: int) -> dict:
    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    item = wit_client.get_work_item(
        work_item_id,
        fields=[
            "System.Title", "System.Description", "Microsoft.VSTS.Common.AcceptanceCriteria",
            "Microsoft.VSTS.Scheduling.StoryPoints",
        ],
    )
    fields = item.fields
    raw_description = fields.get("System.Description", "") or ""
    raw_acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
    return {
        "work_item_id": work_item_id,
        "title": fields.get("System.Title", ""),
        "description": state.html_to_plain_text(raw_description),
        "acceptance_criteria": state.html_to_plain_text(raw_acceptance),
        "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        "figma_urls": state.extract_figma_urls(raw_description + raw_acceptance),
    }


@app.get("/api/usage/{work_item_id}")
def api_get_usage(work_item_id: int) -> dict:
    return history.get_usage_totals(work_item_id)


@app.get("/api/quality/{work_item_id}")
def api_get_quality(work_item_id: int) -> dict | None:
    return history.get_latest_quality_run(work_item_id)


@app.post("/api/quality/{work_item_id}")
def api_run_quality(work_item_id: int) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_IMPLEMENTED not in tags:
        raise HTTPException(409, detail="Le verifiche sono disponibili dopo l'implementazione")

    branch = history.get_branch_for_work_item(work_item_id)
    if branch is None:
        raise HTTPException(409, detail=f"Nessun branch noto per il work item #{work_item_id}")
    try:
        review_loop.checkout_pr_branch(cfg, branch)
        commit_sha, checks = quality_checks.run_quality_checks(cfg)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile preparare il branch {branch}: {exc.stderr}")

    status = "passed" if checks and all(check["status"] == "passed" for check in checks) else (
        "failed" if checks else "unavailable"
    )
    history.record_quality_run(work_item_id, branch, commit_sha, status, checks)
    action = "quality_passed" if status == "passed" else "quality_failed"
    message = (
        f"Work item #{work_item_id}: verifiche tecniche superate"
        if status == "passed"
        else f"Work item #{work_item_id}: verifiche tecniche non superate o non disponibili"
    )
    _log_manual_action(action, message, work_item_id=work_item_id, branch=branch)
    history.add_notification(
        f"quality:{work_item_id}:{commit_sha}:{status}",
        action,
        message,
        work_item_id,
    )
    return history.get_latest_quality_run(work_item_id)


@app.get("/api/ticket-chat/{work_item_id}")
def api_get_ticket_chat(work_item_id: int) -> list[dict]:
    return history.get_ticket_chat_messages(work_item_id)


@app.post("/api/ticket-chat/{work_item_id}")
def api_send_ticket_chat(work_item_id: int, body: TextBody) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, detail="Il messaggio non puo' essere vuoto")

    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    item = wit_client.get_work_item(work_item_id, fields=["System.Title", "System.Description"])
    branch = history.get_branch_for_work_item(work_item_id)
    if branch is None:
        raise HTTPException(409, detail=f"Nessun branch noto per il work item #{work_item_id}")
    try:
        review_loop.checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile fare checkout del branch {branch}: {exc.stderr}")

    history.add_ticket_chat_message(work_item_id, "user", text)
    conversation = history.get_ticket_chat_messages(work_item_id)
    transcript = "\n\n".join(
        f"{'Utente' if message['role'] == 'user' else 'Agente'}: {message['content']}"
        for message in conversation
    )
    title = item.fields.get("System.Title", f"Work item #{work_item_id}")
    description = state.html_to_plain_text(item.fields.get("System.Description", "") or "")
    graphify_section = get_graphify_context(cfg.repo_path, text)
    prompt = (
        f"Stai assistendo l'utente sul work item #{work_item_id}: {title}.\n"
        f"Descrizione:\n{description}\n\nBranch locale: '{branch}'.\n\n"
        f"Conversazione:\n{transcript}\n\n"
        f"{graphify_section}\n\n"
        "Analizza la nuova richiesta e rispondi in italiano con un piano concreto dei prossimi passi, "
        "rischi e verifiche. Non modificare file, non fare commit e non fare push."
    )
    run_id = history.start_run("dashboard")
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Grep", "Glob"],
        work_item_id=work_item_id, run_id=run_id,
    )
    history.add_ticket_chat_message(work_item_id, "assistant", result.output)
    history.log_event(
        run_id, "ticket_chat_planned",
        f"Ticket #{work_item_id}: piano generato dalla chat posteriore",
        work_item_id=work_item_id, branch=branch, detail=result.output,
    )
    history.finish_run(run_id, "success")
    return {"messages": history.get_ticket_chat_messages(work_item_id)}


@app.post("/api/review/{work_item_id}")
def api_trigger_review_for_ticket(work_item_id: int) -> dict:
    """Lancia review_loop.py limitato a un solo work item (REVIEW_WORK_ITEM_ID),
    invece del giro su tutti i ticket con PR aperta: e' il trigger dal
    dettaglio di un singolo PBI, non il bottone globale (rimosso)."""
    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_PR_OPEN not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non ha una PR aperta da revisionare")

    return _start_script_process("review", extra_env={"REVIEW_WORK_ITEM_ID": str(work_item_id)})


@app.get("/api/plan/{work_item_id}")
def api_get_plan(work_item_id: int) -> dict:
    plan = history.get_plan(work_item_id)
    if plan is None:
        raise HTTPException(404, detail=f"Nessun piano trovato per il work item #{work_item_id}")
    return plan


@app.patch("/api/plan/{work_item_id}")
def api_update_plan(work_item_id: int, body: TextBody) -> dict:
    plan = history.get_plan(work_item_id)
    if plan is None:
        raise HTTPException(404, detail=f"Nessun piano trovato per il work item #{work_item_id}")
    if plan["approved_at"] is not None:
        raise HTTPException(409, detail="Il piano e' gia' stato approvato, non e' piu' modificabile")
    history.update_plan_text(work_item_id, body.text)
    return history.get_plan(work_item_id)


@app.post("/api/plan/{work_item_id}/approve")
def api_approve_plan(work_item_id: int) -> dict:
    plan = history.get_plan(work_item_id)
    if plan is None:
        raise HTTPException(404, detail=f"Nessun piano trovato per il work item #{work_item_id}")

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()

    history.approve_plan(work_item_id)
    state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_PLAN_READY)
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_PLAN_APPROVED)
    _log_manual_action(
        "plan_approved", f"Work item #{work_item_id}: piano approvato dall'utente",
        work_item_id=work_item_id,
    )

    # Best-effort: parte subito invece di aspettare il prossimo giro
    # periodico di ingest_loop.py. Se ingest e' gia' occupato con un altro
    # ticket, va bene lo stesso: lo riprendera' al prossimo giro.
    try:
        api_trigger_run("ingest")
    except HTTPException:
        pass

    return history.get_plan(work_item_id)


@app.post("/api/correction/{work_item_id}")
def api_send_correction(work_item_id: int, body: TextBody) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, detail="Testo della correzione vuoto")

    run = history.get_in_progress_run_for_work_item(work_item_id)
    if run is None:
        raise HTTPException(
            409,
            detail=f"Nessuna lavorazione in corso per il work item #{work_item_id}: "
                   "la correzione non verrebbe consegnata",
        )

    correction_id = history.add_correction(run["id"], work_item_id, text)
    history.log_event(
        run["id"], "correction_sent", f"Work item #{work_item_id}: correzione inviata dall'utente",
        work_item_id=work_item_id, detail=text,
    )
    return {"work_item_id": work_item_id, "run_id": run["id"], "correction_id": correction_id}


@app.post("/api/fix/{work_item_id}")
def api_request_fix(work_item_id: int, body: TextBody) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, detail="Testo della correzione vuoto")

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_IMPLEMENTED not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non e' in fase di verifica")

    fix_id = history.add_fix(work_item_id, text)
    state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_FIX_REQUESTED)
    _log_manual_action(
        "fix_requested", f"Work item #{work_item_id}: correzione richiesta dall'utente",
        work_item_id=work_item_id, detail=text,
    )

    try:
        api_trigger_run("ingest")
    except HTTPException:
        pass

    return {"work_item_id": work_item_id, "fix_id": fix_id}


@app.post("/api/review-code/{work_item_id}")
def api_review_code(work_item_id: int) -> dict:
    """Self-review manuale: una sessione Claude indipendente rivede il diff
    implementato (sola lettura/esecuzione, niente Edit) e produce un report.

    Richiede che nessun run ingest/review sia attivo: fa git checkout sulla
    stessa working copy condivisa, e farlo mentre ingest_loop.py/
    review_loop.py stanno operando rischierebbe di corromperne lo stato.
    """
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(
            409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce"
        )

    cfg = load_config()
    wit_client = get_connection(cfg).clients.get_work_item_tracking_client()
    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_IMPLEMENTED not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non e' in fase di verifica")

    branch = history.get_branch_for_work_item(work_item_id)
    if branch is None:
        raise HTTPException(409, detail=f"Nessun branch noto per il work item #{work_item_id}")

    subprocess.run(["git", "fetch", "origin", branch], cwd=cfg.repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", branch], cwd=cfg.repo_path, check=True, capture_output=True, text=True)

    item = wit_client.get_work_item(work_item_id, fields=["System.Title"])
    title = item.fields.get("System.Title", f"Work item #{work_item_id}")
    prompt = (
        f"Sei un revisore di codice indipendente. Rivedi le modifiche del branch corrente "
        f"'{branch}' rispetto a '{cfg.base_branch}' per il work item Azure DevOps "
        f"#{work_item_id} ('{title}').\n\n"
        f"Usa 'git diff {cfg.base_branch}...{branch}' per vedere le modifiche. NON modificare "
        "il codice. Segnala problemi di correttezza, rischi, cose da migliorare o omissioni "
        "rispetto a quello che il work item richiede. Se vuoi, esegui la suite di test "
        "esistente per verificare che passi. Rispondi con un report sintetico in testo semplice."
    )
    run_id = history.start_run("dashboard")
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Grep", "Bash"],
        work_item_id=work_item_id, run_id=run_id,
    )

    history.log_event(
        run_id, "self_review", f"Work item #{work_item_id}: self-review del codice implementato",
        work_item_id=work_item_id, branch=branch, detail=result.output,
    )
    history.finish_run(run_id, "success")

    return {"work_item_id": work_item_id, "branch": branch, "review": result.output}


@app.post("/api/check-pr/{work_item_id}")
def api_check_pr(work_item_id: int) -> dict:
    """"Controlla PR": revisione del codice con l'agente synthetic-review
    (3 persona junior/senior/tech-lead, vedi prompts/synthetic_review.md),
    che posta i commenti come thread REALI sulla PR di Azure DevOps."""
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()

    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_PR_OPEN not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non ha una PR aperta da controllare")

    pr_id = review_loop.find_pr_id_for_work_item(wit_client, work_item_id)
    if pr_id is None:
        raise HTTPException(409, detail=f"Nessuna PR collegata trovata per il work item #{work_item_id}")

    pr = git_client.get_pull_request(cfg.repo_id, pr_id, project=cfg.project)
    branch = pr.source_ref_name.replace("refs/heads/", "", 1)
    try:
        review_loop.checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile fare checkout del branch {branch}: {exc.stderr}")

    run_id = history.start_run("dashboard")
    result = review_loop.run_synthetic_pr_review(cfg, wit_client, git_client, work_item_id, pr_id, branch, run_id)
    history.finish_run(run_id, "success")
    return {"work_item_id": work_item_id, "pr_id": pr_id, **result}


@app.get("/api/pr-comments/{work_item_id}")
def api_get_pr_comments(work_item_id: int) -> list[dict]:
    """"Leggi commenti": elenco dei commenti non risolti sulla PR, senza
    classificarli o applicare nulla — la scelta di cosa risolvere e' lasciata
    all'utente (vedi resolve/dismiss sotto)."""
    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()

    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_PR_OPEN not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non ha una PR aperta")

    pr_id = review_loop.find_pr_id_for_work_item(wit_client, work_item_id)
    if pr_id is None:
        raise HTTPException(409, detail=f"Nessuna PR collegata trovata per il work item #{work_item_id}")

    return review_loop.list_unresolved_comments(
        git_client, cfg, pr_id, work_item_id, include_dismissed=True, include_resolved=True
    )


@app.post("/api/pr-comments/{work_item_id}/{thread_id}/resolve")
def api_resolve_pr_comment(work_item_id: int, thread_id: int) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()

    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_PR_OPEN not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non ha una PR aperta")

    pr_id = review_loop.find_pr_id_for_work_item(wit_client, work_item_id)
    if pr_id is None:
        raise HTTPException(409, detail=f"Nessuna PR collegata trovata per il work item #{work_item_id}")

    pr = git_client.get_pull_request(cfg.repo_id, pr_id, project=cfg.project)
    branch = pr.source_ref_name.replace("refs/heads/", "", 1)

    comments = review_loop.list_unresolved_comments(
        git_client, cfg, pr_id, work_item_id, include_dismissed=True
    )
    target = next((c for c in comments if c["thread_id"] == thread_id), None)
    if target is None:
        raise HTTPException(404, detail=f"Thread #{thread_id} non trovato o gia' valutato")

    try:
        review_loop.checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile fare checkout del branch {branch}: {exc.stderr}")

    run_id = history.start_run("dashboard")
    result = review_loop.resolve_comment(
        cfg, git_client, branch, work_item_id, pr_id, thread_id, target["content"], run_id
    )
    history.finish_run(run_id, "success" if result["applied"] else "error")
    return {"work_item_id": work_item_id, "thread_id": thread_id, **result}


@app.post("/api/pr-comments/{work_item_id}/{thread_id}/dismiss")
def api_dismiss_pr_comment(work_item_id: int, thread_id: int) -> dict:
    """L'utente scegli di non far risolvere questo commento all'agente:
    niente viene toccato su Azure DevOps, resta solo la memoria locale per
    non riproporlo in questa sessione di triage (vedi history.dismiss_thread)."""
    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()
    pr_id = review_loop.find_pr_id_for_work_item(wit_client, work_item_id)
    if pr_id is None:
        raise HTTPException(409, detail=f"Nessuna PR collegata trovata per il work item #{work_item_id}")

    comments = review_loop.list_unresolved_comments(git_client, cfg, pr_id, work_item_id)
    target = next((comment for comment in comments if comment["thread_id"] == thread_id), None)
    if target is None or target["dismissed"]:
        raise HTTPException(404, detail=f"Commento #{thread_id} non trovato o gia' valutato")

    history.dismiss_thread(work_item_id, thread_id)
    _log_manual_action(
        "comment_skipped", f"Ticket #{work_item_id}, thread #{thread_id}: commento ignorato dall'utente",
        work_item_id=work_item_id,
    )
    return {
        "work_item_id": work_item_id,
        "thread_id": thread_id,
        "comments": review_loop.list_unresolved_comments(
            git_client, cfg, pr_id, work_item_id, include_dismissed=True, include_resolved=True
        ),
    }


@app.post("/api/pr-comments/{work_item_id}/{thread_id}/restore")
def api_restore_pr_comment(work_item_id: int, thread_id: int) -> dict:
    """Riabilita un solo commento ignorato per i successivi piani batch."""
    if thread_id not in history.get_dismissed_thread_ids(work_item_id):
        raise HTTPException(404, detail=f"Commento #{thread_id} non e' ignorato")
    history.restore_thread(work_item_id, thread_id)
    _log_manual_action(
        "comment_restored", f"Ticket #{work_item_id}, thread #{thread_id}: commento reincluso nel piano",
        work_item_id=work_item_id,
    )
    return {"work_item_id": work_item_id, "thread_id": thread_id}


@app.post("/api/pr-comments/{work_item_id}/{thread_id}/reply-and-resolve")
def api_reply_and_resolve_pr_comment(work_item_id: int, thread_id: int, body: TextBody) -> dict:
    """Risponde a un thread ignorato e lo risolve direttamente su Azure DevOps."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, detail="Il commento di risposta non puo' essere vuoto")

    cfg, _wit_client, git_client, pr_id, _branch = _get_pr_comment_batch_context(work_item_id)
    comments = review_loop.list_unresolved_comments(
        git_client, cfg, pr_id, work_item_id, include_dismissed=True
    )
    target = next((comment for comment in comments if comment["thread_id"] == thread_id), None)
    if target is None or not target["dismissed"]:
        raise HTTPException(409, detail=f"Il commento #{thread_id} deve essere ignorato e ancora aperto")

    review_loop.reply_to_thread(git_client, cfg, pr_id, thread_id, text)
    review_loop.mark_thread_fixed(git_client, cfg, pr_id, thread_id)

    history.restore_thread(work_item_id, thread_id)
    _log_manual_action(
        "comment_resolved",
        f"Ticket #{work_item_id}, thread #{thread_id}: risposta pubblicata e commento risolto",
        work_item_id=work_item_id,
    )
    return {"work_item_id": work_item_id, "thread_id": thread_id}


def _get_pr_comment_batch_context(work_item_id: int) -> tuple[Config, object, object, int, str]:
    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()
    tags = state.get_tags(wit_client, work_item_id)
    if state.TAG_PR_OPEN not in tags:
        raise HTTPException(409, detail=f"Work item #{work_item_id} non ha una PR aperta")

    pr_id = review_loop.find_pr_id_for_work_item(wit_client, work_item_id)
    if pr_id is None:
        raise HTTPException(409, detail=f"Nessuna PR collegata trovata per il work item #{work_item_id}")
    pr = git_client.get_pull_request(cfg.repo_id, pr_id, project=cfg.project)
    return cfg, wit_client, git_client, pr_id, pr.source_ref_name.replace("refs/heads/", "", 1)


def _get_selected_pr_comments(
    git_client, cfg: Config, pr_id: int, work_item_id: int, thread_ids: list[int],
    planning_notes: dict[int, str] | None = None,
) -> list[dict]:
    if not thread_ids or len(thread_ids) != len(set(thread_ids)):
        raise HTTPException(400, detail="Seleziona almeno un commento distinto")
    available = {
        comment["thread_id"]: comment
        for comment in review_loop.list_unresolved_comments(git_client, cfg, pr_id, work_item_id)
    }
    missing = sorted(set(thread_ids) - available.keys())
    if missing:
        raise HTTPException(409, detail=f"Thread non piu' disponibili: {', '.join(map(str, missing))}")
    notes = planning_notes or {}
    unexpected_notes = set(notes) - set(thread_ids)
    if unexpected_notes:
        raise HTTPException(400, detail="Le note devono riferirsi solo ai commenti selezionati")

    selected = []
    for thread_id in thread_ids:
        comment = dict(available[thread_id])
        note = notes.get(thread_id, "").strip()
        if note:
            comment["planning_note"] = note
        selected.append(comment)
    return selected


@app.get("/api/pr-comment-batch/{work_item_id}")
def api_get_pr_comment_batch(work_item_id: int) -> dict:
    batch = history.get_pr_review_batch(work_item_id)
    if batch is None:
        raise HTTPException(404, detail="Nessun piano di correzione in corso")
    return batch


@app.post("/api/pr-comment-batch/{work_item_id}/plan")
def api_plan_pr_comment_batch(work_item_id: int, body: ThreadBatchRequest) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    existing_batch = history.get_pr_review_batch(work_item_id)
    if existing_batch and existing_batch["status"] == "changes_applied":
        raise HTTPException(
            409, detail="Ci sono modifiche non ancora committate: approva il commit o ripristinale prima"
        )
    cfg, _wit_client, git_client, pr_id, branch = _get_pr_comment_batch_context(work_item_id)
    comments = _get_selected_pr_comments(
        git_client, cfg, pr_id, work_item_id, body.thread_ids, body.planning_notes
    )
    try:
        review_loop.checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile fare checkout del branch {branch}: {exc.stderr}")

    run_id = history.start_run("dashboard")
    try:
        plan_text = review_loop.plan_comment_batch(cfg, work_item_id, branch, comments, run_id)
    except RuntimeError as exc:
        history.log_event(
            run_id, "comment_batch_failed",
            f"Ticket #{work_item_id}: generazione piano non completata",
            level="warning", work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=str(exc),
        )
        history.finish_run(run_id, "error")
        raise HTTPException(409, detail=str(exc)) from exc
    history.save_pr_review_batch(work_item_id, pr_id, branch, body.thread_ids, plan_text)
    history.log_event(
        run_id, "comment_batch_planned",
        f"Ticket #{work_item_id}: piano creato per {len(comments)} commenti PR",
        work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=plan_text,
    )
    history.finish_run(run_id, "success")
    return history.get_pr_review_batch(work_item_id)


@app.post("/api/pr-comment-batch/{work_item_id}/apply")
def api_apply_pr_comment_batch(work_item_id: int) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    batch = history.get_pr_review_batch(work_item_id)
    if batch is None or batch["status"] != "plan_ready":
        raise HTTPException(409, detail="Non c'e' un piano pronto da approvare")
    cfg, _wit_client, git_client, pr_id, branch = _get_pr_comment_batch_context(work_item_id)
    if pr_id != batch["pr_id"] or branch != batch["branch"]:
        raise HTTPException(409, detail="La PR o il branch sono cambiati: genera un nuovo piano")
    comments = _get_selected_pr_comments(git_client, cfg, pr_id, work_item_id, batch["thread_ids"])
    try:
        review_loop.checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(409, detail=f"Impossibile fare checkout del branch {branch}: {exc.stderr}")

    run_id = history.start_run("dashboard")
    try:
        result = review_loop.apply_comment_batch(
            cfg, work_item_id, branch, comments, batch["plan_text"], run_id
        )
    except RuntimeError as exc:
        history.log_event(
            run_id, "comment_batch_failed",
            f"Ticket #{work_item_id}: applicazione piano non completata",
            level="warning", work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=str(exc),
        )
        history.finish_run(run_id, "error")
        raise HTTPException(409, detail=str(exc)) from exc
    if result["applied"]:
        history.update_pr_review_batch_status(work_item_id, "changes_applied")
        action, message, level = "comment_batch_applied", f"Ticket #{work_item_id}: modifiche applicate, in attesa di approvazione commit", "info"
    else:
        action, message, level = "comment_batch_failed", f"Ticket #{work_item_id}: modifiche batch non completate", "warning"
    history.log_event(
        run_id, action, message, level=level, work_item_id=work_item_id, branch=branch, pr_id=pr_id,
        detail=result["output"],
    )
    history.finish_run(run_id, "success" if result["applied"] else "error")
    return {**history.get_pr_review_batch(work_item_id), **result}


@app.post("/api/pr-comment-batch/{work_item_id}/commit")
def api_commit_pr_comment_batch(work_item_id: int) -> dict:
    _reconcile_active_process()
    if _active["process"] is not None:
        raise HTTPException(409, detail=f"Un run e' gia' attivo ({_active['script']}): riprova quando finisce")

    batch = history.get_pr_review_batch(work_item_id)
    if batch is None or batch["status"] != "changes_applied":
        raise HTTPException(409, detail="Prima approva e applica le modifiche del piano")
    cfg, _wit_client, git_client, pr_id, branch = _get_pr_comment_batch_context(work_item_id)
    if pr_id != batch["pr_id"] or branch != batch["branch"]:
        raise HTTPException(409, detail="La PR o il branch sono cambiati: genera un nuovo piano")

    run_id = history.start_run("dashboard")
    result = review_loop.commit_comment_batch(cfg, work_item_id, branch, run_id)
    if result["committed"]:
        for thread_id in batch["thread_ids"]:
            review_loop.reply_to_thread(
                git_client, cfg, pr_id, thread_id, "Ho applicato e pubblicato la correzione approvata."
            )
            review_loop.mark_thread_fixed(git_client, cfg, pr_id, thread_id)
        history.update_pr_review_batch_status(work_item_id, "completed")
        action, message, level = "comment_batch_committed", f"Ticket #{work_item_id}: correzioni committate e pubblicate", "info"
    else:
        action, message, level = "comment_batch_commit_failed", f"Ticket #{work_item_id}: commit o push batch non riuscito", "warning"
    history.log_event(
        run_id, action, message, level=level, work_item_id=work_item_id, branch=branch, pr_id=pr_id,
        detail=result["output"],
    )
    history.finish_run(run_id, "success" if result["committed"] else "error")
    return {**history.get_pr_review_batch(work_item_id), **result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
