"""Observability helpers: structured logging + network retry.

Replaces scattered ``print`` calls and silent ``except Exception`` swallows so
transient failures (DNS after a laptop wake, a flaky source, a bad API key)
show up in the logs instead of vanishing into a rule-based fallback.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, TypeVar

_T = TypeVar("_T")

_CONFIGURED = False


def setup_logging() -> None:
    """Configure root logging once, level from ``LOG_LEVEL`` (default INFO)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def with_retries(
    fn: Callable[[], _T],
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    logger: logging.Logger | None = None,
    label: str = "operation",
) -> _T:
    """Run ``fn`` up to ``attempts`` times with exponential backoff.

    Re-raises the last exception if every attempt fails.
    """
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retry then re-raise
            last_exc = exc
            if logger is not None:
                logger.warning(
                    "%s failed (attempt %d/%d): %s: %s",
                    label,
                    attempt,
                    attempts,
                    type(exc).__name__,
                    exc,
                )
            if attempt < attempts:
                time.sleep(backoff * attempt)
    assert last_exc is not None
    raise last_exc
