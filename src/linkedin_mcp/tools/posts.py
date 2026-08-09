"""Post search: /search/results/content/.

LinkedIn's content search is a lazily-loaded feed: results past the first
screenful only exist in the DOM after scrolling brings them into view, and the
DOM shape of a "post card" has shifted many times over the life of the site.
LinkedIn now also ships hashed, per-build class names (`b0712e9a`, `e3ec3fcb`),
so the old class-based selector tables below are dead against the live site.
They stay as a fallback (in case some accounts still get older markup), but
the primary read is structural, in `_POST_ROWS_JS`, following the same three
rules `people.py` established:

  - A post row is the smallest ancestor containing a posted-at-shaped string
    ("1d •", "21h •"): that string is always the last field LinkedIn renders
    in the actor header before the Follow control, so its appearance is a far
    more reliable "header is complete" signal than a fixed field count would
    be, a company post has no separate headline line and would blow straight
    past a naive count.
  - Reaction and comment counts are NOT in an aria-label on this page (that
    was checked against the fixture and is not what's actually there): they
    are plain text ("5 reactions", "23 comments"), rendered twice for
    accessibility, and for larger counts LinkedIn splits the number and the
    word into separate text nodes with no space between them, so the digit
    ends up directly against the word either way once whitespace is
    collapsed. That adjacency is what's matched: "contains a digit anywhere
    and contains the word anywhere" is not the same check and is too loose,
    proven by a real captured page, where a post's own sentence ("...how to
    stand out with thoughtful, value-adding comments...") several lines
    away from an unrelated digit earlier in the same paragraph was
    misread as a comment count and stolen from the body text.
  - A quoted/reposted post embedded inside another person's post is a decoy
    exactly like the ones `messaging.py` rejects: it carries its own author,
    its own Follow control, its own permalink, and its own body text, all of
    which would be wrong if attributed to the outer post's author. It is
    detected the same way: a second "Follow <name>" control appearing after
    the first marks where the embed begins, and everything gathered for the
    outer post is bounded to before it.

Post bodies are the one field on this page an attacker fully controls: anyone
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
# scroll cap below: a caller passing an unreasonable limit shouldn't be able
# to turn a bounded scroll loop into an unbounded one via a huge number instead.
_MAX_LIMIT = 50

# Each attempt is one scroll-and-wait cycle. This is the actual defence against
# a runaway scroll loop: no matter how sparse the results or how large the
# requested limit, the browser scrolls LinkedIn's feed at most this many times
# before the call returns whatever it has, with an explanation of why it's short.
_MAX_SCROLL_ATTEMPTS = 20
_SCROLL_WAIT_MS = 1200

# Same reasoning as people.py's _POST_TEXT_LIMIT: never hand a model an
# unbounded amount of text a stranger authored: cap it and mark the cut
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

# ---------------------------------------------------------------------------
# Structural read (primary). See the module docstring for the three rules.
# What survives a rebuild here: role="listitem" (LinkedIn's own accessibility
# semantics for "one item in this results list", not a class name), an
# aria-label naming the actor ("Follow Jill Burns"), and the shape of the
# text itself. Verified against a real captured /search/results/content/ page.
# ---------------------------------------------------------------------------

_POST_ROWS_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const scope = document.querySelector('main');
  if (!scope) return [];

  // Each post card is marked role="listitem". Scoping to `main` keeps any
  // unrelated listitem (nav, filter chips) that isn't part of the results
  // feed out of consideration.
  const rows = [...scope.querySelectorAll('div[role="listitem"]')];

  const entityId = (href) => {
    if (!href) return null;
    let m = href.match(/\/in\/([^/?#]+)/);
    if (m) return 'in:' + m[1];
    m = href.match(/\/company\/([^/?#]+)/);
    if (m) return 'company:' + m[1];
    return null;
  };

  const uniqueEntities = (el) => {
    const ids = new Set();
    for (const a of el.querySelectorAll('a[href]')) {
      const eid = entityId(a.getAttribute('href'));
      if (eid) ids.add(eid);
    }
    return ids;
  };

  // Same problem _SEARCH_ROWS_JS solves: every label is rendered twice, once
  // visible and once for screen readers, so textContent gives "Yannic Kilcher
  // Yannic Kilcher • 2nd...". Walking text nodes and dropping repeats (both
  // consecutive and anywhere earlier in the list) keeps the fields separate.
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

  const AGE = /^\d+\s*(mo|yr|[smhdw])\s*[•·]?\s*$/i;
  const DEGREE = /^[•·]?\s*(1st|2nd|3rd\+?)$/i;

  // Climb from the author's own /in/ or /company/ link to the smallest
  // ancestor whose deduped text already contains a posted-at-shaped string
  // ("1d •"). That string is always the last header field before the Follow
  // control, so its appearance means the header is complete, unlike a fixed
  // string-count threshold, this does not overshoot on a company post, which
  // has no separate headline line and would otherwise blow straight past a
  // count-based stop into the post body. Bounded to 8 climbs, and stops
  // (without climbing in) the moment a parent would swallow a second author,
  // the same guard _SEARCH_ROWS_JS uses for search result rows.
  const findHeader = (row) => {
    let anchor = null;
    for (const a of row.querySelectorAll('a[href]')) {
      if (entityId(a.getAttribute('href'))) { anchor = a; break; }
    }
    if (!anchor) return null;

    let node = anchor;
    let strings = [];
    for (let i = 0; i < 8 && node.parentElement && node.parentElement !== row.parentElement; i++) {
      const parent = node.parentElement;
      if (uniqueEntities(parent).size > 1) break;
      node = parent;
      strings = rowStrings(node);
      if (strings.length >= 3 && strings.some((s) => AGE.test(s))) break;
    }
    if (!strings.length) return null;
    return { node, strings };
  };

  // /company/<slug>/posts/ (no trailing segment) is that company's post-tab
  // link, not a permalink to this one post: a real permalink always carries
  // either the activity/ugcPost urn or a slug after /posts/.
  const isRealPostLink = (href) => {
    if (!href) return false;
    if (href.includes('/feed/update/urn:li:')) return true;
    const m = href.match(/\/posts\/([^/?#]+)/);
    return !!(m && m[1]);
  };

  const NOISE = new RegExp(
    '^(' +
      'like|comment|repost|send|' +
      'this is a modal window\\.?|beginning of dialog window.*|end of dialog window\\.?|' +
      'show results|the author can see how you vote.*|' +
      '\\d+\\s*votes?|\\d+\\s*(mo|yr|[smhdw])\\s*left|' +
      '[•·]' +
    ')$',
    'i'
  );

  // A count paragraph has its digit run directly against the word ("5
  // reactions", "216reactions" once concatenated, see the module
  // docstring). Requiring that adjacency, rather than "contains a digit
  // and contains the word anywhere", matters: a long post body easily
  // contains both separately (a "Week 2" earlier in the paragraph and
  // "value-adding comments" later in it is a real example from a captured
  // profile page), and a loose check misreads the post's own words as a
  // comment count and steals that paragraph away from the real body text.
  const COUNT_LIKE = /\d[\d,.]*\s*[kKmM]?\+?\s*(reaction|comment|repost)s?/i;

  const out = [];
  for (const row of rows) {
    const header = findHeader(row);
    if (!header) continue;
    const { node: headerNode, strings } = header;

    // How many distinct actors this row talks about: the primary author,
    // plus one more per quoted/reposted post embedded in it. Each gets
    // exactly one Follow control, the same "one identity, one control"
    // reasoning _FIND_COMPOSE_HREF_JS uses to reject decoy Message links.
    const followEls = [...row.querySelectorAll('[aria-label]')].filter((el) => {
      const al = el.getAttribute('aria-label') || '';
      return al.startsWith('Follow ') || al.startsWith('Following ');
    });

    let authorName = followEls.length
      ? clean(followEls[0].getAttribute('aria-label').replace(/^(Follow|Following)\s+/, ''))
      : null;
    if (!authorName) authorName = strings[0] || null;

    const degree = strings.find((s) => DEGREE.test(s)) || null;
    const postedRaw = strings.find((s) => AGE.test(s)) || null;
    const rest = strings.filter(
      (s) => s !== authorName && s !== degree && s !== postedRaw && !/^(follow|following)\b/i.test(s)
    );
    const headline = rest.length ? rest.reduce((a, b) => (b.length > a.length ? b : a)) : null;

    let authorPublicId = null;
    for (const a of headerNode.querySelectorAll('a[href]')) {
      const m = (a.getAttribute('href') || '').match(/\/in\/([^/?#]+)/);
      if (m) { authorPublicId = m[1]; break; }
    }

    // A second Follow control marks where a quoted/reposted post's own
    // header begins. Its permalink and body text both come after that
    // control in document order; everything below is bounded to before it
    // so the embed's author and words never get attributed to this row's
    // actual author.
    const cutoff = followEls.length > 1 ? followEls[1] : null;
    const isBefore = (el) => !cutoff || !!(el.compareDocumentPosition(cutoff) & Node.DOCUMENT_POSITION_FOLLOWING);

    const headerPs = new Set(headerNode.querySelectorAll('p'));
    let text = null;
    for (const p of row.querySelectorAll('p')) {
      if (headerPs.has(p) || !isBefore(p)) continue;
      const t = clean(p.textContent);
      if (!t || NOISE.test(t) || COUNT_LIKE.test(t)) continue;
      if (!text || t.length > text.length) text = t;
    }

    let reactionText = null;
    let commentText = null;
    for (const p of row.querySelectorAll('p')) {
      const t = clean(p.textContent);
      if (!COUNT_LIKE.test(t)) continue;
      if (!reactionText && /reaction/i.test(t)) reactionText = t;
      if (!commentText && /comment/i.test(t)) commentText = t;
      if (reactionText && commentText) break;
    }

    let postUrl = null;
    for (const a of row.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      if (isRealPostLink(href) && isBefore(a)) {
        postUrl = href.split('?')[0];
        break;
      }
    }

    if (!authorName && !text) continue;
    out.push({
      author_name: authorName,
      author_headline: headline,
      author_public_id: authorPublicId,
      posted_at_raw: postedRaw,
      reaction_text: reactionText,
      comment_text: commentText,
      post_url: postUrl,
      text: text,
    });
  }
  return out;
}"""


