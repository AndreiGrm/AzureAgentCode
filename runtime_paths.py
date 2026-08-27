"""Percorsi separati per risorse dell'app e dati modificabili dell'utente."""
from __future__ import annotations

import sys
import os
from pathlib import Path


APP_DATA_DIR_NAME = "Azure DevOps Agent Dashboard"


def resource_dir() -> Path:
    """Directory che contiene risorse incluse nel pacchetto o i sorgenti."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Directory scrivibile che conserva dati e configurazione dell'utente."""
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is unavailable for saving application data.")
        path = Path(local_app_data) / APP_DATA_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(__file__).resolve().parent


def legacy_data_dirs() -> tuple[Path, ...]:
    """Directory usate dalle build desktop precedenti, per la migrazione iniziale."""
    if not getattr(sys, "frozen", False):
        return ()
    executable_dir = Path(sys.executable).resolve().parent
    return (executable_dir, executable_dir.parent.parent)
