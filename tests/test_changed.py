"""The workspaces' condition, ``Workspaces._changed`` (backend/rcman/workspaces.py), and the
marks of the threads that make or rebuild a workspace: the lock order it keeps, what each way a
making thread ends leaves of its marks, and a deletion that comes just after a workspace is made.
On the host runtime of tests/test_devcontainer_flow.py, the service given a PhasedClock that no
test here moves."""

from __future__ import annotations

import collections
import sqlite3
import threading

import pytest
from rcman import workspaces as ws

from tests.clocks import PhasedClock
from tests.conftest import BOB, ended, host_runtime
from tests.hang_guard import HANG_GUARD
from tests.test_clone import URL, row
from tests.test_devcontainer_flow import (
    Flow,
    a_plan,
    building,
    lost_with_hook,
    publish,  # noqa: F401 - a fixture
    ready_with_plan,
    set_environment,
)
from tests.test_workspaces import _person, insert

# What each recorded lock is: the service's own, the condition's, the images', a workspace's
# and a person's.
OWN, CHANGED, IMAGES, WORKSPACE, PERSON = "_lock", "_changed", "images", "workspace", "person"


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Flow) as made:
        yield made


class Book:
    """Each lock taken, with the locks its thread held then."""

    def __init__(self):
        self._held = threading.local()
        self._guard = threading.Lock()
        self.taken: list[tuple[tuple[str, ...], str]] = []

    def held(self) -> list[str]:
        if not hasattr(self._held, "kinds"):
            self._held.kinds = []
        return self._held.kinds

    def took(self, kind: str) -> None:
        held = self.held()
        with self._guard:
            self.taken.append((tuple(held), kind))
        held.append(kind)

    def released(self, kind: str) -> None:
        held = self.held()
        held.reverse()
        held.remove(kind)
        held.reverse()


class Recorded:
    """A lock, or an RLock, that tells the book when it is taken and given back."""

    def __init__(self, inner, kind: str, book: Book):
        self._inner, self.kind, self._book = inner, kind, book
        self._owner: threading.Thread | None = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._inner.acquire(blocking, timeout)
        if got:
            self._book.took(self.kind)
            self._owner = threading.current_thread()
            self._depth += 1
        return got

    def release(self) -> None:
        self._depth -= 1
        if not self._depth:
            self._owner = None
        self._book.released(self.kind)
        self._inner.release()

    def _is_owned(self) -> bool:  # what a Condition asks of its lock
        return self._owner is threading.current_thread()

    def locked(self) -> bool:  # what a Condition asks of its lock from Python 3.14
        return self._depth > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class Recording(dict):
    """The workspaces' or the people's locks, each made Recorded as it is first asked for."""

    def __init__(self, kind: str, book: Book):
        super().__init__()
        self.kind, self.book = kind, book

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = Recorded(default, self.kind, self.book)
        return self[key]


def recorded(service) -> Book:
    """The service's locks recorded, swapped in before any thread of its starts."""
    book = Book()
    service._lock = Recorded(threading.Lock(), OWN, book)
    service._changed = threading.Condition(Recorded(threading.Lock(), CHANGED, book))
    service._images_lock = Recorded(threading.RLock(), IMAGES, book)
    service._locks = Recording(WORKSPACE, book)
    service._person_locks = Recording(PERSON, book)
    return book


def out_of_order(taken) -> list[str]:
    """(a) nothing taken inside ``_lock``; (b) ``_changed`` never inside it; (c) no workspace,
    person or images lock inside ``_changed``."""
    found = []
    for held, kind in taken:
        if OWN in held:
            found.append(f"(a) {kind} taken inside _lock, holding {held}")
        if kind == CHANGED and OWN in held:
            found.append(f"(b) _changed taken inside _lock, holding {held}")
        if CHANGED in held and kind in (WORKSPACE, PERSON, IMAGES):
            found.append(f"(c) {kind} taken inside _changed, holding {held}")
    return found


class Threads:
    """The threads that make or rebuild each workspace, as each starts."""

    def __init__(self, service):
        self.by_id: dict[int, list[threading.Thread]] = {}
        self.told = threading.Condition()
        for name in ("_make", "_rebuild"):
            setattr(service, name, self._wrapped(getattr(service, name)))

    def _wrapped(self, real):
        def run(workspace, slot):
            with self.told:
                self.by_id.setdefault(workspace.id, []).append(threading.current_thread())
                self.told.notify_all()
            real(workspace, slot)

        return run

    def join(self, workspace_id: int, n: int = 1) -> None:
        with self.told:
            assert self.told.wait_for(
                lambda: len(self.by_id.get(workspace_id, [])) >= n, HANG_GUARD
            )
            threads = list(self.by_id[workspace_id])
        for thread in threads:
            thread.join(HANG_GUARD)
            assert not thread.is_alive()


