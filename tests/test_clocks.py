"""tests/clocks.py: PhasedClock, the clock whose time the test moves, and the registry that
fails a test on its misuse. Every thread a case starts is joined within JOIN; a case that
plants a misuse runs it in a child pytest (pytester), whose teardown is what fails."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import threading
import weakref

import pytest

from tests import clocks, hang_guard
from tests.clocks import POLL_CAP, ClockHung, ClockRefused, PhasedClock
from tests.hang_guard import HANG_GUARD

JOIN = HANG_GUARD
START = 1000.0


class Worker:
    """A thread that runs ``target``, keeping what it returned or raised."""

    def __init__(self, target, *args):
        self.result: list = []
        self.error: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, args=(target, *args), daemon=True)

    def _run(self, target, *args):
        try:
            self.result.append(target(*args))
        except BaseException as error:  # noqa: BLE001 - kept for the test to look at
            self.error.append(error)

    def start(self) -> Worker:
        self.thread.start()
        return self

    def value(self):
        self.thread.join(JOIN)
        assert not self.thread.is_alive(), "it did not end"
        assert not self.error, self.error
        return self.result[0]


def lent(clock: PhasedClock, target, *args) -> Worker:
    """A worker the clock is lent to."""
    made = Worker(target, *args)
    clock.lend(made.thread)
    return made.start()


def stranger(target, *args) -> Worker:
    """A worker the clock is not lent to."""
    return Worker(target, *args).start()


# --- the frozen phase -------------------------------------------------------------------------


def test_time_moves_only_by_advance():
    clock = PhasedClock(start=100.0, wall_start=5000.0)
    assert (clock.now(), clock.wall()) == (100.0, 5000.0)
    clock.advance(2.5)
    assert (clock.now(), clock.wall()) == (102.5, 5002.5)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_a_sleep_parks_until_an_advance_reaches_it():
    clock = PhasedClock()
    sleeper = lent(clock, clock.sleep, 10)
    clock.wait_parked(1)
    clock.advance(5)
    clock.wait_parked(1)  # it looked at the advance and sleeps on
    assert sleeper.thread.is_alive()
    clock.advance(5)
    assert sleeper.value() is None
    assert clock.slept == [10] and ("sleep", 10, sleeper.thread) in clock.waits
    assert clock.now() == START + 10


def test_an_event_wait_ends_on_its_event_or_on_an_advance_past_its_deadline():
    clock = PhasedClock()
    event = threading.Event()
    waiting = lent(clock, clock.wait, event, 10)
    clock.wait_parked(1)
    event.set()
    assert waiting.value() is True
    assert clock.now() == START  # no time passed
    expiring = lent(clock, clock.wait, threading.Event(), 10)
    clock.wait_parked(1)
    clock.advance(10)
    assert expiring.value() is False


def test_a_condition_wait_looks_at_its_predicate_first():
    clock = PhasedClock()
    condition = threading.Condition()
    with condition:
        assert clock.wait_for(condition, lambda: "yes", 0) is True  # though its time is up
        assert clock.wait_for(condition, lambda: "", 0) is False
    held = []

    def waiting(seconds):
        with condition:
            return clock.wait_for(condition, lambda: held, seconds)

    told = lent(clock, waiting, 10)
    clock.wait_parked(1)
    with condition:
        held.append(1)
        condition.notify_all()
    assert told.value() is True
    held.clear()
    expiring = lent(clock, waiting, 10)
    clock.wait_parked(1)
    clock.advance(10)  # no condition of the product's is told: the wait sees it in its slice
    assert expiring.value() is False


def test_a_lock_wait_ends_when_it_is_released_or_on_an_advance_past_its_deadline():
    clock = PhasedClock()
    lock = threading.Lock()
    lock.acquire()
    taking = lent(clock, clock.acquire, lock, 10)
    clock.wait_parked(1)
    lock.release()
    assert taking.value() is True and lock.locked()  # the worker holds it now
    expiring = lent(clock, clock.acquire, lock, 10)
    clock.wait_parked(1)
    clock.advance(10)
    assert expiring.value() is False


def test_a_semaphore_acquired_without_seconds_waits_however_far_time_moves():
    clock = PhasedClock()
    semaphore = threading.Semaphore(0)
    taking = lent(clock, clock.acquire, semaphore, None)
    clock.wait_parked(1)
    clock.advance(10**6)
    clock.wait_parked(1)
    assert taking.thread.is_alive()
    semaphore.release()
    assert taking.value() is True


def test_the_asyncio_waits_are_frozen_too():
    clock = PhasedClock()

    async def scenario():
        event, go, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        held = asyncio.create_task(other.wait())
        on_event = asyncio.create_task(clock.await_event(event, 10))
        sleeping = asyncio.create_task(clock.asleep(5))
        bounded = asyncio.create_task(clock.within(go.wait(), 20))
        tasks = asyncio.create_task(clock.await_tasks({held}, 30))
        await clock.until_parked(4)
        clock.advance(5)
        await clock.until_parked(3)
        assert sleeping.done()
        event.set()
        await clock.until_parked(2)
        assert await on_event is True
        clock.advance(15)
        await clock.until_parked(1)
        with pytest.raises(TimeoutError):
            await bounded
        assert not tasks.done()
        clock.advance(10)
        done, pending = await tasks
        assert (done, pending) == (set(), {held}) and not held.cancelled()
        other.set()
        await held

    asyncio.run(scenario())
    assert clock.now() == START + 30


def test_a_poll_really_sleeps_no_more_than_its_cap_and_moves_no_time(monkeypatch):
    clock = PhasedClock()
    slept = []

    async def asleep(seconds):
        slept.append(("async", seconds))

    monkeypatch.setattr(clocks, "_sleep", lambda seconds: slept.append(("thread", seconds)))
    monkeypatch.setattr(clocks, "_asleep", asleep)
    clock.poll(30)
    asyncio.run(clock.apoll(30))
    clock.poll(POLL_CAP / 2)
    assert slept == [("thread", POLL_CAP), ("async", POLL_CAP), ("thread", POLL_CAP / 2)]
    assert clock.now() == START and clock.waiters() == []
    assert [w[:2] for w in clock.waits] == [("poll", 30), ("apoll", 30), ("poll", POLL_CAP / 2)]


class Until(threading.Event):
    """An event that records each wait on it, and waits on it for none of the time."""

    def __init__(self):
        super().__init__()
        self.waited: list[float | None] = []

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return super().wait(0)


@pytest.mark.parametrize("given", [False, True])
def test_a_poll_until_an_event_waits_on_it_no_more_than_the_cap_and_says_whether_it_is_set(
    monkeypatch, given
):
    clock = PhasedClock()
    slept = []
    monkeypatch.setattr(clocks, "_sleep", slept.append)
    until = Until()
    if given:
        until.set()
    assert clock.poll(30, until=until) is given
    assert clock.poll(POLL_CAP / 2, until=until) is given
    assert until.waited == [POLL_CAP, POLL_CAP / 2] and slept == []  # on the event, not slept
    assert clock.poll(30) is False  # the twin: without one, it sleeps
    assert slept == [POLL_CAP]
    assert clock.now() == START and clock.waiters() == []
    assert [w[:2] for w in clock.waits] == [("poll", 30), ("poll", POLL_CAP / 2), ("poll", 30)]


def test_a_poll_until_an_event_ends_when_it_is_set():
    clock = PhasedClock(hang_after=600)
    until = threading.Event()
    gate = threading.Event()

    def polling():
        while not clock.poll(600, until=until):
            gate.set()
        return clock.now()

    worker = lent(clock, polling)
    assert gate.wait(JOIN)
    until.set()
    assert worker.value() == START  # it moved no time


def test_wait_polled_counts_a_poll_once_it_has_slept(monkeypatch):
    clock = PhasedClock()
    held = threading.Event()
    monkeypatch.setattr(clocks, "_sleep", lambda seconds: held.wait(JOIN))
    worker = lent(clock, clock.poll, 30)
    try:
        with pytest.raises(AssertionError, match="not 1 polled"):  # the twin: still asleep
            clock.wait_polled(1, within=0.05)
    finally:
        held.set()
    clock.wait_polled(1)
    assert worker.value() is False
    with pytest.raises(AssertionError, match="not 2 polled"):
        clock.wait_polled(2, within=0.05)


def test_wait_polled_counts_a_poll_until_an_event_too():
    clock = PhasedClock()
    until = Until()
    assert clock.poll(30, until=until) is False
    clock.wait_polled(1, within=0.05)


def test_wait_polled_counts_an_apoll_once_it_has_slept(monkeypatch):
    clock = PhasedClock()

    async def scenario():
        held = asyncio.Event()

        async def asleep(seconds):
            await held.wait()

        monkeypatch.setattr(clocks, "_asleep", asleep)
        polling = asyncio.create_task(clock.apoll(30))
        await asyncio.sleep(0)
        assert clock.polls == 0  # the twin: still asleep
        held.set()
        await polling
        assert clock.polls == 1

    asyncio.run(scenario())
    clock.wait_polled(1, within=0.05)


def test_wait_polled_is_told_not_polled():
    """``wait_polled`` waits on the clock's condition with the whole of its bound."""
    clock = PhasedClock()
    recorded = clock._changed = Recorded(clock._lock)
    lent(clock, clock.poll, 0).value()
    clock.wait_polled(1)
    mine = [timeout for thread, timeout in recorded.waited if thread is threading.current_thread()]
    assert all(t is not None and t > clock.hang_after / 2 for t in mine), mine
    assert recorded.told >= 1  # the poll told it


