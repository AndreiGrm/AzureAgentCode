"""Loop di review: per ogni PR aperta dal bot (work item taggato
agent:pr-open), guarda i thread di commenti non risolti e, per ogni
commento nuovo, chiede a Claude Code di classificarlo.

Le mutazioni su Azure Repos/Boards (rispondere al thread, marcarlo
risolto, taggare il work item come bloccato) sono fatte qui in Python con
l'SDK ufficiale, non delegate a Claude: Claude si occupa solo di decidere
se il fix e' meccanico e, in tal caso, di applicarlo/commitarlo/pusharlo.
Questo evita di far dipendere l'aggiornamento dello stato su Azure DevOps
da chiamate REST "alla cieca" fatte da un processo agentico via shell.

Da lanciare periodicamente (vedi README.md), tipicamente ogni 15 minuti.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import traceback

from pathlib import Path

from azure.devops.v7_1.git.models import (
    Comment,
    CommentPosition,
    CommentThreadContext,
    GitPullRequestCommentThread,
)
from azure.devops.v7_1.work_item_tracking.models import TeamContext, Wiql

import history
import state
from agent_prompts import load_agent_prompt
from autofix import commit_autofix, run_deterministic_autofix
from claude_runner import run_claude
from config import Config, get_connection, load_config
from graphify_context import get_graphify_context
from retry import retry_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("review_loop")

WIQL_PR_OPEN_QUERY = """
SELECT [System.Id]
FROM WorkItems
WHERE [System.TeamProject] = @project
  AND [System.Tags] CONTAINS 'agent:pr-open'
  AND [System.State] NOT IN ('Done', 'Closed', 'Removed')
"""

BOT_SIGNATURE = "\n\n_Risposta automatica dell'agente._"

_ARTIFACT_LINK_RE = re.compile(r"PullRequestId/[^%]*%2[Ff][^%]*%2[Ff](\d+)$")

SYNTHETIC_REVIEW_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "synthetic_review.md"


@retry_once()
def find_open_pr_work_items(wit_client, cfg: Config) -> list[int]:
    team_context = TeamContext(project=cfg.project)
    result = wit_client.query_by_wiql(Wiql(query=WIQL_PR_OPEN_QUERY), team_context=team_context)
    return [ref.id for ref in (result.work_items or [])]


@retry_once()
def find_pr_id_for_work_item(wit_client, work_item_id: int) -> int | None:
    """Trova la PR collegata leggendo l'ArtifactLink creato da `az repos pr create --work-items`."""
    item = wit_client.get_work_item(work_item_id, expand="Relations")
    for rel in item.relations or []:
        if rel.rel != "ArtifactLink":
            continue
        match = _ARTIFACT_LINK_RE.search(rel.url or "")
        if match:
            return int(match.group(1))
    return None


def is_bot_comment(comment) -> bool:
    return (comment.content or "").rstrip().endswith(BOT_SIGNATURE.strip())


def last_real_comment(thread):
    """Ultimo commento di tipo testo, non cancellato, in un thread."""
    real_comments = [
        c for c in (thread.comments or [])
        if not c.is_deleted and (c.comment_type or "text") == "text"
    ]
    return real_comments[-1] if real_comments else None


