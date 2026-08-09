"""Error translation.

Tools in this server never raise into the transport. They return a structured
`{"error": True, ...}` shape, because an MCP client shows the model a traceback
as an opaque failure it cannot act on, whereas a sentence naming the cause and
the fix is something it can relay or work around.

Three rules the whole codebase follows:

  - Never interpolate an exception into a message. Playwright's exception text
    embeds the URL it was driving, and a LinkedIn URL can carry session-scoped
    query parameters. Report the class of failure, not the raw string.
  - Never include the session cookie, or any header, in any output.
  - Distinguish "LinkedIn said no" from "the page changed shape". The first is
    the user's problem; the second is this server's bug and should say so.
"""

from __future__ import annotations


class LinkedInError(Exception):
    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        self.kind = kind
        self.message = message
        self.hint = hint
        super().__init__(message)

    def as_dict(self) -> dict:
        out = {"error": True, "kind": self.kind, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


def not_authenticated() -> LinkedInError:
    return LinkedInError(
        "not_authenticated",
        "LinkedIn redirected to the login page, so the session cookie is no longer valid.",
        "Cookies are invalidated when you log out of LinkedIn anywhere, change your "
        "password, or after LinkedIn expires the session. Get a fresh `li_at` from your "
        "browser and re-run `linkedin-api-mcp auth`.",
    )


def checkpoint() -> LinkedInError:
    return LinkedInError(
        "checkpoint",
        "LinkedIn presented a security checkpoint (CAPTCHA or verification) instead of the page.",
        "Open linkedin.com in your normal browser and complete the challenge there, then "
        "retry. Repeated checkpoints are LinkedIn signalling that it has noticed automated "
        "access. Slow down or stop rather than retrying, since continuing is what escalates "
        "to a restriction.",
    )


def not_found(what: str) -> LinkedInError:
    return LinkedInError(
        "not_found",
        f"{what} was not found, or your account cannot see it.",
        "LinkedIn hides profiles and companies from accounts outside their network or "
        "region, so 'not found' and 'not visible to you' are indistinguishable from here.",
    )


def blocked() -> LinkedInError:
    return LinkedInError(
        "blocked",
        "LinkedIn returned an authwall or rate-limit page rather than content.",
        "This usually means too many requests in a short window. Stop making LinkedIn "
        "calls for a while; continuing is how an account gets restricted.",
    )


def parse_failed(what: str) -> LinkedInError:
    return LinkedInError(
        "parse_failed",
        f"Loaded the page but could not read {what} from it.",
        "LinkedIn changed its markup: this is a bug in this server rather than anything "
        "you did. Please report it with the tool you called: "
        "https://github.com/JohannsenLum/linkedin-api-mcp/issues",
    )


def timed_out(what: str) -> LinkedInError:
    return LinkedInError(
        "timeout",
        f"Timed out waiting for {what}.",
        "The page may be slow, or LinkedIn may be serving an interstitial. Retry once; "
        "if it persists, check linkedin.com loads normally in your browser.",
    )
