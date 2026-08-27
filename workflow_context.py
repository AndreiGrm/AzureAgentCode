"""Preparazione comune del contesto per gli agenti del workflow."""
from __future__ import annotations

import os

from graphify_context import get_graphify_context

_STRUCTURED_RESPONSE_MARKERS = (
    "respond only with a json",
    "respond only with json",
    "exact format:",
)


def prepare_agent_prompt(
    prompt: str,
    repo_path: str,
    *,
    include_graphify: bool,
) -> str:
    """Aggiunge contesto mirato e istruzioni di output senza alterare contratti JSON."""
    graphify_context = (
        get_graphify_context(repo_path, prompt)
        if include_graphify
        else "Graphify skipped: this execution phase uses the approved plan and repository tools."
    )
    headroom_instruction = ""
    if os.environ.get("AGENT_USE_HEADROOM", "").strip().lower() in {"1", "true", "yes"}:
        headroom_instruction = (
            "Headroom optimization is enabled. Treat compressed context as an index: "
            "retrieve or inspect the original source before relying on a detail.\n\n"
        )

    is_structured_response = any(
        marker in prompt.lower() for marker in _STRUCTURED_RESPONSE_MARKERS
    )
    caveman_instruction = ""
    if not is_structured_response:
        caveman_instruction = (
            "For your final user-facing response, use Caveman Ultra style: retain every "
            "decision, risk, changed file, and verification result, but omit filler and "
            "repetition. Do not abbreviate required status markers or requested headings.\n\n"
        )

    return (
        "Workflow order: first work from the well-scoped request below; use Graphify "
        "context before broad repository searches; verify cited source files before changing "
        "them.\n\n"
        f"{headroom_instruction}"
        f"{graphify_context}\n\n"
        f"{prompt}\n\n"
        f"{caveman_instruction}"
    )
