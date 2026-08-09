"""Job search and job detail tools.

LinkedIn encodes `/jobs/search/` filters as opaque query codes it documents
nowhere public. The mapping below was reverse-engineered from the URLs the
LinkedIn UI itself produces when you click its filter chips, and LinkedIn is
free to change it without notice: treat these as best-effort, not contract.

    f_WT   Workplace type. 1 = On-site, 2 = Remote, 3 = Hybrid. Comma-separated
           for "any of". We map `remote=True` -> "2" (remote only) and
           `remote=False` -> "1,3" (on-site or hybrid, i.e. not remote).
           `remote=None` omits the filter entirely (all workplace types).

    f_TPR  Time posted range, formatted "r<seconds-ago>". e.g. "r86400" is the
           last 24 hours. We convert `posted_within_days` to seconds by
           multiplying by 86400: LinkedIn accepts any integer here, it does
           not have to land on one of the preset chip values (24h/week/month).

    f_E    Experience level, single digit, comma-separated for "any of":
           1 Internship, 2 Entry level, 3 Associate, 4 Mid-Senior level,
           5 Director, 6 Executive. We take a friendly string and map it below.
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

_MAX_LIMIT = 25
_PROBE_TIMEOUT_MS = 3000

# Job descriptions can run long (multi-paragraph postings); cap and mark the
# cut visibly rather than shipping an unbounded amount of someone else's text
# straight into the model's context: same reasoning as people.py's limits.
_DESCRIPTION_LIMIT = 4000

_EXPERIENCE_CODES = {
    "internship": "1",
    "entry": "2",
    "entry level": "2",
    "associate": "3",
    "mid-senior": "4",
    "mid senior": "4",
    "senior": "4",
    "director": "5",
    "executive": "6",
}

_A11Y_SUFFIX_RE = re.compile(r"\s*\(?opens? in a new (tab|window)\)?\s*$", re.I)


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = _A11Y_SUFFIX_RE.sub("", text).strip()
    return text or None


def _job_id_from(value: str) -> str | None:
    """Accept a bare job id or a /jobs/view/<id> URL, return the id."""
    value = value.strip()
    match = re.search(r"/jobs/view/(\d+)", value)
    if match:
        return match.group(1)
    return value if value.isdigit() else None


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


async def _first_matching(root: Any, selectors: list[str]) -> tuple[Any, int]:
    """Return the first selector (as a locator) that actually matches something."""
    for sel in selectors:
        loc = root.locator(sel)
        try:
            count = await loc.count()
        except Exception:
            count = 0
        if count:
            return loc, count
    return root.locator(selectors[-1]), 0


async def _dt_dd_pairs(page: Any) -> dict[str, str]:
    """Read dt/dd label->value pairs, one of two shapes job criteria render as."""
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


async def _criteria_pairs(page: Any) -> dict[str, str]:
    """Read label/value pairs from the job criteria list (Seniority level,
    Employment type, etc). The public job page pairs an h3 label with a
    following sibling holding the value; which exact classes carry that
    pairing has changed release to release, so match on the DOM relationship
    rather than a class name. The length guard keeps an unrelated h3 (e.g. a
    section heading followed by a whole paragraph) from being read as a value.
    """
    try:
        pairs = await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('h3').forEach((h) => {
                    const sib = h.nextElementSibling;
                    if (!sib) return;
                    const label = h.textContent.trim();
                    const value = sib.textContent.trim();
                    if (label && value && value.length < 100) out[label] = value;
                });
                return out;
            }"""
        )
        return pairs or {}
    except Exception:
        return {}


def _lookup(pairs: dict[str, str], *labels: str) -> str | None:
    """Substring-match a label against the scraped pairs, cleaned of a11y noise."""
    for key, value in pairs.items():
        key_norm = key.strip().lower()
        for label in labels:
            if label.lower() in key_norm:
                return _clean(value)
    return None


