"""Limits on a person and on each workspace (backend/rcman/workspaces.py, people.py): set only
by applying first and storing after, never below what is in use, taken from the table and applied
again when rcman starts, and dropped with the person or workspace they were for. On the host
runtime, which records what it is asked to apply."""

import sqlite3
import threading
import time

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


def insert(
    settings,
    person: int,
    name: str,
    state: str = "ready",
    held: Limits | None = None,
    workspace_id: int | None = None,
):
    held = held or Limits()
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (id, person, name, state, memory_mib, cpu_percent, pids) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, person, name, state, held.memory_mib, held.cpu_percent, held.pids),
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


# --- the table --------------------------------------------------------------------------


def test_a_person_and_a_workspace_have_no_limits_until_some_are_set(service, settings):
    person = person_of(settings)
    assert person.limits == Limits()
    workspace = service.get(insert(settings, person.id, "w"))
    assert workspace.limits == Limits()


def test_limits_stored_in_the_table_are_the_person_and_the_workspaces_values(service, settings):
    person = person_of(settings)
    con = service._con()
    try:
        people.set_limits(con, person.id, BIG)
        assert people.by_id(con, person.id).limits == BIG
        assert people.everyone(con)[0].limits == BIG  # by email: alice first
        people.set_limits(con, person.id, Limits())
        assert people.by_id(con, person.id).limits == Limits()
    finally:
        con.close()
    workspace = insert(settings, person.id, "w", held=Limits(512, 25, 70))
    assert service.get(workspace).limits == Limits(512, 25, 70)
    assert service.everything()[0].limits == Limits(512, 25, 70)


# --- setting: apply first, store after --------------------------------------------------


def test_a_persons_limits_are_applied_to_their_slice_and_then_stored(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, BIG)
    assert runtime.limits == {limits.person_slice(person.id): BIG}
    assert stored(service, person) == BIG


def test_a_workspaces_limits_are_applied_to_its_slice_beneath_its_persons_and_stored(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    service.set_limits(person, workspace, BIG)
    assert runtime.limits == {limits.workspace_slice(person.id, workspace): BIG}
    assert service.get(workspace).limits == BIG
    assert stored(service, person) == Limits()  # the person's own are not touched


def test_a_workspace_that_is_not_ready_can_be_given_limits(service, settings, runtime):
    person = person_of(settings)
    for state in ("creating", "failed"):
        workspace = insert(settings, person.id, state, state)
        service.set_limits(person, workspace, BIG)
        assert service.get(workspace).limits == BIG


def test_a_set_replaces_the_whole_set_and_what_is_left_out_is_reset(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, BIG)
    service.set_limits(person, None, Limits(memory_mib=8192))
    assert stored(service, person) == Limits(memory_mib=8192)
    assert runtime.applied[-1] == (limits.person_slice(person.id), Limits(memory_mib=8192))
    service.set_limits(person, None, Limits())
    assert stored(service, person) == Limits()
    assert runtime.applied[-1][1] == Limits()  # applied, not just forgotten: the slice is reset


def test_a_failure_to_apply_stores_nothing_and_says_so(service, settings, runtime):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    assert refusal(service.set_limits, person, None, BIG).code == "limits_not_applied"
    assert refusal(service.set_limits, person, workspace, BIG).code == "limits_not_applied"
    assert stored(service, person) == Limits() and service.get(workspace).limits == Limits()
    assert runtime.limits == {}


def test_a_runtime_that_cannot_be_reached_is_said_to_be_and_stores_nothing(
    service, settings, runtime
):
    person = person_of(settings)
    runtime.limits_error = rt.RuntimeUnavailable("no bus")
    assert refusal(service.set_limits, person, None, BIG).code == "runtime_unavailable"
    assert stored(service, person) == Limits()


def test_a_limit_that_is_not_applied_leaves_the_earlier_one_as_it_was(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, BIG)
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    refusal(service.set_limits, person, None, Limits(memory_mib=512))
    assert stored(service, person) == BIG
    assert runtime.limits == {limits.person_slice(person.id): BIG}


# --- never below what is in use ---------------------------------------------------------


def plant(runtime, person, workspace=None, memory_mib=0, tasks=0, extra_bytes=0):
    runtime.used[limits.slice_of(person.id, workspace)] = Usage(
        memory_mib * MIB + extra_bytes, tasks
    )


def test_a_memory_limit_below_what_is_in_use_is_refused_with_the_use_named(
    service, settings, runtime
):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000)
    problem = refusal(service.set_limits, person, None, Limits(memory_mib=2048))
    assert (problem.code, problem.status) == ("limit_below_use", 409)
    assert problem.extra == {"limit": "memory", "used": "3000M"}
    assert "3000M" in problem.title
    assert runtime.applied == [] and stored(service, person) == Limits()  # nothing, not even a try