def checkout_pr_branch(cfg: Config, branch: str) -> None:
    subprocess.run(["git", "fetch", "origin", branch], cwd=cfg.repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", branch], cwd=cfg.repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "pull", "--ff-only", "origin", branch], cwd=cfg.repo_path, check=True, capture_output=True, text=True)


@retry_once()
def reply_to_thread(git_client, cfg: Config, pr_id: int, thread_id: int, text: str) -> None:
    git_client.create_comment(
        Comment(content=text + BOT_SIGNATURE),
        repository_id=cfg.repo_id,
        pull_request_id=pr_id,
        thread_id=thread_id,
        project=cfg.project,
    )


@retry_once()
def mark_thread_fixed(git_client, cfg: Config, pr_id: int, thread_id: int) -> None:
    git_client.update_thread(
        GitPullRequestCommentThread(status="fixed"),
        repository_id=cfg.repo_id,
        pull_request_id=pr_id,
        thread_id=thread_id,
        project=cfg.project,
    )


def list_unresolved_comments(
    git_client, cfg: Config, pr_id: int, work_item_id: int, include_dismissed: bool = False,
    include_resolved: bool = False,
) -> list[dict]:
    """Commenti su una PR, escludendo quelli dell'agente stesso.
    Con ``include_dismissed`` la dashboard mantiene visibili anche quelli
    marcati "Ignora", identificandoli ma senza renderli selezionabili nel piano.
    Con ``include_resolved`` mantiene visibili anche i thread gia' risolti."""
    dismissed = history.get_dismissed_thread_ids(work_item_id)
    threads = git_client.get_threads(cfg.repo_id, pr_id, project=cfg.project)

    comments = []
    for thread in threads or []:
        if thread.is_deleted:
            continue
        is_resolved = thread.status not in (None, "active", "pending")
        if is_resolved and not include_resolved:
            continue
        comment = last_real_comment(thread)
        if comment is None or is_bot_comment(comment):
            continue
        is_dismissed = thread.id in dismissed
        if is_dismissed and not include_dismissed:
            continue
        thread_context = thread.thread_context
        file_path = (getattr(thread_context, "file_path", "") or "").lstrip("/")
        file_position = getattr(thread_context, "right_file_start", None)
        comments.append({
            "thread_id": thread.id,
            "author": comment.author.display_name if comment.author else "reviewer",
            "content": comment.content,
            "published_date": comment.published_date.isoformat() if comment.published_date else None,
            "dismissed": is_dismissed,
            "resolved": is_resolved,
            "file_path": file_path or None,
            "line": getattr(file_position, "line", None),
        })
    return comments


def resolve_comment(cfg: Config, git_client, branch: str, work_item_id: int, pr_id: int, thread_id: int, comment_content: str, run_id: int) -> dict:
    """Applica il fix per UN commento specifico che l'utente ha scelto di
    risolvere dalla dashboard: a differenza di classify_and_handle_comment
    non c'e' piu' bisogno di classificare (l'utente ha gia' deciso), quindi
    si istruisce Claude direttamente ad applicare la correzione."""
    prompt = (
        f"Stai risolvendo un commento su una pull request Azure DevOps collegata "
        f"al work item #{work_item_id}. L'utente ha deciso di farlo risolvere a te.\n\n"
        f"Commento:\n\"\"\"\n{comment_content}\n\"\"\"\n\n"
        f"Sei sul branch locale '{branch}', che contiene le modifiche della PR.\n\n"
        "1. Applica la modifica richiesta nel codice.\n"
        "2. Fai commit con un messaggio chiaro.\n"
        f"3. Pusha il branch: git push origin {branch}\n"
        "4. Concludi la risposta con una riga nel formato esatto: FIXED: done "
        "(oppure FIXED: failed se non riesci ad applicare la correzione)."
    )
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"],
        work_item_id=work_item_id, run_id=run_id,
    )

    if "FIXED: done" not in result.output:
        history.log_event(
            run_id, "comment_resolve_failed", f"Ticket #{work_item_id}, thread #{thread_id}: fix non confermato dall'agente",
            level="warning", work_item_id=work_item_id, pr_id=pr_id, detail=result.output,
        )
        return {"applied": False, "output": result.output}

    reply_to_thread(git_client, cfg, pr_id, thread_id, "Ho applicato la correzione richiesta.")
    mark_thread_fixed(git_client, cfg, pr_id, thread_id)
    history.log_event(
        run_id, "comment_resolved", f"Ticket #{work_item_id}, thread #{thread_id}: commento risolto su richiesta dell'utente",
        work_item_id=work_item_id, branch=branch, pr_id=pr_id,
    )
    return {"applied": True, "output": result.output}


