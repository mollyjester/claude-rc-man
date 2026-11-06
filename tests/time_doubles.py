"""Test doubles for time: stand-ins a test puts in place of a product's ``time`` module, its
condition, its event and its locks, so that what a test asserts of time never races real time.
A pytest plugin, which tests/conftest.py loads.

Not a test module. The rules are in project-knowledge/tests.md, "Time in tests".

- ``FakeTime`` stands in for a module's ``time`` (``monkeypatch.setattr(module, "time", fake)``):
  ``monotonic`` and ``time`` move only by ``advance``; a sleep, numbered as it begins and counted
  as it returns, really sleeps at most POLL_CAP and moves no time (polling, the default), or,
  with ``parking`` set, parks until an advance covers it; ``parking`` may instead be a test of
  a sleep's seconds, so that only the sleeps it picks park.
- ``WatchedCondition`` stands in for a ``threading.Condition``: its ``wait_for`` is recorded and,
  in virtual mode, its timeout never runs out by itself; ``expire`` ends a wait as if it had.
- ``ControlledEvent`` stands in for a ``threading.Event``: a ``wait`` given seconds is recorded
  and ends only on ``set`` or ``expire``.
- ``RecordingLock`` wraps a lock, recording each acquire and each that blocked.

Every wait is recorded (``Wait``) with its thread, its seconds and an ordinal of its double's,
under the primitive's own lock and only once what it waits for has been seen not to hold: a
recorded waiter really waits, and what is told after the record reaches it. A test waits for
records through the double's own condition (``wait_waiting``, ``wait_parked``,
``wait_blocked``, ``wait_sleep_begun``, ``wait_sleeps_returned``), never by polling, and picks
waits out by a match: a thread, a number of seconds, or a predicate on the record.

A frozen wait, one nothing ends but the test, that goes the double's hang bound of real time
unended raises ``DoubleHung`` in its waiter, and so does a polling sleep loop that goes the bound
with no advance - a loop, since a thread's sleeps count as one only while no more than POLL_GAP
of real time passes between one's return and its next, a longer gap starting the bound again,
as a pooled thread may poll for one thing and later for another; a test's own wait on a double
fails as long after. ``DoubleHung`` is a
BaseException, which the product's ``except Exception`` lets by, and each is also recorded on the
double: the plugin's autouse fixture fails the test at its teardown on any recorded hang of any
double the test made, so that a hang in a thread whose caller never sees it still fails the test.
A double is made in a test or a fixture of a test's own; its bound is the one it is given, or its
tier's: HANG_GUARD, or CONTAINER_HANG_GUARD for a test that carries the container marker. At the
test's end the registry closes its doubles: a closed double records no hang, and its waits go
on until what they wait for ends them.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

import pytest

from tests import hang_guard
from tests.container_marker import MARKER

# The longest real sleep of a polling ``FakeTime.sleep``.
POLL_CAP = 0.02
# A gap longer than this, of real time, between one polling sleep of a thread's and its next
# starts that thread's hang bound again: what it polls for now is another thing.
POLL_GAP = 0.5


class DoubleHung(BaseException):  # noqa: N818 - named as what it says, as the product's are
    """A frozen wait on a double that nothing ended within the double's hang bound."""


class DoubleOutsideTest(Exception):  # noqa: N818
    """A double made outside a test: at import, or in a fixture of wider scope."""


@dataclasses.dataclass(eq=False)
class Wait:
    """One wait on a double: its thread, its seconds (None for none), its ordinal among the
    double's, what kind of wait it is, and how it ended: None while it waits, else one of
    "predicate", "set", "expired", "timeout", "advanced", "returned", "acquired", "hung",
    "raised"."""

    ordinal: int
    thread: threading.Thread
    seconds: float | None
    kind: str
    ended: str | None = None
    seen: int = 0  # FakeTime's generation it last looked at
    expiring: bool = False


@dataclasses.dataclass(frozen=True)
class Call:
    """One acquire of a RecordingLock's."""

    blocking: bool
    timeout: float
    thread: threading.Thread


Match = threading.Thread | float | int | Callable[[Wait], bool] | None