def test_a_process_limit_below_what_is_in_use_is_refused_with_the_use_named(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    plant(runtime, person, workspace, tasks=300)
    problem = refusal(service.set_limits, person, workspace, Limits(pids=299))
    assert (problem.code, problem.extra) == ("limit_below_use", {"limit": "pids", "used": "300"})
    assert "300" in problem.title
    assert runtime.applied == [] and service.get(workspace).limits == Limits()


def test_a_limit_at_what_is_in_use_or_above_it_is_set(service, settings, runtime):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000, tasks=300)
    service.set_limits(person, None, Limits(memory_mib=3000, pids=300))
    assert stored(service, person) == Limits(memory_mib=3000, pids=300)


def test_a_byte_of_memory_over_the_mebibyte_limit_is_in_use_beyond_it(service, settings, runtime):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000, extra_bytes=1)
    assert refusal(service.set_limits, person, None, Limits(memory_mib=3000)).code == (
        "limit_below_use"
    )
    assert refusal(service.set_limits, person, None, Limits(memory_mib=3000)).extra["used"] == (
        "3001M"
    )


def test_the_workspaces_use_is_its_own_and_the_persons_includes_everything_beneath(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    plant(runtime, person, workspace, memory_mib=3000)  # the workspace uses a lot ...
    service.set_limits(
        person, None, Limits(memory_mib=2048)
    )  # ... the person's slice, none planted
    assert refusal(service.set_limits, person, workspace, Limits(memory_mib=2048)).code == (
        "limit_below_use"
    )


def test_raising_a_limit_or_taking_it_away_is_always_allowed(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048, pids=100))
    plant(runtime, person, memory_mib=5000, tasks=900)  # more than any cap: the use is not asked
    service.set_limits(person, None, Limits(memory_mib=4096, pids=200))  # raised
    assert stored(service, person) == Limits(memory_mib=4096, pids=200)
    service.set_limits(person, None, Limits())  # taken away
    assert stored(service, person) == Limits()


def test_a_limit_that_is_not_lowered_is_not_held_to_the_use_either(service, settings, runtime):
    """Changing the CPU, say, sends the whole set again: a memory limit sent unchanged is no
    tightening, whatever the use has risen to (page cache counts as use)."""
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048, cpu_percent=100, pids=100))
    plant(runtime, person, memory_mib=2049, tasks=101)
    service.set_limits(person, None, Limits(memory_mib=2048, cpu_percent=50, pids=100))
    assert stored(service, person).cpu_percent == 50


def test_a_limit_where_there_was_none_is_a_tightening(service, settings, runtime):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000)
    assert stored(service, person).memory_mib is None
    assert refusal(service.set_limits, person, None, Limits(memory_mib=2048)).code == (
        "limit_below_use"
    )


def test_any_change_of_the_cpu_is_allowed_whatever_is_in_use(service, settings, runtime):
    person = person_of(settings)
    plant(runtime, person, memory_mib=3000, tasks=300)
    for percent in (400, 100, 25, None, 25):
        service.set_limits(person, None, Limits(cpu_percent=percent))
        assert stored(service, person).cpu_percent == percent
    assert runtime.applied  # and each was applied


