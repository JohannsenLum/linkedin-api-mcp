"""Regression test: every do_* tool implementation in tools/* must actually
be registered on the FastMCP instance. A tool implemented but never wired
up is invisible to callers. This has bitten this project before, so it is
pinned here rather than left to manual inspection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from linkedin_mcp.config import Config
from linkedin_mcp.server import build_server

EXPECTED_TOOLS = {
    "get_profile",
    "get_my_profile",
    "search_people",
    "get_inbox",
    "get_conversation",
    "search_conversations",
    "send_message",
    "connect",
    "get_company",
    "search_companies",
    "search_jobs",
    "get_job",
    "search_posts",
    "linkedin_status",
}


def make_config() -> Config:
    return Config(cookie="fake-cookie-for-tests")


@pytest.mark.asyncio
async def test_exactly_fourteen_tools_registered() -> None:
    mcp = build_server(make_config())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}

    assert names == EXPECTED_TOOLS
    assert len(names) == 14


def test_every_do_function_in_tools_has_a_matching_registered_name() -> None:
    """Cross-check the source directly: every `async def do_<x>` in
    tools/*.py must correspond to a tool name registered in server.py
    (modulo do_get_my_profile -> get_my_profile, i.e. stripping the do_
    prefix), so a newly added do_* function can't silently go unwired."""
    tools_dir = Path(__file__).resolve().parent.parent / "src" / "linkedin_mcp" / "tools"
    server_py = (
        Path(__file__).resolve().parent.parent / "src" / "linkedin_mcp" / "server.py"
    ).read_text()

    do_functions = set()
    for py_file in tools_dir.glob("*.py"):
        for match in re.finditer(r"^async def (do_\w+)\(", py_file.read_text(), re.MULTILINE):
            do_functions.add(match.group(1))

    expected_from_do = {name[len("do_"):] for name in do_functions}
    assert expected_from_do == EXPECTED_TOOLS - {"linkedin_status"}

    for tool_name in expected_from_do:
        assert tool_name in server_py, f"{tool_name} has a do_ implementation but server.py never mentions it"