def matches(wait: Wait, match: Match) -> bool:
    """Whether ``match`` picks ``wait``: any for None, by thread, by a predicate on the record,
    or else by its seconds."""
    if match is None:
        return True
    if isinstance(match, threading.Thread):
        return wait.thread is match
    if callable(match):
        return bool(match(wait))
    return wait.seconds == match


# --- the registry -------------------------------------------------------------------------------


class _Registry:
    """The doubles of one test, and the name of its tier's hang bound in tests/hang_guard.py."""

    def __init__(self, tier: str):
        self.tier = tier
        self.doubles: list[_Double] = []


# The registries of the tests running now, the innermost last.
_running: list[_Registry] = []


@pytest.fixture(autouse=True)
def time_doubles(request):
    """The registry of the test's doubles, with its tier's hang bound. At teardown, after every
    other fixture of the test's, the test fails on any hang a double recorded, and the doubles
    are closed."""
    tier = "CONTAINER_HANG_GUARD" if request.node.get_closest_marker(MARKER) else "HANG_GUARD"
    registry = _Registry(tier)
    _running.append(registry)
    try:
        yield registry
    finally:
        _running.remove(registry)
    said = [said for made in registry.doubles for said in made.hung]
    for made in registry.doubles:
        made.close()
    if said:
        pytest.fail("a frozen wait on a test double hung:\n" + "\n".join(said), pytrace=False)


def _current() -> _Registry:
    if not _running:
        raise DoubleOutsideTest(
            "a time double is made in a test or a fixture of a test's own, never at import or "
            "in a fixture of wider scope"
        )
    return _running[-1]


# --- what every double has ----------------------------------------------------------------------


class _Double:
    """The records of a double's waits, the test's waits on them, and the hang bound."""

    def __init__(self, hang_after: float | None):
        registry = _current()
        self._tier = registry.tier
        self._hang_after = hang_after
        self._told = threading.Condition()  # the double's own: records, and the test's waits
        self._ordinal = 0
        self._closed = False
        self.waits: list[Wait] = []  # every wait recorded, in order
        self.waiting: list[Wait] = []  # those that wait now
        self.hung: list[str] = []
        registry.doubles.append(self)

    @property
    def hang_after(self) -> float:
        """Real seconds a frozen wait may go unended: the one given, or the tier's, read now."""
        if self._hang_after is not None:
            return self._hang_after
        return getattr(hang_guard, self._tier)

    def close(self) -> None:
        """The test is over: no hang is recorded or raised from here on."""
        with self._told:
            self._closed = True
            self._told.notify_all()

    def mark(self) -> int:
        """The latest ordinal given: a wait recorded after this has a greater one."""
        with self._told:
            return self._ordinal

    def _record(self, kind: str, seconds: float | None, waiting: bool = True) -> Wait:
        with self._told:
            self._ordinal += 1
            made = Wait(self._ordinal, threading.current_thread(), seconds, kind)
            made.seen = getattr(self, "generation", 0)
            self.waits.append(made)
            if waiting:
                self.waiting.append(made)
            self._told.notify_all()
        return made

    def _end(self, wait: Wait, how: str) -> None:
        with self._told:
            if wait.ended is None:
                wait.ended = how
            if wait in self.waiting:
                self.waiting.remove(wait)
            self._told.notify_all()

    def _hang_left(self, wait: Wait, started: float) -> float | None:
        """Real seconds left before ``wait`` is hung, None once the double is closed; raises
        DoubleHung, recorded, once none are."""
        if self._closed:
            return None
        bound = self.hang_after
        left = bound - (time.monotonic() - started)
        if left > 0:
            return left
        said = (
            f"DoubleHung: {type(self).__name__}.{wait.kind}({wait.seconds}) of "
            f"{wait.thread.name} was ended by nothing within {bound}s of real time"
        )
        with self._told:
            self.hung.append(said)
        self._end(wait, "hung")
        raise DoubleHung(said)

    def _until(self, enough: Callable[[], Any], what: str, within: float | None) -> Any:
        """The test's wait, told by the double's condition, at most ``within`` or the bound."""
        bound = self.hang_after if within is None else within
        with self._told:
            found = self._told.wait_for(enough, bound)
        if not found:
            raise AssertionError(f"{what} did not come about within {bound}s")
        return found

    def wait_waiting(self, n: int = 1, match: Match = None, within: float | None = None):
        """Until at least ``n`` waits that ``match`` picks wait now: those waits."""

        def enough():
            picked = [w for w in self.waiting if matches(w, match) and self._looked(w)]
            return picked if len(picked) >= n else None

        return self._until(enough, f"{n} waits on {type(self).__name__} ({match})", within)

    wait_parked = wait_waiting

    def _looked(self, wait: Wait) -> bool:
        return True

    def _expire(self, match: Match, wake: Callable[[], None]) -> int:
        with self._told:
            chosen = [w for w in self.waiting if matches(w, match)]
            if not chosen:
                raise AssertionError(f"no wait on {type(self).__name__} matches {match}")
            for wait in chosen:
                wait.expiring = True
                self.waiting.remove(wait)
            self._told.notify_all()
        wake()
        return len(chosen)


