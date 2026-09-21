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
from urllib.error import HTTPError, URLError

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
    should_retry: Callable[[Exception], bool] | None = None,
) -> _T:
    """Run ``fn`` up to ``attempts`` times with exponential backoff.

    ``should_retry`` can veto a retry: a bad API key fails identically three
    times, so sleeping between attempts only delays the fallback. Re-raises
    the last exception if every allowed attempt fails.
    """
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retry then re-raise
            last_exc = exc
            retryable = should_retry is None or should_retry(exc)
            if logger is not None:
                logger.warning(
                    "%s failed (attempt %d/%d, retryable=%s): %s: %s",
                    label,
                    attempt,
                    attempts,
                    retryable,
                    type(exc).__name__,
                    exc,
                )
            if not retryable:
                raise
            if attempt < attempts:
                time.sleep(backoff * attempt)
    assert last_exc is not None
    raise last_exc


def is_transient_http_error(exc: Exception) -> bool:
    """True for failures worth retrying: throttling, 5xx, and network faults.

    Observed in production: Gemini returned ``503 Service Unavailable`` mid
    send and the brief silently dropped to the rule-based fallback.
    """
    if isinstance(exc, HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (URLError, TimeoutError, OSError))
