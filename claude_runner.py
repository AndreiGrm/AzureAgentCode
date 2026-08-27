"""Runner configurabile per invocare Claude Code o un agente CLI esterno.

Usato sia da ingest_loop.py (piano, implementazione, fix) sia da
review_loop.py (fix dei commenti di review). permission_mode="dontAsk"
abbinato ad allowed_tools fa si' che i tool elencati siano pre-approvati e
qualunque altro tool venga negato subito, invece di restare in attesa di
un'approvazione interattiva che in un run non presidiato (cron) non
arriverebbe mai.

Usa ClaudeSDKClient (sessione persistente, streaming) invece della funzione
one-shot query(): questo permette, quando work_item_id/run_id sono forniti,
di controllare periodicamente se l'utente ha inviato una correzione dalla
dashboard MENTRE l'agente sta ancora lavorando (history.get_pending_correction)
e, in tal caso, interrompere la generazione in corso (client.interrupt()) e
iniettare la correzione come nuovo messaggio sulla stessa sessione
(client.query()), invece di aspettare che il turno corrente finisca.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

import history
from workflow_context import prepare_agent_prompt

logger = logging.getLogger(__name__)

# Rete di sicurezza per uno stream davvero bloccato (bug SDK/CLI), non un
# buffer per la normale latenza di rete/ragionamento: verificato con uno
# spike manuale che un turno normale puo' restare senza messaggi per piu'
# di un minuto (thinking prolungato, chiamate API lente) senza che ci sia
# nulla di rotto. Un valore troppo basso qui causa perdita silenziosa del
# risultato (il turno viene chiuso a forza prima che arrivi).
INTERRUPTED_TURN_GRACE_S = 180.0


@dataclass
class ClaudeRunResult:
    output: str
    corrections_applied: list[str] = field(default_factory=list)


def _positive_environment_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return parsed


def _ensure_token_budget_available() -> None:
    token_limit = _positive_environment_int("AGENT_TOKEN_BUDGET")
    if token_limit is None:
        return
    status = history.get_token_budget_status(token_limit)
    if status["is_exhausted"]:
        raise RuntimeError(
            f"Token budget exhausted ({status['used_tokens']:,}/{token_limit:,}). "
            "Update AGENT_TOKEN_BUDGET in Settings before starting more agents."
        )


def _run_command_agent(
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    model: str | None,
) -> ClaudeRunResult:
    command = os.environ.get("AGENT_COMMAND", "").strip()
    if not command:
        raise RuntimeError(
            "AGENT_COMMAND is required when AGENT_PROVIDER is 'command'. "
            "The command must read the prompt from standard input."
        )

    environment = os.environ.copy()
    environment["AGENT_MODEL"] = model or os.environ.get("AGENT_MODEL", "")
    environment["AGENT_ALLOWED_TOOLS"] = ",".join(allowed_tools)
    environment["AGENT_MAX_OUTPUT_TOKENS"] = os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "")
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=environment,
        shell=True,
        capture_output=True,
        check=False,
    )
    authentication_output = f"{completed.stdout}\n{completed.stderr}".lower()
    if "oauth session expired" in authentication_output:
        raise RuntimeError(
            "The external CLI agent session has expired. "
            "Open a terminal, run `claude login`, then try again."
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no details available"
        raise RuntimeError(f"External agent exited with code {completed.returncode}: {detail}")
    return ClaudeRunResult(output=completed.stdout)


def _run_copilot_cli_agent(
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    model: str | None,
) -> ClaudeRunResult:
    """Esegue Copilot CLI in modalita' non interattiva, limitato alla repository."""
    if shutil.which("copilot") is None:
        raise RuntimeError(
            "GitHub Copilot CLI is not installed or is not on PATH. "
            "Install it with `winget install GitHub.Copilot`, then run `copilot login`."
        )
    scoped_prompt = (
        f"{prompt}\n\nOperational permission boundary: use only the capabilities "
        f"required for this run ({', '.join(allowed_tools)}); do not access paths "
        "outside the current repository or use network tools."
    )
    command = [
        "copilot", "-C", cwd, "--prompt", scoped_prompt, "--silent", "--no-remote",
        "--no-ask-user", "--allow-all",
    ]
    if model:
        command.extend(["--model", model])
    if os.environ.get("AGENT_USE_HEADROOM", "").strip().lower() in {"1", "true", "yes"}:
        if shutil.which("headroom") is None:
            raise RuntimeError(
                "AGENT_USE_HEADROOM is enabled but the 'headroom' command was not found on PATH."
            )
        command = ["headroom", "wrap", "copilot", "--", *command[1:]]
    completed = subprocess.run(
        command, cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no details available"
        raise RuntimeError(f"GitHub Copilot CLI exited with code {completed.returncode}: {detail}")
    return ClaudeRunResult(output=completed.stdout)


def _select_provider(configured_provider: str, allowed_tools: list[str]) -> str:
    """In auto, riserva Claude ai run che possono modificare repository o Git."""
    if configured_provider != "auto":
        return configured_provider
    return "claude_sdk" if {"Bash", "Edit"} & set(allowed_tools) else "copilot_cli"


def run_claude(
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    *,
    model: str | None = None,
    work_item_id: int | None = None,
    run_id: int | None = None,
    poll_interval: float = 1.5,
) -> ClaudeRunResult:
    """Esegue un prompt con Claude Code e ritorna il testo prodotto.

    Blocca fino al termine del turno (e di eventuali turni aggiuntivi
    generati da correzioni dell'utente). Se work_item_id/run_id sono None,
    il comportamento e' equivalente a una singola query one-shot: nessuna
    correzione viene cercata. model e' opzionale: se assente si usa il
    default del CLI (utile per riusare file agente che dichiarano un
    modello specifico nel proprio frontmatter, es. "opus").
    """

    _ensure_token_budget_available()
    configured_model = os.environ.get("AGENT_MODEL", "").strip() or None
    effective_model = model or configured_model
    max_output_tokens = _positive_environment_int("AGENT_MAX_OUTPUT_TOKENS")
    if max_output_tokens is not None:
        prompt = (
            f"{prompt}\n\nOperational constraint: keep the response within "
            f"{max_output_tokens} output tokens."
        )
    provider = _select_provider(
        os.environ.get("AGENT_PROVIDER", "claude_sdk").strip() or "claude_sdk",
        allowed_tools,
    )
    prompt = prepare_agent_prompt(
        prompt,
        cwd,
        include_graphify=provider == "copilot_cli",
    )
    if provider == "command":
        return _run_command_agent(prompt, cwd, allowed_tools, effective_model)
    if provider == "copilot_cli":
        return _run_copilot_cli_agent(prompt, cwd, allowed_tools, effective_model)
    if provider != "claude_sdk":
        raise RuntimeError(f"Unsupported agent provider: {provider}")

    async def _run() -> ClaudeRunResult:
        options = ClaudeAgentOptions(
            cwd=cwd,
            allowed_tools=allowed_tools,
            permission_mode="dontAsk",
            model=effective_model,
        )

        lock = anyio.Lock()
        # La query iniziale (subito dopo il connect) conta gia' come turno
        # "mandato": sent/received si equivalgono solo quando ogni query()
        # inviata ha ricevuto il proprio ResultMessage.
        sent = 1
        received = 0
        turn_chunks: list[str] = []
        turns: list[str] = []
        corrections_applied: list[str] = []
        last_activity = anyio.current_time()

        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)

            async def receiver() -> None:
                nonlocal received, last_activity
                async for message in client.receive_messages():
                    async with lock:
                        last_activity = anyio.current_time()
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                turn_chunks.append(block.text)
                    elif isinstance(message, ResultMessage):
                        # message.result e' il testo finale completo del
                        # turno (non un incremento): usarlo insieme ai
                        # TextBlock gia' accumulati duplicherebbe il testo
                        # (verificato con lo spike manuale). Nei turni senza
                        # result (es. interrotti: is_error=True, result=None)
                        # restano gli eventuali TextBlock parziali accumulati.
                        turns.append(message.result if message.result else "".join(turn_chunks))
                        turn_chunks.clear()
                        async with lock:
                            received += 1
                        if run_id is not None:
                            usage = message.usage or {}
                            history.record_usage(
                                run_id, work_item_id, message.total_cost_usd or 0.0,
                                usage.get("input_tokens", 0) or 0,
                                usage.get("output_tokens", 0) or 0,
                                usage.get("cache_creation_input_tokens", 0) or 0,
                                usage.get("cache_read_input_tokens", 0) or 0,
                            )

            async def corrector(cancel_scope: anyio.CancelScope) -> None:
                nonlocal sent, received
                has_correction_channel = work_item_id is not None and run_id is not None
                while True:
                    await anyio.sleep(poll_interval if has_correction_channel else 0.2)
                    async with lock:
                        pending = (
                            history.get_pending_correction(work_item_id, run_id)
                            if has_correction_channel
                            else None
                        )
                        if pending is not None:
                            if received < sent:
                                await client.interrupt()
                            history.consume_correction(pending["id"])
                            await client.query(
                                "[The user sent this correction while you were working; account for it "
                                f"immediately]\n{pending['text']}"
                            )
                            sent += 1
                            corrections_applied.append(pending["text"])
                            continue

                        if received >= sent:
                            cancel_scope.cancel()
                            return

                        if anyio.current_time() - last_activity > INTERRUPTED_TURN_GRACE_S:
                            logger.warning(
                                "run_claude: no activity for %.0fs with an unresolved "
                                "turn; forcing closure (partial text retained)",
                                INTERRUPTED_TURN_GRACE_S,
                            )
                            if turn_chunks:
                                turns.append("".join(turn_chunks))
                                turn_chunks.clear()
                            received = sent

            async with anyio.create_task_group() as tg:
                tg.start_soon(receiver)
                tg.start_soon(corrector, tg.cancel_scope)

        return ClaudeRunResult(
            output=turns[-1] if turns else "",
            corrections_applied=corrections_applied,
        )

    return anyio.run(_run)
