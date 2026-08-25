"""Verifiche tecniche deterministiche rilevate dalla configurazione del repo."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from config import Config

_SCRIPT_NAMES = {
    "test": ("test:affected", "test", "test:all"),
    "lint": ("lint:affected", "lint", "lint:all"),
    "type-check": ("typecheck", "type-check"),
    "build": ("build:affected", "build"),
}
_MAX_OUTPUT_CHARS = 12_000


def _package_manager(repo_path: Path) -> str:
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def detect_quality_commands(repo_path: str) -> list[dict[str, str]]:
    """Rileva soltanto gli script esplicitamente dichiarati dal repository."""
    package_path = Path(repo_path) / "package.json"
    if not package_path.exists():
        return []
    package = json.loads(package_path.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return []

    package_manager = _package_manager(Path(repo_path))
    commands = []
    for label, candidates in _SCRIPT_NAMES.items():
        script = next((name for name in candidates if name in scripts), None)
        if script is None and label == "build":
            build_scripts = sorted(name for name in scripts if name.startswith("build:"))
            if len(build_scripts) == 1:
                script = build_scripts[0]
            else:
                production_builds = [name for name in build_scripts if name.endswith(":prod")]
                if len(production_builds) == 1:
                    script = production_builds[0]
        if script is not None:
            commands.append({"name": label, "command": f"{package_manager} run {script}"})
    return commands


def current_commit(cfg: Config) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cfg.repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def run_quality_checks(cfg: Config) -> tuple[str, list[dict]]:
    """Esegue gli script rilevati e ritorna commit verificato e risultati."""
    commit_sha = current_commit(cfg)
    checks = []
    for definition in detect_quality_commands(cfg.repo_path):
        started_at = time.monotonic()
        try:
            result = subprocess.run(
                definition["command"],
                cwd=cfg.repo_path,
                shell=sys.platform == "win32",
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
            status = "passed" if result.returncode == 0 else "failed"
            output = (result.stdout + result.stderr).strip()
        except subprocess.TimeoutExpired as exc:
            status = "failed"
            output = f"Timeout dopo 900 secondi.\n{exc.stdout or ''}\n{exc.stderr or ''}".strip()
        checks.append({
            **definition,
            "status": status,
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "output": output[-_MAX_OUTPUT_CHARS:],
        })
    return commit_sha, checks
