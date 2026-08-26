"""Configurazione centralizzata, letta esclusivamente da variabili d'ambiente.

Nessun valore di default viene fornito per organizzazione, progetto o PAT:
se una variabile obbligatoria manca l'esecuzione si interrompe con un
errore chiaro, invece di proseguire con credenziali indovinate.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from azure.devops.connection import Connection
from dotenv import load_dotenv
from msrest.authentication import BasicAuthentication
from runtime_paths import data_dir, legacy_data_dirs

ENV_PATH = data_dir() / ".env"


def _migrate_legacy_env() -> None:
    if ENV_PATH.exists():
        return
    for directory in legacy_data_dirs():
        legacy_path = directory / ".env"
        if legacy_path.is_file():
            shutil.copy2(legacy_path, ENV_PATH)
            return


_migrate_legacy_env()

# Carica le variabili dal file .env dell'utente, se presente.
# Non sovrascrive variabili già impostate nell'ambiente (es. da Task
# Scheduler o da un export nella shell), quindi resta compatibile con chi
# preferisce non usare un file .env.
load_dotenv(ENV_PATH)


class ConfigError(RuntimeError):
    """Variabile d'ambiente obbligatoria mancante o non valida."""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Variabile d'ambiente obbligatoria non impostata: {name}")
    return value


@dataclass(frozen=True)
class Config:
    org_url: str
    project: str
    repo_id: str
    pat: str
    repo_path: str
    team: str | None
    base_branch: str


# Schema delle variabili modificabili dalla pagina Impostazioni della
# dashboard: (nome variabile, etichetta, obbligatoria, segreta). "Segreta"
# significa che il valore non viene mai rimandato in chiaro al browser (solo
# mascherato) e che un submit con campo vuoto lascia il valore esistente
# invariato, invece di svuotarlo.
SETTINGS_SCHEMA: list[tuple[str, str, bool, bool]] = [
    ("ORG_URL", "URL organizzazione Azure DevOps", True, False),
    ("PROJECT", "Progetto", True, False),
    ("TEAM", "Team", False, False),
    ("REPO_ID", "Repository (nome o GUID)", True, False),
    ("REPO_PATH", "Path locale del repository git", True, False),
    ("BASE_BRANCH", "Branch base", False, False),
    ("AZURE_DEVOPS_PAT", "Personal Access Token Azure DevOps", True, True),
    ("AGENT_PROVIDER", "Provider agente", False, False),
    ("AGENT_MODEL", "Modello agente predefinito", False, False),
    ("AGENT_COMMAND", "Comando agente esterno (legge il prompt da stdin)", False, False),
    ("AGENT_MAX_OUTPUT_TOKENS", "Limite token di output per esecuzione", False, False),
    ("AGENT_TOKEN_BUDGET", "Budget token totale", False, False),
    ("GRAPHIFY_ENABLED", "Usa Graphify per ricerca e piani", False, False),
    ("GRAPHIFY_COMMAND", "Comando Graphify (percorso dell'eseguibile)", False, False),
]

SETTINGS_SELECT_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "AGENT_PROVIDER": [
        ("claude_sdk", "Claude Code (predefinito)"),
        ("command", "Agente CLI esterno"),
        ("copilot_cli", "GitHub Copilot CLI (sperimentale)"),
    ],
    "AGENT_MODEL": [
        ("", "Predefinito di Claude Code"),
        ("opus", "Claude Opus — massima qualità"),
        ("sonnet", "Claude Sonnet — bilanciato"),
        ("haiku", "Claude Haiku — veloce ed economico"),
    ],
    "AGENT_MAX_OUTPUT_TOKENS": [
        ("", "Nessun limite aggiuntivo"),
        ("1024", "1.024 token"),
        ("2048", "2.048 token"),
        ("4096", "4.096 token"),
        ("8192", "8.192 token"),
    ],
    "AGENT_TOKEN_BUDGET": [
        ("", "Nessun budget"),
        ("100000", "100.000 token"),
        ("500000", "500.000 token"),
        ("1000000", "1.000.000 token"),
        ("5000000", "5.000.000 token"),
        ("10000000", "10.000.000 token"),
    ],
    "GRAPHIFY_ENABLED": [
        ("true", "Attivo"),
        ("false", "Disattivo"),
    ],
}

SETTINGS_DEFAULTS = {
    "AGENT_PROVIDER": "claude_sdk",
    "GRAPHIFY_ENABLED": "false",
}

def _mask_secret(value: str) -> str:
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else value
    return "•" * 8 + tail


def get_settings() -> list[dict]:
    """Stato corrente delle variabili configurabili, per la pagina
    Impostazioni: legge da os.environ (non rilegge il file .env), cosi'
    riflette anche un update_settings() appena fatto nello stesso processo."""
    settings = []
    for key, label, required, secret in SETTINGS_SCHEMA:
        value = os.environ.get(key, SETTINGS_DEFAULTS.get(key, ""))
        options = SETTINGS_SELECT_OPTIONS.get(key)
        if options is not None and value not in {option[0] for option in options}:
            # I valori esistenti devono restare modificabili anche se non sono
            # tra le scelte predefinite della dashboard.
            options = [(value, f"Valore configurato: {value}"), *options]
        settings.append({
            "key": key,
            "label": label,
            "required": required,
            "secret": secret,
            "is_set": bool(value),
            "value": _mask_secret(value) if secret else value,
            "control": "select" if options is not None else "text",
            "options": [
                {"value": option_value, "label": option_label}
                for option_value, option_label in options
            ] if options is not None else None,
        })
    return settings


def update_settings(updates: dict[str, str]) -> None:
    """Scrive i valori aggiornati nel file .env (preservando le righe non
    gestite) e nell'ambiente del processo corrente. Aggiornare os.environ
    qui, oltre al file, e' quello che rende il cambio effettivo da subito:
    load_config() legge sempre os.environ, e i sottoprocessi ingest/review
    lanciati dalla dashboard dopo questa chiamata erediteranno il nuovo
    valore (vedi dashboard_server._start_script_process, env=os.environ)."""
    known_keys = {key for key, *_ in SETTINGS_SCHEMA}
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []

    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in updates and key in known_keys:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    for key, value in updates.items():
        os.environ[key] = value


def load_config() -> Config:
    """Legge ORG_URL, PROJECT, REPO_ID, AZURE_DEVOPS_PAT, REPO_PATH dall'ambiente.

    TEAM e' opzionale ma va impostato quando il progetto ha piu' team: la
    macro WIQL @CurrentIteration si risolve sull'iterazione corrente di UN
    team specifico, non del progetto nel suo complesso. Senza TEAM, viene
    usato il team di default del progetto, che spesso non e' quello a cui
    appartengono i ticket dell'utente.
    """
    return Config(
        org_url=_require_env("ORG_URL"),
        project=_require_env("PROJECT"),
        repo_id=_require_env("REPO_ID"),
        pat=_require_env("AZURE_DEVOPS_PAT"),
        repo_path=_require_env("REPO_PATH"),
        team=os.environ.get("TEAM") or None,
        base_branch=os.environ.get("BASE_BRANCH") or "main",
    )


def get_connection(cfg: Config) -> Connection:
    """Crea la connessione autenticata via PAT verso l'organizzazione Azure DevOps."""
    credentials = BasicAuthentication("", cfg.pat)
    return Connection(base_url=cfg.org_url, creds=credentials)
