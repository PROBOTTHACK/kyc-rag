"""Bounded retry with exponential backoff for external calls (CLAUDE.md principle 8).

Only for idempotent operations: a retried call must be safe to repeat (Qdrant upsert of
the same point id, delete by id, reads). Every loop is bounded: at most
`DEFAULT_ATTEMPTS` tries (one call plus two retries).
"""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

log = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_ATTEMPTS = 3  # 1 call + 2 retries
DEFAULT_BASE_DELAY_S = 0.5  # then 1.0 s


def is_transient(exc: BaseException) -> bool:
    """Network failures (the Qdrant client wraps transport errors in
    `ResponseHandlingException`), timeouts, 429 and 5xx answers; never other 4xx errors."""
    if isinstance(exc, UnexpectedResponse):
        status = exc.status_code or 0
        return status == 429 or status >= 500
    return isinstance(exc, ResponseHandlingException | ConnectionError | TimeoutError)


def call_with_backoff(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`; retry transient failures with delays base, 2*base, ... (bounded)."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            delay = base_delay_s * 2 ** (attempt - 1)
            log.warning(
                "%s failed (%s: %s); retry %d/%d in %.1fs",
                what, type(exc).__name__, exc, attempt, attempts - 1, delay,
            )  # fmt: skip
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
