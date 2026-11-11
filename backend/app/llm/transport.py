"""Transport helpers shared by the LLM providers (extracted from `gemini.py` in M3 S1).

Gemini's behaviour is unchanged: it uses `TransientError`, `RpmLimiter`,
`is_transport_error`, `final_error` and `exponential_backoff` exactly as before (no
jitter, its own caps). Groq adds jitter through `jittered_backoff`.
"""

import asyncio
import collections
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.llm.common import (
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

TRANSPORT_ERROR_NAMES = frozenset(
    {"TransportError", "ConnectError", "ReadTimeout", "ConnectionError", "APIConnectionError"}
)


class TransientError(Exception):
    """A failure worth retrying. Its text must already be redacted."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind  # "rate_limited" | "server" | "timeout" | "transport"


@dataclass
class RpmLimiter:
    """Sliding 60-second window: at most `rpm` acquisitions per window (in-process)."""

    rpm: int
    clock: Clock = time.monotonic
    sleep: Sleep = asyncio.sleep
    _stamps: collections.deque[float] = field(default_factory=collections.deque)

    async def acquire(self) -> None:
        while True:
            now = self.clock()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            if len(self._stamps) < self.rpm:
                self._stamps.append(now)
                return
            await self.sleep(60.0 - (now - self._stamps[0]))


def is_transport_error(exc: BaseException) -> bool:
    names = {c.__name__ for c in type(exc).__mro__}
    return bool(names & TRANSPORT_ERROR_NAMES)


def exponential_backoff(attempt: int, *, initial_s: float, cap_s: float) -> float:
    """Wait before retry `attempt + 1`: initial x 2^attempt, capped (no jitter)."""
    return float(min(cap_s, initial_s * (2**attempt)))


def jittered_backoff(
    attempt: int, *, initial_s: float, cap_s: float, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with jitter: a uniform draw in [50 %, 100 %] of the capped wait."""
    base = exponential_backoff(attempt, initial_s=initial_s, cap_s=cap_s)
    return base * (0.5 + 0.5 * rng())


def final_error(failure: TransientError, *, attempts: int) -> ProviderError:
    """The error raised after retries ran out; `transient=True` lets a fallback try."""
    msg = f"{failure} (after {attempts} attempt{'s' if attempts > 1 else ''})"
    err: ProviderError
    if failure.kind == "rate_limited":
        err = ProviderRateLimited(msg)
    elif failure.kind == "timeout":
        err = ProviderTimeout(msg)
    else:
        err = ProviderUnavailable(msg)
    err.transient = True  # retries exhausted on a transient failure: fallback may help
    return err