def test_a_waiter_has_the_seconds_it_was_given():
    clock = PhasedClock()
    event = threading.Event()
    waiting = lent(clock, clock.wait, event, 7)
    clock.wait_parked(1)
    assert [(w.method, w.seconds, w.deadline) for w in clock.waiters()] == [("wait", 7, START + 7)]
    event.set()
    assert waiting.value() is True


def test_wait_parked_where_counts_only_the_waiters_that_match():
    clock = PhasedClock()
    other = threading.Event()
    condition = threading.Condition()
    gate = threading.Event()
    looks = []

    def predicate():
        looks.append(1)
        if len(looks) == 2:  # its look after the poke is held
            gate.wait(JOIN)
        return False

    def matching():
        with condition:
            return clock.wait_for(condition, predicate, 5)

    def ours(w):
        return w.seconds == 5

    unmatched = lent(clock, clock.wait, other, 100)
    clock.wait_parked(1)
    with pytest.raises(AssertionError, match="not 1 parked"):  # twin: one parked, not matching
        clock.wait_parked_where(ours, within=0.05)
    matched = lent(clock, matching)
    clock.wait_parked_where(ours)
    clock.wait_parked(2)
    with pytest.raises(AssertionError, match="not 2 parked"):
        clock.wait_parked_where(ours, n=2, within=0.05)
    try:
        clock.poke()
        clock.wait_parked_where(lambda w: w.seconds == 100)  # the other looked again
        with pytest.raises(AssertionError, match="not 1 parked"):  # twin: ours did not yet
            clock.wait_parked_where(ours, within=0.05)
    finally:
        gate.set()
    clock.wait_parked_where(ours)
    clock.advance(5)
    assert matched.value() is False
    other.set()
    assert unmatched.value() is True


