"""Company profile and company-search tools.

LinkedIn's company "About" page (`/company/<slug>/about/`) is the one page that
carries every field `do_get_company` reports: the top card (name, tagline,
follower count) plus the structured "Overview" list (industry, size, HQ,
founded, website) plus the free-text "About us" description all render on that
single URL, so one navigation covers the whole call.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..errors import LinkedInError, not_found, parse_failed
from ..safety import fence
from ..safety import truncate as safety_truncate
from ..session import Session
from ..throttle import ActionQueue, RateLimited

# Sections do_get_company can be asked to skip. "about" is the free-text blurb,
# which can run to a paragraph or more — callers who only want the structured
# facts can opt out of paying to ship it back through the model.
_ALL_SECTIONS = ("overview", "about")

_MAX_LIMIT = 25

# Same reasoning as people.py's _ABOUT_LIMIT: never hand a model an unbounded
# amount of text a stranger authored — cap it and mark the cut visibly.
_ABOUT_LIMIT = 2000

# Selector calls get their own short timeout rather than inheriting the page's
# full navigation timeout (session.py sets that to ~30s) — a single tool call
# tries several fallback selectors per field, and a slow miss on the first
# fallback should not stack into a multi-minute call while the queue is held.
_PROBE_TIMEOUT_MS = 3000

_A11Y_SUFFIX_RE = re.compile(r"\s*\(?opens? in a new (tab|window)\)?\s*$", re.I)


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = _A11Y_SUFFIX_RE.sub("", text).strip()
    return text or None


def _slug_from(company: str) -> str | None:
    """Accept a bare slug or a full /company/<slug> URL, return the slug."""
    company = company.strip()
    match = re.search(r"linkedin\.com/company/([^/?#]+)", company)
    if match:
        return match.group(1) or None
    company = company.removeprefix("/company/").strip("/")
    slug = company.split("/")[0].split("?")[0]
    return slug or None


async def _guarded(queue: ActionQueue, label: str, fn) -> dict:
    """Run `fn` through the queue and translate any failure into a plain dict.

    Centralised here so the two public tools below stay a straight line of
    scraping logic instead of each carrying its own try/except. A bare
    `Exception` is treated as "the page changed shape" (parse_failed) rather
    than re-raised — tools never raise into the transport.
    """
    try:
        return await queue.run(label, fn)
    except LinkedInError as exc:
        return exc.as_dict()
    except RateLimited as exc:
        # Built from the exception's own typed fields, never str(exc) — keeps
        # the "never interpolate a caught exception" rule literal everywhere,
        # even though RateLimited's own message happens to carry no secrets.
        mins = max(1, round(exc.retry_after_s / 60))
        return {
            "error": True,
            "kind": "rate_limited",
            "message": (
                f"Rate limit reached: {exc.used} LinkedIn actions in the last "
                f"hour (ceiling {exc.ceiling}). Try again in about {mins} "
                f"minute{'s' if mins != 1 else ''}."
            ),
            "actions_last_hour": exc.used,
            "hourly_ceiling": exc.ceiling,
            "retry_after_seconds": round(exc.retry_after_s, 1),
        }
    except Exception:
        return parse_failed(f"the {label} page").as_dict()


async def _text_or_none(locator, timeout: int = _PROBE_TIMEOUT_MS) -> str | None:
    try:
        if await locator.count() == 0:
            return None
        text = await locator.first.inner_text(timeout=timeout)
        return text.strip() or None
    except Exception:
        return None


async def _first_text(root: Any, selectors: list[str]) -> str | None:
    """Try each selector against `root` in turn, return the first non-empty match."""
    for sel in selectors:
        text = await _text_or_none(root.locator(sel))
        if text:
            return text
    return None


async def _dt_dd_pairs(page: Any) -> dict[str, str]:
    """Read the dt/dd "Overview" list on the About page into a label->value dict.

    Matching on the visible label text rather than a class name survives the
    class-name churn LinkedIn does on every redesign — the labels ("Website",
    "Industry", ...) are the one thing that has stayed stable across layouts.
    """
    try:
        pairs = await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('dt').forEach((dt) => {
                    const dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD') {
                        const label = dt.textContent.trim();
                        const value = dd.textContent.trim();
                        if (label && value) out[label] = value;
                    }
                });
                return out;
            }"""
        )
        return pairs or {}
    except Exception:
        return {}


def _lookup(pairs: dict[str, str], *labels: str) -> str | None:
    """Substring-match a label against the scraped pairs, cleaned of a11y noise.

    Substring rather than exact match because LinkedIn's <dt> text sometimes
    carries an icon's alt text glued on ("WebsiteWebsite icon"), so an exact
    string comparison against "Website" would miss it.
    """
    for key, value in pairs.items():
        key_norm = key.strip().lower()
        for label in labels:
            if label.lower() in key_norm:
                return _clean(value)
    return None


