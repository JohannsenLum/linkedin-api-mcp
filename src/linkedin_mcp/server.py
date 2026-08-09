"""Server wiring: builds the FastMCP app, the one Session and ActionQueue the
whole process shares, and registers every tool on top of them.

This module is glue only. It never touches Playwright or LinkedIn markup
directly: that lives in linkedin_mcp.tools.* as `do_<tool_name>` coroutines
that take `(session, queue, *args)` and already do their own queuing and
error translation: each one calls `queue.run(label, ...)` itself and catches
`RateLimited` / `LinkedInError` / a bare `Exception` internally, so it always
returns a JSON-safe dict and never raises. `queue` is threaded through as a
plain argument rather than closed over, so there is exactly one ActionQueue
instance and every tool module serialises through the same lock. Nothing
here re-wraps a call in `queue.run` a second time, since `asyncio.Lock` is
not reentrant and doing so would deadlock the tool on its own queue.

The one exception is `linkedin_status`: it isn't backed by a tool module, so
this file drives its single `session.goto` through `queue.run` directly.
`_safe()` below is the defensive net for both cases: for the tool modules
it should never fire (they're already self-guarded), for `linkedin_status`
it's where `RateLimited` actually gets turned into a structured error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastmcp import FastMCP

from .config import Config, ConfigError, store_cookie
from .errors import LinkedInError
from .session import Session
from .throttle import ActionQueue, RateLimited
from .tools import companies, jobs, messaging, people, posts

ISSUES_URL = "https://github.com/JohannsenLum/linkedin-api-mcp/issues"


def _read_annotations(title: str) -> dict:
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }


def _write_annotations(title: str) -> dict:
    # No idempotentHint: sending a message twice sends two messages, and a
    # repeat connection request is LinkedIn's problem to reject, not ours to
    # promise sameness for. Both create something, so destructiveHint=False.
    return {
        "title": title,
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }


async def _safe(label: str, call: Awaitable[dict]) -> dict:
    # Defense in depth, not the primary mechanism: every do_* coroutine passed
    # in here already guards itself (queue.run + RateLimited + LinkedInError +
    # a bare-Exception fallback all live inside the tool module). This only
    # matters if one of them doesn't. A bug there still can't raise into the
    # transport, and still never interpolates the exception, since Playwright's
    # exception text embeds the URL it was driving.
    try:
        return await call
    except RateLimited as exc:
        # Built from the exception's own typed fields, never str(exc): keeps
        # the "never interpolate a caught exception" rule literal here too,
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
    except LinkedInError as exc:
        return exc.as_dict()
    except Exception:
        return {
            "error": True,
            "kind": "internal_error",
            "message": f"'{label}' failed with an unexpected internal error.",
            "hint": f"This is a bug in the server, not something LinkedIn returned. "
            f"Please report it with the tool name above: {ISSUES_URL}",
        }


def build_server(config: Config) -> FastMCP:
    session = Session(config)
    queue = ActionQueue(config.min_action_interval, config.max_actions_per_hour)

    @asynccontextmanager
    async def _lifespan(_: FastMCP) -> AsyncIterator[dict]:
        try:
            yield {}
        finally:
            # The browser must not outlive the MCP process: an orphaned
            # Chromium holding the session cookie is exactly the kind of
            # thing this server exists to avoid.
            await session.close()

    mcp = FastMCP("linkedin-api-mcp", lifespan=_lifespan)

    # ---- people -------------------------------------------------------

    @mcp.tool(
        name="get_profile",
        description=(
            "Fetch a LinkedIn member's profile as visible to your account: headline, "
            "about, current position. `profile` accepts a full profile URL or just the "
            "public identifier (the part after /in/). `sections` selects which extra "
            "sections to include, from {'experience', 'education', 'skills', "
            "'certifications', 'projects', 'languages', 'honors', 'posts'}: defaults to "
            "experience and education. 'posts' costs one extra page load; every other "
            "section is free since it's already on the profile page. Read-only."
        ),
        annotations=_read_annotations("Get LinkedIn Profile"),
    )
    async def get_profile(profile: str, sections: list[str] | None = None) -> dict:
        return await _safe(
            "get_profile", people.do_get_profile(session, queue, profile, sections)
        )

    @mcp.tool(
        name="get_my_profile",
        description="Fetch the profile of the account this server is logged in as. Read-only.",
        annotations=_read_annotations("Get My Profile"),
    )
    async def get_my_profile() -> dict:
        return await _safe("get_my_profile", people.do_get_my_profile(session, queue))

    @mcp.tool(
        name="search_people",
        description=(
            "Search LinkedIn members by keywords (name, title, company, school). "
            "Returns lightweight results. Call get_profile for full detail on any hit. "
            "Read-only."
        ),
        annotations=_read_annotations("Search People"),
    )
    async def search_people(keywords: str, limit: int = 10) -> dict:
        return await _safe(
            "search_people", people.do_search_people(session, queue, keywords, limit)
        )

    # ---- messaging ------------------------------------------------------

    @mcp.tool(
        name="get_inbox",
        description=(
            "List your most recent LinkedIn message threads as previews (participant, "
            "snippet, timestamp, conversation id), not full history. Use get_conversation "
            "with a returned conversation id for the full thread. Read-only."
        ),
        annotations=_read_annotations("Get Inbox"),
    )
    async def get_inbox(limit: int = 20, unread_only: bool = False) -> dict:
        return await _safe(
            "get_inbox", messaging.do_get_inbox(session, queue, limit, unread_only)
        )

    @mcp.tool(
        name="get_conversation",
        description=(
            "Fetch the full message history of one LinkedIn conversation, by the "
            "conversation_id returned from get_inbox or search_conversations. Read-only."
        ),
        annotations=_read_annotations("Get Conversation"),
    )
    async def get_conversation(conversation_id: str, limit: int = 50) -> dict:
        return await _safe(
            "get_conversation",
            messaging.do_get_conversation(session, queue, conversation_id, limit),
        )

    @mcp.tool(
        name="search_conversations",
        description="Search your LinkedIn message threads by participant name or keyword. Read-only.",
        annotations=_read_annotations("Search Conversations"),
    )
    async def search_conversations(query: str, limit: int = 20) -> dict:
        return await _safe(
            "search_conversations",
            messaging.do_search_conversations(session, queue, query, limit),
        )

    @mcp.tool(
        name="send_message",
        description=(
            "Sends a real LinkedIn message to another member, from your account, right now. "
            "This is a WRITE action visible to another person: the recipient sees it arrive "
            "as a message from you, and LinkedIn gives no way to unsend it from here. Pass "
            "exactly one of `recipient` (a profile URL/public identifier, to start or reply "
            "to whichever thread you already have with them) or `conversation_id` (to reply "
            "in a specific existing thread returned by get_inbox)."
        ),
        annotations=_write_annotations("Send LinkedIn Message"),
    )
    async def send_message(
        message: str,
        recipient: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        return await _safe(
            "send_message",
            messaging.do_send_message(session, queue, conversation_id, recipient, message),
        )

    @mcp.tool(
        name="connect",
        description=(
            "Sends a real LinkedIn connection invitation to another member, from your "
            "account, right now. This is a WRITE action visible to another person: the "
            "recipient sees an invitation from you, carrying your note if one is given "
            "(LinkedIn caps notes at 300 characters on free accounts)."
        ),
        annotations=_write_annotations("Send Connection Request"),
    )
    async def connect(profile: str, note: str | None = None) -> dict:
        return await _safe("connect", messaging.do_connect(session, queue, profile, note))

    # ---- companies ------------------------------------------------------

    @mcp.tool(
        name="get_company",
        description=(
            "Fetch a LinkedIn company page: name, tagline, industry, size, headquarters, "
            "website, follower count, and the free-text About blurb. `company` accepts a "
            "full /company/<slug> URL or the bare slug. `sections` optionally restricts the "
            "result to a subset of {'overview', 'about'}. Omit it to get both. Read-only."
        ),
        annotations=_read_annotations("Get Company"),
    )
    async def get_company(company: str, sections: list[str] | None = None) -> dict:
        return await _safe(
            "get_company", companies.do_get_company(session, queue, company, sections)
        )

    @mcp.tool(
        name="search_companies",
        description="Search LinkedIn companies by keywords. Read-only.",
        annotations=_read_annotations("Search Companies"),
    )
    async def search_companies(keywords: str, limit: int = 10) -> dict:
        return await _safe(
            "search_companies",
            companies.do_search_companies(session, queue, keywords, limit),
        )

    # ---- jobs -------------------------------------------------------------

    @mcp.tool(
        name="search_jobs",
        description=(
            "Search LinkedIn job postings by keywords, with optional filters: `location` "
            "(free text), `remote` (true for remote-only, false for on-site/hybrid, omit "
            "for any), `posted_within_days` (e.g. 1 for last 24h), and `experience_level` "
            "(one of 'internship', 'entry', 'associate', 'mid-senior', 'director', "
            "'executive'). Read-only."
        ),
        annotations=_read_annotations("Search Jobs"),
    )
    async def search_jobs(
        keywords: str,
        location: str | None = None,
        remote: bool | None = None,
        posted_within_days: int | None = None,
        experience_level: str | None = None,
        limit: int = 10,
    ) -> dict:
        return await _safe(
            "search_jobs",
            jobs.do_search_jobs(
                session,
                queue,
                keywords,
                location,
                remote,
                posted_within_days,
                experience_level,
                limit,
            ),
        )

    @mcp.tool(
        name="get_job",
        description=(
            "Fetch one LinkedIn job posting in full: description, seniority, employment "
            "type, applicant count. `job_id` accepts a full /jobs/view/<id> URL or the bare "
            "numeric id. Read-only."
        ),
        annotations=_read_annotations("Get Job"),
    )
    async def get_job(job_id: str) -> dict:
        return await _safe("get_job", jobs.do_get_job(session, queue, job_id))

    # ---- posts --------------------------------------------------------------

    @mcp.tool(
        name="search_posts",
        description=(
            "Search LinkedIn feed posts by keywords, optionally restricted to posts from "
            "the last `posted_within_days` days. Read-only."
        ),
        annotations=_read_annotations("Search Posts"),
    )
    async def search_posts(
        keywords: str, limit: int = 10, posted_within_days: int | None = None
    ) -> dict:
        return await _safe(
            "search_posts",
            posts.do_search_posts(session, queue, keywords, limit, posted_within_days),
        )

    # ---- status ---------------------------------------------------------

    @mcp.tool(
        name="linkedin_status",
        description=(
            "Check whether the stored LinkedIn session is still authenticated, and report "
            "usage against this server's own rate limits (see LINKEDIN_MIN_INTERVAL and "
            "LINKEDIN_MAX_PER_HOUR in the README) so you know how close you are to the "
            "hourly ceiling before running more tools. Never reveals the cookie. Confirming "
            "authentication requires one real navigation, so this still counts as one action "
            "against the ceiling like every other tool."
        ),
        annotations=_read_annotations("LinkedIn Session Status"),
    )
    async def linkedin_status() -> dict:
        return await _safe(
            "linkedin_status",
            queue.run("linkedin_status", lambda: _check_status(session, queue, config)),
        )

    return mcp


async def _check_status(session: Session, queue: ActionQueue, config: Config) -> dict:
    authenticated = True
    problem: dict | None = None
    try:
        await session.goto("/feed/")
    except LinkedInError as exc:
        # Not re-raised: a stale cookie or checkpoint *is* the status being
        # reported here, not a failure of the status check itself.
        authenticated = False
        problem = exc.as_dict()
    return {
        "authenticated": authenticated,
        "problem": problem,
        "queue": queue.snapshot(),
        "config": config.redacted(),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _read_cookie_interactively() -> str:
    """Get the cookie without it appearing on screen or in shell history.

    getpass needs a terminal it can switch echo off on. Plenty of places that run
    this do not have one: a piped shell, a container, CI, an editor's embedded
    terminal. There getpass raises, and an unhandled traceback on the very first
    command a user runs is a bad introduction.

    So: if stdin is not a terminal, read a piped line instead. That makes
    `pbpaste | linkedin-api-mcp auth` work, which is better than typing it anyway,
    since the value never reaches the shell history either.
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            print(
                "No terminal available to prompt on, and nothing was piped in.\n\n"
                "Pipe the cookie instead. On macOS, copy it from DevTools and run:\n"
                "    pbpaste | linkedin-api-mcp auth\n\n"
                "Or set it for one session without storing it:\n"
                "    export LINKEDIN_COOKIE='...'",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return line

    try:
        return getpass.getpass("li_at: ")
    except (EOFError, OSError):
        # Some terminals report as a tty but refuse the echo change.
        print(
            "This terminal will not let the prompt hide your input, so nothing was read.\n\n"
            "Pipe it in instead, which keeps it out of your shell history too:\n"
            "    pbpaste | linkedin-api-mcp auth",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _cmd_auth() -> None:
    print(
        "Paste your LinkedIn session cookie (`li_at`).\n"
        "DevTools -> Application -> Cookies -> https://www.linkedin.com -> li_at\n"
        "Input is hidden and never echoed back.",
        file=sys.stderr,
    )
    cookie = _read_cookie_interactively()
    if not cookie.strip():
        print("Nothing entered; not stored.", file=sys.stderr)
        raise SystemExit(1)

    try:
        backend = store_cookie(cookie)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Stored in the OS keyring via {backend}.")
    print("Run `linkedin-api-mcp --test` to verify it works.")


def _cmd_test() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    async def _probe() -> bool:
        session = Session(config)
        try:
            await session.goto("/feed/")
            return True
        except LinkedInError as exc:
            print(f"{exc.kind}: {exc.message}", file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
            return False
        finally:
            await session.close()

    ok = asyncio.run(_probe())
    print(json.dumps(config.redacted(), indent=2))
    print("Session OK." if ok else "Session check failed; see above.")
    raise SystemExit(0 if ok else 1)


def _cmd_serve() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    mcp = build_server(config)
    mcp.run()  # stdio by default: this is what MCP clients spawn as a subprocess.


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="linkedin-api-mcp",
        description="MCP server for LinkedIn, driving a real browser over your own session cookie.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Verify the stored session is authenticated, print the redacted config, and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("auth", help="Prompt for your li_at cookie and store it in the OS keyring.")

    args = parser.parse_args()

    if args.command == "auth":
        _cmd_auth()
    elif args.test:
        _cmd_test()
    else:
        _cmd_serve()


if __name__ == "__main__":
    main()