# --- the doubles --------------------------------------------------------------------------------


class WatchedCondition(threading.Condition, _Double):
    """A condition on ``lock`` (a plain Lock by default, as the product makes) whose
    ``wait_for`` is recorded: in virtual mode its timeout never runs out by itself, and a wait
    ends on its predicate or on ``expire``; in real mode the timeout runs as the standard
    library's would."""

    def __init__(self, lock: Any = None, *, virtual: bool = False, hang_after: float | None = None):
        threading.Condition.__init__(self, threading.Lock() if lock is None else lock)
        _Double.__init__(self, hang_after)
        self.virtual = virtual

    # The whole of wait_for: the standard library's recomputes the timeout from threading's own
    # clock, which no patch of a module's time reaches.
    def wait_for(self, predicate, timeout=None):
        result = predicate()
        if result:
            return result
        wait = self._record("wait_for", timeout)
        started = time.monotonic()
        runs_out = None if self.virtual or timeout is None else started + timeout
        try:
            while True:
                if wait.expiring:
                    result = predicate()
                    self._end(wait, "predicate" if result else "expired")
                    return result
                left = self._hang_left(wait, started)
                if runs_out is not None:
                    real = runs_out - time.monotonic()
                    if real <= 0:
                        result = predicate()
                        self._end(wait, "predicate" if result else "timeout")
                        return result
                    left = real if left is None else min(left, real)
                threading.Condition.wait(self, left)
                result = predicate()
                if result:
                    self._end(wait, "predicate")
                    return result
        except BaseException:
            self._end(wait, "raised")  # none if it is ended already: a hang is "hung"
            raise

    def expire(self, match: Match = None) -> int:
        """End the waits ``match`` picks as if their timeout had run out: each returns its
        predicate as it then is. How many were ended; none is refused."""

        def wake():
            with self:
                self.notify_all()

        return self._expire(match, wake)


class ControlledEvent(threading.Event, _Double):
    """An event whose ``wait`` given seconds is recorded and ends only on ``set`` or ``expire``;
    ``wait()`` with none, ``set`` and ``is_set`` are the event's own."""

    def __init__(self, *, hang_after: float | None = None):
        threading.Event.__init__(self)
        _Double.__init__(self, hang_after)

    def wait(self, timeout=None):
        if timeout is None:
            return threading.Event.wait(self)
        with self._cond:
            if self._flag:
                return True
            wait = self._record("wait", timeout)
            started = time.monotonic()
            try:
                while True:
                    if self._flag:
                        self._end(wait, "set")
                        return True
                    if wait.expiring:
                        self._end(wait, "expired")
                        return False
                    self._cond.wait(self._hang_left(wait, started))
            except BaseException:
                self._end(wait, "raised")
                raise

    def expire(self, match: Match = None) -> int:
        """End the waits ``match`` picks as if their seconds had passed: each returns False. How
        many were ended; none is refused."""

        def wake():
            with self._cond:
                self._cond.notify_all()

        return self._expire(match, wake)


