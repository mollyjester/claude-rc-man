"""The test doubles for time (tests/time_doubles.py): what each records, what ends each wait,
the hang bound of a frozen wait, and the registry that fails a test on a hang its product
swallowed, the registry's own cases in child pytests."""

from __future__ import annotations

import threading

import pytest

from tests import time_doubles
from tests.hang_guard import HANG_GUARD
from tests.time_doubles import (
    ControlledEvent,
    DoubleHung,
    FakeTime,
    RecordingLock,
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


@pytest.mark.parametrize("which", range(4), ids=["virtual", "untimed", "event", "parked"])
def test_a_frozen_wait_that_nothing_ends_raises_double_hung_in_its_waiter(which):
    double, wait = frozen_waits()[which]
    ran = Ran(wait).joined()
    assert isinstance(ran.raised, DoubleHung)
    assert not isinstance(ran.raised, Exception)  # the product's `except Exception` lets it by
    assert len(double.hung) == 1 and "DoubleHung" in double.hung[0]
    double.hung.clear()  # this test's own hang: the registry is held to it in a child pytest


def test_double_hung_is_no_exception():
    assert issubclass(DoubleHung, BaseException) and not issubclass(DoubleHung, Exception)


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


def test_a_polling_loop_an_advance_reaches_returns():
    """The planted positive of the twin above: the same loop, its deadline passed by the test."""
    fake = FakeTime()
    deadline = fake.monotonic() + 10

    def loop():
        while fake.monotonic() < deadline:
            fake.sleep(1)
        return "done"

    ran = Ran(loop)
    fake.wait_sleep_begun(after=0)
    fake.advance(10)
    assert ran.joined().returned == "done"
    assert fake.hung == []


def test_a_test_side_wait_that_nothing_satisfies_fails_at_its_bound():
    event = ControlledEvent()
    with pytest.raises(AssertionError, match="within"):
        event.wait_waiting(1, within=SHORT)


# --- what is recorded, and when ------------------------------------------------------------------


def test_a_record_is_never_seen_before_its_predicate_is_false():
    """The predicate's first look is held: no wait of its thread is recorded meanwhile."""
    condition = WatchedCondition(virtual=True)
    looking, let_go = threading.Event(), threading.Event()
    looks = []

    def predicate():
        looks.append(threading.current_thread())
        if len(looks) == 1:
            looking.set()
            assert let_go.wait(HANG_GUARD)
        return False

    def wait():
        with condition:
            return condition.wait_for(predicate, 3)

    ran = Ran(wait)
    assert looking.wait(HANG_GUARD)
    assert condition.waits == [] and condition.waiting == []  # nothing yet: still looking
    let_go.set()
    [record] = condition.wait_waiting(1)
    assert record.thread is ran.thread and record.seconds == 3
    condition.expire(ran.thread)
    assert ran.joined().returned is False


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


def test_a_match_picks_one_waiter_among_two():
    event = ControlledEvent()
    seven, nine = Ran(event.wait, 7), Ran(event.wait, 9)
    event.wait_waiting(1, match=7)
    event.wait_waiting(1, match=9)
    assert event.expire(9) == 1
    assert nine.joined().returned is False
    [left] = event.waiting  # the other still waits: only set or expire end it
    assert (left.thread, left.seconds, left.ended) == (seven.thread, 7, None)
    assert seven.thread.is_alive()
    assert event.expire(seven.thread) == 1  # by thread
    assert seven.joined().returned is False


def test_a_match_by_predicate_picks_a_wait_on_the_condition():
    condition = WatchedCondition(virtual=True)

    def wait(seconds):
        with condition:
            return condition.wait_for(lambda: False, seconds)

    timed, untimed = Ran(wait, 30.0), Ran(wait, None)
    condition.wait_waiting(1, match=lambda w: w.seconds is None)
    condition.wait_waiting(1, match=30.0)
    condition.expire(lambda w: w.seconds == 30.0)
    assert timed.joined().returned is False
    assert [w.thread for w in condition.waiting] == [untimed.thread]
    condition.expire(untimed.thread)
    untimed.joined()


def test_an_expire_that_matches_no_waiter_is_refused():
    event = ControlledEvent()
    with pytest.raises(AssertionError, match="no wait"):
        event.expire(5)


def test_an_expire_returns_the_predicate_as_it_then_is():
    condition = WatchedCondition(virtual=True)
    state = {"done": False}

    def wait():
        with condition:
            return condition.wait_for(lambda: state["done"], 30.0)

    ran = Ran(wait)
    condition.wait_waiting(1, match=30.0)
    state["done"] = True  # told to no one: only the expire looks again
    condition.expire(30.0)
    assert ran.joined().returned is True


def test_a_real_mode_timeout_runs_out_by_itself():
    condition = WatchedCondition()
    with condition:
        assert condition.wait_for(lambda: False, 0.01) is False
    [record] = condition.waits
    assert (record.seconds, record.ended) == (0.01, "timeout")


def test_a_condition_takes_the_lock_it_is_given():
    lock = threading.RLock()
    condition = WatchedCondition(lock)
    with condition:
        assert lock._is_owned()  # noqa: SLF001 - the lock is the one given
    assert type(WatchedCondition()._lock) is type(threading.Lock())  # noqa: SLF001


def test_an_events_wait_with_no_seconds_is_the_events_own():
    event = ControlledEvent()
    event.set()
    assert event.wait() is True and event.is_set()
    assert event.waits == []


# --- FakeTime -----------------------------------------------------------------------------------


def test_fake_time_moves_only_by_advance_and_the_rest_is_the_real_time():
    import time

    fake = FakeTime(start=50.0, wall_start=1_700_000_000.0)
    assert (fake.monotonic(), fake.time()) == (50.0, 1_700_000_000.0)
    fake.advance(2.5)
    assert (fake.monotonic(), fake.time()) == (52.5, 1_700_000_002.5)
    assert fake.strftime is time.strftime


def test_numbered_sleeps_are_counted_as_they_begin_and_as_they_return():
    fake = FakeTime()
    fake.parking = True
    first = fake.mark()
    ran = Ran(fake.sleep, 5)
    [record] = fake.wait_sleep_begun(after=first)
    assert record.ordinal > first and (fake.sleeps_begun, fake.sleeps_returned) == (1, 0)
    fake.advance(5)
    ran.joined()
    assert (fake.sleeps_begun, fake.sleeps_returned) == (1, 1)
    fake.parking = False
    mark = fake.mark()
    polling = Ran(lambda: [fake.sleep(0.01) for _ in range(3)])
    fake.wait_sleeps_returned(after=mark, n=3)
    polling.joined()
    assert fake.sleeps_returned == 4


def test_a_wait_for_sleeps_begun_counts_those_after_the_mark():
    fake = FakeTime()
    fake.parking = True
    mark = fake.mark()
    first = Ran(fake.sleep, 5)
    fake.wait_sleep_begun(after=mark)
    second = Ran(fake.sleep, 5)
    begun = fake.wait_sleep_begun(after=mark, n=2)
    assert [w.thread for w in begun] == [first.thread, second.thread]
    fake.advance(5)
    first.joined(), second.joined()


def test_a_parked_sleep_is_not_ended_by_an_advance_short_of_it():
    fake = FakeTime()
    fake.parking = True
    ran = Ran(fake.sleep, 5)
    fake.wait_parked(1)
    fake.advance(4)
    [record] = fake.wait_parked(1)  # it has looked at the time after the advance, and waits on
    assert record.ended is None and ran.thread.is_alive()
    fake.advance(1)
    ran.joined()
    assert record.ended == "advanced"


def test_parking_given_a_test_of_the_seconds_parks_only_the_sleeps_it_picks():
    fake = FakeTime()
    fake.parking = lambda seconds: seconds == 5
    parked = Ran(fake.sleep, 5)
    [record] = fake.wait_parked(1)
    assert record.seconds == 5
    fake.sleep(1)  # not picked: it polls, and returns with no advance
    assert parked.thread.is_alive() and fake.sleeps_returned == 1
    fake.advance(5)
    parked.joined()
    assert record.ended == "advanced"


def test_a_polling_sleep_really_sleeps_a_little_and_moves_no_time():
    fake = FakeTime()
    before = fake.monotonic()
    fake.sleep(1000)  # capped at POLL_CAP of real time
    assert fake.monotonic() == before


# --- RecordingLock ------------------------------------------------------------------------------


def test_a_lock_records_each_try_and_a_block_only_once_a_try_failed():
    lock = RecordingLock(threading.Lock())
    assert lock.acquire(blocking=False)
    asking = Ran(lambda: lock.acquire(timeout=HANG_GUARD))
    [blocked] = lock.wait_blocked(1, match=asking.thread)
    assert blocked.seconds == HANG_GUARD
    assert asking.thread.is_alive()  # observed blocked: the lock is still held
    lock.release()
    assert asking.joined().returned is True
    lock.release()
    tries = [(c.blocking, c.timeout, c.thread) for c in lock.calls]
    assert tries == [
        (False, -1, threading.current_thread()),
        (True, HANG_GUARD, asking.thread),
    ]


def test_a_lock_that_is_free_records_no_block():
    lock = RecordingLock(threading.Lock())
    with lock:
        pass
    assert len(lock.calls) == 1 and lock.waits == []


def test_a_lock_wrapping_an_rlock_is_reentrant():
    lock = RecordingLock(threading.RLock())
    with lock, lock:
        assert lock.waits == []
    assert lock.acquire(blocking=False)  # free again
    lock.release()


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
    {end}
    thread.join(HANG_GUARD)
"""


def test_a_hang_the_product_swallowed_fails_the_test_at_teardown(pytester):
    result = child(pytester, SWALLOWED.format(end="pass"))
    result.assert_outcomes(passed=1, errors=1)
    assert "DoubleHung" in result.stdout.str()


def test_a_wait_the_test_ends_fails_nothing(pytester):
    """The planted positive: the same wait, ended by the test."""
    end = "event.wait_waiting(1)\n    event.set()"
    child(pytester, SWALLOWED.format(end=end)).assert_outcomes(passed=1)


def test_a_double_made_outside_a_test_is_refused(pytester):
    planted = "from tests.time_doubles import ControlledEvent\nMADE = ControlledEvent()\n"
    result = child(pytester, planted + "\ndef test_planted():\n    pass\n")
    assert "made in a test" in result.stdout.str()
