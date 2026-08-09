"""Person-facing read tools: a profile, the caller's own profile, and people search.

`/in/<public-id>/` is a single scrollable page that carries the top card (name,
headline, location, connection/follower count), the "About" blurb, and a preview
of every structured section (experience, education, skills, certifications,
projects, languages, honors): LinkedIn renders all of it into that one DOM on
one navigation. So `sections` here selects which of those *already-loaded*
previews to parse and return, not which extra pages to fetch; asking for more
sections costs more DOM queries, not more requests. The one exception is
"posts", which lives on a different route entirely (`/recent-activity/`) and is
therefore the one section that costs a second `session.goto`, which is exactly
why it's excluded from the default set along with the rarer sections, per the
brief: keep the default call small enough that the caller isn't shipping a
huge convenience payload through the model context on every request.

Free text a LinkedIn user authored themselves (the About blurb, a role
description, a post body) is exactly the injection surface `safety.py`'s
module docstring describes: it goes through `clean() -> truncate() -> fence()`
before it leaves this module, same order and same reasoning as `messaging.py`.
Short structured facts (name, company, school, dates) are returned plain, same
as every sibling tool in this package.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from ..errors import LinkedInError, not_found, parse_failed
from ..safety import clean, fence, public_id, truncate
from ..session import Session
from ..throttle import ActionQueue, RateLimited

_BASE = "https://www.linkedin.com"

_ALL_SECTIONS = (
    "experience",
    "education",
    "skills",
    "certifications",
    "projects",
    "languages",
    "honors",
    "posts",
)
# "experience" and "education" are the two facts almost every caller actually
# wants alongside the top card; everything past that is opt-in so a plain
# `do_get_profile(session, queue, "someone")` stays a small, fast call.
_DEFAULT_SECTIONS = ("experience", "education")

# How many entries we'll read out of each section's preview list. LinkedIn
# itself only renders a handful before a "Show all" link to a details
# sub-page we deliberately don't follow: this cap just matches what's
# actually in the DOM, it isn't rationing anything.
_SECTION_LIMITS = {
    "experience": 10,
    "education": 10,
    "skills": 30,
    "certifications": 15,
    "projects": 10,
    "languages": 10,
    "honors": 10,
    "posts": 10,
}

_ABOUT_LIMIT = 2000
_DESCRIPTION_LIMIT = 1500
_POST_TEXT_LIMIT = 1500

_MAX_SEARCH_LIMIT = 25

# Anchor ids LinkedIn glues onto an otherwise-empty <div> right before each
# section, purely so in-page links (and "Show all" navigation) have something
# stable to target: they've survived redesigns that changed every class name
# around them, which is why every section selector below is built off one.
_SECTION_ANCHORS = {
    "experience": "experience",
    "education": "education",
    "certifications": "licenses_and_certifications",
    "projects": "projects",
    "languages": "languages",
    "honors": "honors_and_awards",
}

_NAME_SELECTORS = (
    "main h1",
    "h1.text-heading-xlarge",
    ".pv-text-details__left-panel h1",
)
_HEADLINE_SELECTORS = (
    ".pv-text-details__left-panel > div.text-body-medium",
    "main div.text-body-medium.break-words",
)
_LOCATION_SELECTORS = (
    ".pv-text-details__left-panel span.text-body-small.break-words",
    "main span.text-body-small.inline",
)
_CONNECTIONS_SELECTORS = (
    "main span.t-bold:has-text('connection')",
    "li.text-body-small span.t-bold",
)
_FOLLOWERS_SELECTORS = ("main span:has-text('followers')",)
_ABOUT_SELECTORS = (
    "#about ~ div .inline-show-more-text span[aria-hidden='true']",
    "section:has(#about) span[aria-hidden='true']",
)
_NOT_AVAILABLE_SELECTORS = (
    "text=This profile is not available",
    "text=This LinkedIn Member profile isn't available",
    "text=Page not found",
)

_ENTITY_PRIMARY_SELECTORS = (
    "div.t-bold span[aria-hidden='true']",
    "span.mr1.t-bold span[aria-hidden='true']",
    "div.display-flex.t-bold span[aria-hidden='true']",
)
_ENTITY_SECONDARY_SELECTORS = (
    "span.t-14.t-normal:not(.t-black--light) span[aria-hidden='true']",
    "span.t-normal span[aria-hidden='true']",
)
_ENTITY_CAPTION_SELECTORS = (
    "span.t-14.t-normal.t-black--light span[aria-hidden='true']",
    "span.pvs-entity__caption-wrapper",
)
_ENTITY_DESCRIPTION_SELECTORS = (
    "div.inline-show-more-text span[aria-hidden='true']",
    "div.pvs-entity__sub-components span[aria-hidden='true']",
)

_SKILL_SELECTORS = (
    "#skills ~ div li.artdeco-list__item div.t-bold span[aria-hidden='true']",
    "#skills ~ div li.pvs-list__paged-list-item div.t-bold span[aria-hidden='true']",
    "section:has(#skills) li span[aria-hidden='true']",
)

_POST_ITEM_SELECTORS = (
    "div.feed-shared-update-v2",
    "div[data-urn*='activity']",
)
_POST_TEXT_SELECTORS = (
    "div.feed-shared-update-v2__description span[dir='ltr']",
    "div.update-components-text span[dir='ltr']",
)
_POST_TIME_SELECTORS = (
    "span.feed-shared-actor__sub-description",
    "time",
)

_SEARCH_ITEM_SELECTORS = (
    "ul[role='list'] > li:has(a[href*='/in/'])",
    "li.reusable-search__result-container",
    "div.entity-result",
)
_SEARCH_NAME_SELECTORS = (
    ".entity-result__title-text a span[aria-hidden='true']",
    "a[href*='/in/'] span[aria-hidden='true']",
    "a[href*='/in/'] span[dir='ltr']",
)
_SEARCH_HEADLINE_SELECTORS = (
    ".entity-result__primary-subtitle",
    "div.t-14.t-black.t-normal",
)
_SEARCH_LOCATION_SELECTORS = (
    ".entity-result__secondary-subtitle",
    "div.t-14.t-normal.t-black--light",
)
_SEARCH_PROFILE_LINK_SELECTORS = (
    "a.app-aware-link[href*='/in/']",
    "a[href*='/in/']",
)

_SEPARATOR_RE = re.compile(r"\s*[·•]\s*")


# ---------------------------------------------------------------------------
# Cross-cutting: every do_* function bottoms out here so error handling is
# written once, matching messaging.py: `queue.run` only serialises and
# paces, it does not catch anything, so this is where "tools return
# structured errors, they never raise into the transport" is enforced.
# ---------------------------------------------------------------------------


def _unexpected(tool: str) -> dict:
    return LinkedInError(
        "unexpected",
        f"{tool} hit an unexpected internal error while reading the page.",
        "This is likely a bug in this server rather than anything LinkedIn "
        "returned. Please report it with the tool name: "
        "https://github.com/JohannsenLum/linkedin-api-mcp/issues",
    ).as_dict()


async def _run_tool(queue: ActionQueue, label: str, fn: Any) -> dict:
    try:
        return await queue.run(label, fn)
    except LinkedInError as exc:
        return exc.as_dict()
    except RateLimited as exc:
        # Built from the exception's own typed fields rather than str(exc):
        # RateLimited's message happens to be safe too, but this keeps rule 2
        # ("never interpolate a caught exception into a message") literal.
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
        return _unexpected(label)


# ---------------------------------------------------------------------------
# Generic DOM helpers. Every lookup tries a short list of selectors in turn
# and degrades to None/[] rather than raising: LinkedIn ships more than one
# markup for the same field depending on rollout cohort, and a missing
# optional field (most profiles skip half these sections) is normal, not a
# bug.
# ---------------------------------------------------------------------------


async def _first_text(scope: Any, selectors: tuple[str, ...]) -> str | None:
    for sel in selectors:
        try:
            node = await scope.query_selector(sel)
            if node is None:
                continue
            text = (await node.inner_text()).strip()
        except Exception:
            continue
        if text:
            return text
    return None


async def _first_attr(scope: Any, selectors: tuple[str, ...], attr: str) -> str | None:
    for sel in selectors:
        try:
            node = await scope.query_selector(sel)
            if node is None:
                continue
            value = await node.get_attribute(attr)
        except Exception:
            continue
        if value:
            return value if value.startswith("http") else f"{_BASE}{value}"
    return None


async def _find_items(page: Any, selectors: tuple[str, ...], limit: int) -> list[Any]:
    for sel in selectors:
        try:
            items = await page.query_selector_all(sel)
        except Exception:
            continue
        if items:
            return items[:limit]
    return []


def _split_secondary(secondary: str | None) -> tuple[str | None, str | None]:
    """LinkedIn often joins two facts on one line with a middot, e.g.
    'Acme Corp · Full-time' or 'B.S., Computer Science'. Split on whatever
    separator is actually there rather than assuming two distinct elements:
    the entity markup doesn't reliably give you two."""
    if not secondary:
        return None, None
    parts = _SEPARATOR_RE.split(secondary, maxsplit=1)
    if len(parts) == 2:
        return (parts[0] or None), (parts[1] or None)
    return secondary, None


