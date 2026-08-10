"""Test doubles for `Session` and the Playwright `Page`/`Locator` APIs.

No network and no browser: every tool in linkedin_mcp.tools.* only ever
touches a page through `Session.goto`, and the parsers only ever call a
small, fixed subset of Playwright's Page / ElementHandle / Locator surface
(query_selector*, locator/get_by_text, evaluate, wait_for_selector,
wait_for_timeout, click, fill). This module implements exactly that subset
against a `BeautifulSoup`-parsed HTML string, so a test can hand a tool a
canned HTML page and assert on what the real parsing code extracts from it,
without ever starting Chromium or making a request.

It deliberately does not try to be a general Playwright emulator: unsupported
CSS (Playwright-only pseudo-classes like `:has-text()`) is treated as "no
match" rather than raising, which mirrors how the real page would behave for
a selector Playwright accepts but this fake can't evaluate, and matches the
production code's own pattern of trying several fallback selectors and
moving on.
"""

from __future__ import annotations

import re
from typing import Any, Pattern

from bs4 import BeautifulSoup, Tag


class FakeTimeoutError(Exception):
    """Stands in for Playwright's TimeoutError: a wait that found nothing."""


def _select(scope: Any, selector: str, *, first: bool):
    stripped = selector.strip()
    if stripped.startswith("text="):
        # Playwright's own `text=` selector engine, used by
        # people.py's _NOT_AVAILABLE_SELECTORS, not real CSS, so it's
        # handled separately as a substring text search.
        needle = stripped[len("text="):].strip().strip("'\"")
        matches = _find_by_text([scope], needle)
        if first:
            return matches[0] if matches else None
        return matches
    try:
        if first:
            return scope.select_one(selector)
        return scope.select(selector)
    except Exception:
        # Unsupported/invalid selector syntax (e.g. Playwright-only
        # `:has-text()`) degrades to "no match", same as a selector that is
        # syntactically fine but matches nothing in this DOM.
        return None if first else []


def _as_pattern(pattern: str | Pattern[str]) -> Pattern[str]:
    if isinstance(pattern, re.Pattern):
        return pattern
    return re.compile(re.escape(pattern), re.IGNORECASE)


def _find_by_text(roots: list[Any], pattern: str | Pattern[str]) -> list[Tag]:
    """Approximate Playwright's getByText: innermost elements whose own
    rendered text matches, not every ancestor that merely contains one."""
    rx = _as_pattern(pattern)
    candidates: list[Tag] = []
    seen: set[int] = set()
    for root in roots:
        pool = [root] if isinstance(root, Tag) else []
        if hasattr(root, "find_all"):
            pool += root.find_all(True)
        for tag in pool:
            if id(tag) in seen:
                continue
            seen.add(id(tag))
            text = tag.get_text(" ", strip=True)
            if text and rx.search(text):
                candidates.append(tag)

    candidate_ids = {id(c) for c in candidates}
    leaves = []
    for tag in candidates:
        descendants = tag.find_all(True)
        if any(id(d) in candidate_ids for d in descendants):
            continue
        leaves.append(tag)
    return leaves


def _evaluate(scope: Any, js: str) -> Any:
    """Covers exactly the handful of inline scripts the tools actually run:
    the dt/dd 'Overview' reader (companies.py) and the h3-sibling 'criteria'
    reader (jobs.py), plus the no-op scroll calls used to page through a
    lazily-loaded list."""
    if "querySelectorAll('dt')" in js or 'querySelectorAll("dt")' in js:
        out: dict[str, str] = {}
        for dt in scope.select("dt"):
            dd = dt.find_next_sibling()
            if dd is not None and dd.name == "dd":
                label = dt.get_text(strip=True)
                value = dd.get_text(strip=True)
                if label and value:
                    out[label] = value
        return out

    if "querySelectorAll('h3')" in js or 'querySelectorAll("h3")' in js:
        out = {}
        for h in scope.select("h3"):
            sib = h.find_next_sibling()
            if sib is None:
                continue
            label = h.get_text(strip=True)
            value = sib.get_text(strip=True)
            if label and value and len(value) < 100:
                out[label] = value
        return out

    if "scrollTo" in js or "scrollBy" in js:
        return None

    raise NotImplementedError(f"FakePage.evaluate: no fake implementation for: {js[:60]!r}")


class FakeElement:
    """Stands in for Playwright's ElementHandle, scoped to one bs4 Tag."""

    def __init__(self, tag: Tag, page: "FakePage") -> None:
        self.tag = tag
        self.page = page

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.tag.get_text("\n", strip=True)

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        value = self.tag.get(name)
        if isinstance(value, list):
            return " ".join(value)
        return value

    async def query_selector(self, selector: str) -> "FakeElement | None":
        found = _select(self.tag, selector, first=True)
        return FakeElement(found, self.page) if found is not None else None

    async def query_selector_all(self, selector: str) -> list["FakeElement"]:
        return [FakeElement(t, self.page) for t in _select(self.tag, selector, first=False)]

    async def click(self, timeout: int | None = None) -> None:
        self.page.clicked.append(self.tag)

    async def fill(self, text: str, timeout: int | None = None) -> None:
        self.page.filled.append((self.tag, text))

    async def evaluate(self, js: str) -> Any:
        return _evaluate(self.tag, js)

    async def count(self) -> int:  # Locator/ElementHandle overlap used nowhere, kept cheap
        return 1

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.page, [self.tag], selector)

    def get_by_text(self, pattern: str | Pattern[str]) -> "FakeLocator":
        return FakeLocator(self.page, _find_by_text([self.tag], pattern), None)