def _build_jobs_query(
    keywords: str,
    location: str | None,
    remote: bool | None,
    posted_within_days: int | None,
    experience_level: str | None,
) -> str:
    params: dict[str, str] = {"keywords": keywords}
    if location:
        params["location"] = location
    if remote is True:
        params["f_WT"] = "2"
    elif remote is False:
        params["f_WT"] = "1,3"
    if posted_within_days is not None and posted_within_days > 0:
        params["f_TPR"] = f"r{int(posted_within_days) * 86400}"
    if experience_level:
        code = _EXPERIENCE_CODES.get(experience_level.strip().lower())
        if code:
            params["f_E"] = code
    return urlencode(params)


# ---------------------------------------------------------------------------
# Structural reads (primary path), matching people.py's approach.
#
# Fixture note (jobs_search.html, a captured /jobs/search/ results page): its
# job cards still carry readable BEM-style classes (job-card-container,
# artdeco-entity-lockup__title, ...) alongside some opaque per-build ones on
# the *same* elements, unlike the profile/search pages this rewrite was
# triggered by. That's a property of this one captured account/cohort, not a
# guarantee -- LinkedIn ships different markup to different accounts and can
# hash these at any time -- so the structural read below is still primary and
# the class selectors stay as the fallback, per the house rules. Two things
# the fixture *did* surface that a purely class-based read would have missed:
#
#   - The two-pane search page also renders a full detail view, off to the
#     side, for whichever job is selected -- so the same job id shows up
#     twice on one page load, once as a search-result card and once as the
#     detail top card. Deduping by id (like people.py's SEARCH_ROWS_JS dedupes
#     by public_id) handles this for free instead of needing to fence the
#     list off from the detail pane structurally.
#   - The job detail top card's "On-site" / "Full-time" style badges are
#     short strings drawn from the exact same vocabulary the search filter
#     dropdowns use to list *all* possible values ("Remote", "Hybrid", "Part-
#     time", ...). Matching that vocabulary against the whole document would
#     silently pick up a filter option instead of the job's actual value, so
#     the badge read below is scoped to a small container found by climbing
#     from the title until a company-page link is in view, not to `document`.
#
# The do_get_job detail page itself was NOT captured: no fixture exists for a
# real, standalone /jobs/view/<id>/ navigation. Everything about job detail
# below (_JOB_DETAIL_JS) was instead designed against the two-pane preview
# that jobs_search.html happens to also render for its selected job, which
# reuses the same job-details-jobs-unified-top-card__* component tree the
# standalone page is expected to render too. That is a reasonable bet, not a
# verified one -- do_get_job needs a live smoke test against a real
# /jobs/view/<id>/ page before this is trusted.
# ---------------------------------------------------------------------------