def plan_comment_batch(
    cfg: Config, work_item_id: int, branch: str, comments: list[dict], run_id: int
) -> str:
    """Produce un piano per i thread selezionati, senza modificare il repository."""
    def format_comment(comment: dict) -> str:
        text = f"## Thread #{comment['thread_id']} — {comment['author']}\n{comment['content']}"
        if comment.get("planning_note"):
            text += f"\n\nNota dell'utente per il piano:\n{comment['planning_note']}"
        return text

    comment_list = "\n\n".join(format_comment(comment) for comment in comments)
    diff = subprocess.run(
        ["git", "diff", f"{cfg.base_branch}...{branch}"],
        cwd=cfg.repo_path, check=True, capture_output=True, text=True,
    ).stdout
    graphify_section = get_graphify_context(
        cfg.repo_path,
        f"Come risolvere i commenti PR del work item #{work_item_id}: {comment_list}",
    )
    prompt = (
        f"Prepara un piano di correzione per i commenti di review della PR del work item "
        f"#{work_item_id}, sul branch '{branch}'. Non modificare file, non fare commit e non fare push.\n\n"
        f"Commenti selezionati:\n{comment_list}\n\n{graphify_section}\n\nDiff della PR:\n{diff}\n\n"
        "Esamina il diff e i file necessari. Restituisci un piano conciso, organizzato "
        "per file e thread, indicando le modifiche, i test da eseguire e gli eventuali rischi."
    )
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Grep", "Glob"],
        work_item_id=work_item_id, run_id=run_id,
    )
    return result.output


def apply_comment_batch(
    cfg: Config, work_item_id: int, branch: str, comments: list[dict], plan_text: str, run_id: int
) -> dict:
    """Applica tutte le correzioni approvate, ma lascia commit e push a una seconda approvazione."""
    comment_list = "\n\n".join(
        f"## Thread #{comment['thread_id']} — {comment['author']}\n{comment['content']}"
        for comment in comments
    )
    prompt = (
        f"Applica le correzioni approvate per i commenti della PR del work item #{work_item_id}, "
        f"sul branch '{branch}'.\n\nPiano approvato:\n{plan_text}\n\n"
        f"Commenti da risolvere:\n{comment_list}\n\n"
        "Modifica il codice per risolvere tutti i commenti. "
        "NON fare commit e NON fare push: l'utente deve prima esaminare e approvare esplicitamente "
        "il commit. Concludi con la riga esatta `CHANGES_APPLIED: done` solo se tutte le modifiche "
        "sono state applicate; altrimenti `CHANGES_APPLIED: failed`."
    )
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Edit"],
        work_item_id=work_item_id, run_id=run_id,
    )
    return {"applied": "CHANGES_APPLIED: done" in result.output, "output": result.output}


def commit_comment_batch(cfg: Config, work_item_id: int, branch: str, run_id: int) -> dict:
    """Esegue commit e push solo dopo la seconda approvazione dell'utente."""
    prompt = (
        f"L'utente ha approvato il commit delle correzioni ai commenti della PR del work item "
        f"#{work_item_id}, sul branch '{branch}'. Controlla git diff, esegui i test mirati "
        "disponibili, poi fai un unico commit chiaro e pusha con `git push origin "
        f"{branch}`. Concludi con la riga esatta `COMMITTED: done` solo se commit e push "
        "sono riusciti; altrimenti `COMMITTED: failed`."
    )
    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read"],
        work_item_id=work_item_id, run_id=run_id,
    )
    return {"committed": "COMMITTED: done" in result.output, "output": result.output}


