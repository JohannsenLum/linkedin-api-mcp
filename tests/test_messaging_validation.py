"""Validation tests for the write tools in linkedin_mcp.tools.messaging.

do_send_message and do_connect are the two tools that take a real,
irreversible action on the user's real LinkedIn account, so their refusal
paths matter as much as their happy paths: a write tool that guesses
instead of refusing is how a message goes to the wrong person.

Every case here uses GuardedSession, which raises if `goto` is ever called,
proving these refusals happen from input validation alone, before any
navigation, let alone a browser click.
"""

from __future__ import annotations

import pytest

from linkedin_mcp.throttle import ActionQueue
from linkedin_mcp.tools import messaging

from .fakes import GuardedSession


def make_queue() -> ActionQueue:
    return ActionQueue(min_interval_s=0, max_per_hour=100)


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_refuses_empty_message() -> None:
    session = GuardedSession()
    queue = make_queue()

    result = await messaging.do_send_message(
        session, queue, conversation_id=None, recipient="jane-doe", message="   "
    )

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert "empty" in result["message"].lower()
    assert session.calls == []  # never touched the browser


@pytest.mark.asyncio
async def test_send_message_refuses_both_conversation_id_and_recipient() -> None:
    session = GuardedSession()
    queue = make_queue()

    result = await messaging.do_send_message(
        session,
        queue,
        conversation_id="2-abc123==",
        recipient="jane-doe",
        message="hello",
    )

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert "exactly one" in result["message"].lower()
    assert session.calls == []


@pytest.mark.asyncio
async def test_send_message_refuses_neither_conversation_id_nor_recipient() -> None:
    session = GuardedSession()
    queue = make_queue()

    result = await messaging.do_send_message(
        session, queue, conversation_id=None, recipient=None, message="hello"
    )

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert "exactly one" in result["message"].lower()
    assert session.calls == []


@pytest.mark.asyncio
async def test_send_message_refuses_oversized_message() -> None:
    session = GuardedSession()
    queue = make_queue()

    huge = "a" * (messaging.MAX_MESSAGE_CHARS + 1)
    result = await messaging.do_send_message(
        session, queue, conversation_id=None, recipient="jane-doe", message=huge
    )

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert session.calls == []  # refused before sending anything, not truncated-and-sent


@pytest.mark.asyncio
async def test_send_message_refuses_malformed_conversation_id() -> None:
    """A conversation_id containing a slash or whitespace is refused rather
    than blindly built into a navigation path: see _CONVO_ID_INVALID."""
    session = GuardedSession()
    queue = make_queue()

    result = await messaging.do_send_message(
        session,
        queue,
        conversation_id="../../etc/passwd",
        recipient=None,
        message="hello",
    )

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert session.calls == []


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_refuses_note_over_the_character_cap() -> None:
    session = GuardedSession()
    queue = make_queue()

    too_long_note = "x" * (messaging.MAX_NOTE_CHARS + 1)
    result = await messaging.do_connect(session, queue, profile="jane-doe", note=too_long_note)

    assert result["error"] is True
    assert result["kind"] == "invalid_argument"
    assert session.calls == []


@pytest.mark.asyncio
async def test_connect_refuses_invalid_profile_before_navigating() -> None:
    session = GuardedSession()
    queue = make_queue()

    result = await messaging.do_connect(session, queue, profile="https://evil.example.com/in/jane-doe/")

    assert result["error"] is True
    assert result["kind"] == "invalid_input"
    assert session.calls == []