def test_use_is_not_read_when_nothing_is_tightened(service, settings, runtime):
    person = person_of(settings)
    asked = []
    real = runtime.usage
    runtime.usage = lambda *a: asked.append(a) or real(*a)
    service.set_limits(person, None, Limits(cpu_percent=50))
    service.set_limits(person, None, Limits())
    assert asked == []
    service.set_limits(person, None, Limits(memory_mib=2048))  # a tightening reads it
    assert asked == [(person.id, None)]


def test_a_use_that_cannot_be_read_stops_a_tightening_and_changes_nothing(
    service, settings, runtime
):
    person = person_of(settings)
    runtime.limits_error = rt.RuntimeFailed("cannot read")
    assert refusal(service.set_limits, person, None, Limits(memory_mib=2048)).code == (
        "limits_not_applied"
    )
    runtime.limits_error = None
    assert runtime.applied == [] and stored(service, person) == Limits()


def test_a_limit_is_never_applied_before_the_use_has_been_checked(service, settings, runtime):
    person = person_of(settings)
    events = []
    runtime.usage = lambda *a: events.append("usage") or Usage(0, 0)
    real = runtime.apply_limits
    runtime.apply_limits = lambda *a: events.append("apply") or real(*a)
    service.set_limits(person, None, Limits(memory_mib=2048))
    assert events == ["usage", "apply"]


# --- who and which ----------------------------------------------------------------------


def test_limits_cannot_be_set_on_a_workspace_that_is_another_persons(service, settings, runtime):
    alice, bob = person_of(settings), person_of(settings, BOB)
    theirs = insert(settings, bob.id, "bobs")
    assert refusal(service.set_limits, alice, theirs, BIG).code == "workspace_not_found"
    assert refusal(service.set_limits, alice, 9999, BIG).code == "workspace_not_found"
    assert runtime.applied == [] and service.get(theirs).limits == Limits()


def test_limits_cannot_be_set_on_a_workspace_being_deleted_or_a_person_being_deleted(
    service, settings, runtime
):
    person = person_of(settings)
    going = insert(settings, person.id, "going", "deleting")
    assert refusal(service.set_limits, person, going, BIG).code == "workspace_not_found"
    con = service._con()
    try:
        people.mark_deleting(con, person)
    finally:
        con.close()
    assert refusal(service.set_limits, person, None, BIG).code == "person_not_found"
    assert runtime.applied == []


def test_setting_limits_takes_the_persons_lock(service, settings, runtime):
    person = person_of(settings)
    done = threading.Event()
    with service.person_lock(person.id):
        thread = threading.Thread(
            target=lambda: (service.set_limits(person, None, BIG), done.set()), daemon=True
        )
        thread.start()
        assert not done.wait(0.3)  # held up by the lock
        assert runtime.applied == []
    assert done.wait(10)
    thread.join()
    assert stored(service, person) == BIG


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


def test_a_use_that_cannot_be_read_is_null_in_the_report_and_the_limits_are_still_there(
    service, settings, runtime
):
    person = person_of(settings)
    insert(settings, person.id, "w")
    service.set_limits(person, None, BIG)
    runtime.limits_error = rt.RuntimeFailed("cannot read")
    report = service.limits_report(person)
    assert report["person"] == {"limits": BIG.text(), "usage": None}
    assert report["workspaces"][0]["usage"] is None


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


def test_a_workspace_with_no_limits_has_its_slice_reset_when_it_is_made(service, settings, runtime):
    """An id is given again once the workspace that had it is gone: a slice a failed drop left
    must not hold the new one."""
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", "creating")
    events = events_of(runtime)
    service._make(service.get(workspace), person.slot)
    assert service.get(workspace).state == "ready"
    assert runtime.applied == [(limits.workspace_slice(person.id, workspace), Limits())]
    assert [kind for kind, _ in events[:2]] == ["apply_limits", "create"]


