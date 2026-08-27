"""Singolo retry automatico per le chiamate alle API di Azure DevOps.

Le API remote possono fallire per motivi transitori (timeout, rate limit).
Un solo nuovo tentativo, con un breve ritardo, evita che un errore
momentaneo blocchi l'intero run senza mascherare guasti persistenti.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_once(delay_seconds: float = 2.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - vogliamo ritentare qualsiasi errore di rete/API
                logger.warning(
                    "Call to %s failed (%s): retrying after %.1fs",
                    func.__name__, exc, delay_seconds,
                )
                time.sleep(delay_seconds)
                return func(*args, **kwargs)

        return wrapper

    return decorator