# --- the free phase ---------------------------------------------------------------------------


def test_in_the_free_phase_an_unsatisfied_wait_moves_time_by_its_seconds():
    clock = PhasedClock()
    condition, lock, set_ = threading.Condition(), threading.Lock(), threading.Event()
    set_.set()
    with clock.free():
        clock.sleep(100)
        assert clock.now() == START + 100
        assert clock.wait(threading.Event(), 5) is False
        assert clock.wait(set_, 5) is True
        with condition:
            assert clock.wait_for(condition, lambda: False, 7) is False
            assert clock.wait_for(condition, lambda: True, 7) is True
        assert clock.acquire(lock, 3) is True
        assert clock.acquire(lock, 3) is False
        assert clock.now() == START + 115

        async def scenario():
            await clock.asleep(4)
            assert await clock.await_event(asyncio.Event(), 6) is False

        asyncio.run(scenario())
        assert clock.now() == START + 125
    assert clock.slept == [100, 4]


def test_free_await_tasks_gives_one_turn_first_and_returns_its_pending_tasks_uncancelled():
    clock = PhasedClock()

    async def answer():
        return 7

    async def scenario():
        go = asyncio.Event()
        held = asyncio.create_task(go.wait())
        quick = asyncio.create_task(answer())  # done in the one turn
        with clock.free():
            done, pending = await clock.await_tasks({quick, held}, 30, asyncio.ALL_COMPLETED)
        assert (done, pending) == ({quick}, {held})
        assert not held.cancelled() and not held.done()
        go.set()
        await held

    asyncio.run(scenario())
    assert clock.now() == START + 30


@pytest.mark.parametrize("free", [False, True])
def test_await_tasks_refuses_no_tasks_at_all(free):
    """As asyncio.wait does, in either phase."""
    clock = PhasedClock()

    async def scenario():
        with pytest.raises(ValueError):
            await clock.await_tasks(set(), 1.0)

    if free:
        with clock.free():
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert clock.now() == START


def test_free_within_cancels_and_awaits_its_awaitable_then_times_out():
    clock = PhasedClock()
    cleaned = []

    async def slow():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # its cancellation takes a turn of its own
            cleaned.append(True)
            raise

    async def scenario():
        with clock.free(), pytest.raises(TimeoutError):
            await clock.within(slow(), 8)
        assert cleaned == [True]

    asyncio.run(scenario())
    assert clock.now() == START + 8


def test_free_within_gives_one_turn_and_no_more():
    clock = PhasedClock()

    async def answer():
        return 7

    async def two_turns():
        await asyncio.sleep(0)
        return 8

    async def scenario():
        with clock.free():
            assert await clock.within(answer(), 8) == 7
            assert clock.now() == START  # it ended in its turn: no time passed
            with pytest.raises(TimeoutError):
                await clock.within(two_turns(), 8)
        assert clock.now() == START + 8

    asyncio.run(scenario())


# --- what is refused --------------------------------------------------------------------------


def test_an_advance_past_the_deadline_of_a_thread_that_is_not_the_clocks_is_refused():
    clock = PhasedClock()
    event = threading.Event()
    waiting = stranger(clock.wait, event, 10)
    clock.wait_parked(1)
    with pytest.raises(ClockRefused, match="not the clock's"):
        clock.advance(10)
    assert clock.now() == START  # nothing moved
    clock.advance(5)  # short of it
    event.set()
    assert waiting.value() is True