def test_a_workspace_that_takes_the_id_of_one_deleted_does_not_inherit_its_caps(
    service, settings, runtime
):
    person = person_of(settings)
    old = insert(settings, person.id, "old", workspace_id=7)
    service.set_limits(person, old, BIG)
    runtime.drop_limits = lambda *a: (_ for _ in ()).throw(rt.RuntimeFailed("systemctl refused"))
    service.delete(service.get(old))  # the drop failed: the slice keeps the caps
    assert runtime.limits == {limits.workspace_slice(person.id, 7): BIG}
    del runtime.drop_limits
    new = insert(settings, person.id, "new", "creating", workspace_id=7)
    service._make(service.get(new), person.slot)
    assert runtime.limits[limits.workspace_slice(person.id, 7)] == Limits()
    assert runtime.caps(person.id, 7) == limits.Caps()  # and actually so


def test_a_remade_workspace_with_no_limits_has_its_slice_reset_too(service, settings, runtime):
    person = person_of(settings)
    runtime.apply_limits(person.id, 8, BIG)  # a predecessor's
    workspace = insert(settings, person.id, "w", workspace_id=8)
    events = events_of(runtime)
    service._remake(service.get(workspace), threading.Event())
    assert runtime.limits[limits.workspace_slice(person.id, 8)] == Limits()
    assert [kind for kind, _ in events[:2]] == ["apply_limits", "create"]


