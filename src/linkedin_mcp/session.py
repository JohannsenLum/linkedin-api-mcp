"""Browser session lifecycle.

One browser, one context, one page, for the life of the server. LinkedIn ties a
lot of behaviour to session continuity, and tearing down a browser per tool call
would both be slow and look far stranger than a person browsing.

The cookie is injected into the context rather than driven through a login form.
Automating the login form is what triggers checkpoints; presenting a session that
already exists is what a browser normally does.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .config import Config
from .errors import LinkedInError, checkpoint, not_authenticated, timed_out

BASE = "https://www.linkedin.com"

# Paths LinkedIn redirects to when it does not accept the session, or wants a human.
_LOGIN_MARKERS = ("/login", "/uas/login", "/authwall", "/signup")
_CHECKPOINT_MARKERS = ("/checkpoint", "/challenge")


def _classify_navigation_failure(exc: Exception) -> LinkedInError:
    """Turn a Chromium network error into something the user can act on.

    The exception's text is READ here to classify, and never returned: it embeds
    the URL being driven, which on LinkedIn can carry session-scoped parameters.

    The redirect loop is the case worth singling out. A malformed `li_at` sends
    LinkedIn into a cycle: the feed bounces to login because the session is not
    valid, login bounces back to the feed because a cookie is present, and
    Chromium gives up after twenty hops. An expired cookie does not do this, it
    lands cleanly on /login and is caught below as not_authenticated. So a
    redirect loop on linkedin.com means the cookie is the wrong shape rather
    than merely stale, and telling the user to check their network sends them
    looking in entirely the wrong place.
    """
    text = str(exc)

    if "ERR_TOO_MANY_REDIRECTS" in text:
        return LinkedInError(
            "bad_cookie",
            "LinkedIn redirected in a loop, which means the session cookie is malformed.",
            "This is not a network problem. Copy the `li_at` value again from DevTools -> "
            "Application -> Cookies -> https://www.linkedin.com, taking the Value column "
            "only, then re-run `linkedin-api-mcp auth`. A cookie that is merely expired "
            "fails differently, so this points at a truncated or mistyped value.",
        )

    for marker in ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION_"):
        if marker in text:
            return LinkedInError(
                "network_unreachable",
                "The browser could not reach linkedin.com.",
                "Check this machine's network connection, DNS, and any VPN or proxy that "
                "might be blocking it.",
            )

    if "ERR_CERT" in text or "SSL" in text:
        return LinkedInError(
            "tls_failure",
            "The TLS handshake with linkedin.com failed.",
            "Usually a proxy or corporate network intercepting HTTPS. Try from a different "
            "network to confirm.",
        )

    return LinkedInError(
        "navigation_failed",
        "The browser could not load the page.",
        "Check that linkedin.com is reachable from this machine.",
    )


class Session:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._page is not None:
                return

            try:
                from patchright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - install-time problem
                raise LinkedInError(
                    "missing_browser",
                    "The browser automation dependency is not installed.",
                    "Run `uvx --from linkedin-api-mcp patchright install chromium`, or "
                    "`patchright install chromium` in your environment.",
                ) from exc

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._config.headless,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            await self._context.add_cookies(
                [
                    {
                        "name": "li_at",
                        "value": self._config.cookie,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    }
                ]
            )
            self._context.set_default_timeout(self._config.nav_timeout_ms)
            self._page = await self._context.new_page()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = self._browser = self._context = self._page = None

    async def goto(self, path: str, wait_for: str | None = None) -> Any:
        """Navigate to a LinkedIn path and hand back the page, or raise a typed error.

        Every navigation funnels through here so the logged-out and checkpoint cases
        are detected in exactly one place. Individual tools cannot forget to check.
        """
        await self.start()
        url = path if path.startswith("http") else f"{BASE}{path}"

        try:
            await self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if "Timeout" in type(exc).__name__ or "timeout" in str(type(exc)).lower():
                raise timed_out(f"{path} to load") from exc
            raise _classify_navigation_failure(exc) from exc

        landed = self._page.url
        if any(m in landed for m in _CHECKPOINT_MARKERS):
            raise checkpoint()
        if any(m in landed for m in _LOGIN_MARKERS):
            raise not_authenticated()

        if wait_for:
            try:
                await self._page.wait_for_selector(wait_for, timeout=self._config.nav_timeout_ms)
            except Exception as exc:
                # Deliberately not fatal: LinkedIn ships several markups for the same
                # page, so a missing selector is a signal for the caller to try its
                # fallbacks rather than a reason to abort the whole call.
                del exc

        return self._page

    @property
    def page(self) -> Any:
        if self._page is None:
            raise LinkedInError(
                "not_started",
                "The browser session has not been started.",
                "This is an internal error. Please report it.",
            )
        return self._page
