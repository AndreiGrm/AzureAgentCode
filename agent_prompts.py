"""Caricamento di file "agente" in stile Claude Code (frontmatter YAML +
corpo markdown, es. prompts/synthetic_review.md) per riusarli come corpo di
un prompt passato a claude_runner.run_claude(), fuori dal meccanismo dei
subagent (questo progetto invoca claude_agent_sdk direttamente, non gira
dentro una sessione Claude Code con l'Agent tool).

Il frontmatter di questi file e' sempre "chiave: valore" su una riga: non
serve un parser YAML vero e proprio.
"""
from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def load_agent_prompt(path: Path) -> tuple[str, dict[str, str]]:
    """Ritorna (corpo_markdown, metadata) da un file agente. Se il file non
    ha frontmatter, ritorna il testo intero con metadata vuoto."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text, {}

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    return text[match.end():], meta
