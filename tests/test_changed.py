"""The workspaces' condition, ``Workspaces._changed`` (backend/rcman/workspaces.py), and the
marks of the threads that make or rebuild a workspace: the lock order it keeps, and that a
workspace made ready leaves none of the marks of the thread that made it. On the host runtime
of tests/test_devcontainer_flow.py, the service's condition a virtual WatchedCondition
(tests/time_doubles.py), whose timeouts nothing here makes run out."""

from __future__ import annotations

import collections
import threading

import pytest
from rcman import workspaces as ws

from tests.conftest import BOB, ended, host_runtime
from tests.hang_guard import HANG_GUARD
from tests.test_clone import URL, row
from tests.test_devcontainer_flow import (
    Flow,
    building,
    lost_with_hook,
    publish,  # noqa: F401 - a fixture
    set_environment,
)
from tests.test_workspaces import _person, insert
from tests.time_doubles import WatchedCondition

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
    service._changed = WatchedCondition(Recorded(threading.Lock(), CHANGED, book), virtual=True)
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


def ran(thread_target) -> threading.Thread:
    thread = threading.Thread(target=thread_target, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def scenarios(settings, runtime, publish, monkeypatch):  # noqa: F811 - the fixture imported
    """Every scenario run on a service with its locks recorded: making with one place for a
    build, a deletion in each phase, a rebuild, a person's deletion with a thread of their own
    and a row nothing makes, a restore with one worker, a put-back, and ``close``. The book and
    the service's condition."""
    monkeypatch.setattr(ws, "MAX_BUILDS", 1)  # before the service: it takes the value once
    service = ws.Workspaces(settings, runtime)
    book = recorded(service)
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
    return book, service._changed


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


def test_the_locks_are_taken_in_order_and_every_wait_on_the_condition_ends_on_what_it_waited_for(
    scenarios,
):
    """Nothing is taken inside the workspaces' own lock, and the condition is taken after the
    workspace locks and before the service's own; none of the waits on it expired or hung."""
    book, changed = scenarios
    assert any(kind == OWN for _, kind in book.taken)  # positive: the recorder sees _lock
    # Positives: _changed under a workspace's lock (_stop_working), _lock under _changed.
    assert any(kind == CHANGED and WORKSPACE in held for held, kind in book.taken)
    assert any(kind == OWN and CHANGED in held for held, kind in book.taken)
    assert out_of_order(book.taken) == []
    assert changed.waits  # positive: they waited on it
    assert [w for w in changed.waits if w.ended != "predicate"] == []


def test_the_planted_twin_an_inverse_nesting_is_flagged(settings, runtime):
    service = ws.Workspaces(settings, runtime)
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


# --- membership: what a making thread that ends leaves ---------------------------------------


@pytest.fixture
def service(settings, runtime, changed):
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


def left_nothing(service, workspace_id: int) -> None:
    """The workspace is not being made, no mark of a thread of its is left, its end is told,
    and close returns with no wait expired, its deadline never running out by itself."""
    with service._changed:
        assert not [h for h in service._unended if h.workspace_id == workspace_id]
        assert workspace_id in service._ended_here
    assert not service._being_made(workspace_id)
    service.close()
    assert [w for w in service._changed.waits if w.ended != "predicate"] == []


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_workspace_made_ready_leaves_no_mark(service, settings):
    made = service.create(_person(settings), "ready")
    ended(service, made.id)
    assert row(settings, made.id)["state"] == ws.READY
    left_nothing(service, made.id)