async def do_get_company(
    session: Session,
    queue: ActionQueue,
    company: str,
    sections: list[str] | None = None,
) -> dict:
    slug = _slug_from(company)
    if not slug:
        return LinkedInError(
            "invalid_input", "No company slug could be read from that value."
        ).as_dict()

    wanted = set(sections) if sections is not None else set(_ALL_SECTIONS)

    async def _run() -> dict:
        page = await session.goto(f"/company/{slug}/about/", wait_for="h1")

        name = await _first_text(
            page,
            [
                "h1.org-top-card-summary__title",
                "h1[class*='org-top-card-summary__title']",
                "h1",
            ],
        )
        if not name:
            raise not_found(f"Company '{slug}'")

        tagline = await _first_text(
            page,
            [
                "p.org-top-card-summary__tagline",
                "[class*='org-top-card-summary__tagline']",
            ],
        )

        follower_count = None
        follower_text = await _text_or_none(
            page.get_by_text(re.compile(r"[\d,.]+\s*followers", re.I))
        )
        if follower_text:
            digits = re.sub(r"[^\d]", "", follower_text.split("follower")[0])
            follower_count = int(digits) if digits else None

        result: dict[str, Any] = {
            "slug": slug,
            "url": f"https://www.linkedin.com/company/{slug}/",
            "name": _clean(name),
            "tagline": _clean(tagline),
        }

        if "overview" in wanted:
            pairs = await _dt_dd_pairs(page)
            result.update(
                {
                    "industry": _lookup(pairs, "industry"),
                    "size": _lookup(pairs, "company size"),
                    "headquarters": _lookup(pairs, "headquarters"),
                    "founded": _lookup(pairs, "founded"),
                    "website": _lookup(pairs, "website"),
                    "follower_count": follower_count,
                }
            )

        if "about" in wanted:
            about_text = await _first_text(
                page,
                [
                    "[class*='org-about-us-organization-description__text']",
                    "section[class*='about'] p",
                    "p[class*='break-words']",
                ],
            )
            result["about"] = fence(
                safety_truncate(_clean(about_text), _ABOUT_LIMIT, "company about"),
                "company.about",
            )

        unknown = wanted - set(_ALL_SECTIONS)
        if unknown:
            result["ignored_sections"] = sorted(unknown)

        return result

    return await _guarded(queue, "get_company", _run)


async def do_search_companies(
    session: Session,
    queue: ActionQueue,
    keywords: str,
    limit: int = 10,
) -> dict:
    effective_limit = max(1, min(int(limit), _MAX_LIMIT))

    async def _run() -> dict:
        query = urlencode({"keywords": keywords})
        page = await session.goto(
            f"/search/results/companies/?{query}",
            wait_for=(
                "ul.reusable-search__entity-result-list, "
                "div.entity-result, .search-results-container"
            ),
        )

        cards = page.locator(
            "li.reusable-search__result-container, div.entity-result"
        )
        count = await cards.count()
        take = min(count, effective_limit)

        results: list[dict[str, Any]] = []
        for i in range(take):
            card = cards.nth(i)

            name = await _first_text(
                card,
                [
                    ".entity-result__title-text a span[aria-hidden='true']",
                    ".entity-result__title-text a",
                    "a span[aria-hidden='true']",
                ],
            )
            if not name:
                continue

            href = None
            try:
                link = card.locator(
                    "a.app-aware-link[href*='/company/'], a[href*='/company/']"
                ).first
                if await link.count():
                    href = await link.get_attribute("href", timeout=_PROBE_TIMEOUT_MS)
            except Exception:
                href = None
            slug = _slug_from(href) if href else None

            subtitle = await _first_text(card, [".entity-result__primary-subtitle"])
            industry = None
            size = None
            if subtitle:
                # LinkedIn joins "Industry · N,NNN employees" with a middot on
                # this card; split on it rather than assuming separate elements.
                for part in (p.strip() for p in re.split(r"[·•]", subtitle)):
                    if not part:
                        continue
                    if re.search(r"employee", part, re.I):
                        size = part
                    elif industry is None:
                        industry = part

            results.append(
                {
                    "name": _clean(name),
                    "slug": slug,
                    "industry": _clean(industry),
                    "size": _clean(size),
                    "url": f"https://www.linkedin.com/company/{slug}/"
                    if slug
                    else href,
                }
            )

        out: dict[str, Any] = {"results": results, "count": len(results)}
        if count > take or int(limit) > _MAX_LIMIT:
            out["capped"] = True
            out["note"] = (
                f"Showing {len(results)} of at least {count} matches found "
                f"(capped at {effective_limit})."
            )
        return out

    return await _guarded(queue, "search_companies", _run)