def test_an_advance_past_the_deadline_of_a_lent_thread_goes_through():
    clock = PhasedClock()
    waiting = lent(clock, clock.wait, threading.Event(), 10)
    clock.wait_parked(1)
    clock.advance(10)
    assert waiting.value() is False


@pytest.mark.parametrize("start", [stranger, "lent"])
def test_free_is_refused_while_another_thread_waits_lent_or_not(start):
    clock = PhasedClock()
    event = threading.Event()
    if start == "lent":
        waiting = lent(clock, clock.wait, event, 10)
    else:
        waiting = start(clock.wait, event, 10)
    clock.wait_parked(1)
    with pytest.raises(ClockRefused, match="another thread waits"), clock.free():
        pass
    event.set()
    assert waiting.value() is True
    with clock.free():  # the control: with no other waiter it is not refused
        pass


@pytest.mark.parametrize("start", [stranger, "lent"])
def test_free_is_not_refused_for_another_threads_wait_without_seconds(start):
    """The twin of the refusal above: a wait with no deadline cannot be passed by a move. It
    stays frozen through the free phase and still ends on its primitive."""
    clock = PhasedClock()
    event = threading.Event()
    if start == "lent":
        waiting = lent(clock, clock.wait, event, None)
    else:
        waiting = start(clock.wait, event, None)
    clock.wait_parked(1)
    with clock.free():
        clock.sleep(10**6)  # the free thread moves time far
        clock.wait_parked(1)  # it looked at the move and waits on
        assert waiting.thread.is_alive()
    event.set()
    assert waiting.value() is True
    assert clock.now() == START + 10**6


def test_free_is_refused_to_a_thread_that_is_not_the_clocks():
    clock = PhasedClock()

    def enter():
        with clock.free():
            pass

    entering = stranger(enter)
    entering.thread.join(JOIN)
    assert isinstance(entering.error[0], ClockRefused)
    entering = lent(clock, enter)
    assert entering.value() is None  # the twin: lent, it may


def test_a_lent_thread_stays_frozen_inside_the_free_phase_and_is_not_passed():
    """A lent thread's wait inside the free phase moves nothing, and the free thread may not
    move time past its deadline: the lent thread would run on time it did not wait out."""
    clock = PhasedClock()
    with clock.free():
        sleeper = lent(clock, clock.sleep, 10)
        clock.wait_parked(1)
        assert clock.now() == START  # its sleep moved nothing
        with pytest.raises(ClockRefused, match="free phase"):
            clock.sleep(10)  # the free thread's own sleep, past the lent one's deadline
        with pytest.raises(ClockRefused, match="free phase"):
            clock.advance(10)
        assert clock.now() == START
        clock.sleep(5)  # short of it
    clock.advance(5)  # the twin: outside the free phase, an advance past it goes through
    assert sleeper.value() is None
    assert clock.now() == START + 10
    # the refusal inside a wait is a violation too, which a product could not swallow; the
    # test's own advance raises in the test, and is not
    assert len(clock.violations) == 1 and "ClockRefused" in clock.violations[0]
    assert "sleep" in clock.violations[0]
    clock.violations.clear()  # seen: the teardown would fail the test on it


def test_an_advance_does_not_wait_for_a_product_condition_another_thread_holds():
    clock = PhasedClock()
    condition = threading.Condition()
    holding, release = threading.Event(), threading.Event()

    def waiting():
        with condition:
            return clock.wait_for(condition, lambda: False, 10)

    def hold():
        with condition:
            holding.set()
            return release.wait(JOIN)

    waiter = lent(clock, waiting)
    clock.wait_parked(1)
    holder = stranger(hold)
    assert holding.wait(JOIN)
    try:
        mover = stranger(clock.advance, 10)
        assert mover.value() is None  # while the condition is held
        assert waiter.thread.is_alive()  # it cannot take the condition back yet
    finally:
        release.set()
    assert waiter.value() is False
    assert holder.value() is True


# --- the hang bound ---------------------------------------------------------------------------


def test_a_wait_nothing_ends_is_hung_after_the_bound_read_when_it_waits(monkeypatch):
    clock = PhasedClock()
    assert clock.hang_after == hang_guard.HANG_GUARD  # this tier's
    monkeypatch.setattr(hang_guard, "HANG_GUARD", 0.2)
    assert clock.hang_after == 0.2
    with pytest.raises(ClockHung, match="sleep"):
        clock.sleep(10)
    with pytest.raises(ClockHung, match="await_event"):
        asyncio.run(clock.await_event(asyncio.Event(), None))
    assert [v.split(":")[0] for v in clock.violations] == ["ClockHung", "ClockHung"]
    clock.violations.clear()  # seen: the teardown would fail the test on them
    assert clock.waiters() == []


def test_a_bound_given_to_the_clock_is_its_own():
    clock = PhasedClock(hang_after=0.2)
    with pytest.raises(ClockHung, match="wait"):
        clock.wait(threading.Event(), None)
    clock.violations.clear()


