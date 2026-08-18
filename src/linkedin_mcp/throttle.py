"""Sequential execution queue and rate limiting.

Every tool call in this server passes through `ActionQueue.run`. Two things
happen there, and both exist because the account at risk is the user's real one:

  1. Calls are SERIALISED. One browser, one page, one action at a time. An agent
     that fires eight profile lookups in parallel would otherwise drive the same
     page eight ways at once, which breaks the scrape and looks exactly like
     the automated traffic LinkedIn watches for.

  2. Calls are PACED, with a floor between actions and a ceiling per hour. An
     agent in a retry loop is the realistic way an account gets flagged, and it
     is not something a README can prevent. The limit refuses rather than
     silently sleeping for an hour, so the model gets told to stop instead of
     the user watching a hang.

The hourly ceiling is stored on disk (wall-clock timestamps + a file lock) so a
process restart or a second instance sharing the same state file cannot reset
or double the budget. The minimum gap between actions stays in-process only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

_HOUR_S = 3600.0
_DEFAULT_STATE_NAME = "rate_limit.json"


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
            "This limit is local to this server, not LinkedIn's: it exists so an "
            "agent in a loop cannot get your account restricted. Stop issuing "
            "LinkedIn tool calls and tell the user what happened rather than retrying."
        )


def default_state_path() -> Path:
    """Directory shared by all processes for the same user account."""
    override = (os.getenv("LINKEDIN_RATE_STATE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".linkedin-api-mcp" / _DEFAULT_STATE_NAME


class _FileLock:
    """Best-effort exclusive lock via a sibling `.lock` file.

    The lock file is separate from the JSON state file so Windows can still
    replace the state path while the lock is held. Uses msvcrt on Windows and
    fcntl elsewhere. If locking is unavailable writes still go through; multi
    process races are then best-effort only.
    """

    def __init__(self, path: Path) -> None:
        # Sibling lock file, not the JSON itself.
        self._path = path.with_name(path.name + ".lock")
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                if self._fh.read(1) == "":
                    self._fh.write("0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            # Fall through without a lock rather than crash the server.
            pass
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


def _prune_history(history: deque[float], now: float) -> None:
    cutoff = now - _HOUR_S
    while history and history[0] < cutoff:
        history.popleft()


def _load_history(path: Path) -> deque[float]:
    if not path.is_file():
        return deque()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return deque()
    stamps: list[float] = []
    if isinstance(raw, dict):
        items = raw.get("actions") or raw.get("history") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    for item in items:
        try:
            stamps.append(float(item))
        except (TypeError, ValueError):
            continue
    stamps.sort()
    history: deque[float] = deque(stamps)
    _prune_history(history, time.time())
    return history


def _save_history(path: Path, history: deque[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"actions": list(history)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


class ActionQueue:
    def __init__(
        self,
        min_interval_s: float,
        max_per_hour: int,
        state_path: Path | str | None = None,
        *,
        persist: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        self._min_interval = min_interval_s
        self._max_per_hour = max_per_hour
        self._last_action_at = 0.0  # monotonic; in-process pacing only
        self._persist = persist
        self._state_path = Path(state_path) if state_path else default_state_path()
        # Wall-clock stamps for the rolling hour (survive restarts).
        if self._persist:
            with _FileLock(self._state_path):
                self._history: deque[float] = _load_history(self._state_path)
        else:
            self._history = deque()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _prune(self, now_wall: float) -> None:
        _prune_history(self._history, now_wall)

    def snapshot(self) -> dict:
        """Current usage, for the health tool."""
        now = time.time()
        if self._persist:
            with _FileLock(self._state_path):
                self._history = _load_history(self._state_path)
                self._prune(now)
                used = len(self._history)
        else:
            self._prune(now)
            used = len(self._history)
        return {
            "actions_last_hour": used,
            "hourly_ceiling": self._max_per_hour,
            "min_seconds_between_actions": self._min_interval,
            "rate_state_path": str(self._state_path) if self._persist else None,
        }

    def _reserve_slot_locked(self) -> None:
        """Under file lock: reload, prune, check ceiling, append, save."""
        now = time.time()
        if self._persist:
            self._history = _load_history(self._state_path)
        self._prune(now)
        if len(self._history) >= self._max_per_hour:
            oldest = self._history[0]
            raise RateLimited(
                used=len(self._history),
                ceiling=self._max_per_hour,
                retry_after_s=max(0.0, _HOUR_S - (now - oldest)),
            )
        self._history.append(now)
        if self._persist:
            _save_history(self._state_path, self._history)

    async def run(self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Run `fn` as the only LinkedIn action in flight, after waiting its turn."""
        async with self._lock:
            # Pace against the previous action in this process.
            now_mono = time.monotonic()
            gap = now_mono - self._last_action_at
            if self._last_action_at and gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)

            if self._persist:
                with _FileLock(self._state_path):
                    self._reserve_slot_locked()
            else:
                self._reserve_slot_locked()

            self._last_action_at = time.monotonic()
            return await fn()
