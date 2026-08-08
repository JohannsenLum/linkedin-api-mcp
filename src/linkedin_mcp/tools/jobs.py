"""Job search and job detail tools.

LinkedIn encodes `/jobs/search/` filters as opaque query codes it documents
nowhere public. The mapping below was reverse-engineered from the URLs the
LinkedIn UI itself produces when you click its filter chips, and LinkedIn is
free to change it without notice — treat these as best-effort, not contract.

    f_WT   Workplace type. 1 = On-site, 2 = Remote, 3 = Hybrid. Comma-separated
           for "any of". We map `remote=True` -> "2" (remote only) and
           `remote=False` -> "1,3" (on-site or hybrid, i.e. not remote).
           `remote=None` omits the filter entirely (all workplace types).

    f_TPR  Time posted range, formatted "r<seconds-ago>". e.g. "r86400" is the
           last 24 hours. We convert `posted_within_days` to seconds by
           multiplying by 86400 — LinkedIn accepts any integer here, it does
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
# straight into the model's context — same reasoning as people.py's limits.
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
                ".jobs-search-results-list"
            ),
        )

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
        page = await session.goto(f"/jobs/view/{resolved}/", wait_for="h1")

        title = await _first_text(
            page,
            [
                "h1.job-details-jobs-unified-top-card__job-title",
                "h1.top-card-layout__title",
                "h1",
            ],
        )
        if not title:
            raise not_found(f"Job '{resolved}'")

        company = await _first_text(
            page,
            [
                ".job-details-jobs-unified-top-card__company-name a",
                ".job-details-jobs-unified-top-card__company-name",
                "a.topcard__org-name-link",
                ".topcard__org-name-link",
            ],
        )

        # Location, posting age and applicant count are usually rendered as one
        # bullet-separated line rather than as separate elements, so pull the
        # whole line and split it instead of relying on it having its own
        # selector — that line's markup is some of the most volatile on the page.
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
        applicants = None
        if meta_line:
            for part in (p.strip() for p in re.split(r"[·•]", meta_line)):
                if not part:
                    continue
                if re.search(r"applicant", part, re.I):
                    digits = re.sub(r"[^\d]", "", part)
                    applicants = int(digits) if digits else None
                elif re.search(r"ago|hour|day|week|month|just now", part, re.I):
                    posted = part
                elif location is None:
                    location = part

        pairs = await _dt_dd_pairs(page)
        pairs.update(await _criteria_pairs(page))

        description = await _first_text(
            page,
            [
                "#job-details",
                ".jobs-description__content",
                ".jobs-box__html-content",
                ".description__text",
            ],
        )

        return {
            "job_id": resolved,
            "url": f"https://www.linkedin.com/jobs/view/{resolved}/",
            "title": _clean(title),
            "company": _clean(company),
            "location": _clean(location),
            "posted": _clean(posted),
            "applicants": applicants,
            "employment_type": _lookup(pairs, "employment type"),
            "seniority": _lookup(pairs, "seniority level"),
            "description": fence(
                safety_truncate(_clean(description), _DESCRIPTION_LIMIT, "job description"),
                "job.description",
            ),
        }

    return await _guarded(queue, "get_job", _run)