class RealTime:
    """Real time as tests/clocks.py reads it, which the test moves."""

    def __init__(self):
        self.at = 0.0

    def monotonic(self) -> float:
        return self.at


# The real time each poll takes in ``real``, exact in binary.
TAKES = 0.25


@pytest.fixture
def real(monkeypatch) -> RealTime:
    """The clock's real time the test's, each poll taking TAKES of it."""
    made = RealTime()
    monkeypatch.setattr(clocks, "time", made)

    def sleep(seconds):
        made.at += TAKES

    async def asleep(seconds):
        made.at += TAKES
        await asyncio.sleep(0)

    monkeypatch.setattr(clocks, "_sleep", sleep)
    monkeypatch.setattr(clocks, "_asleep", asleep)
    return made


def polls(clock: PhasedClock, n: int) -> None:
    """``n`` polls one after another, from 0 to n * TAKES of real time after the first began."""
    for _ in range(n):
        clock.poll(1)


@pytest.mark.parametrize("how", ["poll", "apoll"])
def test_polls_that_go_on_past_the_bound_with_no_advance_are_hung(real, how):
    clock = PhasedClock(hang_after=1)

    async def apolling():
        for _ in range(4):  # the twin: the fourth begins within the bound
            await clock.apoll(1)
        with pytest.raises(ClockHung, match="apoll"):  # the fifth, at the bound
            await clock.apoll(1)
        with pytest.raises(ClockHung):  # it stays hung
            await clock.apoll(1)

    if how == "apoll":
        asyncio.run(apolling())
    else:
        polls(clock, 4)  # the twin: the fourth begins within the bound
        with pytest.raises(ClockHung, match="poll"):  # the fifth, at the bound
            clock.poll(1)
        with pytest.raises(ClockHung):  # it stays hung
            clock.poll(1)
    assert [v.split(":")[0] for v in clock.violations] == ["ClockHung"]  # recorded once
    clock.violations.clear()  # seen: the teardown would fail the test on it


@pytest.mark.parametrize("moved", ["advance", "poke"])
def test_a_new_generation_between_polls_starts_their_bound_again(real, moved):
    clock = PhasedClock(hang_after=1)
    polls(clock, 3)
    clock.advance(1) if moved == "advance" else clock.poke()
    polls(clock, 4)
    assert clock.violations == []


@pytest.mark.parametrize("idle,hung", [(clocks.POLL_GAP, True), (clocks.POLL_GAP + TAKES, False)])
def test_a_poll_after_a_gap_starts_the_bound_again(real, idle, hung):
    """A thread that polled, then did something else for longer than POLL_GAP, as a worker of a
    pool between two calls does, polls anew; the twin, idle exactly POLL_GAP, polls on."""
    clock = PhasedClock(hang_after=1)
    polls(clock, 3)
    real.at += idle
    if hung:
        with pytest.raises(ClockHung):
            clock.poll(1)
        clock.violations.clear()
    else:
        polls(clock, 4)
        assert clock.violations == []


def test_each_thread_and_task_polls_within_a_bound_of_its_own(real):
    clock = PhasedClock(hang_after=1)
    polls(clock, 3)
    stranger(clock.poll, 1).value()  # another thread's first poll, at 0.75
    asyncio.run(clock.apoll(1))  # a task's first, at 1.0
    assert clock.violations == []
    with pytest.raises(ClockHung):  # the first thread's, past its bound
        clock.poll(1)
    clock.violations.clear()


@pytest.mark.parametrize("hung", [False, True])
def test_the_clock_keeps_no_task_alive_for_its_polls(real, hung):
    """A task that polled, or whose poll was hung, is the clock's no longer once it is gone.
    (A thread is kept in ``waits``, which the tests read.)"""
    clock = PhasedClock(hang_after=TAKES if hung else 1)
    task = weakref.ref(asyncio.run(task_that_apolls(clock, 2)))
    gc.collect()
    assert task() is None
    assert len(clock.violations) == hung
    clock.violations.clear()


async def task_that_apolls(clock: PhasedClock, n: int) -> asyncio.Task:
    async def apolls():
        with contextlib.suppress(ClockHung):
            for _ in range(n):
                await clock.apoll(1)

    polling = asyncio.create_task(apolls())
    await polling
    return polling


def test_in_the_free_phase_a_wait_that_moves_the_time_starts_the_bound_again(real):
    clock = PhasedClock(hang_after=1)
    with clock.free():
        polls(clock, 3)
        clock.sleep(1)  # it moves the time
        polls(clock, 4)
    assert clock.violations == []


def test_a_closed_clock_polls_on_without_a_bound(real):
    clock = PhasedClock(hang_after=1)
    clock.close()
    polls(clock, 8)
    assert clock.violations == []


# --- asyncio's loops ------------------------------------------------------------------------