def test_a_reset_that_fails_is_logged_and_does_not_stop_the_workspace_being_made(
    service, settings, runtime, caplog
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", "creating")
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    service._make(service.get(workspace), person.slot)
    assert service.get(workspace).state == "ready"  # nothing was to be held: not a failure
    assert "systemctl refused" in caplog.text
    runtime.limits_error = None
    service.restore()  # restore sets the table's set on every slice again
    assert runtime.limits[limits.workspace_slice(person.id, workspace)] == Limits()


def test_a_workspace_whose_limits_cannot_be_applied_fails_without_a_container(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", "creating", held=BIG)
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    service._make(service.get(workspace), person.slot)
    found = service.get(workspace)
    assert found.state == "failed" and "systemctl refused" in found.error
    assert not runtime.exists(rt.container_name(workspace))


def test_limits_set_while_a_workspace_is_being_made_are_the_ones_applied_to_it(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", "creating")
    service.set_limits(person, workspace, BIG)
    events = events_of(runtime)
    service._make(service.get(workspace), person.slot)
    assert events[0] == ("apply_limits", limits.workspace_slice(person.id, workspace))


# --- restoring ------------------------------------------------------------------------


def test_restore_sets_what_the_table_holds_on_every_slice_an_empty_set_resetting(
    service, settings, runtime
):
    alice, bob = person_of(settings, ALICE), person_of(settings, BOB)
    con = service._con()
    try:
        people.set_limits(con, alice.id, BIG)
    finally:
        con.close()
    limited = insert(settings, alice.id, "limited", held=Limits(cpu_percent=50))
    free = insert(settings, alice.id, "free")
    bobs = insert(settings, bob.id, "bobs", held=Limits(pids=77))
    runtime.apply_limits(bob.id, None, BIG)  # a slice that holds what the table does not
    service.restore()
    assert runtime.limits[limits.person_slice(alice.id)] == BIG
    assert runtime.limits[limits.workspace_slice(alice.id, limited)] == Limits(cpu_percent=50)
    assert runtime.limits[limits.workspace_slice(alice.id, free)] == Limits()
    assert runtime.limits[limits.workspace_slice(bob.id, bobs)] == Limits(pids=77)
    assert runtime.limits[limits.person_slice(bob.id)] == Limits()  # reset, as the table says
    assert runtime.caps(bob.id, None) == limits.Caps()


def test_restore_applies_them_all_before_any_container_is_made_or_started(
    service, settings, runtime
):
    alice, bob = person_of(settings), person_of(settings, BOB)
    con = service._con()
    try:
        people.set_limits(con, bob.id, BIG)
    finally:
        con.close()
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


def test_restore_applies_what_a_restart_of_the_machine_cleared(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, BIG)
    runtime.limits.clear()  # /run is gone with the restart
    service.restore()
    assert runtime.limits[limits.person_slice(person.id)] == BIG


def test_restore_goes_on_after_a_limit_that_could_not_be_applied(service, settings, runtime):
    person = person_of(settings)
    first = insert(settings, person.id, "first", held=BIG)
    second = insert(settings, person.id, "second", held=BIG)
    real = runtime.apply_limits

    def failing_for_one(person_id, workspace, held):
        if workspace == first:
            raise rt.RuntimeFailed("systemctl refused")
        real(person_id, workspace, held)

    runtime.apply_limits = failing_for_one
    service.restore()
    assert limits.workspace_slice(person.id, second) in runtime.limits
    assert limits.workspace_slice(person.id, first) not in runtime.limits
    # the one whose limits were not applied is not made without them, and is not lost either:
    # it stays ready, without its container, until rcman next starts, as only a restore makes
    # a lost container again
    assert service.get(first).state == "ready"
    assert not runtime.exists(rt.container_name(first))
    assert runtime.running(rt.container_name(second))


def test_a_lost_container_that_is_made_again_has_its_limits_applied_first(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", held=BIG)
    runtime.limits.clear()
    events = events_of(runtime)
    service._remake(service.get(workspace), threading.Event())
    assert events[:2] == [
        ("apply_limits", limits.workspace_slice(person.id, workspace)),
        ("create", rt.container_name(workspace)),
    ]


def test_restore_never_stops_or_removes_a_running_container_to_apply_limits(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", held=BIG)
    runtime.create(rt.Spec(rt.container_name(workspace), 1, workspace, person.id))
    stops = []
    real_stop, real_remove = runtime.stop, runtime.remove
    runtime.stop = lambda name: stops.append(("stop", name)) or real_stop(name)
    runtime.remove = lambda name: stops.append(("remove", name)) or real_remove(name)
    service.restore()
    assert runtime.running(rt.container_name(workspace)) and stops == []
    assert runtime.limits  # and the limits were applied


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


def test_restore_lowers_a_cap_when_what_is_in_use_is_under_the_new_one(service, settings, runtime):
    person = person_of(settings)
    stored_only(service, person, Limits(memory_mib=2048))
    runtime.apply_limits(person.id, None, Limits(memory_mib=4096))
    plant(runtime, person, memory_mib=1000)  # the control: the same, with less in use
    service.restore()
    assert runtime.limits[limits.person_slice(person.id)].memory_mib == 2048


def test_restore_gives_a_slice_with_no_cap_the_tables_when_nothing_is_in_use(
    service, settings, runtime
):
    person = person_of(settings)
    stored_only(service, person, Limits(memory_mib=2048, pids=100))
    runtime.limits.clear()  # the machine restarted: no caps, and the slices are not active
    service.restore()
    assert runtime.limits[limits.person_slice(person.id)] == Limits(memory_mib=2048, pids=100)


def test_restore_does_not_lower_a_cap_when_the_use_is_not_counted(service, settings, runtime):
    person = person_of(settings)
    stored_only(service, person, Limits(memory_mib=2048, pids=100))
    runtime.apply_limits(person.id, None, Limits(memory_mib=4096, pids=500))
    runtime.used[limits.person_slice(person.id)] = Usage(memory_known=False, tasks_known=False)
    service.restore()
    held = runtime.limits[limits.person_slice(person.id)]
    assert (held.memory_mib, held.pids) == (4096, 500)  # nothing says it is safe: as an edit


def test_restore_raises_and_takes_away_caps_whatever_is_in_use(service, settings, runtime):
    person = person_of(settings)
    stored_only(service, person, Limits(memory_mib=8192))
    runtime.apply_limits(person.id, None, Limits(memory_mib=4096, pids=500))
    plant(runtime, person, memory_mib=3000, tasks=400)
    service.restore()
    assert runtime.limits[limits.person_slice(person.id)] == Limits(memory_mib=8192)  # pids freed


def test_a_remade_container_is_not_put_in_a_slice_capped_below_its_persons_use(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w", held=Limits(memory_mib=2048))
    runtime.apply_limits(person.id, workspace, Limits(memory_mib=4096))
    plant(runtime, person, workspace, memory_mib=3000)
    service._remake(service.get(workspace), threading.Event())
    assert runtime.limits[limits.workspace_slice(person.id, workspace)].memory_mib == 4096


class Crash(BaseException):
    """What the process meeting its end looks like from the code that was running."""


def test_a_raise_is_recorded_before_it_is_applied_so_a_crash_cannot_leave_the_table_lower(
    service, settings, runtime
):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048))
    real = runtime.apply_limits

    def dying(person_id, workspace, held):
        raise Crash

    runtime.apply_limits = dying
    with pytest.raises(Crash):
        service.set_limits(person, None, Limits(memory_mib=8192))
    runtime.apply_limits = real
    assert stored(service, person).memory_mib == 8192  # the table is never under the slice
    assert runtime.caps(person.id, None).memory_bytes == 2048 * MIB


def test_a_raise_that_cannot_be_applied_is_not_left_recorded(service, settings, runtime):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    service.set_limits(person, None, Limits(memory_mib=2048))
    service.set_limits(person, workspace, Limits(pids=100))
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    refusal(service.set_limits, person, None, Limits(memory_mib=8192))
    refusal(service.set_limits, person, workspace, Limits(pids=900))
    assert stored(service, person).memory_mib == 2048
    assert service.get(workspace).limits == Limits(pids=100)


def test_a_lowering_is_applied_before_it_is_recorded(service, settings, runtime):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=8192))
    runtime.apply_limits = lambda *args: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        service.set_limits(person, None, Limits(memory_mib=2048))
    assert stored(service, person).memory_mib == 8192  # not recorded ahead of the slice


def test_a_change_of_limits_waits_for_another_process_that_is_changing_them(
    service, settings, runtime
):
    """rcman-admin is a process of its own; its locks do not hold back the service's."""
    from tests.test_filelock import other_process_holding

    person = person_of(settings)
    holder = other_process_holding(settings.database.parent / ws.LOCK_FILE, 1.5)
    began = time.monotonic()
    service.set_limits(person, None, BIG)
    assert time.monotonic() - began >= 1.0
    holder.wait(10)
    assert runtime.limits[limits.person_slice(person.id)] == BIG


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


def test_a_workspace_with_no_limits_has_its_slice_dropped_too_as_an_id_may_come_again(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    service.delete(service.get(workspace))
    assert runtime.dropped == [limits.workspace_slice(person.id, workspace)]


def test_a_workspace_is_deleted_even_when_its_limits_cannot_be_dropped(service, settings, runtime):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    runtime.create(rt.Spec(rt.container_name(workspace), 1, workspace, person.id))
    real = runtime.drop_limits

    def refusing(*args):
        raise rt.RuntimeFailed("systemctl refused")

    runtime.drop_limits = refusing
    service.delete(service.get(workspace))
    runtime.drop_limits = real
    assert service.get(workspace) is None and not runtime.exists(rt.container_name(workspace))


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


def test_a_person_is_deleted_even_when_their_limits_cannot_be_dropped(service, settings, runtime):
    bob = person_of(settings, BOB)
    runtime.drop_limits = lambda *a: (_ for _ in ()).throw(rt.RuntimeFailed("systemctl refused"))
    service.delete_person(bob)
    con = service._con()
    try:
        assert people.by_id(con, bob.id) is None
    finally:
        con.close()


def test_a_slice_dropped_before_the_workspace_is_gone_is_not_applied_again_by_a_late_set(
    service, settings, runtime
):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    found = service.get(workspace)
    service.delete(found)
    assert refusal(service.set_limits, person, workspace, BIG).code == "workspace_not_found"
    assert runtime.limits == {}


# --- the caps the slice has, not the ones the table has ---------------------------------


def stored_only(service, person, held: Limits, workspace=None):
    """The table says ``held``, whatever the slice has: as after a restore that could not apply
    it, or a set that was applied and not written."""
    if workspace is None:
        con = service._con()
        try:
            people.set_limits(con, person.id, held)
        finally:
            con.close()
    else:
        service._set(
            workspace,
            memory_mib=held.memory_mib,
            cpu_percent=held.cpu_percent,
            pids=held.pids,
        )


def test_an_edit_of_the_cpu_alone_is_refused_when_the_slice_lost_the_cap_the_table_holds(
    service, settings, runtime
):
    """The restore at boot could not apply the person's cap: the table says one, the slice has
    none and the work in it uses more. The page sends the whole set, the memory unchanged
    from the table, and that is a cap far below the use."""
    person = person_of(settings)
    stored_only(service, person, Limits(memory_mib=2048, cpu_percent=100))
    plant(runtime, person, memory_mib=3000)
    assert runtime.caps(person.id, None) == limits.Caps()  # the slice has no cap
    problem = refusal(service.set_limits, person, None, Limits(memory_mib=2048, cpu_percent=50))
    assert (problem.code, problem.extra) == (
        "limit_below_use",
        {"limit": "memory", "used": "3000M"},
    )
    assert runtime.applied == []
    assert stored(service, person) == Limits(
        memory_mib=2048, cpu_percent=100
    )  # and nothing written


def test_the_same_holds_for_processes_and_for_a_workspace(service, settings, runtime):
    person = person_of(settings)
    workspace = insert(settings, person.id, "w")
    stored_only(service, person, Limits(pids=100), workspace)
    plant(runtime, person, workspace, tasks=300)
    problem = refusal(service.set_limits, person, workspace, Limits(pids=100, cpu_percent=50))
    assert (problem.code, problem.extra) == ("limit_below_use", {"limit": "pids", "used": "300"})
    assert runtime.applied == []


def test_a_set_applied_and_not_written_leaves_the_next_edit_judged_by_the_slice(
    service, settings, runtime, monkeypatch
):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048))  # the table and the slice: 2G
    service.set_limits(person, None, Limits(memory_mib=8192))  # raised; ...
    stored_only(service, person, Limits(memory_mib=2048))  # ... but only the slice has it
    assert runtime.caps(person.id, None).memory_bytes == 8192 * MIB
    plant(runtime, person, memory_mib=3000)
    # the page sends the table's 2G with a new CPU: lower than the slice's, below the use
    assert refusal(service.set_limits, person, None, Limits(2048, 50)).code == "limit_below_use"


def test_a_raise_whose_write_failed_is_not_applied_and_a_lowering_whose_write_failed_is_the_slices(
    service, settings, runtime, monkeypatch
):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048))
    plant(runtime, person, memory_mib=1000)

    def failing(con, person_id, held):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(people, "set_limits", failing)
    with pytest.raises(sqlite3.OperationalError):
        service.set_limits(person, None, Limits(memory_mib=4096))  # a raise: recorded first
    assert runtime.caps(person.id, None).memory_bytes == 2048 * MIB  # so never applied
    with pytest.raises(sqlite3.OperationalError):
        service.set_limits(person, None, Limits(memory_mib=1500))  # a lowering: applied first
    monkeypatch.undo()
    assert stored(service, person) == Limits(memory_mib=2048)
    assert runtime.caps(person.id, None).memory_bytes == 1500 * MIB
    plant(runtime, person, memory_mib=1800)  # work that used the room
    service.set_limits(person, None, Limits(memory_mib=2048, cpu_percent=50))  # a raise
    assert stored(service, person) == Limits(memory_mib=2048, cpu_percent=50)