class FakeLocator:
    """Stands in for Playwright's Locator.

    `base` is an already-resolved list of Tags; `pending`, if set, is a CSS
    selector still to be applied under each of them the next time this
    locator is resolved. `.first` / `.nth` narrow `base` and clear
    `pending`; `.locator()` chains a new `pending` selector onto the current
    resolution: the same two-phase laziness Playwright's own Locator has.
    """

    def __init__(self, page: "FakePage", base: list[Tag], pending: str | None) -> None:
        self.page = page
        self.base = base
        self.pending = pending

    def _resolve(self) -> list[Tag]:
        if self.pending is None:
            return self.base
        out: list[Tag] = []
        for tag in self.base:
            out.extend(_select(tag, self.pending, first=False))
        return out

    async def count(self) -> int:
        return len(self._resolve())

    @property
    def first(self) -> "FakeLocator":
        resolved = self._resolve()
        return FakeLocator(self.page, resolved[:1], None)

    def nth(self, index: int) -> "FakeLocator":
        resolved = self._resolve()
        if 0 <= index < len(resolved):
            return FakeLocator(self.page, [resolved[index]], None)
        return FakeLocator(self.page, [], None)

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.page, self._resolve(), selector)

    def get_by_text(self, pattern: str | Pattern[str]) -> "FakeLocator":
        return FakeLocator(self.page, _find_by_text(self._resolve(), pattern), None)

    async def inner_text(self, timeout: int | None = None) -> str:
        resolved = self._resolve()
        if not resolved:
            raise FakeTimeoutError("no element to read text from")
        return resolved[0].get_text("\n", strip=True)

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        resolved = self._resolve()
        if not resolved:
            return None
        value = resolved[0].get(name)
        if isinstance(value, list):
            return " ".join(value)
        return value

    async def click(self, timeout: int | None = None) -> None:
        resolved = self._resolve()
        if resolved:
            self.page.clicked.append(resolved[0])

    async def get_by_text_count(self, pattern: str | Pattern[str]) -> int:  # convenience, unused by prod code
        return len(_find_by_text(self._resolve(), pattern))


class FakePage:
    """Stands in for Playwright's Page, backed by a static HTML document.

    `evaluate_result`, when set, is returned from every `evaluate` call instead
    of running the built-in HTML-backed stubs. That is how tests drive tools
    whose primary path is a structural `page.evaluate(...)` (e.g.
    `search_people`'s `_SEARCH_ROWS_JS`) without a real browser: pass the
    canned rows the production JS would have returned.
    """

    def __init__(
        self,
        html: str,
        url: str = "https://www.linkedin.com/",
        *,
        evaluate_result: Any = None,
    ) -> None:
        self.soup = BeautifulSoup(html, "html.parser")
        self.url = url
        self.clicked: list[Tag] = []
        self.filled: list[tuple[Tag, str]] = []
        self.evaluate_result = evaluate_result

    async def query_selector(self, selector: str) -> FakeElement | None:
        found = _select(self.soup, selector, first=True)
        return FakeElement(found, self) if found is not None else None

    async def query_selector_all(self, selector: str) -> list[FakeElement]:
        return [FakeElement(t, self) for t in _select(self.soup, selector, first=False)]

    async def wait_for_selector(self, selector: str, timeout: int | None = None) -> FakeElement:
        found = _select(self.soup, selector, first=True)
        if found is None:
            raise FakeTimeoutError(f"Timeout waiting for selector: {selector!r}")
        return FakeElement(found, self)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, js: str, *args: Any) -> Any:
        # Production code sometimes passes extra args (e.g. search limit).
        # Structural-path tests supply evaluate_result; otherwise fall through
        # to the handful of HTML-backed stubs (or raise NotImplementedError).
        if self.evaluate_result is not None:
            return self.evaluate_result
        del args  # unused by the HTML-backed stubs
        return _evaluate(self.soup, js)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, [self.soup], selector)

    def get_by_text(self, pattern: str | Pattern[str]) -> FakeLocator:
        return FakeLocator(self, _find_by_text([self.soup], pattern), None)


class GuardedSession:
    """A `Session` double that fails the test loudly if `goto` is ever
    called, for asserting that input validation refuses before any
    navigation happens."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def goto(self, path: str, wait_for: str | None = None) -> None:
        self.calls.append(path)
        raise AssertionError(
            f"Session.goto must not be called for this input (called with {path!r})"
        )


class FakeSession:
    """A `Session` double that returns canned `FakePage`s instead of driving
    a real browser.

    `pages` may be a single `FakePage` (returned for every `goto`), or a
    dict mapping a path prefix to the `FakePage` to return for paths
    starting with it, enough to cover tools that navigate more than once
    per call (e.g. `get_profile` with `sections=["posts"]`).
    """

    def __init__(self, pages: FakePage | dict[str, FakePage]) -> None:
        self._pages: dict[str, FakePage] = {"": pages} if isinstance(pages, FakePage) else pages
        self.calls: list[str] = []

    async def goto(self, path: str, wait_for: str | None = None) -> FakePage:
        self.calls.append(path)
        # Longest matching prefix wins, so a specific route (e.g.
        # "/in/jane/recent-activity/") beats the catch-all "" or "/in/jane/".
        best_prefix = None
        for prefix in self._pages:
            if path.startswith(prefix) and (best_prefix is None or len(prefix) > len(best_prefix)):
                best_prefix = prefix
        if best_prefix is None:
            raise AssertionError(f"FakeSession.goto called with unmapped path: {path!r}")
        return self._pages[best_prefix]
