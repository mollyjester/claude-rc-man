"""PhasedClock: a clock (helpers/clock.py) whose time the test moves, and the registry that
fails a test on its misuse. A pytest plugin, which tests/conftest.py loads.

Not a test module. The rules are in project-knowledge/tests.md, "Time in tests".

A clock belongs to the thread that made it and to the threads lent to it (``lend``). It has two
phases:

- **frozen**, the default: its time moves only by ``advance``. A sleep parks until an advance
  reaches it; a wait ends on its primitive (an event set, a lock released, a predicate that
  holds, a task done) or on an advance past its deadline. A primitive of the standard library
  is looked at again every STDLIB_SLICE of real time, since nothing of the clock's is told when
  it changes; an advance tells no condition of the product's, whose lock another thread may
  hold. ``poll`` really sleeps, at most POLL_CAP, or waits as long on the event it is given to
  end it, and moves no time, and so does ``apoll``; ``wait_polled`` counts those that have
  returned. A thread, or a task for ``apoll``, that polls on for the clock's hang bound of real
  time with no new generation, and no gap of more than POLL_GAP between two polls, is hung: its
  poll raises ClockHung.
- **free** (``with clock.free():``): a wait of the thread that entered it that is not
  satisfied moves the time by its seconds and returns as expired; the other threads' waits
  stay frozen, and no move of the free thread's may pass one of their deadlines.

Every timed wait but ``poll`` is a waiter while it waits, with its thread, its task, its
seconds and deadline, where it was called from and the generation it last looked at. Each
``advance`` and ``poke`` is a new generation; on each look a waiter first looks at its primitive
and its deadline, and only one that waits on records the generation, so that ``parked(n)``, n
waiters that all looked at the latest generation, means what they had to do then is done;
``wait_parked_where`` asks it of the waiters a test picks out.

What is refused raises ClockRefused in the caller: an advance past the deadline of a waiter of
a thread the clock does not own, ``free`` while another thread has a timed waiter, and, in the
free phase, a move past the deadline of a waiter of any thread but the free one. A refusal
raised inside a wait is also a violation, since the product that waits may swallow it. What is
misuse is a violation, which fails the test at its teardown: a thread the clock does not own
reading its time or starting a timed wait in the free phase, and a frozen wait that nothing
ends within the clock's hang bound, which also raises ClockHung in the waiter. At the test's
end the registry closes its clocks: a closed clock records nothing and never raises, and its
waits stay frozen and still end on their primitive.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any, TypeVar

import pytest

from helpers.clock import PollExternal
from tests import hang_guard
from tests.container_marker import MARKER

# The longest real sleep of a ``poll``.
POLL_CAP = PollExternal(0.1)
# A gap between two polls of one thread, or task, longer than this, of real time, starts their
# hang bound again: what it polls for now is another thing.
POLL_GAP = PollExternal(0.5)
# How often a frozen wait looks again at a primitive of the standard library.
STDLIB_SLICE = PollExternal(0.03)

T = TypeVar("T")

# What ``poll`` and ``apoll`` really sleep with, by name here, so that a test can see what they
# were given.
_sleep = time.sleep
_asleep = asyncio.sleep
# What ``until_parked`` waits to be told with.
_within = asyncio.wait_for


class ClockRefused(Exception):
    """An advance, or a free phase, that would move time past a thread that is not the
    clock's."""


class ClockHung(Exception):
    """A frozen wait that nothing ended within the clock's hang bound."""


class ClockOutsideTest(Exception):
    """A PhasedClock made outside a test: at import, or in a fixture of wider scope."""


@dataclasses.dataclass(eq=False)
class Waiter:
    method: str
    thread: threading.Thread
    task: asyncio.Task | None
    deadline: float | None
    site: str
    # The generation it last looked at; None before its first look.
    generation: int | None = None
    loop: asyncio.AbstractEventLoop | None = None
    # The seconds it was given.
    seconds: float | None = None


@dataclasses.dataclass
class _Polling:
    """A thread's, or a task's, polls since its bound began: the generation they are in,
    when the first began, and when the last returned."""

    generation: int
    started: float
    last: float | None = None


_HERE = __file__