def test_a_value_equal_to_the_slices_cap_is_no_tightening_whatever_the_table_says(
    service, settings, runtime
):
    person = person_of(settings)
    service.set_limits(person, None, Limits(memory_mib=2048, pids=100))
    stored_only(service, person, Limits(memory_mib=8192, pids=900))  # the table differs
    plant(runtime, person, memory_mib=2049, tasks=101)  # the cache counts as use
    service.set_limits(person, None, Limits(memory_mib=2048, pids=100, cpu_percent=50))
    assert stored(service, person) == Limits(2048, 50, 100)


def test_the_caps_are_read_only_for_a_value_that_could_be_a_tightening(service, settings, runtime):
    person = person_of(settings)
    asked = []
    real = runtime.caps
    runtime.caps = lambda *a: asked.append(a) or real(*a)
    service.set_limits(person, None, Limits(cpu_percent=50))
    service.set_limits(person, None, Limits())
    assert asked == []
    service.set_limits(person, None, Limits(pids=100))
    assert asked == [(person.id, None)]


def test_caps_that_cannot_be_read_stop_a_value_that_could_be_a_tightening(
    service, settings, runtime
):
    person = person_of(settings)
    runtime.caps = lambda *a: (_ for _ in ()).throw(rt.RuntimeFailed("cannot read"))
    assert refusal(service.set_limits, person, None, Limits(pids=100)).code == "limits_not_applied"
    assert runtime.applied == [] and stored(service, person) == Limits()


