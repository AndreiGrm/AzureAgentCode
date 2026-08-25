"""Recupera contesto locale da Graphify senza costruire o modificare il grafo."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_MAX_OUTPUT_CHARS = 12_000


def get_graphify_context(repo_path: str, question: str) -> str:
    """Interroga un grafo esistente; in assenza di Graphify usa il fallback
    esplicito, lasciando al chiamante l'analisi Read/Grep/Glob."""
    graph_path = Path(repo_path) / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        return "Graphify non disponibile: graphify-out/graph.json non esiste. Prosegui con Read, Grep e Glob."

    executable = shutil.which("graphify")
    if executable is None:
        return "Graphify non disponibile: comando 'graphify' non trovato nel PATH. Prosegui con Read, Grep e Glob."

    try:
        result = subprocess.run(
            [executable, "query", question[:8_000], "--budget", "1500"],
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