_JOB_ROWS_JS = r"""(limit) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const scope = document.querySelector('main') || document.body;

  // Badge/status text a job card carries that is not one of the job's own
  // fields: "Promoted", "Viewed", "Easy Apply", insight lines like "2
  // connections work here", etc.
  const NOISE = /^(promoted|viewed|applied|dismissed|easy apply|be an early applicant|actively (reviewing applicants?|recruiting|hiring)|responses? managed.*|new|reposted|\d[\d,]*\+?\s*(connections?|school alumni)\s*work here)$/i;
  const AGO = /\bago\b|^just now$|^yesterday$/i;
  const WORKPLACE_SUFFIX = /\((on-site|remote|hybrid)\)\s*$/i;

  // Text nodes in document order, deduplicated. LinkedIn renders some labels
  // twice, once visible and once for screen readers; walking nodes (rather
  // than reading textContent) keeps fields separate instead of glued
  // together. Same technique as people.py's _SEARCH_ROWS_JS.
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

  const allLinks = [...scope.querySelectorAll('a[href*="/jobs/view/"]')];
  const idOf = (a) => {
    const m = (a.getAttribute('href') || '').match(/\/jobs\/view\/(\d+)/);
    return m ? m[1] : null;
  };
  // Distinct job ids on the page, independent of `limit`: lets the caller
  // report "showing N of at least TOTAL" the same way the class-based path does.
  const total = new Set(allLinks.map(idOf).filter(Boolean)).size;

  const seen = new Set();
  const out = [];

  for (const a of allLinks) {
    const id = idOf(a);
    // The two-pane detail view (see module comment) echoes one of these ids
    // a second time; skip it rather than double-counting the same job.
    if (!id || seen.has(id)) continue;
    if (out.length >= limit) break;

    // Climb until the container holds the whole card, not just the title
    // link. There is no reliably-stable class to anchor on here either: the
    // structural rule that does hold is that a card contains exactly one
    // job link. Climb while that stays true, and stop the moment a parent
    // would swallow a second job.
    let row = a, strings = [];
    for (let i = 0; i < 8 && row.parentElement && row.parentElement !== scope; i++) {
      const parent = row.parentElement;
      if (parent.querySelectorAll('a[href*="/jobs/view/"]').length > 1) break;
      row = parent;
      strings = rowStrings(row);
      if (strings.length >= 4) break;
    }

    // The title link is the entity itself: read its own text directly
    // rather than picking a field out of the row by position.
    const title = clean(a.textContent) || strings.find(s => !NOISE.test(s)) || null;
    if (!title) continue;
    seen.add(id);

    const fields = strings.filter(s => s !== title && !NOISE.test(s) && s !== '·');
    const posted = fields.find(s => AGO.test(s)) || null;
    const remaining = fields.filter(s => s !== posted);
    const company = remaining[0] || null;
    // Location's shape is distinctive: it ends in "(On-site/Remote/Hybrid)".
    // Fall back to position only when that shape isn't there to lean on.
    const location = remaining.find(s => WORKPLACE_SUFFIX.test(s)) || remaining.find(s => s !== company) || null;
    const easy_apply = strings.some(s => s.toLowerCase() === 'easy apply');

    out.push({ job_id: id, title, company, location, posted, easy_apply });
  }

  return { results: out, total };
}"""