# --- a use that cannot be told --------------------------------------------------------


def test_a_tightening_is_refused_when_the_use_of_what_is_lowered_is_not_known(
    service, settings, runtime
):
    person = person_of(settings)
    runtime.used[limits.person_slice(person.id)] = Usage(0, 5, memory_known=False)
    problem = refusal(service.set_limits, person, None, Limits(memory_mib=2048))
    assert (problem.code, problem.status) == ("usage_unknown", 409)
    assert runtime.applied == [] and stored(service, person) == Limits()
    # what is known is judged as ever, and a value that lowers nothing needs no use
    problem = refusal(service.set_limits, person, None, Limits(pids=4))
    assert problem.code == "limit_below_use"
    service.set_limits(person, None, Limits(pids=100, cpu_percent=50))
    runtime.used[limits.person_slice(person.id)] = Usage(1, 0, tasks_known=False)
    assert refusal(service.set_limits, person, None, Limits(pids=50)).code == "usage_unknown"
    service.set_limits(person, None, Limits(pids=100, cpu_percent=25))  # unchanged: allowed


def test_a_slice_that_is_inactive_is_zero_and_so_lets_a_limit_be_set(service, settings, runtime):
    person = person_of(settings)
    assert runtime.usage(person.id, None) == Usage(0, 0)
    service.set_limits(person, None, Limits(memory_mib=2048))
    assert stored(service, person).memory_mib == 2048