def made_service(settings, runtime, clock) -> ws.Workspaces:
    return ws.Workspaces(settings, runtime, clock=clock)


def ran(thread_target) -> threading.Thread:
    thread = threading.Thread(target=thread_target, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def scenarios(settings, runtime, publish, monkeypatch):  # noqa: F811 - the fixture imported
    """Every scenario run on a service with its locks recorded: making with one place for a
    build, a deletion in each phase, a rebuild, a person's deletion with a thread of their own
    and a row nothing makes, a restore with one worker, a put-back, and ``close``. The book, the
    clock, and the clock's time at the start."""
    monkeypatch.setattr(ws, "MAX_BUILDS", 1)  # before the service: it takes the value once
    clock = PhasedClock()
    service = made_service(settings, runtime, clock)
    book = recorded(service)
    began = clock.now()
    threads = Threads(service)
    waiting: dict[int, threading.Event] = collections.defaultdict(threading.Event)
    real_log = service._log

    def log(workspace_id, text, column="log"):
        if text == ws.WAITING_FOR_BUILD:
            waiting[workspace_id].set()
        real_log(workspace_id, text, column)

    service._log = log
    alice, bob = _person(settings), _person(settings, BOB)
    try:
        # Builds, one place: one builds, one waits and is deleted, one waits and builds.
        publish(building())
        runtime.build_gate = threading.Event()
        first = service.create(alice, None, URL)
        assert runtime.building.wait(HANG_GUARD)
        second = service.create(alice, None, URL)
        assert waiting[second.id].wait(HANG_GUARD)
        service.delete(service.get(second.id))  # while it waits for a place
        threads.join(second.id)
        third = service.create(alice, None, URL)
        assert waiting[third.id].wait(HANG_GUARD)
        runtime.build_gate.set()
        threads.join(first.id)
        threads.join(third.id)
        # A deletion while it builds.
        runtime.build_gate, runtime.building = threading.Event(), threading.Event()
        fourth = service.create(alice, None, URL)
        assert runtime.building.wait(HANG_GUARD)
        service.delete(service.get(fourth.id))
        runtime.build_gate.set()
        threads.join(fourth.id)
        # A rebuild, then the deletion of a ready one.
        service.restored.set()
        service.rebuild(alice, first.id, True)
        threads.join(first.id, 2)
        service.delete(service.get(third.id))
        # A person's deletion: a workspace their own thread makes, and a row nothing makes.
        held, real_create = threading.Event(), runtime.create
        runtime.create = lambda spec: (spec.image or held.wait(HANG_GUARD), real_create(spec))
        own = service.create(bob, "own")
        external = insert(settings, bob.id, "external", ws.CREATING)
        stopping, real_stop = threading.Event(), service._stop_working

        def stop_working(workspace_id):
            real_stop(workspace_id)
            if workspace_id == external:
                stopping.set()

        service._stop_working = stop_working
        deleting = ran(lambda: _quietly(service.delete_person, bob))
        assert stopping.wait(HANG_GUARD)
        _remove_row(settings, external)
        held.set()
        threads.join(own.id)
        deleting.join(HANG_GUARD)
        assert not deleting.is_alive()
        runtime.create = real_create
        # A restore with one worker, the first held by its start command.
        monkeypatch.setattr(ws, "RESTORE_WORKERS", 1)
        runtime.gate = threading.Event()
        lost_with_hook(service, settings, runtime, name="slow1", wanted=False)
        lost_with_hook(service, settings, runtime, name="slow2", wanted=False)
        restoring = ran(service.restore)
        assert runtime.hooking.wait(HANG_GUARD)
        runtime.gate.set()
        restoring.join(HANG_GUARD)
        assert not restoring.is_alive()
        # A put-back, held, and close while it runs.
        left = insert(settings, alice.id, "left", ws.READY)
        set_environment(settings, left, None, rebuild_state=ws.REBUILD_SWITCHING)
        entered, let_go = threading.Event(), threading.Event()
        service._restore_task = lambda workspace, **k: (entered.set(), let_go.wait(HANG_GUARD))
        service.settle_left_over()
        assert entered.wait(HANG_GUARD)
        closing = ran(service.close)
        let_go.set()
        closing.join(HANG_GUARD)
        assert not closing.is_alive()
    finally:
        for gate in (runtime.build_gate, runtime.gate):
            if gate is not None:
                gate.set()
    return book, clock, began


def _quietly(call, *args) -> None:
    try:
        call(*args)
    except Exception:  # noqa: BLE001 - what it ends in is not this test's
        pass


def _remove_row(settings, workspace_id: int) -> None:
    from rcman import db

    con = db.connect(settings.database)
    try:
        con.execute("delete from workspaces where id = ?", (workspace_id,))
        con.commit()
    finally:
        con.close()


def test_nothing_is_taken_inside_the_workspaces_own_lock(scenarios):
    book, _, _ = scenarios
    assert any(kind == OWN for _, kind in book.taken)  # positive: the recorder sees _lock
    assert [f for f in out_of_order(book.taken) if f.startswith("(a)")] == []


def test_the_condition_is_taken_after_the_workspace_locks_and_before_its_own(scenarios):
    book, _, _ = scenarios
    # Positives: _changed under a workspace's lock (_stop_working), _lock under _changed.
    assert any(kind == CHANGED and WORKSPACE in held for held, kind in book.taken)
    assert any(kind == OWN and CHANGED in held for held, kind in book.taken)
    assert out_of_order(book.taken) == []


def test_every_wait_on_the_condition_ends_on_a_frozen_clock(scenarios):
    _, clock, began = scenarios
    assert [w for w in clock.waits if w[0] == "wait_for"]  # positive: they waited on it
    assert clock.now() == began  # no time moved: each ended on what it waited for
    assert clock.violations == []


def test_the_planted_twin_an_inverse_nesting_is_flagged(settings, runtime):
    service = made_service(settings, runtime, PhasedClock())
    book = recorded(service)
    with service._changed:
        with service.lock(1):  # the plant: a workspace's lock inside the condition
            pass
    assert (("_changed",), WORKSPACE) in book.taken  # the plant took effect
    assert out_of_order(book.taken) == [
        f"(c) {WORKSPACE} taken inside _changed, holding ('_changed',)"
    ]
    with service._lock:
        with service._changed:  # and the condition inside the service's own lock
            pass
    assert any(f.startswith("(b)") for f in out_of_order(book.taken))
    service.close()


# --- membership: what each way a making thread ends leaves -----------------------------------


@pytest.fixture
def clock():
    return PhasedClock()


@pytest.fixture
def service(settings, runtime, clock):
    made = made_service(settings, runtime, clock)
    yield made
    made.close()


@pytest.fixture
def thrown(monkeypatch) -> list[BaseException]:
    """What a thread let escape, caught here."""
    caught: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: caught.append(args.exc_value))
    return caught


