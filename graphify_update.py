"""Aggiorna Graphify e apre una PR separata quando il grafo cambia."""
from __future__ import annotations

import logging
import subprocess
import traceback
from datetime import datetime, timezone

from azure.devops.v7_1.git.models import GitPullRequest

import history
from config import ConfigError, get_connection, load_config
from graphify_context import graphify_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("graphify_update")


def _git(cfg, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cfg.repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _ensure_clean_worktree(cfg) -> None:
    if _git(cfg, "status", "--porcelain"):
        raise RuntimeError("La working copy contiene modifiche locali: aggiornamento Graphify rimandato.")


def update_graphify() -> None:
    cfg = load_config()
    status = graphify_status(cfg.repo_path)
    if not status["enabled"]:
        logger.info("Graphify disattivato: nessun aggiornamento programmato.")
        return
    if not status["command"]:
        raise RuntimeError("Comando Graphify non configurato.")

    run_id = history.start_or_reuse_run("graphify")
    history.log_event(run_id, "graphify_update_started", "Aggiornamento giornaliero Graphify avviato")
    try:
        _ensure_clean_worktree(cfg)
        _git(cfg, "fetch", "origin", cfg.base_branch)
        _git(cfg, "checkout", cfg.base_branch)
        _git(cfg, "pull", "--ff-only", "origin", cfg.base_branch)

        result = subprocess.run(
            [status["command"], "update", cfg.repo_path],
            cwd=cfg.repo_path,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"graphify update fallito: {error[:1000]}")

        changed_files = _git(cfg, "status", "--porcelain")
        if not changed_files:
            history.log_event(
                run_id,
                "graphify_update_finished",
                "Graphify aggiornato: nessuna modifica da pubblicare",
            )
            history.finish_run(run_id, "success")
            return

        branch = f"chore/graphify-update-{datetime.now(timezone.utc):%Y%m%d}"
        _git(cfg, "checkout", "-b", branch)
        _git(cfg, "add", "graphify-out")
        if not _git(cfg, "diff", "--cached", "--name-only"):
            raise RuntimeError("Graphify ha modificato file fuori da graphify-out: aggiornamento annullato per sicurezza.")
        _git(cfg, "commit", "-m", "chore: update Graphify graph")
        _git(cfg, "push", "-u", "origin", branch)

        git_client = get_connection(cfg).clients.get_git_client()
        pr = git_client.create_pull_request(
            GitPullRequest(
                source_ref_name=f"refs/heads/{branch}",
                target_ref_name=f"refs/heads/{cfg.base_branch}",
                title="chore: aggiornamento giornaliero Graphify",
                description="Aggiornamento automatico del grafo Graphify. Nessun agente AI è stato eseguito.",
            ),
            cfg.repo_id,
            project=cfg.project,
        )
        history.log_event(
            run_id,
            "graphify_update_finished",
            f"Graphify aggiornato: PR #{pr.pull_request_id} creata",
            branch=branch,
            pr_id=pr.pull_request_id,
            detail=changed_files,
        )
        history.finish_run(run_id, "success")
    except Exception as exc:
        logger.exception("Aggiornamento Graphify fallito")
        history.log_event(
            run_id,
            "graphify_update_error",
            f"Aggiornamento Graphify non riuscito: {exc}",
            level="error",
            detail=traceback.format_exc(),
        )
        history.finish_run(run_id, "error")
        raise


def main() -> None:
    try:
        update_graphify()
    except ConfigError:
        logger.exception("Configurazione Graphify/Azure DevOps incompleta")
        raise


if __name__ == "__main__":
    main()