async def _read_structural_post_rows(page: Any) -> list[dict[str, Any]]:
    try:
        return await page.evaluate(_POST_ROWS_JS) or []
    except Exception:
        return []


def _finalize_structural_post(raw: dict[str, Any]) -> dict[str, Any] | None:
    author_name = (raw.get("author_name") or "").strip() or None
    text = (raw.get("text") or "").strip() or None
    if not author_name and not text:
        # Neither an actor nor a body: not a post this server failed to
        # read, almost certainly a module that only looked like one.
        return None

    return {
        "author_name": author_name,
        "author_headline": (raw.get("author_headline") or "").strip() or None,
        "author_public_id": raw.get("author_public_id") or None,
        "posted_at": _clean_posted_at(raw.get("posted_at_raw")),
        "reaction_count": _parse_count(raw.get("reaction_text")),
        "comment_count": _parse_count(raw.get("comment_text")),
        "post_url": raw.get("post_url") or None,
        "text": fence(safety_truncate(text, _POST_TEXT_LIMIT, "post text"), "post.text"),
    }


# ---------------------------------------------------------------------------
# Selector-based read (fallback). Reached only when the structural read above
# finds no role="listitem" rows at all, e.g. an account served an older
# layout. See the module docstring: LinkedIn's per-build class hashing means
# every selector below is expected to be dead against the current site.
# ---------------------------------------------------------------------------


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
    # The actor sub-description often bundles "<age> • Edited • <visibility>".
    # Keep just the leading relative-time token rather than the whole string.
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
        # Strip tracking query params: the bare path is a stable permalink.
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
        # rather than a post this server failed to read. Drop it silently.
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
    # The results list itself doesn't scroll independently: the window does.
    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
    # Give the lazy-load fetch + render time to land before the next read.
    await page.wait_for_timeout(_SCROLL_WAIT_MS)