def run_synthetic_pr_review(cfg: Config, wit_client, git_client, work_item_id: int, pr_id: int, branch: str, run_id: int) -> dict:
    """"Controlla PR": una sessione Claude indipendente (le tre persona
    junior/senior/tech-lead di prompts/synthetic_review.md) rivede il diff
    e POSTA i commenti come thread reali sulla PR, ancorati a file/riga —
    a differenza della self-review pre-PR (dashboard_server.api_review_code),
    che produce solo un report privato."""
    agent_body, agent_meta = load_agent_prompt(SYNTHETIC_REVIEW_PROMPT_PATH)

    item = wit_client.get_work_item(work_item_id, fields=["System.Title", "System.Description"])
    title = item.fields.get("System.Title", f"Work item #{work_item_id}")
    description = item.fields.get("System.Description", "") or ""

    prompt = (
        f"{agent_body}\n\n"
        "---\n\n"
        f"# PR da revisionare\n\n"
        f"Work item #{work_item_id}: {title}\n\nDescrizione:\n{description}\n\n"
        f"Sei sul branch locale '{branch}'. Usa 'git diff {cfg.base_branch}...{branch}' "
        "(con il tool Bash) per vedere le modifiche della PR, poi Read/Grep/Glob per "
        "aprire i file reali quando serve piu' contesto. Rispondi SOLO con il JSON "
        "richiesto dal contratto di output sopra, nessun testo prima o dopo."
    )

    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Read", "Grep", "Glob", "Bash"],
        model=agent_meta.get("model"), work_item_id=work_item_id, run_id=run_id,
    )

    match = re.search(r"\{.*\}", result.output, re.DOTALL)
    if not match:
        history.log_event(
            run_id, "error", f"Work item #{work_item_id} / PR #{pr_id}: risposta della synthetic review non interpretabile",
            level="error", work_item_id=work_item_id, pr_id=pr_id, detail=result.output,
        )
        return {"posted": 0, "failed": 0, "summary": None}

    try:
        review = json.loads(match.group(0))
    except json.JSONDecodeError:
        history.log_event(
            run_id, "error", f"Work item #{work_item_id} / PR #{pr_id}: JSON della synthetic review non valido",
            level="error", work_item_id=work_item_id, pr_id=pr_id, detail=match.group(0),
        )
        return {"posted": 0, "failed": 0, "summary": None}

    posted, failed = 0, 0
    for comment in review.get("comments", []):
        file_path = comment.get("filePath")
        line = comment.get("line")
        if not file_path or not line:
            failed += 1
            continue
        if not file_path.startswith("/"):
            file_path = "/" + file_path

        header = f"**[{comment.get('category', 'review')} · {comment.get('severity', 'info')}]**"
        body = comment.get("message", "")
        suggestion = comment.get("suggestion")
        content = f"{header}\n\n{body}" + (f"\n\n{suggestion}" if suggestion else "")

        try:
            thread = GitPullRequestCommentThread(
                comments=[Comment(content=content, comment_type="text")],
                status="active",
                thread_context=CommentThreadContext(
                    file_path=file_path,
                    right_file_start=CommentPosition(line=line, offset=1),
                    right_file_end=CommentPosition(line=line, offset=1),
                ),
            )
            git_client.create_thread(thread, cfg.repo_id, pr_id, project=cfg.project)
            posted += 1
        except Exception:
            logger.exception(
                "Work item #%s / PR #%s: impossibile postare il commento su %s:%s",
                work_item_id, pr_id, file_path, line,
            )
            failed += 1

    summary = review.get("summary", "")
    uncertainties = review.get("uncertainties", [])
    detail_parts = [summary] if summary else []
    if uncertainties:
        detail_parts.append(
            "Dubbi non pubblicati (bassa confidenza):\n"
            + "\n".join(f"- {u.get('topic', '?')}: {u.get('doubt', '')}" for u in uncertainties)
        )
    history.log_event(
        run_id, "pr_checked",
        f"Work item #{work_item_id} / PR #{pr_id}: synthetic review completata, {posted} commenti pubblicati"
        + (f", {failed} non pubblicabili" if failed else ""),
        work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail="\n\n".join(detail_parts) or None,
    )
    return {"posted": posted, "failed": failed, "summary": summary}


def classify_and_handle_comment(cfg: Config, branch: str, work_item_id: int, comment, run_id: int) -> dict:
    author = comment.author.display_name if comment.author else "reviewer"
    prompt = (
        f"Stai revisionando un commento su una pull request Azure DevOps collegata "
        f"al work item #{work_item_id}.\n\n"
        f"Commento di {author}:\n\"\"\"\n{comment.content}\n\"\"\"\n\n"
        f"Sei sul branch locale '{branch}', che contiene le modifiche della PR.\n\n"
        "Classifica il commento:\n"
        '- "fix" se e\' un fix meccanico non ambiguo (lint, naming, refactor minore, '
        "richiesta chiara e circoscritta) che puoi applicare con sicurezza.\n"
        '- "needs_human" se richiede una decisione di giudizio, e\' ambiguo, o proviene '
        "da un reviewer esterno su un punto non banale.\n\n"
        'Se la classificazione e\' "fix":\n'
        "1. Applica la modifica nel codice.\n"
        "2. Fai commit con un messaggio chiaro.\n"
        f"3. Pusha il branch: git push origin {branch}\n\n"
        'Se la classificazione e\' "needs_human": NON modificare il codice.\n\n'
        "Concludi SEMPRE la risposta con una riga nel formato esatto (JSON su una sola riga):\n"
        'DECISION_JSON: {"action": "fix|needs_human", "reply": "testo breve da postare come risposta al thread"}'
    )

    result = run_claude(
        prompt=prompt, cwd=cfg.repo_path, allowed_tools=["Bash", "Read", "Edit"],
        work_item_id=work_item_id, run_id=run_id,
    )

    match = re.search(r"DECISION_JSON:\s*(\{.*\})", result.output)
    if not match:
        return {"action": "needs_human", "reply": "Non sono riuscito a classificare il commento in modo affidabile: serve revisione umana."}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"action": "needs_human", "reply": "Risposta dell'agente non interpretabile: serve revisione umana."}


