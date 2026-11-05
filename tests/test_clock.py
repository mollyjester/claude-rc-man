"""helpers/clock.py: the clock every part of rcman that reads time or waits is given, and the
real one that is every part's default, which hands each call straight to the standard library.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import threading
import typing
from pathlib import Path

import pytest

from helpers import clock as clock_module
from helpers.clock import REAL, Clock, RealClock

SOURCE = Path(clock_module.__file__)


# --- what it hands on ---------------------------------------------------------------------------


def test_now_wall_sleep_and_poll_are_the_standard_librarys(monkeypatch):
    slept = []
    monkeypatch.setattr(clock_module.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(clock_module.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(clock_module.time, "sleep", slept.append)
    assert REAL.now() == 5.0
    assert REAL.wall() == 1_700_000_000.0
    REAL.sleep(2.5)
    REAL.poll(0.25)
    assert slept == [2.5, 0.25]


def test_the_async_sleeps_are_asyncios(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(clock_module.asyncio, "sleep", sleep)
    asyncio.run(REAL.asleep(2.5))
    asyncio.run(REAL.apoll(0.25))
    assert slept == [2.5, 0.25]


class Recorder:
    """An event, a condition and a lock at once, which records how it is waited on."""

    def __init__(self):
        self.calls: list[tuple] = []

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        return True

    def wait_for(self, predicate, timeout=None):
        self.calls.append(("wait_for", timeout))
        return predicate()

    def acquire(self, *args, **kwargs):
        self.calls.append(("acquire", args, kwargs))
        return True


@pytest.mark.parametrize("seconds", [3.0, None])
def test_each_wait_hands_its_seconds_on(seconds):
    made = Recorder()
    assert REAL.wait(made, seconds) is True
    assert REAL.wait_for(made, lambda: "yes", seconds) is True
    assert REAL.acquire(made, seconds) is True
    blocking = ("acquire", (), {}) if seconds is None else ("acquire", (), {"timeout": seconds})
    assert made.calls == [("wait", seconds), ("wait_for", seconds), blocking]


def test_wait_for_answers_whether_the_predicate_held_last():
    made = Recorder()
    assert REAL.wait_for(made, lambda: [], 1.0) is False
    assert REAL.wait_for(made, lambda: [1], 1.0) is True


def test_negative_seconds_become_none_at_all(monkeypatch):
    slept = []
    monkeypatch.setattr(clock_module.time, "sleep", slept.append)
    made = Recorder()
    REAL.wait(made, -1.0)
    REAL.wait_for(made, lambda: True, -1.0)
    REAL.acquire(made, -1.0)
    REAL.sleep(-1.0)
    REAL.poll(-1.0)
    assert made.calls == [("wait", 0.0), ("wait_for", 0.0), ("acquire", (), {"timeout": 0.0})]
    assert slept == [0.0, 0.0]


def test_seconds_beyond_what_the_standard_library_takes_are_cut_to_it():
    """threading's waits refuse more than TIMEOUT_MAX with OverflowError, which
    ``Condition.wait_for`` can reach from a little less, by its own arithmetic: half of it."""
    made = Recorder()
    beyond = threading.TIMEOUT_MAX * 10
    REAL.wait(made, beyond)
    REAL.wait_for(made, lambda: True, beyond)
    REAL.acquire(made, beyond)
    most = threading.TIMEOUT_MAX / 2
    assert made.calls == [("wait", most), ("wait_for", most), ("acquire", (), {"timeout": most})]
    assert REAL.wait(threading.Event(), -1) is False  # the control: a real event takes it


def test_a_semaphore_acquired_without_seconds_blocks_until_it_is_released():
    """timeout=-1 does not block on a Semaphore, as it does on a Lock: None is acquire()."""
    semaphore = threading.Semaphore(0)
    got: list[bool] = []
    done = threading.Event()

    def take():
        got.append(REAL.acquire(semaphore, None))
        done.set()

    worker = threading.Thread(target=take, daemon=True)
    worker.start()
    try:
        assert not done.wait(0.3)  # still blocked
    finally:
        semaphore.release()
        worker.join(10)
    assert not worker.is_alive() and got == [True]


def test_a_lock_acquired_with_seconds_gives_up_after_them():
    lock = threading.Lock()
    lock.acquire()
    try:
        assert REAL.acquire(lock, 0.05) is False
    finally:
        lock.release()
    assert REAL.acquire(lock, 0.05) is True
    lock.release()


# --- the async forms ------------------------------------------------------------------------------


def test_await_event_on_an_event_already_set_with_no_seconds_is_true():
    async def scenario():
        event = asyncio.Event()
        event.set()
        assert await REAL.await_event(event, 0) is True
        unset = asyncio.Event()
        assert await REAL.await_event(unset, 0) is False
        assert await REAL.await_event(unset, 0.01) is False
        later = asyncio.Event()
        asyncio.get_running_loop().call_soon(later.set)
        assert await REAL.await_event(later, None) is True

    asyncio.run(scenario())


def test_await_tasks_leaves_the_pending_tasks_running():
    async def scenario():
        go = asyncio.Event()
        quick = asyncio.create_task(asyncio.sleep(0))
        held = asyncio.create_task(go.wait())
        done, pending = await REAL.await_tasks({quick, held}, 0.05, asyncio.ALL_COMPLETED)
        assert done == {quick} and pending == {held}
        assert not held.cancelled() and not held.done()
        go.set()
        done, pending = await REAL.await_tasks({held}, None, asyncio.FIRST_COMPLETED)
        assert done == {held} and pending == set()

    asyncio.run(scenario())


def test_await_tasks_refuses_no_tasks_at_all():
    async def scenario():
        with pytest.raises(ValueError):
            await REAL.await_tasks(set(), 1.0)

    asyncio.run(scenario())


def test_within_is_asyncios_wait_for():
    async def scenario():
        async def answer():
            return 7

        assert await REAL.within(answer(), 1.0) == 7
        assert await REAL.within(answer(), None) == 7
        with pytest.raises(TimeoutError):
            await REAL.within(asyncio.Event().wait(), 0.01)

    asyncio.run(scenario())


# --- the clock and its markers ----------------------------------------------------------------


def test_the_real_clock_is_a_clock_and_the_default():
    assert isinstance(REAL, RealClock)
    assert isinstance(REAL, Clock)  # runtime-checkable
    assert vars(REAL) == {}  # stateless


MARKERS = ("Deadline", "Grace", "Pace", "PollExternal", "RealBound", "Handed")


@pytest.mark.parametrize("name", MARKERS)
def test_each_marker_is_a_float_named_for_its_class(name):
    marker = getattr(clock_module, name)
    assert marker.__supertype__ is float and marker.__name__ == name
    assert isinstance(marker, typing.NewType)
    assert marker(3.0) == 3.0 and type(marker(3.0)) is float


def test_polling_own_state_has_no_marker():
    assert hasattr(clock_module, "PollExternal")  # the control: the markers are found by name
    assert not hasattr(clock_module, "PollOwn")


# --- what it imports --------------------------------------------------------------------------


def imported(source: str) -> set[str]:
    """The top-level modules ``source`` imports; a relative import is refused."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, node.lineno
            found.add(node.module.split(".")[0])
    return found


def foreign(source: str) -> set[str]:
    """What ``source`` imports that is not of the standard library."""
    return imported(source) - {"__future__"} - set(sys.stdlib_module_names)


def test_the_clock_imports_the_standard_library_only():
    """It reaches the canary through the helpers mount, which has nothing else."""
    source = SOURCE.read_text(encoding="utf-8")
    assert {"time", "asyncio"} <= imported(source)  # non-vacuity
    assert "from __future__ import annotations" in source
    assert foreign(source) == set()


def test_the_import_check_finds_a_module_from_outside_the_standard_library():
    planted = SOURCE.read_text(encoding="utf-8") + "\nimport pydantic\nfrom rcman import db\n"
    assert foreign(planted) == {"pydantic", "rcman"}
