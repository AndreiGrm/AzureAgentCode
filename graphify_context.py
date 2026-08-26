"""Recupera contesto locale da Graphify senza costruire o modificare il grafo."""
from __future__ import annotations

import shutil
import subprocess
import os
from pathlib import Path

_MAX_OUTPUT_CHARS = 12_000


def graphify_status(repo_path: str) -> dict:
    """Restituisce lo stato della connessione Graphify senza eseguire query."""
    enabled = os.environ.get("GRAPHIFY_ENABLED", "false").lower() == "true"
    configured_command = os.environ.get("GRAPHIFY_COMMAND", "").strip() or "graphify"
    executable = shutil.which(configured_command)
    if executable is None and Path(configured_command).is_file():
        executable = configured_command
    graph_path = Path(repo_path) / "graphify-out" / "graph.json"
    if not enabled:
        return {
            "enabled": False,
            "ready": False,
            "message": "Graphify è disattivato nelle Impostazioni.",
            "command": configured_command,
            "graph_path": str(graph_path),
        }
    if executable is None:
        return {
            "enabled": True,
            "ready": False,
            "message": "Comando Graphify non trovato. Indica il percorso dell'eseguibile nelle Impostazioni.",
            "command": configured_command,
            "graph_path": str(graph_path),
        }
    if not graph_path.is_file():
        return {
            "enabled": True,
            "ready": False,
            "message": "Grafo non ancora generato. Il job giornaliero lo creerà al prossimo aggiornamento.",
            "command": executable,
            "graph_path": str(graph_path),
        }
    return {
        "enabled": True,
        "ready": True,
        "message": "Graphify è pronto: ricerca e piani useranno il grafo come primo contesto.",
        "command": executable,
        "graph_path": str(graph_path),
    }


def get_graphify_context(repo_path: str, question: str) -> str:
    """Interroga un grafo esistente; in assenza di Graphify usa il fallback
    esplicito, lasciando al chiamante l'analisi Read/Grep/Glob."""
    status = graphify_status(repo_path)
    if not status["ready"]:
        return f"Graphify non disponibile: {status['message']} Prosegui con Read, Grep e Glob."

    try:
        result = subprocess.run(
            [status["command"], "query", question[:8_000], "--budget", "1500", "--graph", status["graph_path"]],
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "Graphify non disponibile: la query ha superato 60 secondi. Prosegui con Read, Grep e Glob."
    except OSError as exc:
        return f"Graphify non disponibile: impossibile avviare il comando ({exc}). Prosegui con Read, Grep e Glob."

    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        return (
            f"Graphify non disponibile: query fallita ({error[:500]}). "
            "Prosegui con Read, Grep e Glob."
        )
    return f"Contesto Graphify (usalo come primo riferimento, poi verifica nei file):\n{result.stdout[:_MAX_OUTPUT_CHARS]}"