def test_the_tick_of_a_loop_that_is_closed_is_ignored():
    clock = PhasedClock()
    loop = asyncio.new_event_loop()
    try:
        waiting = loop.create_task(clock.await_event(asyncio.Event(), 10))
        loop.run_until_complete(clock.until_parked(1))
    finally:
        loop.close()
    with pytest.raises(RuntimeError):  # the control: the loop takes nothing now
        loop.call_soon_threadsafe(lambda: None)
    clock.advance(1)  # its waiter's tick is set through it
    assert clock.now() == START + 1
    assert not waiting.done()


class Told(asyncio.SelectorEventLoop):
    """A loop that counts what it is handed from outside a step of its own."""

    def __init__(self):
        super().__init__()
        self.handed = 0

    def call_soon_threadsafe(self, *args, **kwargs):
        self.handed += 1
        return super().call_soon_threadsafe(*args, **kwargs)


def test_a_loop_is_told_of_a_new_generation_only_while_it_has_a_waiter():
    """Its tick is forgotten when its last waiter returns: a later advance hands it nothing."""
    clock = PhasedClock()
    loop = Told()
    try:

        async def scenario():
            event = asyncio.Event()
            waiting = asyncio.create_task(clock.await_event(event, 10))
            await clock.until_parked(1)
            before = loop.handed
            clock.advance(1)  # the control: with a waiter, its tick is set from outside
            assert loop.handed == before + 1
            await clock.until_parked(1)
            event.set()
            assert await waiting is True

        loop.run_until_complete(scenario())
        before = loop.handed
        clock.advance(1)
        clock.poke()
        assert loop.handed == before
    finally:
        loop.close()


# --- the gate: parked, by generation ----------------------------------------------------------


def own_generation(clock: PhasedClock) -> int | None:
    """The generation the waiter asking last looked at."""
    me, task = threading.current_thread(), None
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no loop in this thread
        pass
    (found,) = [w for w in clock.waiters() if w.thread is me and w.task is task]
    return found.generation


def test_a_thread_records_the_generation_only_after_it_has_looked():
    """A waiter that finds what it waits for when it looks at a new generation returns, and was
    never parked at it; one that has returned and waits for nothing is not parked."""
    clock = PhasedClock()
    first = clock.generation
    seen: list[tuple[int | None, int, bool]] = []

    class Watched(threading.Event):
        """Set by the clock's next generation."""

        def is_set(self):
            if clock.generation == first:
                return False
            seen.append((own_generation(clock), clock.generation, clock.parked(1)))
            return True

    returned, go = threading.Event(), threading.Event()

    def waiting():
        result = clock.wait(Watched(), 100)
        returned.set()
        go.wait(JOIN)
        return result, clock.wait(threading.Event(), 100)

    waiter = lent(clock, waiting)
    clock.wait_parked(1)
    clock.poke()
    assert returned.wait(JOIN)
    assert seen == [(first, first + 1, False)]  # looked at, not yet recorded: not parked
    assert not clock.parked(1) and clock.waiters() == []  # woken, and not waiting again
    go.set()
    clock.wait_parked(1)  # waiting again
    clock.advance(100)
    assert waiter.value() == (True, False)


def test_a_condition_wait_records_the_generation_only_after_its_predicate():
    clock = PhasedClock()
    condition = threading.Condition()
    first = clock.generation
    seen: list[tuple[int | None, int]] = []

    def predicate():
        if clock.generation == first:
            return False
        seen.append((own_generation(clock), clock.generation))
        return True

    def waiting():
        with condition:
            return clock.wait_for(condition, predicate, 100)

    waiter = lent(clock, waiting)
    clock.wait_parked(1)
    clock.poke()
    assert waiter.value() is True
    assert seen == [(first, first + 1)]


def test_tasks_record_the_generation_only_after_they_have_looked():
    clock = PhasedClock()
    first = clock.generation
    seen: list[tuple[int | None, int]] = []

    class Watched(asyncio.Event):
        def is_set(self):
            if clock.generation == first or asyncio.current_task() is not watched:
                return super().is_set()
            seen.append((own_generation(clock), clock.generation))
            return True

    watched = None

    async def scenario():
        nonlocal watched
        other = asyncio.Event()
        watched = asyncio.create_task(clock.await_event(Watched(), 100))
        waiting = asyncio.create_task(clock.await_event(other, 100))
        await clock.until_parked(2)
        assert not clock.parked(1) and not clock.parked(3)  # by count too
        clock.poke()
        await clock.until_parked(1)  # the watched one returned, the other waits on
        assert watched.done() and seen == [(first, first + 1)]
        assert [w.task for w in clock.waiters()] == [waiting]
        clock.advance(1)
        await clock.until_parked(1)
        assert clock.waiters()[0].generation == clock.generation
        other.set()
        assert await waiting is True

    asyncio.run(scenario())


def test_parked_counts_every_waiter_of_every_thread_by_generation():
    clock = PhasedClock()
    events = [threading.Event(), threading.Event()]
    waiting = [lent(clock, clock.wait, e, 100) for e in events]
    clock.wait_parked(2)
    assert clock.parked(2) and not clock.parked(1)
    for move in (clock.poke, lambda: clock.advance(1)):
        move()
        clock.wait_parked(2)
        assert {w.generation for w in clock.waiters()} == {clock.generation}
    events[0].set()
    clock.wait_parked(1)
    assert waiting[0].value() is True
    events[1].set()
    assert waiting[1].value() is True
    assert clock.parked(0)


