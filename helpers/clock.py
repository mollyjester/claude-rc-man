"""The clock: what a part of rcman that reads the time or waits for something is given, and
the real one, REAL, which is each such part's default.

A part given a clock (``create_app``'s ``clocks``, main.py) reads the time from it, not from
``time``, and makes through it, not through ``time`` or ``asyncio``, those of its waits that go
through the clock: the tests give the part a clock of their own (tests/clocks.py) and move its
time themselves. The real clock holds nothing and hands each call straight to the standard
library.

The markers say what kind of seconds a value is, where it is written, in call form
(``Grace(3.0)``): a Deadline, by which something must have happened; a Grace, a wait given to
something to finish of itself; a Pace, a wait between two things done in turn; PollExternal, a
wait between two looks at something outside rcman, which only ``poll`` is given; RealBound, a
bound in real time handed to the standard library or a program (``subprocess``'s ``timeout=``);
and Handed, a value written into what another program is given, its arguments or a header. A
wait between two looks at rcman's own state has no marker: it is an event or a condition
instead. Every timing constant carries its marker: tests/test_on_the_clock.py holds the product
to that, and names what is not on the clock yet.

The standard library only, with nothing beyond Python 3.12: the canary reaches it through the
helpers mount (runtime.py), and a helper that uses it imports it at the top of its module.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, NewType, Protocol, TypeVar, runtime_checkable

Deadline = NewType("Deadline", float)
Grace = NewType("Grace", float)
Pace = NewType("Pace", float)
PollExternal = NewType("PollExternal", float)
RealBound = NewType("RealBound", float)
Handed = NewType("Handed", float)

T = TypeVar("T")


@runtime_checkable
class Clock(Protocol):
    """The time, and every wait that has seconds. Seconds of None wait without end where the
    standard library's call does."""

    def now(self) -> float:
        """Monotonic seconds."""
        ...

    def wall(self) -> float:
        """Seconds since the epoch."""
        ...

    def sleep(self, seconds: float) -> None:
        """A Pace or a Grace."""
        ...

    def poll(self, seconds: float) -> None:
        """A PollExternal: between two looks at something outside rcman."""
        ...

    def wait(self, event: threading.Event, seconds: float | None) -> bool:
        """Until ``event`` is set: whether it is."""
        ...

    def wait_for(
        self, condition: threading.Condition, predicate: Callable[[], Any], seconds: float | None
    ) -> bool:
        """Until ``predicate`` holds; the caller holds ``condition``. Whether it held last."""
        ...

    def acquire(self, lock: Any, seconds: float | None) -> bool:
        """Until ``lock`` (a Lock, an RLock or a Semaphore) is taken: whether it was."""
        ...

    async def asleep(self, seconds: float) -> None: ...

    async def apoll(self, seconds: float) -> None: ...

    async def await_event(self, event: asyncio.Event, seconds: float | None) -> bool:
        """Until ``event`` is set: whether it is, looked at before and after waiting."""
        ...

    async def within(self, awaitable: Awaitable[T], seconds: float | None) -> T:
        """As ``asyncio.wait_for``: the result, or TimeoutError with the awaitable cancelled."""
        ...

    async def await_tasks(
        self,
        tasks: Iterable[asyncio.Future],
        seconds: float | None,
        return_when: str = asyncio.FIRST_COMPLETED,
    ) -> tuple[set[asyncio.Future], set[asyncio.Future]]:
        """As ``asyncio.wait``: (done, pending); a task still pending is not cancelled."""
        ...


def _at_least_none(seconds: float | None) -> float | None:
    return None if seconds is None else max(seconds, 0.0)


def _threading(seconds: float | None) -> float | None:
    """Seconds as threading's waits take them: none below 0, and at most half TIMEOUT_MAX,
    which ``Condition.wait_for``'s own arithmetic can otherwise carry past it."""
    return None if seconds is None else min(max(seconds, 0.0), threading.TIMEOUT_MAX / 2)


class RealClock:
    """The standard library's time and waits, holding nothing."""

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(seconds, 0.0))

    def poll(self, seconds: float) -> None:
        time.sleep(max(seconds, 0.0))

    def wait(self, event: threading.Event, seconds: float | None) -> bool:
        return event.wait(_threading(seconds))

    def wait_for(
        self, condition: threading.Condition, predicate: Callable[[], Any], seconds: float | None
    ) -> bool:
        return bool(condition.wait_for(predicate, _threading(seconds)))

    def acquire(self, lock: Any, seconds: float | None) -> bool:
        # A Semaphore does not block on timeout=-1, as a Lock does: None is acquire() itself.
        if seconds is None:
            return lock.acquire()
        return lock.acquire(timeout=_threading(seconds))

    async def asleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))

    async def apoll(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))

    async def await_event(self, event: asyncio.Event, seconds: float | None) -> bool:
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), _at_least_none(seconds))
        except TimeoutError:
            pass
        return event.is_set()

    async def within(self, awaitable: Awaitable[T], seconds: float | None) -> T:
        return await asyncio.wait_for(awaitable, _at_least_none(seconds))

    async def await_tasks(
        self,
        tasks: Iterable[asyncio.Future],
        seconds: float | None,
        return_when: str = asyncio.FIRST_COMPLETED,
    ) -> tuple[set[asyncio.Future], set[asyncio.Future]]:
        return await asyncio.wait(
            set(tasks), timeout=_at_least_none(seconds), return_when=return_when
        )


REAL = RealClock()