def _site() -> str:
    """Where the clock was called from: the first frame outside this module."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == _HERE:
        frame = frame.f_back
    if frame is None:
        return "?"
    return f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"


class _Registry:
    """The clocks of one test, and the name of its tier's hang bound in tests/hang_guard.py."""

    def __init__(self, tier: str):
        self.tier = tier
        self.clocks: list[PhasedClock] = []


_REGISTRY = pytest.StashKey[_Registry]()
# The configs whose test is running now, the innermost last: a PhasedClock is made with no
# request to ask, so the fixture says which test's registry, in its config's stash, is the one.
_running: list[pytest.Config] = []


@pytest.fixture(autouse=True)
def phased_clocks(request):
    """The registry of the test's clocks, with its tier's hang bound: CONTAINER_HANG_GUARD for
    a test that carries the container marker, HANG_GUARD for the rest. At teardown, after every
    other fixture of the test's, the test fails on any violation and its clocks are closed."""
    tier = "CONTAINER_HANG_GUARD" if request.node.get_closest_marker(MARKER) else "HANG_GUARD"
    registry = _Registry(tier)
    request.config.stash[_REGISTRY] = registry
    _running.append(request.config)
    try:
        yield registry
    finally:
        _running.remove(request.config)
        del request.config.stash[_REGISTRY]
    said = [f"{v}" for made in registry.clocks for v in made.violations]
    for made in registry.clocks:
        made.close()
    if said:
        pytest.fail("the test's clocks were misused:\n" + "\n".join(said), pytrace=False)


def _current() -> _Registry:
    registry = _running[-1].stash.get(_REGISTRY, None) if _running else None
    if registry is None:
        raise ClockOutsideTest(
            "a PhasedClock is made in a test or a fixture of a test's own, never at import or "
            "in a fixture of wider scope"
        )
    return registry