def process_pull_request(cfg: Config, wit_client, git_client, work_item_id: int, pr_id: int, run_id: int) -> None:
    pr = git_client.get_pull_request(cfg.repo_id, pr_id, project=cfg.project)
    if pr.status != "active":
        logger.info("Work item #%s / PR #%s: stato '%s', non attiva, salto", work_item_id, pr_id, pr.status)
        if pr.status in ("completed", "abandoned"):
            # La PR e' stata mergiata o abbandonata direttamente su Azure
            # DevOps: sposta il ticket nella sezione "Completed" della
            # dashboard invece di lasciarlo taggato agent:pr-open per
            # sempre (nessun run futuro lo ritroverebbe piu' come "attivo").
            state.remove_tag(wit_client, cfg.project, work_item_id, state.TAG_PR_OPEN)
            state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_COMPLETED)
            if pr.status == "completed":
                history.log_event(
                    run_id, "pr_completed", f"Work item #{work_item_id} / PR #{pr_id}: PR completata (merge)",
                    work_item_id=work_item_id, branch=pr.source_ref_name.replace("refs/heads/", "", 1), pr_id=pr_id,
                )
            else:
                state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_ABANDONED)
                history.log_event(
                    run_id, "pr_abandoned", f"Work item #{work_item_id} / PR #{pr_id}: PR abbandonata",
                    level="warning", work_item_id=work_item_id,
                    branch=pr.source_ref_name.replace("refs/heads/", "", 1), pr_id=pr_id,
                )
        return

    branch = pr.source_ref_name.replace("refs/heads/", "", 1)
    try:
        checkout_pr_branch(cfg, branch)
    except subprocess.CalledProcessError as exc:
        logger.error("Work item #%s / PR #%s: impossibile fare checkout del branch %s: %s", work_item_id, pr_id, branch, exc.stderr)
        history.log_event(
            run_id, "error", f"Work item #{work_item_id} / PR #{pr_id}: impossibile fare checkout del branch {branch}",
            level="error", work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=exc.stderr,
        )
        return

    try:
        if run_deterministic_autofix(cfg):
            commit_autofix(cfg, branch)
            logger.info("Work item #%s / PR #%s: autofix lint/format applicato", work_item_id, pr_id)
            history.log_event(
                run_id, "autofix_applied", f"Work item #{work_item_id} / PR #{pr_id}: autofix lint/format applicato",
                work_item_id=work_item_id, branch=branch, pr_id=pr_id,
            )
    except subprocess.CalledProcessError as exc:
        logger.warning("Work item #%s / PR #%s: autofix lint/format fallito, proseguo comunque: %s", work_item_id, pr_id, exc.stderr)

    threads = git_client.get_threads(cfg.repo_id, pr_id, project=cfg.project)

    for thread in threads or []:
        if thread.is_deleted or (thread.status not in (None, "active", "pending")):
            continue

        comment = last_real_comment(thread)
        if comment is None or is_bot_comment(comment):
            continue

        logger.info(
            "Ticket #%s, branch %s, PR #%s, thread #%s: nuovo commento da valutare",
            work_item_id, branch, pr_id, thread.id,
        )
        history.log_event(
            run_id, "classifying", f"Ticket #{work_item_id}, thread #{thread.id}: valutazione commento in corso",
            work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=comment.content,
        )

        try:
            decision = classify_and_handle_comment(cfg, branch, work_item_id, comment, run_id)
        except Exception:
            logger.exception(
                "Ticket #%s, branch %s, PR #%s, thread #%s: errore durante la classificazione, lo salto",
                work_item_id, branch, pr_id, thread.id,
            )
            history.log_event(
                run_id, "error", f"Ticket #{work_item_id}, thread #{thread.id}: errore durante la classificazione",
                level="error", work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=traceback.format_exc(),
            )
            continue

        action = decision.get("action", "needs_human")
        reply = decision.get("reply") or ""

        if action == "fix":
            reply_to_thread(git_client, cfg, pr_id, thread.id, reply or "Ho applicato una correzione meccanica.")
            mark_thread_fixed(git_client, cfg, pr_id, thread.id)
            logger.info(
                "Ticket #%s, branch %s, PR #%s, thread #%s: fix meccanico applicato",
                work_item_id, branch, pr_id, thread.id,
            )
            history.log_event(
                run_id, "mechanical_fix", f"Ticket #{work_item_id}, thread #{thread.id}: fix meccanico applicato",
                work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=reply,
            )
        else:
            reply_to_thread(
                git_client, cfg, pr_id, thread.id,
                reply or "Questo commento richiede una revisione umana, non ho applicato modifiche.",
            )
            state.add_tag(wit_client, cfg.project, work_item_id, state.TAG_BLOCKED)
            logger.info(
                "Ticket #%s, branch %s, PR #%s, thread #%s: richiede revisione umana, work item bloccato",
                work_item_id, branch, pr_id, thread.id,
            )
            history.log_event(
                run_id, "needs_human",
                f"Ticket #{work_item_id}, thread #{thread.id}: richiede revisione umana, work item bloccato",
                level="warning", work_item_id=work_item_id, branch=branch, pr_id=pr_id, detail=reply,
            )
            # "fermati": non processiamo altri thread di questa PR in questo run.
            break