def left_nothing(service, clock, workspace_id: int | None, made: bool = True) -> None:
    """The workspace is not being made, no mark of a thread of its is left, its end is told,
    and close returns on the frozen clock with no time moved; with no workspace, no thread is
    left unended at all."""
    began = clock.now()
    with service._changed:
        if workspace_id is None:
            assert service._unended == set()
        else:
            assert not [h for h in service._unended if h.workspace_id == workspace_id]
            assert (workspace_id in service._ended_here) is made
    if workspace_id is not None:
        assert not service._being_made(workspace_id)
    service.close()
    assert clock.now() == began


class StartFails(RuntimeError):
    pass


@pytest.fixture
def no_start(monkeypatch):
    """``with no_start():`` a thread started meanwhile does not start."""
    import contextlib

    @contextlib.contextmanager
    def failing():
        real = threading.Thread.start

        def start(self):
            raise StartFails("planted: no thread starts")

        monkeypatch.setattr(threading.Thread, "start", start)
        try:
            yield
        finally:
            monkeypatch.setattr(threading.Thread, "start", real)

    return failing


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_workspace_made_ready_leaves_no_mark(service, clock, settings):
    made = service.create(_person(settings), "ready")
    ended(service, made.id)
    assert row(settings, made.id)["state"] == ws.READY
    left_nothing(service, clock, made.id)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_workspace_that_failed_leaves_no_mark(service, clock, settings, runtime, monkeypatch):
    def refusing(spec):
        raise ws.RuntimeFailed("planted: it cannot be made")

    monkeypatch.setattr(runtime, "create", refusing)
    made = service.create(_person(settings), "failed")
    ended(service, made.id)
    assert row(settings, made.id)["state"] == ws.FAILED
    left_nothing(service, clock, made.id)


def test_a_final_look_that_raises_leaves_no_mark(service, clock, settings, thrown, monkeypatch):
    real = service._set

    def failing(workspace_id, **columns):
        if columns.get("state") == ws.READY:
            raise sqlite3.OperationalError("planted: the database is locked")
        real(workspace_id, **columns)

    monkeypatch.setattr(service, "_set", failing)
    made = service.create(_person(settings), "unset")
    ended(service, made.id)
    assert [type(e) for e in thrown] == [sqlite3.OperationalError]  # the planted one only
    left_nothing(service, clock, made.id)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_closing_while_a_workspace_is_made_leaves_no_mark_and_it_creating(
    service, clock, settings, runtime, monkeypatch
):
    def closing(spec):
        raise ws.Closing()

    monkeypatch.setattr(runtime, "create", closing)
    made = service.create(_person(settings), "cut")
    ended(service, made.id)
    assert row(settings, made.id)["state"] == ws.CREATING  # the next start finishes it
    left_nothing(service, clock, made.id)


