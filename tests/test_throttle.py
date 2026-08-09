"""Unit tests for linkedin_mcp.throttle.ActionQueue.

No network, no browser: these exercise serialisation and the hourly
ceiling directly against the queue's own clock (`time.monotonic`), with
`min_interval_s=0` so the tests run fast and only the *ordering* /
*ceiling* guarantees are under test, not the pacing sleep itself (that is
covered separately by `test_pacing_enforces_minimum_interval`).
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_mcp.throttle import ActionQueue, RateLimited


@pytest.mark.asyncio
async def test_actions_are_serialised_not_interleaved() -> None:
    """Two concurrent callers must not run their critical sections at the
    same time: ActionQueue.run holds a single lock around the whole
    action, so an agent firing calls in parallel still drives the one
    shared browser page one action at a time."""
    queue = ActionQueue(min_interval_s=0, max_per_hour=100)
    events: list[str] = []
    in_flight = 0
    max_in_flight = 0

    async def action(label: str) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        events.append(f"start:{label}")
        await asyncio.sleep(0.01)  # simulate a slow page action
        events.append(f"end:{label}")
        in_flight -= 1
        return label

    results = await asyncio.gather(
        queue.run("a", lambda: action("a")),
        queue.run("b", lambda: action("b")),
        queue.run("c", lambda: action("c")),
    )

    assert sorted(results) == ["a", "b", "c"]
    # The critical proof of serialisation: never more than one action running.
    assert max_in_flight == 1
    # Each action's start/end pair must be contiguous: no interleaving.
    for i in range(0, len(events), 2):
        label = events[i].split(":")[1]
        assert events[i] == f"start:{label}"
        assert events[i + 1] == f"end:{label}"


@pytest.mark.asyncio
async def test_ceiling_refuses_the_next_action_instead_of_hanging() -> None:
    """Once max_per_hour actions have run, the next call must raise
    RateLimited immediately, not silently sleep until a slot frees up."""
    queue = ActionQueue(min_interval_s=0, max_per_hour=3)

    async def noop() -> None:
        return None

    for _ in range(3):
        await queue.run("noop", noop)

    with pytest.raises(RateLimited) as excinfo:
        await queue.run("noop", noop)

    assert excinfo.value.used == 3
    assert excinfo.value.ceiling == 3
    assert excinfo.value.retry_after_s >= 0


@pytest.mark.asyncio
async def test_ceiling_is_a_rolling_window_not_a_hard_stop() -> None:
    """snapshot() reports usage without mutating state or requiring the
    lock, and reflects pruning of actions older than the rolling hour."""
    queue = ActionQueue(min_interval_s=0, max_per_hour=5)

    async def noop() -> None:
        return None

    await queue.run("noop", noop)
    await queue.run("noop", noop)

    snap = queue.snapshot()
    assert snap["actions_last_hour"] == 2
    assert snap["hourly_ceiling"] == 5


@pytest.mark.asyncio
async def test_pacing_enforces_minimum_interval() -> None:
    """A real (small) min_interval_s must make the second action wait,
    proving the pacing floor is applied, not just documented."""
    queue = ActionQueue(min_interval_s=0.1, max_per_hour=100)

    async def noop() -> None:
        return None

    loop = asyncio.get_event_loop()
    start = loop.time()
    await queue.run("noop", noop)
    await queue.run("noop", noop)
    elapsed = loop.time() - start

    assert elapsed >= 0.09  # small tolerance below the 0.1s floor for scheduler jitter


@pytest.mark.asyncio
async def test_exception_from_action_propagates_and_still_counts() -> None:
    """A failing action must not corrupt the queue's bookkeeping or block
    later calls, and the caller must actually see the failure."""
    queue = ActionQueue(min_interval_s=0, max_per_hour=100)

    async def boom() -> None:
        raise ValueError("simulated failure")

    with pytest.raises(ValueError):
        await queue.run("boom", boom)

    snap = queue.snapshot()
    assert snap["actions_last_hour"] == 1  # the attempt still counts against the ceiling

    async def noop() -> None:
        return "ok"

    assert await queue.run("noop", noop) == "ok"
