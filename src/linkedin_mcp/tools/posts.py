"""Post search: /search/results/content/.

LinkedIn's content search is a lazily-loaded feed — results past the first
screenful only exist in the DOM after scrolling brings them into view, and the
DOM shape of a "post card" has shifted many times over the life of the site.
Every field here therefore has more than one selector to try, and every helper
degrades to `None` instead of raising when a field it's looking for isn't there;
a missing reaction count should never fail the whole search.

Post bodies are the one field on this page an attacker fully controls — anyone
can publish a LinkedIn post whose text is written to look like instructions to
whatever eventually reads these results. `_fence` wraps that text in an inert,
clearly-labelled block before it leaves this module; nothing here ever returns
a raw post body.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from ..errors import LinkedInError, parse_failed
from ..safety import fence
from ..safety import truncate as safety_truncate
from ..session import Session
from ..throttle import ActionQueue, RateLimited

_TOOL_NAME = "search_posts"

# Hard ceiling on how many posts a single call will chase, independent of the
# scroll cap below — a caller passing an unreasonable limit shouldn't be able
# to turn a bounded scroll loop into an unbounded one via a huge number instead.
_MAX_LIMIT = 50

# Each attempt is one scroll-and-wait cycle. This is the actual defence against
# a runaway scroll loop: no matter how sparse the results or how large the
# requested limit, the browser scrolls LinkedIn's feed at most this many times
# before the call returns whatever it has, with an explanation of why it's short.
_MAX_SCROLL_ATTEMPTS = 20
_SCROLL_WAIT_MS = 1200

# Same reasoning as people.py's _POST_TEXT_LIMIT: never hand a model an
# unbounded amount of text a stranger authored — cap it and mark the cut
# visibly rather than silently.
_POST_TEXT_LIMIT = 1500

# --- selector tables --------------------------------------------------------
# LinkedIn ships more than one markup for the same card depending on rollout
# cohort, so every field is tried against a short list of alternatives in order.

_RESULT_ITEM_SELECTORS = (
    "div.search-results-container ul[role='list'] > li",
    "ul.reusable-search__entity-result-list > li.reusable-search__result-container",
    "div[data-view-name='search-entity-result-universal-template']",
)

_AUTHOR_NAME_SELECTORS = (
    "span.update-components-actor__name span[aria-hidden='true']",
    "span.update-components-actor__name",
    ".entity-result__title-text a span[aria-hidden='true']",
    ".entity-result__title-text a",
)

_AUTHOR_HEADLINE_SELECTORS = (
    "span.update-components-actor__description",
    ".entity-result__primary-subtitle",
)

_AUTHOR_PROFILE_LINK_SELECTORS = (
    "a.update-components-actor__meta-link",
    ".entity-result__title-text a",
    "a.app-aware-link[href*='/in/']",
)

_POSTED_AT_SELECTORS = (
    "span.update-components-actor__sub-description",
    "span.update-components-actor__supplementary-actor-info",
)

_POST_TEXT_SELECTORS = (
    "div.update-components-text",
    ".feed-shared-update-v2__description .break-words",
    ".feed-shared-text",
)

_REACTION_COUNT_SELECTORS = (
    "span.social-details-social-counts__reactions-count",
    "button[aria-label*='reaction'] span[aria-hidden='true']",
)

_COMMENT_COUNT_SELECTORS = (
    "li.social-details-social-counts__comments button span[aria-hidden='true']",
    "a[href*='#comments'] span[aria-hidden='true']",
)

_POST_URN_ATTRS = ("data-chameleon-result-urn", "data-urn", "data-entity-urn")
_POST_LINK_SELECTORS = (
    "a[href*='/feed/update/urn:li:activity']",
    "a[href*='/posts/']",
)

_PUBLIC_ID_RE = re.compile(r"/in/([^/?]+)")
_COUNT_RE = re.compile(r"([\d,.]+)\s*([kKmM])?")
# "mo" (months) is tried before the bare-letter class so "2mo" doesn't parse as
# "2 minutes"; a plain "m" only matches when there's no trailing "o".
_AGE_RE = re.compile(r"(\d+)\s*(mo|yr|[smhdw])\b", re.IGNORECASE)
_UNIT_TO_DAYS = {
    "s": 1 / 86_400,
    "m": 1 / 1_440,
    "h": 1 / 24,
    "d": 1.0,
    "w": 7.0,
    "mo": 30.0,
    "yr": 365.0,
}

async def _first_text(item: Any, selectors: tuple[str, ...]) -> str | None:
    for sel in selectors:
        try:
            el = await item.query_selector(sel)
            if el is None:
                continue
            text = (await el.inner_text()).strip()
        except Exception:
            continue
        if text:
            return text
    return None


async def _first_attr(item: Any, selectors: tuple[str, ...], attr: str) -> str | None:
    for sel in selectors:
        try:
            el = await item.query_selector(sel)
            if el is None:
                continue
            value = await el.get_attribute(attr)
        except Exception:
            continue
        if value:
            return value
    return None


def _extract_public_id(href: str | None) -> str | None:
    if not href:
        return None
    m = _PUBLIC_ID_RE.search(href)
    return m.group(1) if m else None


def _parse_count(text: str | None) -> int | None:
    if not text:
        return None
    m = _COUNT_RE.search(text)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


def _clean_posted_at(text: str | None) -> str | None:
    if not text:
        return None
    # The actor sub-description often bundles "<age> • Edited • <visibility>" —
    # keep just the leading relative-time token rather than the whole string.
    m = _AGE_RE.search(text)
    return m.group(0) if m else text.strip() or None


def _parse_age_days(text: str | None) -> float | None:
    if not text:
        return None
    m = _AGE_RE.search(text)
    if not m:
        return None
    per_unit = _UNIT_TO_DAYS.get(m.group(2).lower())
    if per_unit is None:
        return None
    return int(m.group(1)) * per_unit


async def _extract_post_url(item: Any) -> str | None:
    urn: str | None = None
    for attr in _POST_URN_ATTRS:
        try:
            urn = await item.get_attribute(attr)
        except Exception:
            urn = None
        if urn:
            break

    if not urn:
        # The urn sometimes lives on a nested wrapper rather than the result
        # container element itself.
        try:
            child = await item.query_selector("[data-urn], [data-chameleon-result-urn]")
        except Exception:
            child = None
        if child is not None:
            for attr in _POST_URN_ATTRS:
                try:
                    urn = await child.get_attribute(attr)
                except Exception:
                    urn = None
                if urn:
                    break

    if urn and "activity" in urn:
        return f"https://www.linkedin.com/feed/update/{urn}/"

    href = await _first_attr(item, _POST_LINK_SELECTORS, "href")
    if href:
        # Strip tracking query params — the bare path is a stable permalink.
        return href.split("?", 1)[0]
    return None


async def _find_result_items(page: Any) -> list[Any]:
    for selector in _RESULT_ITEM_SELECTORS:
        try:
            items = await page.query_selector_all(selector)
        except Exception:
            continue
        if items:
            return items
    return []


async def _extract_post(item: Any) -> dict[str, Any] | None:
    author_name = await _first_text(item, _AUTHOR_NAME_SELECTORS)
    author_headline = await _first_text(item, _AUTHOR_HEADLINE_SELECTORS)
    profile_href = await _first_attr(item, _AUTHOR_PROFILE_LINK_SELECTORS, "href")
    posted_at = _clean_posted_at(await _first_text(item, _POSTED_AT_SELECTORS))
    post_text = await _first_text(item, _POST_TEXT_SELECTORS)
    reaction_count = _parse_count(await _first_text(item, _REACTION_COUNT_SELECTORS))
    comment_count = _parse_count(await _first_text(item, _COMMENT_COUNT_SELECTORS))
    post_url = await _extract_post_url(item)

    if not author_name and not post_text:
        # Neither an actor nor a body means this card almost certainly isn't a
        # post at all (a promoted module, a "people also viewed" strip, etc.)
        # rather than a post this server failed to read — drop it silently.
        return None

    return {
        "author_name": author_name,
        "author_headline": author_headline,
        "author_public_id": _extract_public_id(profile_href),
        "posted_at": posted_at,
        "reaction_count": reaction_count,
        "comment_count": comment_count,
        "post_url": post_url,
        "text": fence(safety_truncate(post_text, _POST_TEXT_LIMIT, "post text"), "post.text"),
    }


async def _scroll(page: Any) -> None:
    # The results list itself doesn't scroll independently — the window does.
    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
    # Give the lazy-load fetch + render time to land before the next read.
    await page.wait_for_timeout(_SCROLL_WAIT_MS)


def _explain_stop(stop_reason: str | None, found: int, limit: int) -> str:
    if stop_reason == "max_scroll_attempts":
        return (
            f"Found {found} of the requested {limit} posts before hitting the "
            f"{_MAX_SCROLL_ATTEMPTS}-scroll safety cap. This cap exists so a "
            "broad query can't make the server scroll LinkedIn indefinitely — "
            "try a narrower search, a smaller limit, or accept this result set."
        )
    if stop_reason == "no_more_results":
        return (
            f"Found {found} of the requested {limit} posts — LinkedIn had no "
            "further results to lazily load for this search."
        )
    return f"Found {found} of the requested {limit} posts."


async def _collect_results(
    page: Any, limit: int, posted_within_days: int | None
) -> tuple[list[dict[str, Any]], bool, str | None]:
    matched: list[dict[str, Any]] = []
    processed_count = 0
    prev_total = -1
    stop_reason: str | None = None

    for attempt in range(_MAX_SCROLL_ATTEMPTS + 1):
        items = await _find_result_items(page)
        total = len(items)

        for item in items[processed_count:]:
            record = await _extract_post(item)
            if record is not None:
                if posted_within_days is not None:
                    age_days = _parse_age_days(record["posted_at"])
                    # An age we can't parse is kept rather than dropped — an
                    # unrecognised time format is not evidence the post is old.
                    if age_days is not None and age_days > posted_within_days:
                        continue
                matched.append(record)
                if len(matched) >= limit:
                    break
        processed_count = total

        if len(matched) >= limit:
            break
        if total == prev_total:
            # The last scroll surfaced nothing new — we've reached the end of
            # what LinkedIn will lazily load for this query.
            stop_reason = "no_more_results"
            break
        if attempt == _MAX_SCROLL_ATTEMPTS:
            stop_reason = "max_scroll_attempts"
            break

        prev_total = total
        await _scroll(page)

    return matched[:limit], len(matched) < limit, stop_reason


async def _search_posts(
    session: Session, keywords: str, limit: int, posted_within_days: int | None
) -> dict[str, Any]:
    page = await session.goto(
        f"/search/results/content/?keywords={quote_plus(keywords)}",
        wait_for="div.search-results-container, ul.reusable-search__entity-result-list",
    )

    try:
        matched, truncated, stop_reason = await _collect_results(page, limit, posted_within_days)
    except LinkedInError:
        raise
    except Exception:
        # The page loaded (goto didn't detect a login/checkpoint redirect) but
        # something below broke in an unexpected way — LinkedIn's markup is the
        # likely cause, not the caller, so this is a server bug, not a refusal.
        raise parse_failed("post search results")

    result: dict[str, Any] = {
        "results": matched,
        "count": len(matched),
        "limit": limit,
    }
    if posted_within_days is not None:
        result["posted_within_days"] = posted_within_days
    if truncated:
        result["truncated"] = True
        result["truncated_reason"] = _explain_stop(stop_reason, len(matched), limit)
    return result


async def do_search_posts(
    session: Session,
    queue: ActionQueue,
    keywords: str,
    limit: int = 10,
    posted_within_days: int | None = None,
) -> dict[str, Any]:
    keywords = (keywords or "").strip()
    if not keywords:
        return LinkedInError(
            "invalid_argument", "keywords must be a non-empty string.",
        ).as_dict()

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, _MAX_LIMIT))

    if posted_within_days is not None:
        try:
            posted_within_days = max(1, int(posted_within_days))
        except (TypeError, ValueError):
            posted_within_days = None

    try:
        return await queue.run(
            _TOOL_NAME,
            lambda: _search_posts(session, keywords, limit, posted_within_days),
        )
    except LinkedInError as exc:
        return exc.as_dict()
    except RateLimited as exc:
        # Built from the exception's own typed fields, not its rendered text —
        # str(exc) is safe here too (no cookie/URL in it) but this keeps the
        # rule literal: never interpolate a caught exception into a message.
        mins = max(1, round(exc.retry_after_s / 60))
        return {
            "error": True,
            "kind": "rate_limited",
            "message": (
                f"Rate limit reached: {exc.used} LinkedIn actions in the last "
                f"hour (ceiling {exc.ceiling}). Try again in about {mins} "
                f"minute{'s' if mins != 1 else ''}."
            ),
        }
    except Exception:
        return parse_failed("post search results").as_dict()