def _list_item_selectors(anchor_id: str) -> tuple[str, ...]:
    return (
        f"#{anchor_id} ~ div li.artdeco-list__item",
        f"#{anchor_id} ~ div li.pvs-list__paged-list-item",
        f"section:has(#{anchor_id}) li.pvs-list__paged-list-item",
    )



# LinkedIn serves hashed, per-build class names (`b0712e9a`, `_129ac5aa`), so any
# selector naming a class is broken within the week. It also dropped the section
# anchors (`id="experience"`) that used to make sections addressable.
#
# What survives a rebuild: document.title, href patterns, heading structure, and
# the shape of the text itself. This extracts the whole top card from those alone.
_TOP_CARD_JS = r"""() => {
  const txt = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : null;

  // The name: document.title is "Name | LinkedIn" and survives every rebuild.
  // Fall back to the heading inside the profile anchor if the title is unusual.
  let name = (document.title || '').split('|')[0].trim() || null;

  // The top card is the section containing the anchor back to this profile.
  const selfLink = document.querySelector('main a[href*="/in/"]');
  const card = selfLink ? (selfLink.closest('section') || selfLink.parentElement?.parentElement?.parentElement) : null;
  if (!name && selfLink) name = txt(selfLink.querySelector('h1,h2,h3'));

  // public id comes from the URL, which has resolved by now, else from the anchor.
  const fromUrl = window.location.pathname.match(/\/in\/([^/?#]+)/);
  const fromLink = selfLink ? (selfLink.getAttribute('href') || '').match(/\/in\/([^/?#]+)/) : null;
  const public_id = (fromUrl && fromUrl[1] !== 'me') ? fromUrl[1] : (fromLink ? fromLink[1] : null);

  // The card's paragraphs, in visual order, are: pronouns?, headline, org line, location.
  // Classifying by position is fragile, so classify by shape instead.
  const lines = card ? [...card.querySelectorAll('p')]
      .map(p => txt(p)).filter(Boolean)
      .filter((v, i, a) => a.indexOf(v) === i)          // LinkedIn duplicates for a11y
      .filter(v => v !== name) : [];

  const PRONOUNS = /^(he|she|they|him|her|them)\b.{0,20}$/i;
  const pronouns = lines.find(l => PRONOUNS.test(l)) || null;
  const rest = lines.filter(l => l !== pronouns);

  // The org line uses a middot to join employer and school; the headline rarely does.
  const orgLine = rest.find(l => l.includes('·')) || null;
  // Location is short, has no middot, and is not the headline.
  const headline = rest.find(l => l !== orgLine && l.length > 25) || rest[0] || null;
  const place = rest.find(l => l !== orgLine && l !== headline && l.length <= 60) || null;

  return {
    name, public_id, pronouns, headline, location: place,
    organisations: orgLine ? orgLine.split('·').map(s => s.trim()).filter(Boolean) : [],
  };
}"""