_JOB_DETAIL_JS = r"""(jobId) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // Unlike a profile page, a job posting page does carry an <h1> (confirmed
  // against a captured two-pane job detail view; see module comment on why
  // the standalone page itself is unverified). Prefer the h1 whose own link
  // points back at this job id, in case some other rail on the page (e.g.
  // "similar jobs") renders another h1.
  const h1s = [...document.querySelectorAll('h1')];
  let titleEl = h1s.find(h => {
    const a = h.querySelector('a[href*="/jobs/view/"]');
    return a && jobId && (a.getAttribute('href') || '').includes(jobId);
  }) || h1s[0] || null;
  const title = titleEl ? clean(titleEl.textContent) : null;
  if (!title) return null;

  // Climb from the title until the container also holds a link to the
  // employer's company page. That's the smallest region that reliably
  // bundles company, the meta line (location/posted/applicant interest) and
  // the workplace/employment badges together, without reaching out far
  // enough to also swallow the search filter bar or the results list --
  // both of which render the exact same short strings ("Remote", "Full-
  // time") as selectable filter options, which would otherwise be misread
  // as this job's own values.
  let container = titleEl;
  for (let i = 0; i < 6 && container.parentElement; i++) {
    container = container.parentElement;
    if (container.querySelector('a[href*="/company/"]')) break;
  }

  let company = null;
  for (const a of container.querySelectorAll('a[href*="/company/"]')) {
    const t = clean(a.textContent);
    if (t) { company = t; break; }
  }

  // Text nodes inside that container, in order, deduplicated. Worse than
  // just the usual a11y duplication here: block-level siblings render with
  // no separator at all via plain textContent ("...clicked apply" and
  // "Promoted by hirer" glue into one word), which a per-node walk avoids.
  const strings = [];
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = clean(n.nodeValue);
    if (t && strings[strings.length - 1] !== t && !strings.includes(t)) strings.push(t);
  }

  const NOISE = /^(share|show more options|apply|save|easy apply|·|promoted by hirer|responses? managed.*)$/i;
  const AGO = /\bago\b|^just now$|^yesterday$/i;
  const WORKPLACE = /^(on-site|remote|hybrid)$/i;
  const EMPLOYMENT = /^(full-time|part-time|contract|temporary|internship|volunteer|other|self-employed)$/i;
  const INTEREST = /applicant|clicked apply/i;

  const fields = strings.filter(s =>
    s !== title && s !== company && !NOISE.test(s) && !s.toLowerCase().startsWith('save ')
  );

  const posted = fields.find(s => AGO.test(s)) || null;
  const workplace_type = fields.find(s => WORKPLACE.test(s)) || null;
  const employment_type = fields.find(s => EMPLOYMENT.test(s)) || null;
  const applicant_line = fields.find(s => INTEREST.test(s)) || null;
  const location = fields.find(s =>
    s !== posted && s !== workplace_type && s !== employment_type && s !== applicant_line
  ) || null;

  // "About the job" is LinkedIn's own section label, not the employer's
  // text, and (per the fixture) sits as the very first line inside the
  // description container rather than as a heading with its own sibling.
  const headings = [...document.querySelectorAll('h1, h2, h3, h4')];
  const descHeading = headings.find(h => /^about the job\b/i.test(clean(h.textContent)));
  let description = null;
  if (descHeading) {
    const box = descHeading.parentElement || descHeading;
    // innerText, not textContent: textContent has no notion of block-level
    // layout and glues adjacent paragraphs/list items together with no
    // separator at all ("...impact.About the job" with no space), which
    // matters far more here than in the short field reads above since this
    // is thousands of characters of free-form prose. innerText renders line
    // breaks at block boundaries the same way a human reading the page
    // would see them, matching how the rest of this codebase reads long
    // text via Playwright's own inner_text().
    description = clean(box.innerText).replace(/^about the job\s*/i, '').trim() || null;
  }

  return {
    title, company, location, posted, applicant_line,
    workplace_type, employment_type, description,
  };
}"""


async def _read_job_rows(page: Any, limit: int) -> tuple[list[dict[str, Any]], int]:
    try:
        result = await page.evaluate(_JOB_ROWS_JS, limit)
    except Exception:
        return [], 0
    if not result:
        return [], 0
    return result.get("results") or [], int(result.get("total") or 0)


async def _read_job_detail(page: Any, job_id: str) -> dict[str, Any] | None:
    try:
        return await page.evaluate(_JOB_DETAIL_JS, job_id)
    except Exception:
        return None


_APPLICANTS_EXACT_RE = re.compile(r"^(\d[\d,]*)\+?\s*applicants?$", re.I)


def _parse_applicant_line(text: str | None) -> tuple[int | None, str | None]:
    """Split the "how much interest" line into an exact count when the text
    says so plainly ("142 applicants"), or an unparsed note when it's the
    softer, non-numeric signal LinkedIn's newer markup also uses ("Over 100
    people clicked apply" counts clicks on the button, not submitted
    applications, so it is deliberately not coerced into the same integer
    field the old wording gave a clean count for).
    """
    if not text:
        return None, None
    text = text.strip()
    match = _APPLICANTS_EXACT_RE.match(text)
    if match:
        return int(match.group(1).replace(",", "")), None
    return None, text


