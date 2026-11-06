"""until(condition): a test's wait for something to come about, looked at again and again.

Not a test module. A test that waits for what it cannot be told of waits with ``until`` (or
``until_async`` in a coroutine), which gives the condition's first truthy value. It takes no
limit: it gives up only at the tier's hang guard (tests.hang_guard.tier_guard), with an
AssertionError naming the condition and what it last gave, so that the test fails as any other
does and its teardown runs. ``pace`` is how long it waits between looks: PACE, or SLOW where a
look is dear, as a podman exec is. The rules are in project-knowledge/tests.md, "Time in tests".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from tests.hang_guard import tier_guard

# Between two looks.
PACE = 0.05
# Between two looks that cost a process of their own, as a podman exec does.
SLOW = 1.0


def _gave_up(condition: Callable[[], Any], last: Any, bound: float) -> AssertionError:
    return AssertionError(
        f"{condition!r} did not come about within the hang bound ({bound}s); it last gave {last!r}"
    )


def until(condition: Callable[[], Any], pace: float = PACE) -> Any:
    """The first truthy value of ``condition()``, asked every ``pace`` seconds; an
    AssertionError at the tier's hang guard. An error ``condition`` raises ends it."""
    bound = tier_guard()
    started = time.monotonic()
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() - started >= bound:
            raise _gave_up(condition, value, bound)
        time.sleep(pace)


async def until_async(condition: Callable[[], Any], pace: float = PACE) -> Any:
    """until, for a coroutine: it waits with asyncio.sleep, so the loop runs between looks."""
    bound = tier_guard()
    started = time.monotonic()
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() - started >= bound:
            raise _gave_up(condition, value, bound)
        await asyncio.sleep(pace)