# --- an orphan's slice ---------------------------------------------------------------


def test_removing_a_container_no_row_is_for_drops_the_slice_it_was_in(service, settings, runtime):
    person = person_of(settings)
    insert(settings, person.id, "has-a-row")  # an empty table removes nothing (see the restore)
    runtime.create(rt.Spec(rt.container_name(999), person.slot, 999, person.id))
    runtime.apply_limits(person.id, 999, BIG)
    service.restore()
    assert not runtime.exists(rt.container_name(999))
    assert limits.workspace_slice(person.id, 999) in runtime.dropped
    assert limits.workspace_slice(person.id, 999) not in runtime.limits


# --- the report is bounded -------------------------------------------------------------


def test_the_report_gives_up_on_the_rest_once_its_time_is_used(
    service, settings, runtime, monkeypatch
):
    person = person_of(settings)
    for n in range(4):
        insert(settings, person.id, f"w{n}")
    monkeypatch.setattr(ws, "LIMITS_REPORT_SECONDS", 0.5)
    asked = []
    real = runtime.usage

    def slow(person_id, workspace):
        asked.append(workspace)
        time.sleep(0.3)
        return real(person_id, workspace)

    runtime.usage = slow
    report = service.limits_report(person)
    assert len(asked) == 2  # the person's and the first workspace's, then no time left
    assert report["person"]["usage"] is not None
    assert [w["usage"] is None for w in report["workspaces"]] == [False, True, True, True]
    assert all(w["limits"] == Limits().text() for w in report["workspaces"])  # still shown


def test_the_time_a_report_may_take_is_a_named_constant_longer_than_one_reading():
    assert ws.LIMITS_REPORT_SECONDS > rt.SYSTEMCTL_TIMEOUT


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