class RecordingLock(_Double):
    """``inner``, a lock or an RLock, with each acquire recorded (``calls``) and each that
    blocked recorded as a wait once a try without blocking has failed and before it blocks."""

    def __init__(self, inner: Any, *, hang_after: float | None = None):
        _Double.__init__(self, hang_after)
        self._inner = inner
        self.calls: list[Call] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._told:
            self.calls.append(Call(blocking, timeout, threading.current_thread()))
        if self._inner.acquire(False):
            return True
        if not blocking:
            return False
        wait = self._record("acquire", None if timeout < 0 else timeout)
        got = False
        try:
            got = self._inner.acquire(True, timeout)
        finally:
            self._end(wait, "acquired" if got else "timeout")
        return got

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def wait_blocked(self, n: int = 1, match: Match = None, within: float | None = None):
        """Until at least ``n`` acquires that ``match`` picks have blocked, whether or not they
        still do: those acquires."""

        def enough():
            picked = [w for w in self.waits if matches(w, match)]
            return picked if len(picked) >= n else None

        return self._until(enough, f"{n} blocked acquires ({match})", within)


class FakeTime(_Double):
    """A module's ``time``, whose ``monotonic`` and ``time`` move only by ``advance``; every
    other attribute is the real module's. Used only where the test owns every thread that reads
    the module's ``time``."""

    def __init__(
        self,
        start: float = 1000.0,
        wall_start: float = 1_700_000_000.0,
        *,
        hang_after: float | None = None,
    ):
        _Double.__init__(self, hang_after)
        self._now = float(start)
        self._wall_offset = float(wall_start) - float(start)
        self.parking: bool | Callable[[float], bool] = False
        self.generation = 0  # each advance is one
        self.sleeps_begun = 0
        self.sleeps_returned = 0
        # Each polling thread's streak: (real time it began, real time of its last return,
        # generation it began at).
        self._streaks: weakref.WeakKeyDictionary[threading.Thread, list] = (
            weakref.WeakKeyDictionary()
        )

    def monotonic(self) -> float:
        with self._told:
            return self._now

    def time(self) -> float:
        with self._told:
            return self._now + self._wall_offset

    def __getattr__(self, name: str):
        return getattr(time, name)

    def advance(self, seconds: float) -> None:
        with self._told:
            self._now += seconds
            self.generation += 1
            self._told.notify_all()

    def sleep(self, seconds: float) -> None:
        with self._told:  # its end taken with its record: no advance comes between them
            parking = self.parking(seconds) if callable(self.parking) else self.parking
            until = self._now + seconds
            wait = self._record("sleep", seconds, waiting=parking)
            self.sleeps_begun += 1
        try:
            if parking:
                self._park(wait, until)
            else:
                self._poll(wait)
        finally:
            with self._told:
                self.sleeps_returned += 1
                if wait.ended is None:
                    wait.ended = "returned"
                if wait in self.waiting:
                    self.waiting.remove(wait)
                self._told.notify_all()

    def _park(self, wait: Wait, until: float) -> None:
        started = time.monotonic()
        with self._told:  # an RLock: _hang_left and _end take it again
            while self._now < until:
                wait.seen = self.generation
                self._told.notify_all()
                self._told.wait(self._hang_left(wait, started))
            wait.ended = "advanced"

    def _poll(self, wait: Wait) -> None:
        thread = threading.current_thread()
        real = time.monotonic()
        with self._told:
            streak = self._streaks.get(thread)
            if streak is None or streak[2] != self.generation or real - streak[1] > POLL_GAP:
                streak = [real, real, self.generation]
                self._streaks[thread] = streak
        if not self._closed and real - streak[0] >= self.hang_after:
            self._hang_left(wait, streak[0])  # raises
        time.sleep(min(max(wait.seconds, 0.0), POLL_CAP))
        with self._told:
            streak[1] = time.monotonic()

    def _looked(self, wait: Wait) -> bool:
        return wait.seen == self.generation

    def wait_sleep_begun(self, after: int, n: int = 1, within: float | None = None) -> list[Wait]:
        """Until ``n`` sleeps numbered after ``after`` have begun: those begun so far."""

        def enough():
            begun = [w for w in self.waits if w.ordinal > after]
            return begun if len(begun) >= n else None

        return self._until(enough, f"{n} sleeps begun after {after}", within)

    def wait_sleeps_returned(self, after: int, n: int, within: float | None = None) -> list[Wait]:
        """Until ``n`` sleeps numbered after ``after`` have returned: those."""

        def enough():
            done = [w for w in self.waits if w.ordinal > after and w.ended is not None]
            return done if len(done) >= n else None

        return self._until(enough, f"{n} sleeps returned after {after}", within)
