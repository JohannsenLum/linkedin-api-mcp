"""Sequential execution queue and rate limiting.

Every tool call in this server passes through `ActionQueue.run`. Two things
happen there, and both exist because the account at risk is the user's real one:

  1. Calls are SERIALISED. One browser, one page, one action at a time. An agent
     that fires eight profile lookups in parallel would otherwise drive the same
     page eight ways at once — which breaks the scrape and looks exactly like
     the automated traffic LinkedIn watches for.

  2. Calls are PACED, with a floor between actions and a ceiling per hour. An
     agent in a retry loop is the realistic way an account gets flagged, and it
     is not something a README can prevent. The limit refuses rather than
     silently sleeping for an hour, so the model gets told to stop instead of
     the user watching a hang.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class RateLimited(RuntimeError):
    """Raised when the hourly action ceiling is reached."""

    def __init__(self, used: int, ceiling: int, retry_after_s: float) -> None:
        self.used = used
        self.ceiling = ceiling
        self.retry_after_s = retry_after_s
        mins = max(1, round(retry_after_s / 60))
        super().__init__(
            f"Rate limit reached: {used} LinkedIn actions in the last hour "
            f"(ceiling {ceiling}). The next slot frees up in about {mins} minute"
            f"{'s' if mins != 1 else ''}.\n\n"
            "This limit is local to this server, not LinkedIn's — it exists so an "
            "agent in a loop cannot get your account restricted. Stop issuing "
            "LinkedIn tool calls and tell the user what happened rather than retrying."
        )


class ActionQueue:
    def __init__(self, min_interval_s: float, max_per_hour: int) -> None:
        self._lock = asyncio.Lock()
        self._min_interval = min_interval_s
        self._max_per_hour = max_per_hour
        self._last_action_at = 0.0
        self._history: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        while self._history and self._history[0] < cutoff:
            self._history.popleft()

    def snapshot(self) -> dict:
        """Current usage, for the health tool. Cheap and lock-free by design."""
        now = time.monotonic()
        self._prune(now)
        return {
            "actions_last_hour": len(self._history),
            "hourly_ceiling": self._max_per_hour,
            "min_seconds_between_actions": self._min_interval,
        }

    async def run(self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Run `fn` as the only LinkedIn action in flight, after waiting its turn."""
        async with self._lock:
            now = time.monotonic()
            self._prune(now)

            if len(self._history) >= self._max_per_hour:
                oldest = self._history[0]
                raise RateLimited(
                    used=len(self._history),
                    ceiling=self._max_per_hour,
                    retry_after_s=max(0.0, 3600 - (now - oldest)),
                )

            # Pace against the previous action rather than sleeping unconditionally,
            # so a user who thinks between prompts is never made to wait.
            gap = now - self._last_action_at
            if self._last_action_at and gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)

            self._last_action_at = time.monotonic()
            self._history.append(self._last_action_at)
            return await fn()