def _explain_stop(stop_reason: str | None, found: int, limit: int) -> str:
    if stop_reason == "max_scroll_attempts":
        return (
            f"Found {found} of the requested {limit} posts before hitting the "
            f"{_MAX_SCROLL_ATTEMPTS}-scroll safety cap. This cap exists so a "
            "broad query can't make the server scroll LinkedIn indefinitely: "
            "try a narrower search, a smaller limit, or accept this result set."
        )
    if stop_reason == "no_more_results":
        return (
            f"Found {found} of the requested {limit} posts: LinkedIn had no "
            "further results to lazily load for this search."
        )
    return f"Found {found} of the requested {limit} posts."


def _consider(
    matched: list[dict[str, Any]],
    record: dict[str, Any] | None,
    posted_within_days: int | None,
) -> None:
    if record is None:
        return
    if posted_within_days is not None:
        age_days = _parse_age_days(record["posted_at"])
        # An age we can't parse is kept rather than dropped: an unrecognised
        # time format is not evidence the post is old.
        if age_days is not None and age_days > posted_within_days:
            return
    matched.append(record)


async def _collect_results(
    page: Any, limit: int, posted_within_days: int | None
) -> tuple[list[dict[str, Any]], bool, str | None]:
    matched: list[dict[str, Any]] = []
    processed_count = 0
    prev_total = -1
    stop_reason: str | None = None
    # Decided once, from the first read, and held for the rest of this call:
    # LinkedIn either ships the role="listitem" markup this account is
    # seeing or it doesn't, and switching source mid-scroll would track two
    # independently-growing lists against one `processed_count`, double
    # counting or skipping rows.
    structural_mode: bool | None = None

    for attempt in range(_MAX_SCROLL_ATTEMPTS + 1):
        if structural_mode is None or structural_mode:
            raw_rows = await _read_structural_post_rows(page)
            if structural_mode is None:
                structural_mode = bool(raw_rows)

        if structural_mode:
            total = len(raw_rows)
            for raw in raw_rows[processed_count:]:
                _consider(matched, _finalize_structural_post(raw), posted_within_days)
                if len(matched) >= limit:
                    break
        else:
            items = await _find_result_items(page)
            total = len(items)
            for item in items[processed_count:]:
                _consider(matched, await _extract_post(item), posted_within_days)
                if len(matched) >= limit:
                    break
        processed_count = total

        if len(matched) >= limit:
            break
        if total == prev_total:
            # The last scroll surfaced nothing new: we've reached the end of
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
    # The old wait target (div.search-results-container, a class-based
    # selector) is dead against the current site; "main" is what people.py's
    # search waits on too, and _collect_results does its own structural wait
    # via role="listitem" on the first read.
    page = await session.goto(
        f"/search/results/content/?keywords={quote_plus(keywords)}",
        wait_for="main",
    )

    try:
        matched, truncated, stop_reason = await _collect_results(page, limit, posted_within_days)
    except LinkedInError:
        raise
    except Exception:
        # The page loaded (goto didn't detect a login/checkpoint redirect) but
        # something below broke in an unexpected way: LinkedIn's markup is the
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
        # Built from the exception's own typed fields, not its rendered text:
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