async def _read_top_card(page: Any) -> dict[str, Any]:
    try:
        return await page.evaluate(_TOP_CARD_JS) or {}
    except Exception:
        return {}

async def _extract_section_items(page: Any, anchor_id: str, limit: int) -> list[Any]:
    """Up to `limit` entries under a named profile section, or [] if the
    person simply doesn't have that section.

    `session.goto`'s own `wait_for` only waits for the top-card h1, so a
    section a beat slower to hydrate gets its own short wait here rather than
    being read before it exists: a timeout here means "not present", not
    "server bug", so it degrades to an empty list rather than raising."""
    try:
        await page.wait_for_selector(f"#{anchor_id}", timeout=2500)
    except Exception:
        return []
    return await _find_items(page, _list_item_selectors(anchor_id), limit)


async def _entity_fields(item: Any) -> tuple[str | None, str | None, str | None, str | None]:
    primary = clean(await _first_text(item, _ENTITY_PRIMARY_SELECTORS))
    secondary = clean(await _first_text(item, _ENTITY_SECONDARY_SELECTORS))
    caption = clean(await _first_text(item, _ENTITY_CAPTION_SELECTORS))
    description = clean(await _first_text(item, _ENTITY_DESCRIPTION_SELECTORS))
    return primary, secondary, caption, description


