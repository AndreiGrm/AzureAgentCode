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
    "Before writing or modifying a user interface, check how similar existing "
    "components in the repository are structured and styled (design system, "
    "shared components, style variables) and follow the same conventions "
    "instead of inventing ad-hoc styling.\n\n"
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
        f"The work item references this/these Figma link(s):\n{links}\n"
        "Use the available Figma tools (get_design_context, get_screenshot, etc.) "
        "to understand the design before proceeding. If the tool does not work or "
        "you do not have access, do NOT guess the design: explicitly report this in "
        "the final plan/summary instead of ignoring the link.\n\n"
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
    logger.info("Work item #%s: generating implementation plan", work_item.id)
    history.log_event(
        run_id, "generating_plan", f"Work item #{work_item.id}: generating implementation plan",
        work_item_id=work_item.id,
    )

    design_section, design_tools = _design_context_prompt(work_item.description)
    graphify_section = get_graphify_context(
        cfg.repo_path,
        f"How to implement work item #{work_item.id}: {work_item.title}. {work_item.description}",
    )
    prompt = (
        f"You must ONLY plan, not implement anything. For Azure DevOps work item "
        f"#{work_item.id} in the current repository, write a concise implementation plan.\n\n"
        f"Title: {work_item.title}\n\nDescription:\n{work_item.description}\n\n"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"{graphify_section}\n\n"
        "Explore the code as needed (read-only) and write a plan with:\n"
        "- the main implementation steps, in order;\n"
        "- the files/modules likely involved;\n"
        "- any risks, ambiguities, or concerns to report to the user (including if you could not "
        "access a design link referenced by the work item).\n"
        "Respond only with the plain-text plan in English; do not modify code."
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Glob", "Grep"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: error generating plan; will retry on the next run", work_item.id)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: error generating plan",
            level="error", work_item_id=work_item.id, detail=traceback.format_exc(),
        )
        return

    history.save_plan(work_item.id, run_id, result.output)
    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_PLAN_READY)
    history.log_event(
        run_id, "plan_ready", f"Work item #{work_item.id}: plan generated, awaiting approval",
        work_item_id=work_item.id, detail=result.output,
    )


