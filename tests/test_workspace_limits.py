"""Limits on a person and on each workspace (backend/rcman/workspaces.py, people.py): set only
by applying first and storing after, never below what is in use, taken from the table and applied
again when rcman starts, and dropped with the person or workspace they were for. On the host
runtime, which records what it is asked to apply."""

import threading

import pytest
from rcman import db, limits, people
from rcman import runtime as rt
from rcman import workspaces as ws
from rcman.limits import Limits, Usage
from rcman.problems import Problem

from tests.conftest import ALICE, BOB, TMUX_CONF

MIB = 1024**2
BIG = Limits(memory_mib=4096, cpu_percent=400, pids=1000)


@pytest.fixture
def service(settings, runtime):
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


def person_of(settings, email=ALICE) -> people.Person:
    con = db.connect(settings.database)
    try:
        return people.by_email(con, email)
    finally:
        con.close()


def insert(settings, person: int, name: str, state: str = "ready", held: Limits | None = None):
    held = held or Limits()
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state, memory_mib, cpu_percent, pids) "
            "values (?, ?, ?, ?, ?, ?)",
            (person, name, state, held.memory_mib, held.cpu_percent, held.pids),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def refusal(call, *args, **kwargs) -> Problem:
    with pytest.raises(Problem) as raised:
        call(*args, **kwargs)
    return raised.value


def stored(service, person) -> Limits:
    con = service._con()
    try:
        return people.by_id(con, person.id).limits
    finally:
        con.close()


# --- setting: apply first, store after --------------------------------------------------


def test_a_persons_and_a_workspaces_limits_are_applied_to_their_slices_and_then_stored(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    service.set_limits(person, workspace, BIG)
    assert runtime.limits == {limits.workspace_slice(person.id, workspace): BIG}
    assert service.get(workspace).limits == BIG
    assert stored(service, person) == Limits()  # the person's own are not touched
    held = Limits(memory_mib=8192)
    service.set_limits(person, None, held)
    assert runtime.limits[limits.person_slice(person.id)] == held
    assert stored(service, person) == held


def test_a_failure_to_apply_stores_nothing_and_says_so(service, settings, runtime):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    assert refusal(service.set_limits, person, None, BIG).code == "limits_not_applied"
    assert refusal(service.set_limits, person, workspace, BIG).code == "limits_not_applied"
    assert stored(service, person) == Limits() and service.get(workspace).limits == Limits()
    assert runtime.limits == {}


# --- never below what is in use ---------------------------------------------------------


def plant(runtime, person, workspace=None, memory_mib=0, tasks=0):
    runtime.used[limits.slice_of(person.id, workspace)] = Usage(memory_mib * MIB, tasks)


def test_a_memory_limit_below_what_is_in_use_is_refused_with_the_use_named_and_one_at_it_is_set(
    service, settings, runtime
):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000)
    problem = refusal(service.set_limits, person, None, Limits(memory_mib=2048))
    assert (problem.code, problem.status) == ("limit_below_use", 409)
    assert problem.extra == {"limit": "memory", "used": "3000M"}
    assert "3000M" in problem.title
    assert runtime.applied == [] and stored(service, person) == Limits()  # nothing, not even a try
    service.set_limits(person, None, Limits(memory_mib=3000))  # at what is in use: set
    assert stored(service, person) == Limits(memory_mib=3000)


# --- what is reported ------------------------------------------------------------------


def test_the_report_has_the_limits_and_the_use_of_the_person_and_of_each_workspace(
    service, settings, runtime
):
    person = person_of(settings)
    first = insert(settings, person.id, "first", held=Limits(memory_mib=1024))
    second = insert(settings, person.id, "second")
    insert(settings, person_of(settings, BOB).id, "not-theirs")
    service.set_limits(person, None, BIG)
    plant(runtime, person, memory_mib=1500, tasks=70)
    plant(runtime, person, first, memory_mib=900, tasks=20)
    report = service.limits_report(person)
    assert report["person"] == {
        "limits": BIG.text(),
        "usage": {"memory": "1500M", "memory_bytes": 1500 * MIB, "tasks": 70},
    }
    assert [(w["id"], w["name"], w["state"]) for w in report["workspaces"]] == [
        (first, "first", "ready"),
        (second, "second", "ready"),
    ]
    assert report["workspaces"][0]["limits"] == Limits(memory_mib=1024).text()
    assert report["workspaces"][0]["usage"] == {
        "memory": "900M",
        "memory_bytes": 900 * MIB,
        "tasks": 20,
    }
    assert report["workspaces"][1]["usage"] == {"memory": "0M", "memory_bytes": 0, "tasks": 0}


# --- making: applied before the container is made ---------------------------------------