def main() -> None:
    cfg = load_config()
    connection = get_connection(cfg)
    wit_client = connection.clients.get_work_item_tracking_client()
    git_client = connection.clients.get_git_client()

    run_id = history.start_or_reuse_run("review")
    run_status = "success"

    # Se lanciato dalla dashboard su un singolo PBI (bottone "Esegui Review"
    # nel dettaglio del ticket), salta la query su tutti i ticket con PR
    # aperta e limita il run a quel solo work item.
    single_work_item_id = os.environ.get("REVIEW_WORK_ITEM_ID")

    try:
        if single_work_item_id:
            work_item_ids = [int(single_work_item_id)]
        else:
            try:
                work_item_ids = find_open_pr_work_items(wit_client, cfg)
            except Exception:
                logger.exception("Impossibile recuperare i work item con PR aperta, interrompo il run")
                history.log_event(
                    run_id, "error", "Impossibile recuperare i work item con PR aperta",
                    level="error", detail=traceback.format_exc(),
                )
                run_status = "error"
                return

        logger.info("Trovati %d work item con PR aperta", len(work_item_ids))

        for work_item_id in work_item_ids:
            try:
                tags = state.get_tags(wit_client, work_item_id)
            except Exception:
                logger.exception("Work item #%s: impossibile leggere i tag, lo salto", work_item_id)
                continue

            if state.TAG_BLOCKED in tags:
                logger.info("Work item #%s: bloccato, salto", work_item_id)
                continue

            try:
                pr_id = find_pr_id_for_work_item(wit_client, work_item_id)
            except Exception:
                logger.exception("Work item #%s: impossibile trovare la PR collegata, lo salto", work_item_id)
                continue

            if pr_id is None:
                logger.warning("Work item #%s: taggato agent:pr-open ma nessuna PR collegata trovata, salto", work_item_id)
                continue

            try:
                process_pull_request(cfg, wit_client, git_client, work_item_id, pr_id, run_id)
            except Exception:
                logger.exception(
                    "Work item #%s / PR #%s: errore non gestito, lo salto senza bloccare gli altri ticket",
                    work_item_id, pr_id,
                )
                history.log_event(
                    run_id, "error", f"Work item #{work_item_id} / PR #{pr_id}: errore non gestito",
                    level="error", work_item_id=work_item_id, pr_id=pr_id, detail=traceback.format_exc(),
                )
                continue
    finally:
        history.finish_run(run_id, run_status)


if __name__ == "__main__":
    main()
