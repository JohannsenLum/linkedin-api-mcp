"""Configuration and credential storage.

The only credential this server holds is a LinkedIn session cookie (`li_at`).
That cookie IS the account: anyone holding it is logged in as you, it does not
expire when you change your password, and LinkedIn does not surface a way to see
which sessions are active. It is treated accordingly — kept in the OS keyring by
default, never written to a log, and never returned by a tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE = "linkedin-api-mcp"
COOKIE_ACCOUNT = "li_at"

SETUP_URL = "https://github.com/JohannsenLum/linkedin-api-mcp#getting-your-session-cookie"


class ConfigError(RuntimeError):
    """Raised when the server cannot start with the configuration it was given."""


def cookie_help() -> str:
    return (
        "No LinkedIn session cookie found.\n\n"
        "Log in to linkedin.com in your browser, open DevTools -> Application -> "
        "Cookies -> https://www.linkedin.com, and copy the value of `li_at`.\n"
        "Then store it once with:\n\n"
        "    linkedin-api-mcp auth\n\n"
        "or set LINKEDIN_COOKIE in the environment for a throwaway session.\n"
        f"Full instructions: {SETUP_URL}"
    )


def _from_keyring() -> str | None:
    """Read the cookie from the OS keyring, if `keyring` is installed and working.

    Import is deferred and failures are swallowed deliberately: on a headless
    Linux box there is often no keyring backend at all, and that should degrade
    to the environment variable rather than crash the server on startup.
    """
    try:
        import keyring

        return keyring.get_password(SERVICE, COOKIE_ACCOUNT)
    except Exception:
        return None


def store_cookie(cookie: str) -> str:
    """Persist the cookie in the OS keyring. Returns the backend used, for display."""
    try:
        import keyring

        keyring.set_password(SERVICE, COOKIE_ACCOUNT, cookie.strip())
        return type(keyring.get_keyring()).__name__
    except Exception as exc:  # pragma: no cover - backend-specific
        # Report the failure class, not str(exc): some keyring backends embed
        # the account/service lookup (and therefore, indirectly, details of
        # what was being stored) in their exception text. Never interpolate a
        # caught exception into a message — same rule as errors.py.
        raise ConfigError(
            f"Could not write to the OS keyring ({type(exc).__name__}). "
            "Set LINKEDIN_COOKIE in the environment instead."
        ) from exc


@dataclass(frozen=True)
class Config:
    cookie: str
    headless: bool = True
    # Minimum seconds between browser actions. This is the main defence against
    # an agent looping and hammering LinkedIn from your account — see queue.py.
    min_action_interval: float = 2.0
    # Hard ceiling on actions per rolling hour, again per-account.
    max_actions_per_hour: int = 120
    nav_timeout_ms: int = 30_000

    @staticmethod
    def from_env() -> "Config":
        cookie = (os.getenv("LINKEDIN_COOKIE") or "").strip() or _from_keyring()
        if not cookie:
            raise ConfigError(cookie_help())

        # A pasted cookie often arrives wrapped in quotes, or as the whole
        # `li_at=AQED...` pair. Accept both rather than failing cryptically later
        # with "not logged in".
        cookie = cookie.strip().strip('"').strip("'")
        if cookie.lower().startswith("li_at="):
            cookie = cookie[len("li_at="):]

        return Config(
            cookie=cookie,
            headless=_flag("LINKEDIN_HEADLESS", True),
            min_action_interval=_float("LINKEDIN_MIN_INTERVAL", 2.0),
            max_actions_per_hour=_int("LINKEDIN_MAX_PER_HOUR", 120),
            nav_timeout_ms=_int("LINKEDIN_NAV_TIMEOUT_MS", 30_000),
        )

    def redacted(self) -> dict:
        """Everything about the config except the thing that must never be shown."""
        return {
            "cookie": f"<set, {len(self.cookie)} chars>",
            "headless": self.headless,
            "min_action_interval": self.min_action_interval,
            "max_actions_per_hour": self.max_actions_per_hour,
        }


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default
