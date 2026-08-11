"""CLI tests for linkedin-api-mcp flags (--version, --test --json).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp import __version__
from linkedin_mcp.errors import LinkedInError
from linkedin_mcp.server import main


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints program name, version string and exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["linkedin-api-mcp", "--version"]):
            main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out
    assert "linkedin-api-mcp" in captured.out


def test_cli_test_json_success(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--test --json emits valid JSON with ok: True and redacted cookie only."""
    secret_cookie = "secret_li_at_cookie_value_99999999"
    monkeypatch.setenv("LINKEDIN_COOKIE", secret_cookie)

    mock_session = AsyncMock()
    mock_session.goto.return_value = None
    mock_session.close.return_value = None

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["linkedin-api-mcp", "--test", "--json"]), patch(
            "linkedin_mcp.server.Session", return_value=mock_session
        ):
            main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()

    # The raw secret cookie value MUST NOT appear anywhere in the output
    assert secret_cookie not in captured.out
    assert secret_cookie not in captured.err

    # Output must be valid JSON carrying ok: True and redacted config
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert "config" in data
    assert data["config"]["cookie"] == f"<set, {len(secret_cookie)} chars>"


def test_cli_test_json_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--test --json emits valid JSON with ok: False when session probe fails."""
    secret_cookie = "secret_li_at_cookie_value_88888888"
    monkeypatch.setenv("LINKEDIN_COOKIE", secret_cookie)

    mock_session = AsyncMock()
    mock_session.goto.side_effect = LinkedInError("session_expired", "Cookie is invalid")
    mock_session.close.return_value = None

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["linkedin-api-mcp", "--test", "--json"]), patch(
            "linkedin_mcp.server.Session", return_value=mock_session
        ):
            main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()

    # The raw secret cookie value MUST NOT appear anywhere in output
    assert secret_cookie not in captured.out
    assert secret_cookie not in captured.err

    # Output must be valid JSON carrying ok: False and error info
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert "config" in data
    assert data["config"]["cookie"] == f"<set, {len(secret_cookie)} chars>"
    assert "session_expired: Cookie is invalid" in data["error"]


def test_cli_test_json_missing_cookie(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--test --json emits valid JSON with ok: False when no cookie is found."""
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["linkedin-api-mcp", "--test", "--json"]), patch(
            "linkedin_mcp.config._from_keyring", return_value=None
        ):
            main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()

    data = json.loads(captured.out)
    assert data["ok"] is False
    assert "No LinkedIn session cookie found" in data["error"]
