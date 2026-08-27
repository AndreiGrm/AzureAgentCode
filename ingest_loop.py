"""Loop di ingest: prende i ticket assegnati all'utente corrente nella
iterazione in corso, scompone le Epic in PBI figli, e per ogni PBI/Task
crea un branch locale e delega l'implementazione a Claude Code, che
committa e pusha. La pull request NON viene aperta qui: resta un passo
manuale (bottone "Crea PR" nella dashboard) per poter rivedere il codice
implementato prima che diventi visibile ai reviewer.

Da lanciare periodicamente (vedi README.md), tipicamente ogni ora.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import traceback
from dataclasses import dataclass

from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation, TeamContext, Wiql

import history
import state
from autofix import commit_autofix, run_deterministic_autofix
from claude_runner import run_claude
from config import Config, get_connection, load_config
from graphify_context import get_graphify_context
from retry import retry_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_loop")

WIQL_QUERY = """
SELECT [System.Id]
FROM WorkItems
WHERE [System.TeamProject] = @project
  AND [System.AssignedTo] = @Me
  AND [System.IterationPath] = @CurrentIteration
  AND [System.State] IN ('Committed', 'In Progress')
ORDER BY [Microsoft.VSTS.Common.BacklogPriority]
"""

FIELDS = [
    "System.Id",
    "System.Title",
    "System.WorkItemType",
    "System.Description",
]

STYLE_GUIDE_INSTRUCTION = (
    "Prima di scrivere o modificare interfaccia utente, controlla come sono "
    "strutturati/stilizzati componenti simili gia' esistenti nel repo (design "
    "system, componenti condivisi, variabili di stile) e segui le stesse "
    "convenzioni invece di inventare stile ad-hoc.\n\n"
)

# Tool Figma di sola lettura (niente use_figma/create_new_file/upload_assets:
# l'agente deve solo CAPIRE il design, non modificarlo su Figma).
FIGMA_READ_TOOLS = [
    "mcp__claude_ai_Figma__get_design_context",
    "mcp__claude_ai_Figma__get_screenshot",
    "mcp__claude_ai_Figma__get_metadata",
    "mcp__claude_ai_Figma__get_variable_defs",
    "mcp__claude_ai_Figma__get_libraries",
]


def _design_context_prompt(description: str) -> tuple[str, list[str]]:
    """Se la descrizione del work item cita un link Figma, ritorna una
    sezione di prompt che istruisce l'agente a consultarlo (con i tool
    Figma da abilitare per quel run) invece di ignorarlo o indovinare il
    design. Se non c'e' nessun link, ritorna una sezione vuota e nessun
    tool extra."""
    figma_urls = state.extract_figma_urls(description)
    if not figma_urls:
        return "", []
    links = "\n".join(f"- {url}" for url in figma_urls)
    section = (
        f"Il work item cita questo/i link Figma:\n{links}\n"
        "Usa i tool Figma disponibili (get_design_context, get_screenshot, ecc.) "
        "per capire il design prima di procedere. Se il tool non funziona o non "
        "hai accesso, NON indovinare il design: segnalalo esplicitamente nel "
        "piano/riassunto finale invece di ignorare il link.\n\n"
    )
    return section, FIGMA_READ_TOOLS


@dataclass
class WorkItemInfo:
    id: int
    title: str
    work_item_type: str
    description: str
    url: str


def snake_case_title(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"_{2,}", "_", slug).strip("_")
    return slug[:50] or "ticket"


def branch_name(work_item: WorkItemInfo) -> str:
    """Restituisce il nome standard del branch per il work item Azure DevOps."""
    branch_type = "bugfix" if work_item.work_item_type.strip().lower() == "bug" else "feature"
    return f"{branch_type}/{work_item.id}__{snake_case_title(work_item.title)}"


@retry_once()
def fetch_candidate_work_items(wit_client, cfg: Config) -> list[WorkItemInfo]:
    team_context = TeamContext(project=cfg.project, team=cfg.team)
    wiql_result = wit_client.query_by_wiql(Wiql(query=WIQL_QUERY), team_context=team_context)
    ids = [ref.id for ref in (wiql_result.work_items or [])]
    if not ids:
        return []

    items = wit_client.get_work_items(ids=ids, project=cfg.project, fields=FIELDS)
    by_id = {
        item.id: WorkItemInfo(
            id=item.id,
            title=item.fields.get("System.Title", ""),
            work_item_type=item.fields.get("System.WorkItemType", ""),
            description=item.fields.get("System.Description", "") or "",
            url=item.url,
        )
        for item in items
    }
    # get_work_items non garantisce l'ordine: lo ripristiniamo secondo la
    # priorita' restituita dalla WIQL.
    return [by_id[i] for i in ids if i in by_id]


def run_git(*args: str, cwd: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def branch_exists(cwd: str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch], cwd=cwd, capture_output=True, text=True
    )
    return result.returncode == 0


def ensure_branch(cfg: Config, branch: str) -> None:
    """Crea il branch da main se non esiste ancora, altrimenti lo riusa.

    Il riuso rende l'operazione idempotente: se un run precedente ha gia'
    creato il branch (es. dopo un errore a valle), non lo si ricrea.
    """
    if branch_exists(cfg.repo_path, branch):
        run_git("checkout", branch, cwd=cfg.repo_path)
        return
    run_git("fetch", "origin", cfg.base_branch, cwd=cfg.repo_path)
    run_git("checkout", cfg.base_branch, cwd=cfg.repo_path)
    run_git("pull", "--ff-only", "origin", cfg.base_branch, cwd=cfg.repo_path)
    run_git("checkout", "-b", branch, cwd=cfg.repo_path)


def generate_plan(cfg: Config, wit_client, work_item: WorkItemInfo, run_id: int) -> None:
    """Genera un mini-piano implementativo per un PBI candidato, SENZA
    toccare il codice: il piano resta in attesa di approvazione dell'utente
    dalla dashboard (agent:plan-ready) prima che implement_work_item parta."""
    logger.info("Work item #%s: generazione piano implementativo", work_item.id)
    history.log_event(
        run_id, "generating_plan", f"Work item #{work_item.id}: generazione piano implementativo",
        work_item_id=work_item.id,
    )

    design_section, design_tools = _design_context_prompt(work_item.description)
    graphify_section = get_graphify_context(
        cfg.repo_path,
        f"Come implementare il work item #{work_item.id}: {work_item.title}. {work_item.description}",
    )
    prompt = (
        f"Devi SOLO pianificare, non implementare nulla. Per il work item Azure DevOps "
        f"#{work_item.id} nel repository corrente, scrivi un piano implementativo sintetico.\n\n"
        f"Titolo: {work_item.title}\n\nDescrizione:\n{work_item.description}\n\n"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"{graphify_section}\n\n"
        "Esplora il codice quanto serve (solo lettura) e scrivi un piano con:\n"
        "- i passi principali dell'implementazione, in ordine;\n"
        "- i file/moduli probabilmente coinvolti;\n"
        "- eventuali rischi, ambiguita' o dubbi da segnalare all'utente (incluso se non hai "
        "potuto consultare un link di design citato nel work item).\n"
        "Rispondi solo con il piano in testo semplice, niente modifiche al codice."
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Glob", "Grep"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: errore generando il piano, riprovero' al prossimo run", work_item.id)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: errore generando il piano",
            level="error", work_item_id=work_item.id, detail=traceback.format_exc(),
        )
        return

    history.save_plan(work_item.id, run_id, result.output)
    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_PLAN_READY)
    history.log_event(
        run_id, "plan_ready", f"Work item #{work_item.id}: piano generato, in attesa di approvazione",
        work_item_id=work_item.id, detail=result.output,
    )


def fix_work_item(cfg: Config, wit_client, work_item: WorkItemInfo, run_id: int, fix_text: str) -> None:
    """Applica una correzione richiesta dall'utente in fase di verifica
    (dopo che implement_work_item ha gia' concluso): riusa il branch
    esistente invece di crearne uno nuovo."""
    branch = history.get_branch_for_work_item(work_item.id)
    if branch is None:
        logger.error("Work item #%s: nessun branch noto per applicare il fix, lo marco come bloccato", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: nessun branch noto per applicare il fix richiesto",
            level="error", work_item_id=work_item.id,
        )
        return

    try:
        run_git("checkout", branch, cwd=cfg.repo_path)
    except subprocess.CalledProcessError as exc:
        logger.error("Work item #%s: impossibile passare al branch %s per il fix: %s", work_item.id, branch, exc.stderr)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: impossibile passare al branch {branch} per il fix",
            level="error", work_item_id=work_item.id, branch=branch, detail=exc.stderr,
        )
        return

    last_summary = _find_latest_technical_summary(work_item.id)
    summary_section = f"Riassunto tecnico dell'implementazione precedente:\n{last_summary}\n\n" if last_summary else ""
    design_section, design_tools = _design_context_prompt(work_item.description)
    prompt = (
        f"Hai gia' implementato il work item Azure DevOps #{work_item.id} sul branch "
        f"'{branch}'. L'utente ha rivisto il risultato e chiede una correzione.\n\n"
        f"Titolo: {work_item.title}\n\nDescrizione originale:\n{work_item.description}\n\n"
        f"{summary_section}"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"Correzione richiesta dall'utente:\n{fix_text}\n\n"
        "Istruzioni:\n"
        "1. Applica la correzione richiesta nel codice.\n"
        "2. Fai commit delle modifiche con un messaggio chiaro.\n"
        f"3. Pusha il branch: git push origin {branch}\n"
        "4. Concludi con un blocco 'RIASSUNTO TECNICO:' che descrive cosa hai corretto, "
        "poi una riga nel formato esatto: IMPLEMENTED: done (oppure IMPLEMENTED: failed)."
    )

    history.log_event(
        run_id, "fixing", f"Work item #{work_item.id}: Claude Code sta applicando il fix richiesto su {branch}",
        work_item_id=work_item.id, branch=branch,
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: errore durante l'invocazione di Claude Code per il fix", work_item.id)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: errore invocando Claude Code per il fix",
            level="error", work_item_id=work_item.id, branch=branch, detail=traceback.format_exc(),
        )
        return

    if "IMPLEMENTED: done" not in result.output:
        logger.warning("Work item #%s: fix non confermato dall'agente, lo marco come bloccato", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: fix non confermato dall'agente",
            level="warning", work_item_id=work_item.id, branch=branch, detail=result.output,
        )
        return

    fix = history.get_pending_fix(work_item.id)
    if fix:
        history.mark_fix_applied(fix["id"])
    state.remove_tag(wit_client, cfg.project, work_item.id, state.TAG_FIX_REQUESTED)

    detail = _extract_technical_summary(result)
    history.log_event(
        run_id, "fix_applied", f"Work item #{work_item.id}: fix applicato su {branch}",
        work_item_id=work_item.id, branch=branch, detail=detail,
    )


def _find_latest_technical_summary(work_item_id: int) -> str | None:
    """L'ultimo riassunto tecnico noto per questo ticket (evento 'implemented'
    o 'fix_applied' con detail), per dare contesto a un fix successivo."""
    for event in history.get_history(work_item_id=work_item_id, limit=20):
        if event["action"] in ("implemented", "fix_applied") and event["detail"]:
            return event["detail"]
    return None


def _extract_technical_summary(result) -> str | None:
    """Estrae il blocco 'RIASSUNTO TECNICO:' dall'output dell'agente (se
    presente) ed eventuali correzioni live applicate durante il run, per
    persisterlo come detail dell'evento e mostrarlo nel pannello di verifica
    della dashboard."""
    match = re.search(r"RIASSUNTO TECNICO:\s*(.+?)(?:\n*IMPLEMENTED:|\Z)", result.output, re.DOTALL)
    summary = match.group(1).strip() if match else None
    parts = [p for p in (summary,) if p]
    if result.corrections_applied:
        parts.append(
            "Correzioni applicate durante il run:\n"
            + "\n".join(f"- {c}" for c in result.corrections_applied)
        )
    return "\n\n".join(parts) or None


def decompose_epic(cfg: Config, wit_client, epic: WorkItemInfo, run_id: int) -> None:
    logger.info("Epic #%s '%s': scomposizione in PBI figli", epic.id, epic.title)
    history.log_event(
        run_id, "decomposing", f"Scomposizione Epic #{epic.id} '{epic.title}' in PBI figli",
        work_item_id=epic.id,
    )

    prompt = (
        "Sei un Product Owner. Scomponi la seguente Epic in 2-6 Product Backlog "
        "Item (PBI) indipendenti, ciascuno implementabile singolarmente.\n\n"
        f"Epic #{epic.id}: {epic.title}\n\nDescrizione:\n{epic.description}\n\n"
        "Rispondi SOLO con un array JSON, senza testo aggiuntivo prima o dopo, "
        'nel formato: [{"title": "...", "description": "..."}, ...]'
    )
    try:
        raw = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Glob", "Grep"],
            work_item_id=epic.id, run_id=run_id,
        ).output
    except Exception:
        logger.exception("Epic #%s: errore durante l'invocazione di Claude Code, riprovero' al prossimo run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: errore invocando Claude Code per la decomposizione",
            level="error", work_item_id=epic.id,
        )
        return

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        logger.error("Epic #%s: risposta di decomposizione non interpretabile, riprovero' al prossimo run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: risposta di decomposizione non interpretabile",
            level="error", work_item_id=epic.id, detail=raw,
        )
        return
    try:
        children = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.error("Epic #%s: JSON di decomposizione non valido, riprovero' al prossimo run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: JSON di decomposizione non valido",
            level="error", work_item_id=epic.id, detail=match.group(0),
        )
        return

    created_count = 0
    created_titles: list[str] = []
    for child in children:
        title = (child.get("title") or "").strip()
        description = (child.get("description") or "").strip()
        if not title:
            continue
        try:
            document = [
                JsonPatchOperation(op="add", path="/fields/System.Title", value=title),
                JsonPatchOperation(op="add", path="/fields/System.Description", value=description),
            ]
            created = wit_client.create_work_item(document, cfg.project, "Product Backlog Item")
            relation_patch = [
                JsonPatchOperation(
                    op="add",
                    path="/relations/-",
                    value={"rel": "System.LinkTypes.Hierarchy-Forward", "url": created.url},
                )
            ]
            wit_client.update_work_item(relation_patch, epic.id, project=cfg.project)
            logger.info("Epic #%s: creato PBI figlio #%s '%s'", epic.id, created.id, title)
            created_count += 1
            created_titles.append(f"#{created.id} {title}")
        except Exception:
            logger.exception("Epic #%s: errore creando il PBI figlio '%s', continuo con gli altri", epic.id, title)

    if created_count > 0:
        state.add_tag(wit_client, cfg.project, epic.id, state.TAG_DECOMPOSED)
        history.log_event(
            run_id, "epic_decomposed",
            f"Epic #{epic.id}: creati {created_count} PBI figli",
            work_item_id=epic.id, detail="\n".join(created_titles),
        )
    else:
        logger.error("Epic #%s: nessun PBI figlio creato, riprovero' al prossimo run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: nessun PBI figlio creato",
            level="error", work_item_id=epic.id,
        )


def implement_work_item(
    cfg: Config, wit_client, work_item: WorkItemInfo, run_id: int, plan_text: str | None = None,
) -> None:
    branch = branch_name(work_item)
    logger.info("Work item #%s: preparazione branch %s", work_item.id, branch)

    try:
        ensure_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Work item #%s: impossibile creare/passare al branch %s: %s",
            work_item.id, branch, exc.stderr,
        )
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: impossibile creare/passare al branch {branch}",
            level="error", work_item_id=work_item.id, branch=branch, detail=exc.stderr,
        )
        return

    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BRANCH_CREATED)
    history.log_event(
        run_id, "branch_created", f"Work item #{work_item.id}: creato branch {branch}",
        work_item_id=work_item.id, branch=branch,
    )

    plan_section = (
        f"Piano approvato dall'utente (segui questo piano; se devi deviare in modo "
        f"significativo, spiega perche' nel riassunto finale):\n{plan_text}\n\n"
        if plan_text else ""
    )
    design_section, design_tools = _design_context_prompt(work_item.description)
    prompt = (
        f"Implementa il work item Azure DevOps #{work_item.id} nel repository corrente.\n\n"
        f"Titolo: {work_item.title}\n\n"
        f"Descrizione:\n{work_item.description}\n\n"
        f"{plan_section}"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"Sei gia' sul branch locale '{branch}', creato da {cfg.base_branch}.\n\n"
        "Istruzioni:\n"
        "1. Implementa le modifiche necessarie nel codice.\n"
        "2. Fai commit delle modifiche con un messaggio chiaro.\n"
        f"3. Pusha il branch: git push -u origin {branch}\n"
        "4. NON aprire una pull request: la apre un umano dopo aver revisionato il codice.\n"
        "5. Concludi con un blocco 'RIASSUNTO TECNICO:' che descriva cosa hai implementato, "
        "come hai mockato eventuali parti mancanti del backend, quali componenti/dialog/file "
        "hai creato o modificato.\n"
        "6. Poi una riga nel formato esatto: IMPLEMENTED: done "
        "(oppure IMPLEMENTED: failed se non riesci a completare l'implementazione)"
    )

    history.log_event(
        run_id, "implementing", f"Work item #{work_item.id}: Claude Code sta implementando sul branch {branch}",
        work_item_id=work_item.id, branch=branch,
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: errore durante l'invocazione di Claude Code", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: errore invocando Claude Code",
            level="error", work_item_id=work_item.id, branch=branch, detail=traceback.format_exc(),
        )
        return

    if "IMPLEMENTED: done" not in result.output:
        logger.warning("Work item #%s: implementazione non confermata dall'agente, lo marco come bloccato", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: implementazione non confermata dall'agente",
            level="warning", work_item_id=work_item.id, branch=branch, detail=result.output,
        )
        return

    try:
        if run_deterministic_autofix(cfg):
            commit_autofix(cfg, branch)
            history.log_event(
                run_id, "autofix_applied", f"Work item #{work_item.id}: autofix lint/format applicato su {branch}",
                work_item_id=work_item.id, branch=branch,
            )
    except subprocess.CalledProcessError as exc:
        logger.warning("Work item #%s: autofix lint/format fallito, proseguo comunque: %s", work_item.id, exc.stderr)

    state.add_note(wit_client, cfg.project, work_item.id, f"Implementazione pushata su {branch}, in attesa di creazione PR manuale")
    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_IMPLEMENTED)
    logger.info("Work item #%s: implementato e pushato su %s, in attesa di PR manuale", work_item.id, branch)
    history.log_event(
        run_id, "implemented", f"Work item #{work_item.id}: implementato e pushato su {branch}, pronto per la PR",
        work_item_id=work_item.id, branch=branch, detail=_extract_technical_summary(result),
    )


# Azioni che significano "non e' compito di ingest_loop seguirlo oltre":
# pr_opened e' territorio di review_loop.py (che ha gia' un suo filtro sugli
# stati terminali nella WIQL); blocked e' gia' segnalato, non serve rifarlo.
_TERMINAL_ADO_STATES = {"done", "closed", "removed", "completed", "chiuso", "completato"}
_RECONCILE_SKIP_ACTIONS = {"pr_opened", "blocked", "external_change", "external_completed"}


def reconcile_stale_work_items(cfg: Config, wit_client, run_id: int, fresh_ids: set[int]) -> None:
    """I ticket che ingest_loop sta seguendo (piano in attesa, in
    implementazione, in verifica, fix richiesto...) vengono normalmente
    ricontrollati ad ogni run SOLO se rientrano ancora nella WIQL
    (Committed/In Progress, assegnati a me, iterazione corrente). Se nel
    frattempo qualcuno li sposta o li rimuove direttamente su Azure Boards,
    escono dalla query e la dashboard resterebbe bloccata sull'ultimo stato
    conosciuto: qui si ricontrollano esplicitamente quelli USCITI dalla
    query per aggiornare lo stato locale."""
    for ticket in history.get_tickets(limit=500):
        work_item_id = ticket["work_item_id"]
        if work_item_id in fresh_ids or ticket["action"] in _RECONCILE_SKIP_ACTIONS:
            continue

        try:
            item = wit_client.get_work_item(work_item_id, fields=["System.State"])
            ado_state = item.fields.get("System.State")
        except Exception:
            ado_state = None  # non piu' leggibile: probabilmente rimosso

        normalized_state = str(ado_state or "").strip().casefold()
        completed_externally = normalized_state in _TERMINAL_ADO_STATES
        action = "external_completed" if completed_externally else "external_change"
        reason = f"spostato allo stato '{ado_state}'" if ado_state else "non piu' leggibile (probabilmente rimosso)"
        logger.info("Work item #%s: %s su Azure Boards, lo segno come non piu' seguito", work_item_id, reason)
        try:
            state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
            if completed_externally:
                state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_COMPLETED)
        except Exception:
            logger.warning("Work item #%s: impossibile aggiornare i tag (forse davvero rimosso), procedo comunque", work_item_id)
        history.log_event(
            run_id, action,
            f"Work item #{work_item_id}: {reason} su Azure Boards, ingest_loop non lo segue piu'",
            level="warning", work_item_id=work_item_id,
        )


def main() -> None:
    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()

    run_id = history.start_or_reuse_run("ingest")
    run_status = "success"

    try:
        try:
            candidates = fetch_candidate_work_items(wit_client, cfg)
        except Exception:
            logger.exception("Impossibile recuperare i work item da Azure Boards, interrompo il run")
            history.log_event(
                run_id, "error", "Impossibile recuperare i work item da Azure Boards",
                level="error", detail=traceback.format_exc(),
            )
            run_status = "error"
            return

        logger.info("Trovati %d work item candidati", len(candidates))

        for work_item in candidates:
            try:
                tags = state.get_tags(wit_client, work_item.id)
            except Exception:
                logger.exception("Work item #%s: impossibile leggere i tag, lo salto", work_item.id)
                continue

            if state.TAG_PR_OPEN in tags:
                logger.info("Work item #%s: gia' con PR aperta, salto", work_item.id)
                continue
            if state.TAG_BLOCKED in tags:
                logger.info("Work item #%s: bloccato, salto", work_item.id)
                continue

            try:
                if work_item.work_item_type == "Epic":
                    if state.TAG_DECOMPOSED in tags:
                        continue
                    decompose_epic(cfg, wit_client, work_item, run_id)
                elif state.TAG_FIX_REQUESTED in tags:
                    fix = history.get_pending_fix(work_item.id)
                    if fix:
                        fix_work_item(cfg, wit_client, work_item, run_id, fix["text"])
                    else:
                        logger.info("Work item #%s: tag fix-requested ma nessun fix pendente, salto", work_item.id)
                elif state.TAG_IMPLEMENTED in tags:
                    logger.info("Work item #%s: in fase di verifica, salto", work_item.id)
                elif state.TAG_PLAN_APPROVED in tags:
                    plan = history.get_plan(work_item.id)
                    implement_work_item(cfg, wit_client, work_item, run_id, plan_text=plan["text"] if plan else None)
                elif state.TAG_PLAN_READY in tags:
                    logger.info("Work item #%s: piano generato, in attesa di approvazione, salto", work_item.id)
                else:
                    generate_plan(cfg, wit_client, work_item, run_id)
            except Exception:
                logger.exception(
                    "Work item #%s: errore non gestito, lo salto senza bloccare gli altri ticket", work_item.id
                )
                history.log_event(
                    run_id, "error", f"Work item #{work_item.id}: errore non gestito",
                    level="error", work_item_id=work_item.id, detail=traceback.format_exc(),
                )
                continue

        try:
            reconcile_stale_work_items(cfg, wit_client, run_id, {w.id for w in candidates})
        except Exception:
            logger.exception("Errore durante la riconciliazione dei ticket usciti dalla WIQL, non blocco il run")
    finally:
        history.finish_run(run_id, run_status)


if __name__ == "__main__":
    main()
