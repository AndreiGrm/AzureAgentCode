"""Percorsi separati per risorse dell'app e dati modificabili dell'utente."""
from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    """Directory che contiene risorse incluse nel pacchetto o i sorgenti."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Directory accanto all'exe per configurazione, storico e log."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