class Recorded(threading.Condition):
    """The clock's condition, recording each wait on it by the thread that waits, and each time
    it is told."""

    def __init__(self, lock):
        super().__init__(lock)
        self.waited: list[tuple[threading.Thread, float | None]] = []
        self.told = 0

    def wait(self, timeout=None):
        self.waited.append((threading.current_thread(), timeout))
        return super().wait(timeout)

    def notify_all(self):
        self.told += 1
        super().notify_all()


def told_not_polled(clock: PhasedClock) -> None:
    """``wait_parked`` waits on the clock's condition with the whole of its bound, and wakes no
    more often than the clock is told of a change: AssertionError otherwise."""
    recorded = clock._changed = Recorded(clock._lock)
    event = threading.Event()
    waiting = lent(clock, clock.wait, event, 100)
    clock.wait_parked(1)
    clock.poke()
    clock.wait_parked(1)
    event.set()
    assert waiting.value() is True
    mine = [timeout for thread, timeout in recorded.waited if thread is threading.current_thread()]
    assert mine, "wait_parked did not wait on the clock's condition"  # non-vacuity
    assert all(t is not None and t > clock.hang_after / 2 for t in mine), mine
    assert len(mine) <= recorded.told + 1, (len(mine), recorded.told)


def told_not_polled_in_a_loop(clock: PhasedClock, monkeypatch) -> None:
    """``until_parked`` waits to be told (``clocks._within``) with the whole of its bound, and
    wakes no more often than the clock told its watchers: AssertionError otherwise."""
    waited: list[float | None] = []
    told = []
    real_within, real_told = clocks._within, clock._told

    async def within(awaitable, timeout):
        waited.append(timeout)
        return await real_within(awaitable, timeout)

    def telling():
        told.append(1)
        real_told()

    monkeypatch.setattr(clocks, "_within", within)
    clock._told = telling

    async def scenario():
        other = asyncio.Event()
        task = asyncio.create_task(clock.await_event(other, 100))
        await clock.until_parked(1)
        clock.poke()
        await clock.until_parked(1)
        other.set()
        assert await task is True

    asyncio.run(scenario())
    assert waited, "until_parked did not wait to be told"  # non-vacuity
    assert all(t is not None and t > clock.hang_after / 2 for t in waited), waited
    assert len(waited) <= len(told) + 1, (len(waited), len(told))


def test_waiting_for_waiters_to_be_parked_is_told_not_polled(monkeypatch):
    told_not_polled(PhasedClock())
    told_not_polled_in_a_loop(PhasedClock(), monkeypatch)


def test_the_check_finds_an_until_parked_that_polls(monkeypatch):
    """The twin: a planted ``until_parked`` that looks every hundredth of a second."""

    async def polling(self, n, within=None):
        told = asyncio.Event()
        while not self.parked(n):
            with contextlib.suppress(TimeoutError):
                await clocks._within(told.wait(), 0.01)

    monkeypatch.setattr(PhasedClock, "until_parked", polling)
    with pytest.raises(AssertionError):
        told_not_polled_in_a_loop(PhasedClock(), monkeypatch)


def test_the_check_finds_a_wait_parked_that_polls(monkeypatch):
    """The twin: a planted ``wait_parked`` that looks every hundredth of a second."""

    def polling(self, n, within=None):
        with self._changed:
            while not self._parked(n):
                self._changed.wait(0.01)

    monkeypatch.setattr(PhasedClock, "wait_parked", polling)
    with pytest.raises(AssertionError):
        told_not_polled(PhasedClock())


def test_waiting_for_waiters_to_be_parked_gives_up_after_its_bound():
    clock = PhasedClock()
    with pytest.raises(AssertionError, match="not 1 parked"):
        clock.wait_parked(1, within=0.05)

    async def scenario():
        with pytest.raises(AssertionError, match="not 1 parked"):
            await clock.until_parked(1, within=0.05)

    asyncio.run(scenario())


# --- the registry, in a child pytest ----------------------------------------------------------


def child(pytester, source: str):
    """A child pytest over ``source`` with only the clocks' plugin loaded."""
    pytester.makeini(
        f"[pytest]\npythonpath = {clocks.__file__.rsplit('/tests/', 1)[0]}\n"
        "addopts = -p no:cacheprovider -p tests.clocks\n"
        "markers =\n    container: derived\n"
    )
    pytester.makepyfile(test_planted=source)
    return pytester.runpytest_subprocess("-v")


MISUSE = """
import threading
from tests.clocks import PhasedClock
from tests.hang_guard import HANG_GUARD


def test_planted():
    clock = PhasedClock()
    done = threading.Event()
    done.set()
    with clock.{phase}:
        reader = threading.Thread(target={what})
        reader.start()
        reader.join(HANG_GUARD)
"""


