"""Navigation failures must name the real cause.

The redirect loop is the one worth a test: it is what a malformed cookie
produces, it is indistinguishable from a network fault unless you look for it,
and getting it wrong sends a first-time user to check their wifi.
"""

from __future__ import annotations

import pytest

from linkedin_mcp.session import _classify_navigation_failure


def test_redirect_loop_is_reported_as_a_bad_cookie():
    err = _classify_navigation_failure(
        Exception("Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://www.linkedin.com/feed/")
    )
    assert err.kind == "bad_cookie"
    assert "network" not in err.message.lower()
    assert "li_at" in err.hint


@pytest.mark.parametrize(
    "marker",
    ["ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION_REFUSED"],
)
def test_network_errors_are_reported_as_unreachable(marker):
    err = _classify_navigation_failure(Exception(f"Page.goto: net::{marker} at https://x/"))
    assert err.kind == "network_unreachable"


def test_unrecognised_failure_falls_back_without_leaking_the_url():
    url = "https://www.linkedin.com/in/someone?verifier=SECRET123"
    err = _classify_navigation_failure(Exception(f"Page.goto: net::ERR_WEIRD at {url}"))
    assert err.kind == "navigation_failed"
    # The whole point of classifying rather than interpolating.
    assert "SECRET123" not in err.message
    assert "SECRET123" not in err.hint