class PhasedClock:
    """A clock whose time only the test moves: see the module's docstring."""

    def __init__(
        self,
        start: float = 1000.0,
        wall_start: float = 1_700_000_000.0,
        hang_after: float | None = None,
    ):
        registry = _current()
        self._tier = registry.tier
        self._hang_after = hang_after
        self._lock = threading.RLock()  # a primitive's look may ask the clock (``parked``)
        self._changed = threading.Condition(self._lock)
        self._now = float(start)
        self._wall_offset = float(wall_start) - float(start)
        self._owned: set[threading.Thread] = {threading.current_thread()}
        self._waiters: list[Waiter] = []
        self._ticks: dict[asyncio.AbstractEventLoop, asyncio.Event] = {}
        self._in_loop: dict[asyncio.AbstractEventLoop, int] = {}
        self._watchers: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._free: threading.Thread | None = None
        self._closed = False
        self.generation = 0
        self.waits: list[tuple[str, float | None, threading.Thread]] = []
        self.slept: list[float] = []
        # The polls that have returned.
        self.polls = 0
        self.violations: list[str] = []
        # Each thread's, or task's, polls, held weakly: a pooled thread or a task is not kept.
        self._polling: weakref.WeakKeyDictionary[object, _Polling] = weakref.WeakKeyDictionary()
        self._hung: weakref.WeakSet[object] = weakref.WeakSet()
        registry.clocks.append(self)

    # --- what the test asks ---

    @property
    def hang_after(self) -> float:
        """Real seconds a frozen wait may go unended: the one given, or the tier's, read now."""
        if self._hang_after is not None:
            return self._hang_after
        return getattr(hang_guard, self._tier)

    def lend(self, *threads: threading.Thread) -> None:
        """The clock is ``threads``' too: an advance may pass their waiters' deadlines."""
        with self._lock:
            self._owned.update(threads)

    def advance(self, seconds: float) -> None:
        """Move the time on by ``seconds``: a new generation."""
        if seconds < 0:
            raise ValueError(f"time does not go back: {seconds}")
        with self._lock:
            self._move(seconds)

    def poke(self) -> None:
        """A new generation with no time passing: every waiter looks again."""
        with self._lock:
            self._new_generation()

    def parked(self, n: int) -> bool:
        """Whether exactly ``n`` waiters wait, each having looked at the latest generation."""
        with self._lock:
            return self._parked(n)

    def wait_parked(self, n: int, within: float | None = None) -> None:
        """Until ``parked(n)``, told by each waiter's look, within ``within`` real seconds, by
        default the hang bound, so that a test whose waiters never park fails only after it;
        AssertionError after."""
        bound = self.hang_after if within is None else within
        with self._changed:
            if not self._changed.wait_for(lambda: self._parked(n), bound):
                raise AssertionError(f"not {n} parked within {bound}s: {self._said()}")

    def wait_parked_where(
        self, match: Callable[[Waiter], bool], n: int = 1, within: float | None = None
    ) -> None:
        """``wait_parked`` over the waiters ``match`` takes, the others ignored: until exactly
        ``n`` of them wait, each having looked at the latest generation."""
        bound = self.hang_after if within is None else within
        with self._changed:
            if not self._changed.wait_for(lambda: self._parked(n, match), bound):
                raise AssertionError(f"not {n} parked within {bound}s: {self._said()}")

    def wait_polled(self, n: int, within: float | None = None) -> None:
        """Until at least ``n`` polls, ``poll`` and ``apoll`` alike, have returned, told by each
        as it returns, within ``within`` real seconds, by default the hang bound; AssertionError
        after. A poll is counted once it has slept: in a loop that looks at the clock between
        its polls, a second poll counted after an ``advance`` follows a look at the time after
        it."""
        bound = self.hang_after if within is None else within
        with self._changed:
            if not self._changed.wait_for(lambda: self.polls >= n, bound):
                raise AssertionError(f"not {n} polled within {bound}s: {self.polls} were")

    async def until_parked(self, n: int, within: float | None = None) -> None:
        """``wait_parked`` for a coroutine, whose loop goes on meanwhile."""
        bound = self.hang_after if within is None else within
        loop = asyncio.get_running_loop()
        told = asyncio.Event()
        watcher = (loop, told)
        ends = time.monotonic() + bound
        with self._lock:
            self._watchers.append(watcher)
        try:
            while True:
                with self._lock:
                    told.clear()
                    if self._parked(n):
                        return
                left = ends - time.monotonic()
                try:
                    if left <= 0:
                        raise TimeoutError
                    await _within(told.wait(), left)
                except TimeoutError:
                    raise AssertionError(
                        f"not {n} parked within {bound}s: {self._said()}"
                    ) from None
        finally:
            with self._lock:
                self._watchers.remove(watcher)

    def waiters(self) -> list[Waiter]:
        """A copy of each waiter as it is now."""
        with self._lock:
            return [dataclasses.replace(w) for w in self._waiters]

    @contextlib.contextmanager
    def free(self) -> Iterator[PhasedClock]:
        """The free phase, for the thread that enters it: refused from a thread the clock does
        not own, and while another thread, lent or not, has a timed waiter: a wait with no
        deadline cannot be passed by a move, and stays frozen."""
        me = threading.current_thread()
        with self._lock:
            if not self._closed:
                if me not in self._owned:
                    raise ClockRefused(f"{me.name} is not the clock's and cannot free it")
                if self._free is not None:
                    raise ClockRefused(f"the clock is free already, for {self._free.name}")
                others = [w for w in self._waiters if w.thread is not me and w.deadline is not None]
                if others:
                    raise ClockRefused(f"another thread waits: {self._said(others)}")
            self._free = me
        try:
            yield self
        finally:
            with self._lock:
                self._free = None

    def close(self) -> None:
        """The test is over: nothing is recorded, nothing raises, and the waits stay frozen."""
        with self._lock:
            self._closed = True
            self._changed.notify_all()

    # --- the clock ---

    def now(self) -> float:
        with self._lock:
            self._reading("now")
            return self._now

    def wall(self) -> float:
        with self._lock:
            self._reading("wall")
            return self._now + self._wall_offset

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._record("sleep", seconds)
            if not self._closed:
                self.slept.append(seconds)
            if self._moves_freely(seconds):
                self._move(seconds, "sleep")
                return
        self._block("sleep", seconds, None)

    def poll(self, seconds: float, until: threading.Event | None = None) -> bool:
        with self._lock:
            self._record("poll", seconds)
            self._polls_on(threading.current_thread(), "poll")
        capped = min(max(seconds, 0.0), POLL_CAP)
        if until is None:
            _sleep(capped)
        else:
            until.wait(capped)
        with self._lock:
            self._polled(threading.current_thread())
        return until is not None and until.is_set()

    def wait(self, event: threading.Event, seconds: float | None) -> bool:
        with self._lock:
            self._record("wait", seconds)
            if self._moves_freely(seconds):
                if event.is_set():
                    return True
                self._move(seconds, "wait")
                return event.is_set()
        return self._block("wait", seconds, event.is_set)

    def acquire(self, lock: Any, seconds: float | None) -> bool:
        def taken() -> bool:
            return lock.acquire(blocking=False)

        with self._lock:
            self._record("acquire", seconds)
            if self._moves_freely(seconds):
                if taken():
                    return True
                self._move(seconds, "acquire")
                return False
        return self._block("acquire", seconds, taken)

    def wait_for(
        self, condition: threading.Condition, predicate: Callable[[], Any], seconds: float | None
    ) -> bool:
        """The caller holds ``condition``. Its predicate first, then ``condition.wait`` in
        slices, since an advance tells no condition of the product's. The predicate is looked at
        outside the clock's lock, which is never taken while a product's lock is waited for. So
        this wait alone has a gap of one look: its predicate and the generation it records are
        not one step, and a poke between them is recorded as looked at though the predicate was
        looked at before it. State the predicate reads under ``condition``, which the waiter
        holds across both, cannot change in that gap."""
        with self._lock:
            self._record("wait_for", seconds)
            freely = self._moves_freely(seconds)
            waiter = None if freely else self._register("wait_for", seconds)
            started = time.monotonic()
        if freely:
            if predicate():
                return True
            with self._lock:
                self._move(seconds, "wait_for")
            return bool(predicate())
        try:
            while True:
                if predicate():
                    return True
                with self._lock:
                    if self._expired(waiter):
                        return False
                    self._keep_waiting(waiter)
                    left = self._hang_left(waiter, started)
                condition.wait(STDLIB_SLICE if left is None else min(STDLIB_SLICE, left))
        finally:
            with self._lock:
                self._deregister(waiter)

    async def asleep(self, seconds: float) -> None:
        with self._lock:
            self._record("asleep", seconds)
            if not self._closed:
                self.slept.append(seconds)
            if self._moves_freely(seconds):
                self._move(seconds, "asleep")
                return
        await self._await("asleep", seconds, None, None, lambda: [])

    async def apoll(self, seconds: float) -> None:
        with self._lock:
            self._record("apoll", seconds)
            self._polls_on(asyncio.current_task(), "apoll")
        await _asleep(min(max(seconds, 0.0), POLL_CAP))
        with self._lock:
            self._polled(asyncio.current_task())

    async def await_event(self, event: asyncio.Event, seconds: float | None) -> bool:
        with self._lock:
            self._record("await_event", seconds)
            if self._moves_freely(seconds):
                if event.is_set():
                    return True
                self._move(seconds, "await_event")
                return event.is_set()
        await self._await("await_event", seconds, event.is_set, event.wait, lambda: [])
        return event.is_set()

    async def within(self, awaitable: Awaitable[T], seconds: float | None) -> T:
        inner = asyncio.ensure_future(awaitable)
        with self._lock:
            self._record("within", seconds)
            freely = self._moves_freely(seconds)
        try:
            if freely:
                await asyncio.sleep(0)  # one turn of the loop first
                if not inner.done():
                    with self._lock:
                        self._move(seconds, "within")
            else:
                await self._await("within", seconds, inner.done, None, lambda: [inner])
        except BaseException:
            if not inner.done():
                inner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await inner
            raise
        if inner.done():
            return inner.result()
        inner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inner
        raise TimeoutError

    async def await_tasks(
        self,
        tasks: Iterable[asyncio.Future],
        seconds: float | None,
        return_when: str = asyncio.FIRST_COMPLETED,
    ) -> tuple[set[asyncio.Future], set[asyncio.Future]]:
        """As ``asyncio.wait``: a task still pending is returned as it is, never cancelled."""
        held = set(tasks)
        if not held:
            raise ValueError("no tasks to wait for")  # as asyncio.wait

        def enough() -> bool:
            done = [t for t in held if t.done()]
            if len(done) == len(held):
                return True
            if return_when == asyncio.FIRST_COMPLETED:
                return bool(done)
            if return_when == asyncio.FIRST_EXCEPTION:
                return any(not t.cancelled() and t.exception() is not None for t in done)
            return False

        with self._lock:
            self._record("await_tasks", seconds)
            freely = self._moves_freely(seconds)
        if freely:
            await asyncio.sleep(0)  # one turn of the loop first
            if not enough():
                with self._lock:
                    self._move(seconds, "await_tasks")
        else:
            await self._await(
                "await_tasks", seconds, enough, None, lambda: [t for t in held if not t.done()]
            )
        done = {t for t in held if t.done()}
        return done, held - done

    # --- inside; the lock is held where it says ---

    def _said(self, waiters: list[Waiter] | None = None) -> str:
        shown = self._waiters if waiters is None else waiters
        return (
            "; ".join(
                f"{w.method} of {w.thread.name} at {w.site}, deadline {w.deadline}, "
                f"looked at {w.generation} of {self.generation}"
                for w in shown
            )
            or "no waiter"
        )

    def _violate(self, said: str) -> None:
        if not self._closed:
            self.violations.append(said)

    def _owns(self, thread: threading.Thread) -> bool:
        return thread in self._owned

    def _reading(self, what: str) -> None:
        me = threading.current_thread()
        if self._free is not None and not self._owns(me):
            self._violate(
                f"{me.name}, not the clock's, read its {what}() in the free phase at {_site()}"
            )

    def _record(self, method: str, seconds: float | None) -> None:
        if not self._closed:
            self.waits.append((method, seconds, threading.current_thread()))

    def _moves_freely(self, seconds: float | None) -> bool:
        return seconds is not None and self._free is threading.current_thread()

    def _parked(self, n: int, match: Callable[[Waiter], bool] | None = None) -> bool:
        shown = self._waiters if match is None else [w for w in self._waiters if match(w)]
        return len(shown) == n and all(w.generation == self.generation for w in shown)

    def _polled(self, key: object) -> None:
        self.polls += 1
        polling = self._polling.get(key)
        if polling is not None:
            polling.last = time.monotonic()
        self._changed.notify_all()

    def _polls_on(self, key: object, method: str) -> None:
        """A poll of ``key``, a thread or a task: ClockHung once it has polled for the hang
        bound of real time with no new generation, and no gap of more than POLL_GAP since its
        last poll returned; a closed clock's go on without end."""
        if self._closed:
            return
        now = time.monotonic()
        since = self._polling.get(key)
        if (
            since is None
            or since.generation != self.generation
            or (since.last is not None and now - since.last > POLL_GAP)
        ):
            self._polling[key] = _Polling(self.generation, now)
            self._hung.discard(key)
            return
        bound = self.hang_after
        if now - since.started < bound:
            return
        said = (
            f"ClockHung: {method} of {threading.current_thread().name} at {_site()} polled on "
            f"for {bound}s of real time with no advance"
        )
        if key not in self._hung:  # recorded once, raised on each poll after
            self._hung.add(key)
            self._violate(said)
        raise ClockHung(said)

    def _move(self, seconds: float | None, waiting: str | None = None) -> None:
        """Move the time on; ``waiting``, the wait that moves it in the free phase, whose
        refusal is also a violation, since the product that waits may swallow it."""
        target = self._now + max(seconds or 0.0, 0.0)
        if not self._closed:
            passed = [
                w
                for w in self._waiters
                if w.deadline is not None and w.deadline <= target and not self._owns(w.thread)
            ]
            if passed:
                self._refuse(
                    f"moving to {target} passes the deadline of a thread that is not the "
                    f"clock's: {self._said(passed)}",
                    waiting,
                )
            if self._free is not None:
                passed = [
                    w
                    for w in self._waiters
                    if w.deadline is not None
                    and w.deadline <= target
                    and w.thread is not self._free
                ]
                if passed:
                    self._refuse(
                        f"moving to {target} in the free phase passes the deadline of another "
                        f"thread, which would run on time it did not wait: {self._said(passed)}",
                        waiting,
                    )
        self._now = target
        self._new_generation()

    def _refuse(self, said: str, waiting: str | None) -> None:
        if waiting is not None:
            self._violate(f"ClockRefused in {waiting} at {_site()}: {said}")
        raise ClockRefused(said)

    def _new_generation(self) -> None:
        self.generation += 1
        self._changed.notify_all()
        for loop, tick in self._ticks.items():
            with contextlib.suppress(RuntimeError):  # its loop is closed
                loop.call_soon_threadsafe(tick.set)
        self._ticks.clear()

    def _register(
        self,
        method: str,
        seconds: float | None,
        task: asyncio.Task | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> Waiter:
        me = threading.current_thread()
        site = _site()
        if self._free is not None and not self._owns(me):
            self._violate(
                f"{me.name}, not the clock's, started {method}({seconds}) in the free phase at "
                f"{site}"
            )
        deadline = None if seconds is None else self._now + max(seconds, 0.0)
        waiter = Waiter(method, me, task, deadline, site, None, loop, seconds)
        self._waiters.append(waiter)
        if loop is not None:
            self._in_loop[loop] = self._in_loop.get(loop, 0) + 1
        return waiter

    def _deregister(self, waiter: Waiter) -> None:
        self._waiters.remove(waiter)
        loop = waiter.loop
        if loop is not None:
            self._in_loop[loop] -= 1
            if not self._in_loop[loop]:  # its last waiter: its tick is forgotten
                del self._in_loop[loop]
                self._ticks.pop(loop, None)
        self._told()

    def _expired(self, waiter: Waiter) -> bool:
        return waiter.deadline is not None and self._now >= waiter.deadline

    def _keep_waiting(self, waiter: Waiter) -> None:
        """A waiter that looked and waits on: the generation it looked at."""
        if waiter.generation != self.generation:
            waiter.generation = self.generation
            self._told()

    def _told(self) -> None:
        """Those that wait for waiters to be parked look again."""
        self._changed.notify_all()
        for loop, told in self._watchers:
            with contextlib.suppress(RuntimeError):  # its loop is closed
                loop.call_soon_threadsafe(told.set)

    def _hang_left(self, waiter: Waiter, started: float) -> float | None:
        """Real seconds left before the wait is hung, None for a closed clock; ClockHung once
        none are."""
        if self._closed:
            return None
        bound = self.hang_after
        left = bound - (time.monotonic() - started)
        if left <= 0:
            said = (
                f"ClockHung: {waiter.method} of {waiter.thread.name} at {waiter.site} was ended "
                f"by nothing within {bound}s of real time"
            )
            self._violate(said)
            raise ClockHung(said)
        return left

    def _block(self, method: str, seconds: float | None, ready: Callable[[], bool] | None) -> bool:
        """A frozen wait of a thread: True once ``ready``, False once its deadline is passed."""
        with self._lock:
            waiter = self._register(method, seconds)
            started = time.monotonic()
            try:
                while True:
                    if ready is not None and ready():
                        return True
                    if self._expired(waiter):
                        return False
                    self._keep_waiting(waiter)
                    left = self._hang_left(waiter, started)
                    if ready is None:
                        self._changed.wait(left)
                    else:
                        self._changed.wait(
                            STDLIB_SLICE if left is None else min(STDLIB_SLICE, left)
                        )
            finally:
                self._deregister(waiter)

    async def _await(
        self,
        method: str,
        seconds: float | None,
        ready: Callable[[], bool] | None,
        helper: Callable[[], Awaitable[Any]] | None,
        theirs: Callable[[], list[asyncio.Future]],
    ) -> bool:
        """A frozen wait of a task: True once ``ready``, False once its deadline is passed. It
        waits on the loop's tick, on ``helper()`` made anew for each look and cancelled after,
        and on ``theirs()``, which are never cancelled."""
        loop = asyncio.get_running_loop()
        with self._lock:
            waiter = self._register(method, seconds, asyncio.current_task(), loop)
            started = time.monotonic()
        try:
            while True:
                with self._lock:
                    if ready is not None and ready():
                        return True
                    if self._expired(waiter):
                        return False
                    self._keep_waiting(waiter)
                    left = self._hang_left(waiter, started)
                    tick = self._ticks.get(loop)
                    if tick is None:
                        tick = self._ticks[loop] = asyncio.Event()
                mine = [asyncio.ensure_future(tick.wait())]
                if helper is not None:
                    mine.append(asyncio.ensure_future(helper()))
                try:
                    await asyncio.wait([*mine, *theirs()], timeout=left,
                                       return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                finally:
                    for made in mine:
                        with contextlib.suppress(RuntimeError):  # its loop is closed
                            made.cancel()
        finally:
            with self._lock:
                self._deregister(waiter)