@pytest.mark.parametrize(
    "what, said", [("clock.now", "read its now()"), ("lambda: clock.wait(done, 5)", "started wait")]
)
def test_a_stranger_using_the_clock_in_the_free_phase_fails_the_test_at_teardown(
    pytester, what, said
):
    result = child(pytester, MISUSE.format(phase="free()", what=what))
    result.assert_outcomes(passed=1, errors=1)
    assert "the test's clocks were misused" in result.stdout.str() and said in result.stdout.str()


@pytest.mark.parametrize("what", ["clock.now", "lambda: clock.wait(done, 5)"])
def test_a_stranger_using_the_clock_in_the_frozen_phase_is_no_misuse(pytester, what):
    planted = MISUSE.format(phase="free()", what=what).replace("with clock.free():", "if True:")
    assert "free()" not in planted  # the plant took effect
    child(pytester, planted).assert_outcomes(passed=1)


SWALLOWED = """
import threading
from tests.clocks import PhasedClock
from tests.hang_guard import HANG_GUARD


def test_planted():
    clock = PhasedClock()
    with clock.free():
        sleeper = threading.Thread(target=clock.sleep, args=(10,), daemon=True)
        clock.lend(sleeper)
        sleeper.start()
        clock.wait_parked(1)
        try:  # a product's loop that goes on whatever fails
            clock.{move}
        except Exception:
            pass
    clock.advance(10)
    sleeper.join(HANG_GUARD)
"""


def test_a_refusal_a_product_swallows_still_fails_the_test_at_teardown(pytester):
    result = child(pytester, SWALLOWED.format(move="sleep(10)"))
    result.assert_outcomes(passed=1, errors=1)
    assert "ClockRefused" in result.stdout.str() and "free phase" in result.stdout.str()


def test_a_refusal_of_the_tests_own_advance_is_no_violation(pytester):
    """The twin: an advance's refusal is raised in the test, which it fails itself."""
    child(pytester, SWALLOWED.format(move="advance(10)")).assert_outcomes(passed=1)


OUTSIDE = """
import pytest
from tests.clocks import PhasedClock


@pytest.fixture(scope="{scope}")
def clock():
    return PhasedClock()


def test_planted(clock):
    pass
"""


def test_a_clock_made_in_a_fixture_of_wider_scope_is_refused(pytester):
    result = child(pytester, OUTSIDE.format(scope="session"))
    result.assert_outcomes(errors=1)
    assert "ClockOutsideTest" in result.stdout.str()


def test_a_clock_made_in_a_fixture_of_the_tests_own_is_not(pytester):
    child(pytester, OUTSIDE.format(scope="function")).assert_outcomes(passed=1)


LEFT_WAITING = """
import threading
from tests.clocks import PhasedClock
from tests.hang_guard import HANG_GUARD

held = {{}}


def test_leaves_a_waiter():
    clock = PhasedClock(hang_after=0.2)
    event = threading.Event()
    got, errors = [], []

    def wait():
        try:
            got.append(clock.wait(event, 100))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    clock.wait_parked(1)
    held.update(clock=clock, event=event, got=got, errors=errors, thread=thread)
    {linger}


def test_the_closed_clock_is_quiet():
    threading.Event().wait(0.6)  # past the clock's hang bound
    assert held["thread"].is_alive() and held["errors"] == [] and held["clock"].violations == []
    held["event"].set()
    held["thread"].join(HANG_GUARD)
    assert held["got"] == [True]
"""


def test_a_closed_clock_is_quiet_and_its_waits_still_end_on_their_primitive(pytester):
    child(pytester, LEFT_WAITING.format(linger="")).assert_outcomes(passed=2)


def test_an_open_clock_with_a_waiter_that_nothing_ends_fails_its_test(pytester):
    """The twin: the same waiter, with the clock still open past its bound."""
    linger = "thread.join(HANG_GUARD); assert isinstance(errors[0], ClockHung)"
    planted = LEFT_WAITING.format(linger=linger).replace(
        "from tests.clocks import PhasedClock", "from tests.clocks import ClockHung, PhasedClock"
    )
    assert "ClockHung, PhasedClock" in planted  # the plant took effect
    result = child(pytester, planted)
    result.assert_outcomes(passed=1, failed=1, errors=1)
    assert "ClockHung" in result.stdout.str()


TIERS = """
import pytest
from tests import hang_guard
from tests.clocks import PhasedClock


def test_host():
    assert PhasedClock().hang_after == hang_guard.HANG_GUARD


@pytest.mark.container
def test_container():
    assert PhasedClock().hang_after == hang_guard.CONTAINER_HANG_GUARD
"""


def test_a_clock_has_its_tiers_hang_bound(pytester):
    assert hang_guard.HANG_GUARD != hang_guard.CONTAINER_HANG_GUARD  # the two can be told apart
    child(pytester, TIERS).assert_outcomes(passed=2)