async def do_search_jobs(
    session: Session,
    queue: ActionQueue,
    keywords: str,
    location: str | None = None,
    remote: bool | None = None,
    posted_within_days: int | None = None,
    experience_level: str | None = None,
    limit: int = 10,
) -> dict:
    effective_limit = max(1, min(int(limit), _MAX_LIMIT))

    async def _run() -> dict:
        query = _build_jobs_query(
            keywords, location, remote, posted_within_days, experience_level
        )
        page = await session.goto(
            f"/jobs/search/?{query}",
            wait_for=(
                "li[data-occludable-job-id], ul.jobs-search__results-list, "
                ".jobs-search-results-list, main"
            ),
        )

        # Structural read first: a card is the smallest ancestor holding
        # exactly one /jobs/view/ link, and every field on it is classified
        # by shape (the entity link's own text, a middot/parenthetical, an
        # "ago" pattern) rather than by class name or position. The selector
        # path below stays as a fallback in case an account is served an
        # older layout.
        rows, total_found = await _read_job_rows(page, effective_limit)
        if rows:
            results = [
                {
                    "title": _clean(r.get("title")),
                    "company": _clean(r.get("company")),
                    "location": _clean(r.get("location")),
                    "posted": _clean(r.get("posted")),
                    "job_id": r.get("job_id"),
                    "url": f"https://www.linkedin.com/jobs/view/{r.get('job_id')}/",
                    "easy_apply": bool(r.get("easy_apply")),
                }
                for r in rows
                if r.get("job_id") and r.get("title")
            ]
            out: dict[str, Any] = {"results": results, "count": len(results)}
            if total_found > len(results) or int(limit) > _MAX_LIMIT:
                out["capped"] = True
                out["note"] = (
                    f"Showing {len(results)} of at least {total_found} matches "
                    f"found (capped at {effective_limit})."
                )
            return out

        cards, count = await _first_matching(
            page,
            [
                "li[data-occludable-job-id]",
                "li.jobs-search-results__list-item",
                "div.job-card-container",
                "div.base-card[data-entity-urn*='jobPosting']",
            ],
        )
        take = min(count, effective_limit)

        results: list[dict[str, Any]] = []
        for i in range(take):
            card = cards.nth(i)

            job_id = await card.get_attribute(
                "data-occludable-job-id", timeout=_PROBE_TIMEOUT_MS
            )
            if not job_id:
                urn = await card.get_attribute(
                    "data-entity-urn", timeout=_PROBE_TIMEOUT_MS
                )
                if urn and "jobPosting:" in urn:
                    job_id = urn.rsplit("jobPosting:", 1)[-1]

            title = await _first_text(
                card,
                [
                    "a.job-card-list__title",
                    ".job-card-container__link",
                    ".base-search-card__title",
                    "a[href*='/jobs/view/']",
                ],
            )

            if not job_id:
                href = None
                try:
                    link = card.locator("a[href*='/jobs/view/']").first
                    if await link.count():
                        href = await link.get_attribute(
                            "href", timeout=_PROBE_TIMEOUT_MS
                        )
                except Exception:
                    href = None
                if href:
                    job_id = _job_id_from(href)

            if not job_id or not title:
                continue

            company = await _first_text(
                card,
                [
                    ".job-card-container__company-name",
                    ".job-card-container__primary-description",
                    ".base-search-card__subtitle",
                    "a[href*='/company/']",
                ],
            )
            location_text = await _first_text(
                card,
                [
                    ".job-card-container__metadata-item",
                    ".job-search-card__location",
                ],
            )

            posted = None
            try:
                time_el = card.locator("time").first
                if await time_el.count():
                    posted = await time_el.get_attribute(
                        "datetime", timeout=_PROBE_TIMEOUT_MS
                    ) or await _text_or_none(time_el)
            except Exception:
                posted = None

            easy_apply = False
            try:
                easy_apply = (
                    await card.get_by_text(re.compile(r"easy apply", re.I)).count()
                    > 0
                )
            except Exception:
                easy_apply = False

            results.append(
                {
                    "title": _clean(title),
                    "company": _clean(company),
                    "location": _clean(location_text),
                    "posted": _clean(posted),
                    "job_id": job_id,
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                    "easy_apply": easy_apply,
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

    return await _guarded(queue, "search_jobs", _run)


async def do_get_job(session: Session, queue: ActionQueue, job_id: str) -> dict:
    resolved = _job_id_from(job_id)
    if not resolved:
        return LinkedInError(
            "invalid_input", "No job ID could be read from that value."
        ).as_dict()

    async def _run() -> dict:
        # NOTE: this navigation, and everything _JOB_DETAIL_JS assumes about
        # it, has not been exercised against a live /jobs/view/<id>/ page --
        # see the fixture note above _JOB_DETAIL_JS. Treat this tool as
        # needing a live smoke test before relying on it.
        page = await session.goto(
            f"/jobs/view/{resolved}/", wait_for="h1, #job-details, main"
        )

        # Structural read first, same reasoning as do_search_jobs.
        detail = await _read_job_detail(page, resolved)

        if detail and detail.get("title"):
            title = _clean(detail.get("title"))
            company = _clean(detail.get("company"))
            location = _clean(detail.get("location"))
            posted = _clean(detail.get("posted"))
            applicants, applicant_interest = _parse_applicant_line(
                detail.get("applicant_line")
            )
            employment_type = _clean(detail.get("employment_type"))
            seniority = _clean(detail.get("seniority"))
            workplace_type = _clean(detail.get("workplace_type"))
            description = _clean(detail.get("description"))
        else:
            # Fallback: older/differently-cohorted accounts may still serve
            # markup the class selectors below can read directly.
            title = await _first_text(
                page,
                [
                    "h1.job-details-jobs-unified-top-card__job-title",
                    "h1.top-card-layout__title",
                    "h1",
                ],
            )
            company = await _first_text(
                page,
                [
                    ".job-details-jobs-unified-top-card__company-name a",
                    ".job-details-jobs-unified-top-card__company-name",
                    "a.topcard__org-name-link",
                    ".topcard__org-name-link",
                ],
            )

            # Location, posting age and applicant count are usually rendered
            # as one bullet-separated line rather than as separate elements,
            # so pull the whole line and split it instead of relying on it
            # having its own selector: that line's markup is some of the
            # most volatile on the page.
            meta_line = await _first_text(
                page,
                [
                    ".job-details-jobs-unified-top-card__primary-description-container",
                    ".job-details-jobs-unified-top-card__tertiary-description-container",
                    ".topcard__flavor-row",
                ],
            )

            location = None
            posted = None
            applicant_interest = None
            applicants = None
            if meta_line:
                for part in (p.strip() for p in re.split(r"[·•]", meta_line)):
                    if not part:
                        continue
                    if re.search(r"applicant|clicked apply", part, re.I):
                        applicants, applicant_interest = _parse_applicant_line(part)
                    elif re.search(r"ago|hour|day|week|month|just now", part, re.I):
                        posted = part
                    elif location is None:
                        location = part

            employment_type = None
            seniority = None
            workplace_type = None
            description = await _first_text(
                page,
                [
                    "#job-details",
                    ".jobs-description__content",
                    ".jobs-box__html-content",
                    ".description__text",
                ],
            )

        if not title:
            raise not_found(f"Job '{resolved}'")

        # The "Job criteria" list (Seniority level, Employment type, ...)
        # lives in a different part of the page than the top card either
        # extraction above reads, so it's always worth trying regardless of
        # which path supplied title/company: it only fills in gaps.
        pairs = await _dt_dd_pairs(page)
        pairs.update(await _criteria_pairs(page))
        employment_type = employment_type or _lookup(pairs, "employment type")
        seniority = seniority or _lookup(pairs, "seniority level")

        return {
            "job_id": resolved,
            "url": f"https://www.linkedin.com/jobs/view/{resolved}/",
            "title": title,
            "company": company,
            "location": location,
            "posted": posted,
            "applicants": applicants,
            "applicant_interest": applicant_interest,
            "employment_type": employment_type,
            "seniority": seniority,
            "workplace_type": workplace_type,
            "description": fence(
                safety_truncate(description, _DESCRIPTION_LIMIT, "job description"),
                "job.description",
            ),
        }

    return await _guarded(queue, "get_job", _run)