def fix_work_item(cfg: Config, wit_client, work_item: WorkItemInfo, run_id: int, fix_text: str) -> None:
    """Applica una correzione richiesta dall'utente in fase di verifica
    (dopo che implement_work_item ha gia' concluso): riusa il branch
    esistente invece di crearne uno nuovo."""
    branch = history.get_branch_for_work_item(work_item.id)
    if branch is None:
        logger.error("Work item #%s: no known branch to apply the fix; marking it blocked", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: no known branch to apply the requested fix",
            level="error", work_item_id=work_item.id,
        )
        return

    try:
        run_git("checkout", branch, cwd=cfg.repo_path)
    except subprocess.CalledProcessError as exc:
        logger.error("Work item #%s: unable to switch to branch %s for the fix: %s", work_item.id, branch, exc.stderr)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: unable to switch to branch {branch} for the fix",
            level="error", work_item_id=work_item.id, branch=branch, detail=exc.stderr,
        )
        return

    last_summary = _find_latest_technical_summary(work_item.id)
    summary_section = f"Technical summary of the previous implementation:\n{last_summary}\n\n" if last_summary else ""
    design_section, design_tools = _design_context_prompt(work_item.description)
    prompt = (
        f"You already implemented Azure DevOps work item #{work_item.id} on branch "
        f"'{branch}'. The user reviewed the result and requested a correction.\n\n"
        f"Title: {work_item.title}\n\nOriginal description:\n{work_item.description}\n\n"
        f"{summary_section}"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"Correction requested by the user:\n{fix_text}\n\n"
        "Instructions:\n"
        "1. Apply the requested correction in the code.\n"
        "2. Commit the changes with a clear message.\n"
        f"3. Push the branch: git push origin {branch}\n"
        "4. End with a 'TECHNICAL SUMMARY:' block describing what you fixed, "
        "then a line in the exact format: IMPLEMENTED: done (or IMPLEMENTED: failed). Respond in English."
    )

    history.log_event(
        run_id, "fixing", f"Work item #{work_item.id}: Claude Code is applying the requested fix on {branch}",
        work_item_id=work_item.id, branch=branch,
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: error invoking Claude Code for the fix", work_item.id)
        history.log_event(
            run_id, "error", f"Work item #{work_item.id}: error invoking Claude Code for the fix",
            level="error", work_item_id=work_item.id, branch=branch, detail=traceback.format_exc(),
        )
        return

    if "IMPLEMENTED: done" not in result.output:
        logger.warning("Work item #%s: fix not confirmed by the agent; marking it blocked", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: fix not confirmed by the agent",
            level="warning", work_item_id=work_item.id, branch=branch, detail=result.output,
        )
        return

    fix = history.get_pending_fix(work_item.id)
    if fix:
        history.mark_fix_applied(fix["id"])
    state.remove_tag(wit_client, cfg.project, work_item.id, state.TAG_FIX_REQUESTED)

    detail = _extract_technical_summary(result)
    history.log_event(
        run_id, "fix_applied", f"Work item #{work_item.id}: fix applied on {branch}",
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
    match = re.search(r"(?:TECHNICAL SUMMARY|RIASSUNTO TECNICO):\s*(.+?)(?:\n*IMPLEMENTED:|\Z)", result.output, re.DOTALL)
    summary = match.group(1).strip() if match else None
    parts = [p for p in (summary,) if p]
    if result.corrections_applied:
        parts.append(
            "Corrections applied during the run:\n"
            + "\n".join(f"- {c}" for c in result.corrections_applied)
        )
    return "\n\n".join(parts) or None


def decompose_epic(cfg: Config, wit_client, epic: WorkItemInfo, run_id: int) -> None:
    logger.info("Epic #%s '%s': decomposing into child PBIs", epic.id, epic.title)
    history.log_event(
        run_id, "decomposing", f"Decomposing Epic #{epic.id} '{epic.title}' into child PBIs",
        work_item_id=epic.id,
    )

    prompt = (
        "You are a Product Owner. Decompose the following Epic into 2–6 independent "
        "Product Backlog Items (PBIs), each of which can be implemented individually.\n\n"
        f"Epic #{epic.id}: {epic.title}\n\nDescription:\n{epic.description}\n\n"
        "Respond ONLY with a JSON array, with no additional text before or after, "
        'in the format: [{"title": "...", "description": "..."}, ...]. Use English for titles and descriptions.'
    )
    try:
        raw = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Glob", "Grep"],
            work_item_id=epic.id, run_id=run_id,
        ).output
    except Exception:
        logger.exception("Epic #%s: error invoking Claude Code; will retry on the next run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: error invoking Claude Code for decomposition",
            level="error", work_item_id=epic.id,
        )
        return

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        logger.error("Epic #%s: unparseable decomposition response; will retry on the next run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: unparseable decomposition response",
            level="error", work_item_id=epic.id, detail=raw,
        )
        return
    try:
        children = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.error("Epic #%s: invalid decomposition JSON; will retry on the next run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: invalid decomposition JSON",
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
            logger.info("Epic #%s: created child PBI #%s '%s'", epic.id, created.id, title)
            created_count += 1
            created_titles.append(f"#{created.id} {title}")
        except Exception:
            logger.exception("Epic #%s: error creating child PBI '%s'; continuing with the others", epic.id, title)

    if created_count > 0:
        state.add_tag(wit_client, cfg.project, epic.id, state.TAG_DECOMPOSED)
        history.log_event(
            run_id, "epic_decomposed",
            f"Epic #{epic.id}: created {created_count} child PBIs",
            work_item_id=epic.id, detail="\n".join(created_titles),
        )
    else:
        logger.error("Epic #%s: no child PBIs created; will retry on the next run", epic.id)
        history.log_event(
            run_id, "error", f"Epic #{epic.id}: no child PBIs created",
            level="error", work_item_id=epic.id,
        )


def implement_work_item(
    cfg: Config, wit_client, work_item: WorkItemInfo, run_id: int, plan_text: str | None = None,
) -> None:
    branch = branch_name(work_item)
    logger.info("Work item #%s: preparing branch %s", work_item.id, branch)

    try:
        ensure_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Work item #%s: unable to create/switch to branch %s: %s",
            work_item.id, branch, exc.stderr,
        )
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: unable to create/switch to branch {branch}",
            level="error", work_item_id=work_item.id, branch=branch, detail=exc.stderr,
        )
        return

    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BRANCH_CREATED)
    history.log_event(
        run_id, "branch_created", f"Work item #{work_item.id}: created branch {branch}",
        work_item_id=work_item.id, branch=branch,
    )

    plan_section = (
        f"Plan approved by the user (follow this plan; if you need to deviate "
        f"significantly, explain why in the final summary):\n{plan_text}\n\n"
        if plan_text else ""
    )
    design_section, design_tools = _design_context_prompt(work_item.description)
    prompt = (
        f"Implement Azure DevOps work item #{work_item.id} in the current repository.\n\n"
        f"Title: {work_item.title}\n\n"
        f"Description:\n{work_item.description}\n\n"
        f"{plan_section}"
        f"{STYLE_GUIDE_INSTRUCTION}"
        f"{design_section}"
        f"You are already on local branch '{branch}', created from {cfg.base_branch}.\n\n"
        "Instructions:\n"
        "1. Implement the required code changes.\n"
        "2. Commit the changes with a clear message.\n"
        f"3. Push the branch: git push -u origin {branch}\n"
        "4. Do NOT open a pull request: a human opens it after reviewing the code.\n"
        "5. End with a 'TECHNICAL SUMMARY:' block describing what you implemented, "
        "how you mocked any missing backend parts, and what components, dialogs, or files "
        "you created or modified.\n"
        "6. Then add a line in the exact format: IMPLEMENTED: done "
        "(or IMPLEMENTED: failed if you cannot complete the implementation). Respond in English."
    )

    history.log_event(
        run_id, "implementing", f"Work item #{work_item.id}: Claude Code is implementing on branch {branch}",
        work_item_id=work_item.id, branch=branch,
    )
    try:
        result = run_claude(
            prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"] + design_tools,
            work_item_id=work_item.id, run_id=run_id,
        )
    except Exception:
        logger.exception("Work item #%s: error invoking Claude Code", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: error invoking Claude Code",
            level="error", work_item_id=work_item.id, branch=branch, detail=traceback.format_exc(),
        )
        return

    if "IMPLEMENTED: done" not in result.output:
        logger.warning("Work item #%s: implementation not confirmed by the agent; marking it blocked", work_item.id)
        state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_BLOCKED)
        history.log_event(
            run_id, "blocked", f"Work item #{work_item.id}: implementation not confirmed by the agent",
            level="warning", work_item_id=work_item.id, branch=branch, detail=result.output,
        )
        return

    try:
        if run_deterministic_autofix(cfg):
            commit_autofix(cfg, branch)
            history.log_event(
                run_id, "autofix_applied", f"Work item #{work_item.id}: lint/format autofix applied on {branch}",
                work_item_id=work_item.id, branch=branch,
            )
    except subprocess.CalledProcessError as exc:
        logger.warning("Work item #%s: lint/format autofix failed; continuing anyway: %s", work_item.id, exc.stderr)

    state.add_note(wit_client, cfg.project, work_item.id, f"Implementation pushed to {branch}, awaiting manual PR creation")
    state.add_tag(wit_client, cfg.project, work_item.id, state.TAG_IMPLEMENTED)
    logger.info("Work item #%s: implemented and pushed to %s, awaiting manual PR", work_item.id, branch)
    history.log_event(
        run_id, "implemented", f"Work item #{work_item.id}: implemented and pushed to {branch}, ready for the PR",
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
        reason = f"moved to state '{ado_state}'" if ado_state else "no longer readable (probably removed)"
        logger.info("Work item #%s: %s in Azure Boards; marking it as no longer tracked", work_item_id, reason)
        try:
            state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
            if completed_externally:
                state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_COMPLETED)
        except Exception:
            logger.warning("Work item #%s: unable to update tags (it may have been removed); continuing anyway", work_item_id)
        history.log_event(
            run_id, action,
            f"Work item #{work_item_id}: {reason} in Azure Boards; ingest_loop no longer tracks it",
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
            logger.exception("Unable to retrieve work items from Azure Boards; stopping the run")
            history.log_event(
                run_id, "error", "Unable to retrieve work items from Azure Boards",
                level="error", detail=traceback.format_exc(),
            )
            run_status = "error"
            return

        logger.info("Found %d candidate work items", len(candidates))

        for work_item in candidates:
            try:
                tags = state.get_tags(wit_client, work_item.id)
            except Exception:
                logger.exception("Work item #%s: unable to read tags; skipping it", work_item.id)
                continue

            if state.TAG_PR_OPEN in tags:
                logger.info("Work item #%s: already has an open PR; skipping it", work_item.id)
                continue
            if state.TAG_BLOCKED in tags:
                logger.info("Work item #%s: blocked; skipping it", work_item.id)
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
                        logger.info("Work item #%s: has fix-requested tag but no pending fix; skipping it", work_item.id)
                elif state.TAG_IMPLEMENTED in tags:
                    logger.info("Work item #%s: in verification stage; skipping it", work_item.id)
                elif state.TAG_PLAN_APPROVED in tags:
                    plan = history.get_plan(work_item.id)
                    implement_work_item(cfg, wit_client, work_item, run_id, plan_text=plan["text"] if plan else None)
                elif state.TAG_PLAN_READY in tags:
                    logger.info("Work item #%s: plan generated and awaiting approval; skipping it", work_item.id)
                else:
                    generate_plan(cfg, wit_client, work_item, run_id)
            except Exception:
                logger.exception(
                    "Work item #%s: unhandled error; skipping it without blocking other tickets", work_item.id
                )
                history.log_event(
                    run_id, "error", f"Work item #{work_item.id}: unhandled error",
                    level="error", work_item_id=work_item.id, detail=traceback.format_exc(),
                )
                continue

        try:
            reconcile_stale_work_items(cfg, wit_client, run_id, {w.id for w in candidates})
        except Exception:
            logger.exception("Error reconciling tickets no longer returned by WIQL; not blocking the run")
    finally:
        history.finish_run(run_id, run_status)


if __name__ == "__main__":
    main()
