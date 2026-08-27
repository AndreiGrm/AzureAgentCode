"""Autofix deterministico (prettier + lint --fix), senza invocare Claude.

Molti commenti di review "meccanici" sono di formattazione/lint puro: un
task per cui un LLM non serve, il progetto ha già il tool giusto. Chiamato
sia da ingest_loop.py (pulizia finale dopo l'implementazione) sia da
review_loop.py (prima di valutare i thread con Claude), per togliere
all'agente lavoro che il codice deterministico fa meglio e a costo zero.
"""
from __future__ import annotations

import logging
import subprocess
import sys

from config import Config

logger = logging.getLogger(__name__)

# Su Windows npx/nx sono script .cmd, non eseguibili diretti: senza
# shell=True, subprocess non li trova (WinError 2) perche' CreateProcess
# non interpreta i batch file come farebbe una shell.
_USE_SHELL = sys.platform == "win32"


def _changed_files(cfg: Config) -> list[str]:
    """File toccati dal branch corrente rispetto al branch base.

    Limita prettier a questi file: --write sull'intero repo riformatterebbe
    anche file che il ticket non ha toccato, sporcando il diff della PR con
    modifiche irrilevanti per il reviewer.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", cfg.base_branch],
        cwd=cfg.repo_path, capture_output=True, text=True, check=True,
    )
    return [f for f in result.stdout.splitlines() if f.strip()]


def run_deterministic_autofix(cfg: Config) -> bool:
    """Esegue prettier --write (solo sui file cambiati) e lint --fix
    (nx affected, gia' di suo scoperto al branch) sul branch corrente.

    Ritorna True se ha prodotto modifiche non commesse (da committare).
    Non solleva eccezioni sui comandi npm/nx: se il progetto non ha uno di
    questi tool configurato, l'autofix è semplicemente un no-op.
    """
    files = _changed_files(cfg)
    if files:
        subprocess.run(
            ["npx", "prettier", "--write", *files],
            cwd=cfg.repo_path, capture_output=True, text=True, check=False, shell=_USE_SHELL,
        )
    subprocess.run(
        ["npx", "nx", "affected:lint", "--fix", "--base", cfg.base_branch],
        cwd=cfg.repo_path, capture_output=True, text=True, check=False, shell=_USE_SHELL,
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cfg.repo_path, capture_output=True, text=True, check=True,
    )
    return bool(status.stdout.strip())


def commit_autofix(cfg: Config, branch: str) -> None:
    """Committa e pusha le modifiche di autofix sul branch indicato.

    Da chiamare solo se run_deterministic_autofix ha ritornato True.
    """
    subprocess.run(["git", "add", "-A"], cwd=cfg.repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: autofix lint/format"],
        cwd=cfg.repo_path, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "push", "origin", branch],
        cwd=cfg.repo_path, check=True, capture_output=True, text=True,
    )
    logger.info("Autofix committed and pushed to %s", branch)
