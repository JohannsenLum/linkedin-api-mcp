"""Company profile and company-search tools.

LinkedIn's company "About" page (`/company/<slug>/about/`) is the one page that
carries every field `do_get_company` reports: the top card (name, tagline,
follower count) plus the structured "Overview" list (industry, size, HQ,
founded, website) plus the free-text "About us" description all render on that
single URL, so one navigation covers the whole call.

LinkedIn serves hashed, per-build class names on company pages the same way it
does everywhere else (`b0712e9a`, `_129ac5aa`, ...), so any selector naming a
class is one redesign from returning `parse_failed`. What survives a rebuild
here, the same way it does in `people.py`:

  - `document.title`, which LinkedIn renders as
    "(N) <Company Name>: About | LinkedIn" (the unread-count prefix and the
    ": About" page-tab suffix both vary; neither is assumed present).
  - The page's own `/company/<slug>/` href, present in the verified badge and
    in the page's own nav tabs (Home/About/Posts/...), always before any
    other company's link on an About page.
  - The `<dt>`/`<dd>` tag relationship the "Overview" facts list is built
    from, and the tag relationship between that list and the "About us"
    paragraph LinkedIn always renders immediately before it in the same
    card. Neither depends on a class name, only on tag adjacency.
  - The label text inside each `<dt>` ("Industry", "Company size", ...),
    which LinkedIn does not hash because it's user-facing copy, not a style
    hook.

The class-based selectors below stay as a fallback after each structural
read, in case some accounts are served an older layout, same as people.py.
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
# which can run to a paragraph or more. Callers who only want the structured
# facts can opt out of paying to ship it back through the model.
_ALL_SECTIONS = ("overview", "about")

_MAX_LIMIT = 25

# Same reasoning as people.py's _ABOUT_LIMIT: never hand a model an unbounded
# amount of text a stranger authored: cap it and mark the cut visibly.
_ABOUT_LIMIT = 2000

# Selector calls get their own short timeout rather than inheriting the page's
# full navigation timeout (session.py sets that to ~30s): a single tool call
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
    than re-raised: tools never raise into the transport.
    """
    try:
        return await queue.run(label, fn)
    except LinkedInError as exc:
        return exc.as_dict()
    except RateLimited as exc:
        # Built from the exception's own typed fields, never str(exc): keeps
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
    class-name churn LinkedIn does on every redesign: the labels ("Website",
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


# ---------------------------------------------------------------------------
# Structural reads. Each one degrades to {} / None on any failure (including
# running against a page shape it doesn't recognise) rather than raising, so
# the caller always has the class-based selector list to fall back to.
# ---------------------------------------------------------------------------

# document.title survives every rebuild. LinkedIn renders it as
# "(N) <Company Name>: About | LinkedIn" — the unread-count prefix and the
# page-tab suffix both vary independently, so both are stripped rather than
# assumed. The self link (this page's own /company/<slug>/ href) is read the
# same way people.py reads /in/<id>: it's present in the verified badge and
# in the page's own nav tabs, always before any other company's link on an
# About page, so `main`'s first match is this company, not a related one.
_TOP_CARD_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  let name = (document.title || '').split('|')[0].trim();
  name = name.replace(/^\(\d+\)\s*/, '');
  name = name.replace(/:\s*(About|Overview|Home|Posts|Jobs|Life|People)\s*$/i, '');
  name = name.trim() || null;

  const main = document.querySelector('main');
  const h1 = main ? main.querySelector('h1') : null;
  if (!name && h1) name = clean(h1.textContent);

  const selfLink = main ? main.querySelector('a[href*="/company/"]') : null;

  // The top card is the section holding the name heading; fall back to
  // climbing from the self link the way people.py does, in case a future
  // redesign drops the heading the way it already has on profile pages.
  const card = h1
    ? h1.closest('section')
    : (selfLink ? (selfLink.closest('section') || selfLink.parentElement?.parentElement?.parentElement) : null);

  // Chrome that lives inside the same card as the real facts: nav tabs, the
  // follow/message buttons, "<name> works here". Dropped by exact text
  // rather than by class, since none of it is reliably classed either.
  const UI = /^(home|about|posts|jobs|life|people|follow|following|message|visit website)$/i;
  const NOISE = /works here$/i;

  // Walk text nodes and drop repeats: LinkedIn renders several of these
  // labels twice, once visible and once for screen readers.
  const lines = [];
  if (card) {
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const t = clean(n.nodeValue);
      if (t && !lines.includes(t)) lines.push(t);
    }
  }
  const rest = lines.filter((l) => l !== name && !UI.test(l) && !NOISE.test(l));

  // Classify by shape: the followers/size chips match on their own unit
  // words, which LinkedIn does not hash.
  const followersLine = rest.find((l) => /\bfollowers?\b/i.test(l)) || null;
  const sizeLine = rest.find((l) => /\bemployees?\b/i.test(l)) || null;

  // Whatever short, comma-free line is left over (i.e. not the location
  // chip, which is "City, Country" and always has a comma) is the closest
  // thing to a tagline this layout still renders. The caller cross-checks
  // it against the Overview industry value before trusting it, since a
  // one-line industry chip has the exact same shape as a one-line tagline.
  const tagline = rest.find((l) => l !== followersLine && l !== sizeLine && !l.includes(',') && l.length > 2) || null;

  return { name, tagline, followers_text: followersLine, size_text: sizeLine };
}"""

# The "About us" description has no surviving class either, but LinkedIn always
# renders it as the paragraph immediately before the Overview dt/dl list, in
# the same card: a tag relationship, not a class name.
_ABOUT_TEXT_JS = r"""() => {
  const dl = document.querySelector('main dl');
  if (!dl) return null;
  const sib = dl.previousElementSibling;
  return (sib && sib.tagName === 'P') ? sib.textContent.trim() : null;
}"""


async def _read_top_card(page: Any) -> dict[str, Any]:
    try:
        return await page.evaluate(_TOP_CARD_JS) or {}
    except Exception:
        return {}


async def _read_about_text(page: Any) -> str | None:
    try:
        return await page.evaluate(_ABOUT_TEXT_JS)
    except Exception:
        return None


# LinkedIn shows an abbreviated follower count on the top card ("126K
# followers") rather than an exact figure. Handle the K/M suffix rather than
# silently truncating "126K" down to the integer 126.
_FOLLOWER_COUNT_RE = re.compile(r"([\d][\d,.]*)\s*([kKmM])?\s*followers?", re.I)
_COUNT_MULTIPLIER = {"k": 1_000, "m": 1_000_000}


def _parse_follower_count(text: str | None) -> int | None:
    if not text:
        return None
    match = _FOLLOWER_COUNT_RE.search(text)
    if not match:
        return None
    digits, suffix = match.groups()
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    return int(round(value * _COUNT_MULTIPLIER.get((suffix or "").lower(), 1)))


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

        # Structural read first. It is the only one that works against
        # LinkedIn's current hashed markup; the selector lists below stay as
        # a fallback in case an account is served an older layout.
        card = await _read_top_card(page)

        name = _clean(card.get("name")) or await _first_text(
            page,
            [
                "h1.org-top-card-summary__title",
                "h1[class*='org-top-card-summary__title']",
                "h1",
            ],
        )
        if not name:
            raise not_found(f"Company '{slug}'")

        # Read the Overview facts unconditionally, even when only "about" was
        # requested: it's one cheap evaluate() call, and the tagline
        # heuristic above needs the industry value to tell a real tagline
        # apart from the industry chip it's shaped just like.
        pairs = await _dt_dd_pairs(page)
        industry = _lookup(pairs, "industry")

        tagline = _clean(card.get("tagline"))
        if tagline and industry and tagline.lower() == industry.lower():
            tagline = None  # the structural read picked up the industry chip, not a tagline
        if not tagline:
            tagline = await _first_text(
                page,
                [
                    "p.org-top-card-summary__tagline",
                    "[class*='org-top-card-summary__tagline']",
                ],
            )
        tagline = _clean(tagline)

        result: dict[str, Any] = {
            "slug": slug,
            "url": f"https://www.linkedin.com/company/{slug}/",
            "name": _clean(name),
            "tagline": tagline,
        }

        if "overview" in wanted:
            follower_text = card.get("followers_text")
            if not follower_text:
                follower_text = await _text_or_none(
                    page.get_by_text(re.compile(r"[\d,.]+\s*[kKmM]?\s*followers", re.I))
                )

            result.update(
                {
                    "industry": industry,
                    "size": _lookup(pairs, "company size") or _clean(card.get("size_text")),
                    "headquarters": _lookup(pairs, "headquarters"),
                    "founded": _lookup(pairs, "founded"),
                    "website": _lookup(pairs, "website"),
                    "follower_count": _parse_follower_count(follower_text),
                }
            )

        if "about" in wanted:
            about_text = await _read_about_text(page)
            if not about_text:
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


# ---------------------------------------------------------------------------
# Company search
# ---------------------------------------------------------------------------

# Same infrastructure as people search (same nested hashed divs, no <li>,
# every label rendered twice), just filtered to the companies vertical, so
# this reuses people.py's proven technique rather than guessing at new one:
# a result row is the smallest ancestor containing exactly ONE company link,
# found by climbing from the link until a parent would swallow a second one.
_SEARCH_COMPANY_ROWS_JS = r"""(limit) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const UI = /^(follow|following|message|visit website|view page|save|more|\+)$/i;
  const NOISE = /^(\d+(st|nd|rd)\s*degree|shared connection|mutual)/i;

  // Text nodes in document order, deduplicated: LinkedIn renders every label
  // twice, once visible and once for screen readers, so a plain textContent
  // read would glue "Acme Corp Acme Corp" together. Walking text nodes and
  // dropping repeats keeps the fields separate.
  const rowStrings = (root) => {
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const t = clean(n.nodeValue);
      if (t && out[out.length - 1] !== t && !out.includes(t)) out.push(t);
    }
    return out;
  };

  const seen = new Set();
  const out = [];

  const scope = document.querySelector('main');
  if (!scope) return [];

  for (const a of scope.querySelectorAll('a[href*="/company/"]')) {
    const m = (a.getAttribute('href') || '').match(/\/company\/([^/?#]+)/);
    if (!m || seen.has(m[1])) continue;

    // Climb until the container holds the whole row, not just the name
    // link, stopping the moment a parent would swallow a second company.
    let row = a, strings = [];
    for (let i = 0; i < 8 && row.parentElement && row.parentElement !== scope; i++) {
      const parent = row.parentElement;
      if (parent.querySelectorAll('a[href*="/company/"]').length > 1) break;
      row = parent;
      strings = rowStrings(row);
      if (strings.length >= 3) break;
    }
    if (strings.length < 2) continue;

    const fields = strings.filter((s) => !UI.test(s) && !NOISE.test(s));
    if (!fields.length) continue;

    // LinkedIn joins "Industry · N,NNN employees" on one line with a
    // middot; split on whatever separator is actually there, same as the
    // fallback subtitle-parsing this replaces.
    let industry = null, size = null;
    for (const field of fields.slice(1)) {
      for (const part of field.split(/[·•]/)) {
        const p = part.trim();
        if (!p) continue;
        if (/employee/i.test(p)) size = size || p;
        else if (industry === null) industry = p;
      }
    }

    seen.add(m[1]);
    out.push({
      name: fields[0] || null,
      slug: m[1],
      url: 'https://www.linkedin.com/company/' + m[1] + '/',
      industry,
      size,
    });
    if (out.length >= limit) break;
  }
  return out;
}"""


async def _read_search_company_rows(page: Any, limit: int) -> list[dict[str, Any]]:
    try:
        return await page.evaluate(_SEARCH_COMPANY_ROWS_JS, limit) or []
    except Exception:
        return []


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
            wait_for="main",
        )

        # Structural read first. It is the only one that works against
        # LinkedIn's current markup; the selector path below stays as a
        # fallback in case an account is served an older layout, matching
        # people.py's do_search_people.
        rows = await _read_search_company_rows(page, effective_limit)
        if rows:
            return {
                "results": [
                    {
                        "name": _clean(r.get("name")),
                        "slug": r.get("slug"),
                        "industry": _clean(r.get("industry")),
                        "size": _clean(r.get("size")),
                        "url": r.get("url"),
                    }
                    for r in rows
                ],
                "count": len(rows),
            }

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