# ---------------------------------------------------------------------------
# Section extractors: each returns [] rather than raising when the person
# has no entries in that section, which is the common case, not an error.
# ---------------------------------------------------------------------------


async def _extract_experience(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["experience"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        title, secondary, dates, description = await _entity_fields(item)
        company, employment_type = _split_secondary(secondary)
        if not title and not company:
            continue
        out.append(
            {
                "title": title,
                "company": company,
                "employment_type": employment_type,
                "dates": dates,
                "description": fence(
                    truncate(description, _DESCRIPTION_LIMIT, "experience description"),
                    "profile.experience.description",
                ),
            }
        )
    return out


async def _extract_education(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["education"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        school, secondary, dates, _description = await _entity_fields(item)
        degree, field_of_study = _split_secondary(secondary)
        if not school:
            continue
        out.append(
            {
                "school": school,
                "degree": degree,
                "field_of_study": field_of_study,
                "dates": dates,
            }
        )
    return out


async def _extract_certifications(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["certifications"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        name, issuer, issued, _description = await _entity_fields(item)
        if not name:
            continue
        out.append({"name": name, "issuer": issuer, "issued": issued})
    return out


async def _extract_projects(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["projects"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        name, _secondary, dates, description = await _entity_fields(item)
        if not name:
            continue
        out.append(
            {
                "name": name,
                "dates": dates,
                "description": fence(
                    truncate(description, _DESCRIPTION_LIMIT, "project description"),
                    "profile.project.description",
                ),
            }
        )
    return out


async def _extract_languages(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["languages"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        name, proficiency, _caption, _description = await _entity_fields(item)
        if not name:
            continue
        out.append({"language": name, "proficiency": proficiency})
    return out


async def _extract_honors(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _extract_section_items(page, _SECTION_ANCHORS["honors"], limit)
    out: list[dict[str, Any]] = []
    for item in items:
        title, issuer, date, _description = await _entity_fields(item)
        if not title:
            continue
        out.append({"title": title, "issuer": issuer, "date": date})
    return out


async def _extract_skills(page: Any, limit: int) -> list[str]:
    try:
        await page.wait_for_selector("#skills", timeout=2500)
    except Exception:
        return []
    out: list[str] = []
    for sel in _SKILL_SELECTORS:
        try:
            nodes = await page.query_selector_all(sel)
        except Exception:
            continue
        if not nodes:
            continue
        for node in nodes[:limit]:
            try:
                text = clean((await node.inner_text()).strip())
            except Exception:
                continue
            if text:
                out.append(text)
        break
    return out


async def _extract_posts(page: Any, limit: int) -> list[dict[str, Any]]:
    items = await _find_items(page, _POST_ITEM_SELECTORS, limit)
    out: list[dict[str, Any]] = []
    for item in items:
        text = clean(await _first_text(item, _POST_TEXT_SELECTORS))
        posted_at = clean(await _first_text(item, _POST_TIME_SELECTORS))
        if not text and not posted_at:
            continue
        out.append(
            {
                "posted_at": posted_at,
                "text": fence(truncate(text, _POST_TEXT_LIMIT, "post text"), "profile.post.text"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Section selection
# ---------------------------------------------------------------------------


def _normalize_sections(sections: list[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Filter to known section names, preserving canonical order, and hand
    back anything unrecognised too: reported by the caller as
    `ignored_sections` rather than silently dropped."""
    if not sections:
        return _DEFAULT_SECTIONS, ()
    requested = {s.strip().lower() for s in sections if isinstance(s, str) and s.strip()}
    wanted = tuple(name for name in _ALL_SECTIONS if name in requested)
    ignored = tuple(sorted(requested - set(_ALL_SECTIONS)))
    return (wanted or _DEFAULT_SECTIONS), ignored


# ---------------------------------------------------------------------------
# Shared profile scrape: both do_get_profile and do_get_my_profile bottom
# out here on a page that's already landed, so the two extraction paths
# cannot drift apart.
# ---------------------------------------------------------------------------


async def _scrape_profile(
    session: Session, page: Any, pid: str, sections: tuple[str, ...]
) -> dict[str, Any]:
    card = await _read_top_card(page)

    name = clean(card.get("name")) or clean(await _first_text(page, _NAME_SELECTORS))
    if not name:
        if await _first_text(page, _NOT_AVAILABLE_SELECTORS):
            raise not_found(f"Profile '{pid}'")
        raise parse_failed("the profile name")

    headline = clean(card.get("headline")) or clean(await _first_text(page, _HEADLINE_SELECTORS))
    location = clean(card.get("location")) or clean(await _first_text(page, _LOCATION_SELECTORS))
    connections = clean(await _first_text(page, _CONNECTIONS_SELECTORS))
    followers = clean(await _first_text(page, _FOLLOWERS_SELECTORS))
    about = clean(await _first_text(page, _ABOUT_SELECTORS))

    # Current position is always the top experience entry, and that entry is
    # on this same page load whether or not "experience" was requested, so
    # fetching one entry here is free, it's only the full list (up to
    # _SECTION_LIMITS["experience"]) that's gated behind the section flag.
    experience_cap = _SECTION_LIMITS["experience"] if "experience" in sections else 1
    experience = await _extract_experience(page, experience_cap)
    current_position = experience[0] if experience else None

    result: dict[str, Any] = {
        "public_id": pid,
        "profile_url": f"{_BASE}/in/{pid}/",
        "name": name,
        "headline": headline,
        "location": location,
        "connections": connections,
        "followers": followers,
        "current_position": current_position,
        "about": fence(truncate(about, _ABOUT_LIMIT, "about"), "profile.about"),
        "sections": list(sections),
    }

    if "experience" in sections:
        result["experience"] = experience
    if "education" in sections:
        result["education"] = await _extract_education(page, _SECTION_LIMITS["education"])
    if "skills" in sections:
        result["skills"] = await _extract_skills(page, _SECTION_LIMITS["skills"])
    if "certifications" in sections:
        result["certifications"] = await _extract_certifications(
            page, _SECTION_LIMITS["certifications"]
        )
    if "projects" in sections:
        result["projects"] = await _extract_projects(page, _SECTION_LIMITS["projects"])
    if "languages" in sections:
        result["languages"] = await _extract_languages(page, _SECTION_LIMITS["languages"])
    if "honors" in sections:
        result["honors"] = await _extract_honors(page, _SECTION_LIMITS["honors"])
    if "posts" in sections:
        # The one section not on this page: its own navigation, and if that
        # navigation hits a checkpoint/login redirect the error should
        # surface (not be swallowed into an empty list), since it means the
        # session broke, not that this person has no posts.
        posts_page = await session.goto(f"/in/{pid}/recent-activity/all/", wait_for="main")
        result["posts"] = await _extract_posts(posts_page, _SECTION_LIMITS["posts"])

    return result


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


async def do_get_profile(
    session: Session,
    queue: ActionQueue,
    profile: str,
    sections: list[str] | None = None,
) -> dict:
    wanted, ignored = _normalize_sections(sections)

    async def _run() -> dict:
        pid = public_id(profile)  # raises LinkedInError("invalid_input", ...) on bad input
        page = await session.goto(f"/in/{pid}/", wait_for="h1")
        data = await _scrape_profile(session, page, pid, wanted)
        if ignored:
            data["ignored_sections"] = list(ignored)
        return data

    return await _run_tool(queue, "get_profile", _run)


async def do_get_my_profile(session: Session, queue: ActionQueue) -> dict:
    async def _run() -> dict:
        # /in/me/ redirects to the caller's real profile URL in one hop, so
        # this is a single navigation, not "resolve id, then fetch profile".
        page = await session.goto("/in/me/", wait_for="h1")
        pid = public_id(page.url)
        return await _scrape_profile(session, page, pid, _DEFAULT_SECTIONS)

    return await _run_tool(queue, "get_my_profile", _run)


async def do_search_people(
    session: Session,
    queue: ActionQueue,
    keywords: str,
    limit: int = 10,
) -> dict:
    keywords = (keywords or "").strip()
    if not keywords:
        return LinkedInError(
            "invalid_argument", "keywords must be a non-empty string."
        ).as_dict()

    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        requested_limit = 10
    capped = requested_limit > _MAX_SEARCH_LIMIT
    effective_limit = max(1, min(requested_limit, _MAX_SEARCH_LIMIT))

    async def _run() -> dict:
        page = await session.goto(
            f"/search/results/people/?keywords={quote_plus(keywords)}",
            wait_for="ul[role='list'], div.search-results-container",
        )

        items = await _find_items(page, _SEARCH_ITEM_SELECTORS, effective_limit)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if len(results) >= effective_limit:
                break

            href = await _first_attr(item, _SEARCH_PROFILE_LINK_SELECTORS, "href")
            if not href:
                # Out-of-network results LinkedIn blurs to "LinkedIn Member"
                # carry no usable profile link: skip rather than return a
                # dead entry.
                continue
            profile_url = href.split("?", 1)[0]
            if profile_url in seen:
                continue
            seen.add(profile_url)

            try:
                pid = public_id(profile_url)
            except LinkedInError:
                pid = None

            results.append(
                {
                    "name": clean(await _first_text(item, _SEARCH_NAME_SELECTORS)),
                    "headline": clean(await _first_text(item, _SEARCH_HEADLINE_SELECTORS)),
                    "location": clean(await _first_text(item, _SEARCH_LOCATION_SELECTORS)),
                    "profile_url": profile_url,
                    "public_id": pid,
                }
            )

        out: dict[str, Any] = {
            "results": results,
            "count": len(results),
            "limit_applied": effective_limit,
        }
        if capped:
            out["limit_capped"] = True
            out["note"] = (
                f"Requested limit {requested_limit} was capped at {_MAX_SEARCH_LIMIT}."
            )
        return out

    return await _run_tool(queue, "search_people", _run)