def rebuildable(service, settings, runtime) -> int:
    service.restored.set()
    return ready_with_plan(service, settings, runtime, a_plan())


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_closing_while_a_workspace_is_rebuilt_leaves_no_mark_and_it_preparing(
    service, clock, settings, runtime, monkeypatch
):
    workspace = rebuildable(service, settings, runtime)

    def closing(*args):
        raise ws.Closing()

    monkeypatch.setattr(service, "_replace", closing)
    service.rebuild(_person(settings), workspace, True)
    ended(service, workspace)
    assert row(settings, workspace)["rebuild_state"] == ws.REBUILD_PREPARING
    left_nothing(service, clock, workspace)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_making_thread_that_does_not_start_leaves_no_mark_and_no_row(
    service, clock, settings, no_start
):
    recorded: list[int] = []
    real_get = service.get
    service.get = lambda workspace_id: (recorded.append(workspace_id), real_get(workspace_id))[1]
    with no_start(), pytest.raises(StartFails):
        service.create(_person(settings), "unstarted")
    assert len(recorded) == 1  # the row create recorded, read back once
    assert [w for w in service.everything() if w.name == "unstarted"] == []
    left_nothing(service, clock, recorded[0], made=False)  # its row is gone, so is its record


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_rebuild_that_does_not_start_leaves_no_mark_and_no_phase(
    service, clock, settings, runtime, no_start
):
    workspace = rebuildable(service, settings, runtime)
    with no_start(), pytest.raises(StartFails):
        service.rebuild(_person(settings), workspace, True)
    assert row(settings, workspace)["rebuild_state"] is None
    left_nothing(service, clock, workspace)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_put_back_that_does_not_start_is_not_registered(
    service, clock, settings, runtime, no_start
):
    left = insert(settings, 1, "left", ws.READY)
    set_environment(settings, left, None, rebuild_state=ws.REBUILD_SWITCHING)
    service.restored.set()
    with no_start(), pytest.raises(StartFails):
        service.settle_left_over()
    with service._changed:
        assert service._putting_back == {}
    left_nothing(service, clock, left, made=False)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_restore_that_does_not_start_leaves_close_nothing_to_wait_for(service, clock, no_start):
    with no_start(), pytest.raises(StartFails):
        service.start_background()
    with service._changed:
        assert service._restore_running is False
    left_nothing(service, clock, None)


def test_a_collection_that_raises_as_a_rebuild_ends_leaves_no_mark(
    service, clock, settings, runtime, thrown, monkeypatch
):
    workspace = rebuildable(service, settings, runtime)
    monkeypatch.setattr(service, "_replace", lambda *args: "rebuilt")

    def collecting():
        raise RuntimeError("planted: the collection failed")

    monkeypatch.setattr(service, "_collect_images_quietly", collecting)
    service.rebuild(_person(settings), workspace, True)
    ended(service, workspace)
    assert [str(e) for e in thrown] == ["planted: the collection failed"]  # the planted one only
    left_nothing(service, clock, workspace)


# --- a deletion just after the making ends ------------------------------------------------------


@pytest.fixture
def plain(settings, runtime):
    """The service on the real clock: nothing here waits on it."""
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


def held_after(service, name: str):
    """``service``'s ``name`` (``_make`` or ``_rebuild``) held once it has returned, until
    ``go``: the event set as it returns, ``go``, and the thread it ran in."""
    returned, go, threads = threading.Event(), threading.Event(), []
    real = getattr(service, name)

    def run(*args):
        threads.append(threading.current_thread())
        try:
            real(*args)
        finally:
            returned.set()
            assert go.wait(HANG_GUARD)

    setattr(service, name, run)
    return returned, go, threads


def test_a_deletion_just_after_a_workspace_is_made_removes_it(plain, settings):
    returned, go, threads = held_after(plain, "_make")
    made = plain.create(_person(settings), "racing")
    try:
        assert returned.wait(HANG_GUARD)
        assert row(settings, made.id)["state"] == ws.READY  # positive: it was made
        plain.delete(plain.get(made.id))
    finally:
        go.set()
    threads[0].join(HANG_GUARD)
    assert not threads[0].is_alive()
    assert row(settings, made.id) is None


def test_the_control_a_deletion_just_after_a_rebuild_removes_it(plain, settings, runtime):
    workspace = rebuildable(plain, settings, runtime)
    returned, go, threads = held_after(plain, "_rebuild")
    plain.rebuild(_person(settings), workspace, True)
    try:
        assert returned.wait(HANG_GUARD)
        assert row(settings, workspace)["rebuild_state"] is None  # positive: it ended
        plain.delete(plain.get(workspace))
    finally:
        go.set()
    threads[0].join(HANG_GUARD)
    assert not threads[0].is_alive()
    assert row(settings, workspace) is None
