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
        return "Graphify unavailable: graphify-out/graph.json does not exist. Continue with Read, Grep, and Glob."

    executable = shutil.which("graphify")
    if executable is None:
        return "Graphify unavailable: the 'graphify' command was not found on PATH. Continue with Read, Grep, and Glob."

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
        return "Graphify unavailable: the query exceeded 60 seconds. Continue with Read, Grep, and Glob."
    except OSError as exc:
        return f"Graphify unavailable: unable to start the command ({exc}). Continue with Read, Grep, and Glob."

    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        return (
            f"Graphify unavailable: query failed ({error[:500]}). "
            "Continue with Read, Grep, and Glob."
        )
    return f"Graphify context (use it as the first reference, then verify it in the files):\n{result.stdout[:_MAX_OUTPUT_CHARS]}"
