"""The test doubles for time (tests/time_doubles.py): what each records, what ends each wait,
the hang bound of a frozen wait, and the registry that fails a test on a hang its product
swallowed, the registry's own cases in child pytests."""

from __future__ import annotations

import threading

from tests import time_doubles
from tests.hang_guard import HANG_GUARD
from tests.time_doubles import (
    ControlledEvent,
    DoubleHung,
    FakeTime,
    WatchedCondition,
)

# A bound short enough for a twin that waits it out.
SHORT = 0.2


class Ran:
    """A call in a daemon thread: what it returned, or what it raised, BaseException included."""

    def __init__(self, call, *args):
        self.returned = self.raised = None
        self.thread = threading.Thread(target=self._run, args=(call, *args), daemon=True)
        self.thread.start()

    def _run(self, call, *args):
        try:
            self.returned = call(*args)
        except BaseException as error:  # noqa: BLE001 - what it ended in is the test's
            self.raised = error

    def joined(self) -> Ran:
        self.thread.join(HANG_GUARD)
        assert not self.thread.is_alive()
        return self


# --- the hang bound -----------------------------------------------------------------------------


def frozen_waits():
    """Each double's frozen wait, which nothing the test does ends: (double, the wait)."""
    virtual = WatchedCondition(virtual=True, hang_after=SHORT)
    untimed = WatchedCondition(hang_after=SHORT)
    event = ControlledEvent(hang_after=SHORT)
    parked = FakeTime(hang_after=SHORT)
    parked.parking = True

    def on(condition, seconds):
        def wait():
            with condition:
                return condition.wait_for(lambda: False, seconds)

        return wait

    return [
        (virtual, on(virtual, 5)),
        (untimed, on(untimed, None)),
        (event, lambda: event.wait(5)),
        (parked, lambda: parked.sleep(5)),
    ]


def test_a_frozen_wait_that_nothing_ends_raises_double_hung_in_its_waiter():
    for double, wait in frozen_waits():
        ran = Ran(wait).joined()
        assert isinstance(ran.raised, DoubleHung)
        assert not isinstance(ran.raised, Exception)  # the product's `except Exception` lets it by
        assert len(double.hung) == 1 and "DoubleHung" in double.hung[0]
        double.hung.clear()  # this test's own hang: the registry is held to it in a child pytest


def test_a_polling_loop_that_no_advance_reaches_raises_double_hung_at_the_bound():
    fake = FakeTime(hang_after=SHORT)
    deadline = fake.monotonic() + 10

    def loop():
        while fake.monotonic() < deadline:
            fake.sleep(1)

    ran = Ran(loop).joined()
    assert isinstance(ran.raised, DoubleHung)
    assert len(fake.hung) == 1
    fake.hung.clear()


# --- what is recorded, and when ------------------------------------------------------------------


def test_a_notify_after_the_record_ends_the_wait():
    condition = WatchedCondition(virtual=True)
    state = {"done": False}

    def wait():
        with condition:
            return condition.wait_for(lambda: state["done"], None)

    ran = Ran(wait)
    [record] = condition.wait_waiting(1)
    with condition:
        state["done"] = True
        condition.notify_all()
    assert ran.joined().returned is True
    assert record.ended == "predicate"
    assert condition.waiting == []


def test_a_set_after_the_record_ends_the_events_wait():
    event = ControlledEvent()
    ran = Ran(event.wait, 7)
    [record] = event.wait_waiting(1)
    event.set()
    assert ran.joined().returned is True
    assert record.ended == "set"


# --- FakeTime -----------------------------------------------------------------------------------


def test_fake_time_moves_only_by_advance_and_a_parked_sleep_ends_once_an_advance_covers_it():
    fake = FakeTime(start=50.0, wall_start=1_700_000_000.0)
    assert (fake.monotonic(), fake.time()) == (50.0, 1_700_000_000.0)
    fake.parking = True
    mark = fake.mark()
    ran = Ran(fake.sleep, 5)
    [record] = fake.wait_sleep_begun(after=mark)
    fake.advance(5)
    ran.joined()
    assert (fake.monotonic(), fake.time()) == (55.0, 1_700_000_005.0)
    assert record.ended == "advanced" and fake.sleeps_returned == 1


# --- the registry, in a child pytest ------------------------------------------------------------


def child(pytester, source: str):
    """A child pytest over ``source`` with only the doubles' plugin loaded."""
    pytester.makeini(
        f"[pytest]\npythonpath = {time_doubles.__file__.rsplit('/tests/', 1)[0]}\n"
        "addopts = -p no:cacheprovider -p tests.time_doubles\n"
        "markers =\n    container: derived\n"
    )
    pytester.makepyfile(test_planted=source)
    return pytester.runpytest_subprocess("-v")


SWALLOWED = """
import threading
from tests.time_doubles import ControlledEvent
from tests.hang_guard import HANG_GUARD


def test_planted():
    event = ControlledEvent(hang_after=0.2)

    def product():  # a loop that goes on whatever fails
        try:
            event.wait(5)
        except Exception:
            pass

    thread = threading.Thread(target=product, daemon=True)
    thread.start()
    thread.join(HANG_GUARD)
"""


def test_a_hang_the_product_swallowed_fails_the_test_at_teardown(pytester):
    result = child(pytester, SWALLOWED)
    result.assert_outcomes(passed=1, errors=1)
    assert "DoubleHung" in result.stdout.str()