def events_of(runtime) -> list:
    """Record, in order, each limit applied and each container made or started."""
    events = []
    for name in ("apply_limits", "create", "start"):
        real = getattr(runtime, name)

        def recording(*args, _name=name, _real=real, **kwargs):
            events.append(
                (_name, args[0] if _name != "apply_limits" else limits.slice_of(*args[:2]))
                if _name != "create"
                else (_name, args[0].name)
            )
            return _real(*args, **kwargs)

        setattr(runtime, name, recording)
    return events


def test_a_workspace_with_limits_has_them_applied_before_its_container_is_made(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", "creating", held=BIG)
    events = events_of(runtime)
    service._make(service.get(workspace), person.slot)
    assert service.get(workspace).state == "ready"
    assert events[:2] == [
        ("apply_limits", limits.workspace_slice(person.id, workspace)),
        ("create", rt.container_name(workspace)),
    ]


# --- restoring ------------------------------------------------------------------------


def stored_only(service, person, held: Limits):
    """The table says ``held`` for the person, whatever their slice has: as after a restore that
    could not apply it, or a set that was applied and not written."""
    con = service._con()
    try:
        people.set_limits(con, person.id, held)
    finally:
        con.close()


def test_restore_sets_what_the_table_holds_on_every_slice_an_empty_set_resetting(
    service, settings, runtime
):
    """And the slice of a container no row is for, which restore removes, is dropped."""
    alice, bob = person_of(settings, ALICE), person_of(settings, BOB)
    stored_only(service, alice, BIG)
    limited = insert(settings, alice.id, "limited", held=Limits(cpu_percent=50))
    free = insert(settings, alice.id, "free")
    bobs = insert(settings, bob.id, "bobs", held=Limits(pids=77))
    runtime.apply_limits(bob.id, None, BIG)  # a slice that holds what the table does not
    runtime.create(rt.Spec(rt.container_name(999), alice.slot, 999, alice.id))  # an orphan
    runtime.apply_limits(alice.id, 999, BIG)
    service.restore()
    assert runtime.limits[limits.person_slice(alice.id)] == BIG
    assert runtime.limits[limits.workspace_slice(alice.id, limited)] == Limits(cpu_percent=50)
    assert runtime.limits[limits.workspace_slice(alice.id, free)] == Limits()
    assert runtime.limits[limits.workspace_slice(bob.id, bobs)] == Limits(pids=77)
    assert runtime.limits[limits.person_slice(bob.id)] == Limits()  # reset, as the table says
    assert runtime.caps(bob.id, None) == limits.Caps()
    assert not runtime.exists(rt.container_name(999))
    assert limits.workspace_slice(alice.id, 999) in runtime.dropped
    assert limits.workspace_slice(alice.id, 999) not in runtime.limits


def test_restore_applies_them_all_before_any_container_is_made_or_started(
    service, settings, runtime
):
    alice, bob = person_of(settings), person_of(settings, BOB)
    stored_only(service, bob, BIG)
    lost = insert(settings, alice.id, "lost", held=BIG)  # no container: made again
    stopped = insert(settings, alice.id, "stopped", held=BIG)
    runtime.create(rt.Spec(rt.container_name(stopped), 1, stopped, alice.id))
    runtime.stop(rt.container_name(stopped))
    events = events_of(runtime)
    service.restore()
    kinds = [kind for kind, _ in events]
    assert "create" in kinds and "start" in kinds  # positive control
    stored_sets = {
        limits.person_slice(bob.id),
        limits.workspace_slice(alice.id, lost),
        limits.workspace_slice(alice.id, stopped),
    }
    first_container = min(i for i, kind in enumerate(kinds) if kind in ("create", "start"))
    before = {slice_ for kind, slice_ in events[:first_container] if kind == "apply_limits"}
    assert stored_sets <= before  # every stored set, then the containers


def test_restore_never_lowers_a_cap_of_the_slice_below_what_is_in_use(service, settings, runtime):
    """The table says 2G; the slice has 4G, as after a raise that was applied and did not reach
    the table, and 3G is in use. The table's 2G on the slice would end work that is running."""
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", held=Limits(pids=100))
    stored_only(service, person, Limits(memory_mib=2048, cpu_percent=100))
    runtime.apply_limits(person.id, None, Limits(memory_mib=4096))
    runtime.apply_limits(person.id, workspace, Limits(pids=500))
    plant(runtime, person, memory_mib=3000)
    plant(runtime, person, workspace, tasks=300)
    service.restore()
    held = runtime.limits[limits.person_slice(person.id)]
    assert held.memory_mib == 4096  # the slice's own cap, not the table's
    assert held.cpu_percent == 100  # and the rest of the set is applied
    assert runtime.limits[limits.workspace_slice(person.id, workspace)].pids == 500
    assert stored(service, person).memory_mib == 2048  # the table is the person's word, as set


# --- dropping --------------------------------------------------------------------------


def test_deleting_a_workspace_drops_the_limits_of_its_slice(service, settings, runtime):
    person = person_of(settings)
    kept = insert(settings, person.id, "kept")
    workspace = insert(settings, person.id, "w")
    service.set_limits(person, kept, BIG)
    service.set_limits(person, workspace, BIG)
    service.set_limits(person, None, BIG)
    runtime.create(rt.Spec(rt.container_name(workspace), 1, workspace, person.id))
    service.delete(service.get(workspace))
    assert runtime.dropped == [limits.workspace_slice(person.id, workspace)]
    assert set(runtime.limits) == {
        limits.person_slice(person.id),
        limits.workspace_slice(person.id, kept),
    }


def test_deleting_a_person_drops_every_slice_of_theirs_and_theirs_last(service, settings, runtime):
    alice, bob = person_of(settings), person_of(settings, BOB)
    one = insert(settings, bob.id, "one")
    two = insert(settings, bob.id, "two")
    service.set_limits(bob, None, BIG)
    service.set_limits(bob, one, BIG)
    service.set_limits(alice, None, BIG)
    service.delete_person(bob)
    assert runtime.dropped == [
        limits.workspace_slice(bob.id, one),
        limits.workspace_slice(bob.id, two),
        limits.person_slice(bob.id),
    ]
    assert set(runtime.limits) == {limits.person_slice(alice.id)}


# --- nothing here ever stops a slice -----------------------------------------------------


class Through(rt.HostRuntime):
    """The host runtime's containers, and the Podman runtime's limits through a recording
    systemctl: so every limit path of the service is seen as it would reach systemd."""

    podman: rt.PodmanRuntime

    def apply_limits(self, person, workspace, held):
        self.podman.apply_limits(person, workspace, held)

    def usage(self, person, workspace):
        return self.podman.usage(person, workspace)

    def caps(self, person, workspace):
        return self.podman.caps(person, workspace)

    def drop_limits(self, person, workspace):
        self.podman.drop_limits(person, workspace)


ENDING = {"stop", "kill", "restart", "disable", "mask", "reset-failed", "start", "isolate"}


def ending(systemctl) -> list[list[str]]:
    """The calls that end or restart a unit, or anything a slice stopped would end."""
    return [call for call in systemctl.calls() if ENDING & set(call)]


def test_no_way_to_set_drop_delete_restore_or_remake_ever_stops_a_slice(tmp_path, settings):
    from devtools.held_folders import tmux_sockets
    from tests.conftest import fake_claude
    from tests.test_runtime_limits import Systemctl, runtime_with

    shown = (
        "ActiveState=active\nMemoryCurrent=1\nMemorySwapCurrent=0\nTasksCurrent=1\n"
        "MemoryMax=infinity\nTasksMax=infinity\n"
    )
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    root = tmp_path / "workspaces"
    root.mkdir()
    claude = tmp_path / "claude"
    fake_claude(claude)
    with tmux_sockets("rcw-") as sockets:
        host = Through(root, sockets, claude, TMUX_CONF)
        host.podman = runtime_with(tmp_path, systemctl)
        service = ws.Workspaces(settings, host)
        try:
            alice, bob = person_of(settings), person_of(settings, BOB)
            gone = insert(settings, alice.id, "gone", held=BIG)
            lost = insert(settings, alice.id, "lost", held=BIG)
            insert(settings, bob.id, "his", held=BIG)
            host.create(rt.Spec(rt.container_name(gone), 1, gone, alice.id))
            host.create(rt.Spec(rt.container_name(999), 1, 999, alice.id))  # an orphan
            service.set_limits(alice, None, Limits(memory_mib=4096))  # a tightening reads
            service.set_limits(alice, gone, BIG)
            service.delete(service.get(gone))  # delete a workspace
            service.restore()  # restore: resets and sets, remakes `lost`, removes the orphan
            host.discard(rt.container_name(lost))
            service._remake(service.get(lost), threading.Event())  # remake
            service.delete_person(bob)  # delete a person, their workspace and slices
            verbs = {call[1] for call in systemctl.calls()}
            assert verbs == {"set-property", "show", "revert"}  # all three, and no other
            assert ending(systemctl) == []
            # the sequence reached the slices of a person, a workspace and an orphan
            slices = {call[-1] for call in systemctl.calls() if call[1] == "revert"}
            assert limits.workspace_slice(alice.id, 999) in slices
            assert limits.person_slice(bob.id) in slices
        finally:
            service.close()
            for name in host.labelled():
                host.remove(name)


def test_a_call_that_stops_a_slice_is_seen_by_the_check(tmp_path):
    """Planted twin: a stop issued through the same runtime is found by ``ending``."""
    from tests.test_runtime_limits import Systemctl, runtime_with

    systemctl = Systemctl(tmp_path)
    runtime = runtime_with(tmp_path, systemctl)
    runtime.drop_limits(7, None)
    assert ending(systemctl) == []  # the control: a revert is no stop
    runtime._systemctl("stop", limits.person_slice(7))
    assert ending(systemctl) == [["--user", "stop", "rcman-p7.slice"]]
