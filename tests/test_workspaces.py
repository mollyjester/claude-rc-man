"""Workspaces: made, listed and deleted (backend/rcman/workspaces.py and its routes), and
what rcman brings back when it starts, on the host runtime."""

import contextlib
import dataclasses
import threading

import pytest
from rcman import db
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import (
    ALICE,
    BOB,
    as_user,
    changing,
    host_runtime,
    made_workspace,
    signed_in,
)
from tests.hang_guard import HANG_GUARD, runs_out
from tests.time_doubles import FakeTime
from tests.until import until


def listed(client, who=ALICE) -> list[dict]:
    return client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]


def row(settings, workspace_id: int):
    con = db.connect(settings.database)
    try:
        return con.execute("select * from workspaces where id = ?", (workspace_id,)).fetchone()
    finally:
        con.close()


# --- making and listing ----------------------------------------------------------------


def test_a_workspace_is_made_in_the_background_and_becomes_ready(client, app):
    release = threading.Event()
    runtime = app.state.workspaces.runtime
    real = runtime.create

    def slow(spec):
        release.wait(HANG_GUARD)
        real(spec)

    runtime.create = slow
    try:
        r = client.post("/api/workspaces", json={"name": "first"}, headers=changing(ALICE))
        assert r.status_code == 202, r.text
        made = r.json()["workspace"]
        assert (made["name"], made["state"], made["server"]) == ("first", "creating", None)
        assert [w["state"] for w in listed(client)] == ["creating"]
    finally:
        release.set()
    ready = made_workspace_ready(client, made["id"])
    assert ready["state"] == "ready"
    labels = runtime.labelled()[rt.container_name(made["id"])]
    assert labels == {rt.LABEL_WORKSPACE: str(made["id"]), rt.LABEL_PERSON: "1"}


def made_workspace_ready(client, workspace_id, who=ALICE) -> dict:
    def made():
        found = next(w for w in listed(client, who) if w["id"] == workspace_id)
        return None if found["state"] == "creating" else found

    return until(made)


def test_people_see_only_their_own_workspaces_and_each_runs_in_its_persons_slot(client, app):
    made = []
    runtime = app.state.workspaces.runtime
    real = runtime.create
    runtime.create = lambda spec: (made.append(spec), real(spec))
    mine = made_workspace(client, ALICE, "mine")
    two = made_workspace(client, ALICE, "two")
    theirs = made_workspace(client, BOB, "mine")  # a name is a person's own
    assert [(s.person, s.slot) for s in made] == [(1, 1), (1, 1), (2, 2)]
    assert {w["id"] for w in listed(client, ALICE)} == {mine["id"], two["id"]}
    assert [w["id"] for w in listed(client, BOB)] == [theirs["id"]]


# --- deleting --------------------------------------------------------------------------


def test_deleting_a_workspace_stops_its_server_and_removes_it(client, app):
    made = made_workspace(client, ALICE, "gone")
    signed_in(app, made)
    client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(ALICE))
    runtime = app.state.workspaces.runtime
    socket = runtime.layout(rt.container_name(made["id"])).tmux_socket
    assert __import__("os").path.exists(socket)  # positive control: its tmux runs
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert listed(client) == []
    assert runtime.labelled() == {}
    assert not __import__("os").path.exists(socket)


# --- what rcman brings back when it starts ---------------------------------------------


@pytest.fixture
def service(settings, runtime):
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


def insert(settings, person: int, name: str, state: str, wanted=0, server=0, prefix=None) -> int:
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state, wanted, server, prefix) "
            "values (?, ?, ?, ?, ?, ?)",
            (person, name, state, wanted, server, prefix),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def spec(workspace_id: int, person: int = 1) -> rt.Spec:
    return rt.Spec(
        rt.container_name(workspace_id), slot=person, workspace=workspace_id, person=person
    )


def test_restore_finishes_what_was_left_and_brings_back_what_was_lost(service, settings, runtime):
    """What was being made is made, and what was being deleted removed, a person being deleted
    with it; a lost workspace is made again and a stopped one started; a container no row is for
    is removed."""
    from rcman import people

    con = db.connect(settings.database)
    try:
        bob = people.by_email(con, "bob@example.com")
        people.mark_deleting(con, bob)
    finally:
        con.close()
    theirs = insert(settings, bob.id, "his", "deleting")
    runtime.create(spec(theirs, person=bob.id))
    creating = insert(settings, 1, "creating", "creating")
    deleting = insert(settings, 1, "deleting", "deleting")
    runtime.create(spec(deleting))
    lost = insert(settings, 1, "lost", "ready")
    stopped = insert(settings, 1, "stopped", "ready")
    runtime.create(spec(stopped))
    runtime.stop(rt.container_name(stopped))
    # A stopped workspace (halted) follows its container's real state: one whose container
    # runs, as after a restart of rcman that cut its Stop short, is ready again and keeps
    # running; one whose container is stopped is left as it is.
    halted_stopped = insert(settings, 1, "halted-stopped", "halted")
    runtime.create(spec(halted_stopped))
    runtime.stop(rt.container_name(halted_stopped))
    halted_running = insert(settings, 1, "halted-running", "halted")
    runtime.create(spec(halted_running))
    runtime.create(spec(999))  # no row is for it
    service.restore()
    assert row(settings, creating)["state"] == "ready"
    assert runtime.running(rt.container_name(creating))
    assert row(settings, deleting) is None
    assert not runtime.exists(rt.container_name(deleting))
    assert runtime.running(rt.container_name(lost))
    assert runtime.running(rt.container_name(stopped))
    assert row(settings, halted_stopped)["state"] == ws.HALTED
    assert not runtime.running(rt.container_name(halted_stopped))
    assert row(settings, halted_running)["state"] == ws.READY
    assert runtime.running(rt.container_name(halted_running))
    assert not runtime.exists(rt.container_name(999))
    assert not runtime.exists(rt.container_name(theirs))
    assert _person(settings, "bob@example.com") is None
    assert _person(settings) is not None  # positive control
    assert "bob@example.com" not in settings.allowed_emails.read_text()


def server_of(service, workspace_id):
    return service.server_status(service.get(workspace_id))


def test_restore_starts_a_wanted_server_with_its_prefix_and_leaves_a_running_one(
    service, settings, runtime
):
    from pathlib import Path

    from devtools import fake_claude as fake
    from devtools.world import record_sign_in

    wanted = insert(settings, 1, "wanted", "ready", wanted=1, server=1, prefix="pfx")
    running = insert(settings, 1, "running", "ready", wanted=1, server=1, prefix="pfx")
    unwanted = insert(settings, 1, "unwanted", "ready", wanted=0, server=0)
    for workspace_id in (wanted, running, unwanted):
        runtime.create(spec(workspace_id))
        layout = runtime.layout(rt.container_name(workspace_id))
        fake.write_state(Path(layout.config), logged_in=True, trusted=[layout.work])
    record_sign_in(settings.database, 1)
    service.start_server(service.get(running), "pfx")
    pid = service.server(service.get(running)).tmux.pane()[1]

    service.restore()
    assert server_of(service, wanted)["state"] in ("connected", "ready")
    assert service.server(service.get(running)).tmux.pane()[1] == pid  # left as it was
    assert server_of(service, unwanted) is None


def test_rcman_answers_while_it_restores(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    release = threading.Event()
    real = runtime.wait_ready
    runtime.wait_ready = lambda timeout: (release.wait(HANG_GUARD), real(timeout))
    app = create_app(settings, runtime=runtime)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/workspaces", headers=as_user(ALICE)).status_code == 200
        assert not app.state.workspaces.restored.is_set()
        release.set()
        assert app.state.workspaces.restored.wait(HANG_GUARD)


# --- channels --------------------------------------------------------------------------


def test_an_idle_channel_is_closed_and_the_next_request_opens_one(
    service, settings, runtime, monkeypatch
):
    workspace = insert(settings, 1, "idle", "ready")
    runtime.create(spec(workspace))
    service.server(service.get(workspace)).status()
    assert service.open_channels() == [rt.container_name(workspace)]
    runs_out(monkeypatch, ws, "CHANNEL_IDLE_SECONDS")  # idle for longer than none at all
    service.close_idle()
    assert service.open_channels() == []
    assert service.run_in(service.get(workspace))(["true"]).code == 0
    assert service.open_channels() == [rt.container_name(workspace)]


def test_each_workspace_is_listed_with_its_own_device_name_in_every_state(
    service, settings, runtime
):
    # The device name Claude shows for a workspace's server is its container's hostname, which
    # is the container's name; two workspaces in each state, so that a constant cannot pass.
    states = (ws.CREATING, ws.READY, ws.FAILED, ws.DELETING)
    made = [insert(settings, 1, f"{state}-{n}", state) for state in states for n in (1, 2)]
    for workspace in made:
        runtime.create(spec(workspace))
    listed = service.listing(service.everything())
    assert {w["state"] for w in listed} == set(states)
    assert [(w["id"], w["device"]) for w in listed] == [(i, f"rcman-ws-{i}") for i in made]
    assert len({w["device"] for w in listed}) == len(made)
    assert service.open_channels() == []  # listed without reaching a workspace with no server


def _person(settings, email="alice@example.com"):
    from rcman import people

    con = db.connect(settings.database)
    try:
        return people.by_email(con, email)
    finally:
        con.close()


# Routes that name a workspace and are not a person's own: an administrator's, behind the
# People page. They answer anyone but an administrator as admin_only, and an administrator's
# workspace of another person's than the person in the path as not found
# (tests/test_people.py, test_a_workspace_that_is_another_persons_is_not_found).
ADMINISTRATORS_WORKSPACE_ROUTES = {("PUT", "/api/people/{person}/workspaces/{workspace}/limits")}


def test_every_route_taking_a_workspace_answers_anothers_as_not_found(client, app):
    """Derived from the route table: each route with a workspace id, in its path, query or
    body, given alice's workspace by bob; but the administrators' own, which are still routes
    and still behind the administrators' gate."""
    import copy

    from rcman import routes

    from tests.conftest import CALLS, concrete
    from tests.test_problems import request_fields

    named = {(f.method, f.path) for f in request_fields(app) if f.name == "workspace"}
    admin = {
        (method, c.path) for c in routes.api_routes(app) if routes.admin_only(c.route)
        for method in c.methods
    }  # fmt: skip
    assert ADMINISTRATORS_WORKSPACE_ROUTES  # non-vacuity
    assert ADMINISTRATORS_WORKSPACE_ROUTES <= named  # stale-checked: each is still a route
    assert ADMINISTRATORS_WORKSPACE_ROUTES <= admin  # and still behind the administrators' gate
    taking = sorted(named - ADMINISTRATORS_WORKSPACE_ROUTES)
    assert ("POST", "/api/servers") in taking and (
        "DELETE",
        "/api/workspaces/{workspace}",
    ) in taking
    made = made_workspace(client, ALICE, "hers")
    for method, path in taking:
        kwargs = copy.deepcopy(CALLS[(method, path)])
        for where in ("json", "params"):
            if "workspace" in kwargs.get(where, {}):
                kwargs[where]["workspace"] = made["id"]
        url = concrete(path).replace("/1", f"/{made['id']}") if "{workspace}" in path else path
        r = client.request(method, url, headers=changing(BOB), **kwargs)
        assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found"), (method, path)
    assert [w["id"] for w in listed(client)] == [made["id"]]


# --- a server pins the Claude Code version it starts with -------------------------------------

FIRST, SECOND = "1.0.0", "1.1.0"  # the versions in the store; the first is conftest's FAKE_VERSION


@pytest.fixture
def pinned(tmp_path, runtime, settings):
    """The host runtime's store holding two versions, each recording the path it was run as in
    a log; the second was approved before the first, which is approved and current. Listed
    before ``client``."""
    from tests.conftest import fake_claude

    log = tmp_path / "ran"
    for version in (SECOND, FIRST):
        fake_claude(runtime.claude, version=version, before=f'echo "$0" >> {log}\n')
    for version in (SECOND, FIRST):
        approve(settings, version)
    return log


def approve(settings, version: str) -> None:
    """``version`` approved, as a rollout does it: a row, and the link current to it."""
    con = db.connect(settings.database)
    try:
        con.execute(
            "insert into claude_approvals (version, at, by) values (?, '2026-01-01', 'a')",
            (version,),
        )
        con.commit()
    finally:
        con.close()
    link = settings.claude_store / "current"
    link.unlink()
    link.symlink_to(f"versions/{version}")


def start(client, made, verb=""):
    path = "/api/servers" + (f"/{verb}" if verb else "")
    return client.post(path, json={"workspace": made["id"]}, headers=changing(ALICE))


def record_of(settings, workspace) -> str | None:
    return row(settings, workspace["id"])["claude_version"]


def test_a_start_records_the_version_it_runs_and_runs_that_exact_file(
    pinned, client, app, settings
):
    """And administrators see the version each server runs."""
    made = made_workspace(client, ALICE, "alpha")
    made_workspace(client, BOB, "beta")
    signed_in(app, made)
    assert record_of(settings, made) is None
    assert start(client, made).status_code == 200
    assert record_of(settings, made) == FIRST
    runtime = app.state.workspaces.runtime
    ran = pinned.read_text().split()
    assert ran and set(ran) == {str(runtime.claude / "versions" / FIRST)}  # not the link current
    people = {
        p["email"]: p
        for p in client.get("/api/servers/everyone", headers=as_user(ALICE)).json()["people"]
    }
    [alpha] = people[ALICE]["workspaces"]
    [beta] = people[BOB]["workspaces"]
    assert (alpha["claude_version"], beta["claude_version"]) == (FIRST, None)


def test_a_running_server_keeps_its_version_when_another_is_approved(pinned, client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    start(client, made)
    approve(settings, SECOND)
    assert record_of(settings, made) == FIRST
    shown = next(w for w in listed(client) if w["id"] == made["id"])
    assert shown["claude_version"] == FIRST
    assert client.get("/api/workspaces", headers=as_user(ALICE)).json()["claude_approved"] == SECOND
    # it starts again on the approved version only when its person stops and starts it
    assert start(client, made, "stop").status_code == 200
    assert record_of(settings, made) is None
    assert start(client, made).status_code == 200
    assert record_of(settings, made) == SECOND
    assert pinned.read_text().split()[-1] == str(
        app.state.workspaces.runtime.claude / "versions" / SECOND
    )


def test_a_lifecycle_command_runs_with_the_autoupdater_off_and_gh_at_the_persons_git_folder(
    runtime,
):
    """gh's folder whatever the plan says of it."""
    base = runtime.layout(rt.container_name(1))
    layout = dataclasses.replace(base, env={"GH_CONFIG_DIR": "/elsewhere", "A": "b"})
    env = ws.Workspaces._hook_env(layout)
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["A"] == "b"  # the plan's variables are there
    assert env["GH_CONFIG_DIR"] == f"{base.home}/{rt.GIT_HOME}" == rt.gh_config_dir(base)


# --- a deletion from another process (rcman-admin) stops what is being done ---------------

# The clone below is held by the test's runtime until a removal stops it, or else until the
# test is over: a deletion that does not stop it is not seen gone within the guard.


@pytest.fixture
def recording(tmp_path):
    from tests.test_clone import Recording

    with host_runtime(tmp_path, Recording) as made:
        yield made


@contextlib.contextmanager
def two_processes(settings, runtime):
    """The service and rcman-admin: two Workspaces on the same database and the same Podman,
    each with only its own threads and events, as two processes have."""
    service = ws.Workspaces(settings, runtime)
    shell = ws.Workspaces(settings, runtime, prunes=False)
    try:
        yield service, shell
    finally:
        shell.close()
        service.close()


def watchers(workspace_id: int) -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.name == f"{ws.WATCHER_PREFIX}{workspace_id}" and t.is_alive()
    ]


def gone(settings, workspace_id: int) -> None:
    """Until the workspace's row is gone."""
    until(lambda: row(settings, workspace_id) is None)


def test_a_deletion_from_another_process_stops_a_clone_promptly(settings, recording, quick_watch):
    """A clone the service runs, its git held until its container is removed, and rcman-admin
    deleting the workspace, which marks it deleting and has nothing of its own to stop: the
    service's watcher stops the git."""
    from tests.test_clone import URL

    recording.gate = threading.Event()
    with two_processes(settings, recording) as (service, shell):
        try:
            made = service.create(_person(settings), None, URL)
            assert recording.cloning.wait(HANG_GUARD)
            assert watchers(made.id)
            assert recording.removed == []  # positive control: nothing has removed it yet
            shell.delete(shell.get(made.id))
            assert shell._working == {}
            gone(settings, made.id)
        finally:
            recording.gate.set()
    assert made.container in recording.removed and not recording.exists(made.container)


# --- a person's deletion waits for what makes their workspaces -------------------------------


@pytest.fixture
def fake(monkeypatch):
    """The workspaces module's time, frozen unless the test moves it: only a person's deletion,
    in the test's own thread, reads it here."""
    made = FakeTime()
    monkeypatch.setattr(ws, "time", made)
    return made


@pytest.fixture
def timed(settings, runtime, changed, fake):
    """The service with the test's condition (``changed``) and time (``fake``)."""
    made = ws.Workspaces(settings, runtime)
    assert made._changed is changed
    yield made
    made.close()


def held_create(runtime) -> threading.Event:
    """Every container's making held until the event is set."""
    held, real = threading.Event(), runtime.create
    runtime.create = lambda spec: (held.wait(HANG_GUARD), real(spec))
    return held


def deleting_person(service, person) -> tuple[threading.Thread, list]:
    """``delete_person`` in a thread of its own: the thread, and what it ended in."""
    from rcman.problems import Problem

    out: list = []

    def run():
        try:
            service.delete_person(person)
            out.append(None)
        except Problem as refused:
            out.append(refused)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def joined(thread: threading.Thread) -> None:
    thread.join(HANG_GUARD)
    assert not thread.is_alive()


def waits_of(double, thread) -> list:
    """The waits recorded on ``double`` by ``thread``."""
    return [w for w in double.waits if w.thread is thread]


def test_deleting_a_person_waits_for_their_making_thread(timed, changed, settings, runtime):
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "own")
    thread, out = deleting_person(timed, bob)
    changed.wait_waiting(1, match=thread)
    assert thread.is_alive()
    held.set()
    joined(thread)
    assert out == [None] and row(settings, made.id) is None
    assert _person(settings, BOB) is None


# --- close ---------------------------------------------------------------------------------


@pytest.fixture
def on_changed(settings, runtime, changed):
    """The service, its condition the test's (``changed``)."""
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


def test_close_waits_for_a_making_thread(on_changed, changed, settings, runtime):
    timed = on_changed
    held = held_create(runtime)
    made = timed.create(_person(settings), "slow")
    closing = threading.Thread(target=timed.close, daemon=True)
    closing.start()
    changed.wait_waiting(1, match=closing)
    assert closing.is_alive()
    held.set()
    joined(closing)
    with timed._changed:
        assert timed._unended == set()
    assert row(settings, made.id)["state"] == ws.READY


# --- the periodic checks ---------------------------------------------------------------------


def test_the_periodic_checks_run_once_per_pace_and_end_at_once_with_the_service(
    settings, runtime, periodic, monkeypatch
):
    """One round each time the wait of IDLE_CHECK_SECONDS runs out, none before. The twin a
    second short of the pace is given up: the wait never runs out by itself, so its seconds stand
    in."""
    service = ws.Workspaces(settings, runtime)
    assert service._closing is periodic
    rounds = []
    monkeypatch.setattr(service, "close_idle", lambda: rounds.append(len(rounds)))
    try:
        service.start_background()
        [waiter] = periodic.wait_waiting(1, match=ws.IDLE_CHECK_SECONDS)
        assert rounds == []  # none before its pace has passed
        periodic.expire(ws.IDLE_CHECK_SECONDS)
        periodic.wait_waiting(1, match=ws.IDLE_CHECK_SECONDS)
        assert rounds == [0]
        periodic.expire(ws.IDLE_CHECK_SECONDS)
        periodic.wait_waiting(1, match=ws.IDLE_CHECK_SECONDS)
        assert rounds == [0, 1]
    finally:
        service.close()
    waiter.thread.join(HANG_GUARD)
    assert not waiter.thread.is_alive() and len(rounds) == 2
    assert [w.ended for w in waits_of(periodic, waiter.thread)] == ["expired", "expired", "set"]


# --- stopping and starting a workspace --------------------------------------------------------


def power(client, made: dict, verb: str, who=ALICE):
    """POST the workspace's ``verb``: stop or start."""
    return client.post(f"/api/workspaces/{made['id']}/{verb}", headers=changing(who))


def entry_of(client, made: dict, who=ALICE) -> dict:
    return next(w for w in listed(client, who) if w["id"] == made["id"])


def audited(settings, kind: str = "workspace.") -> list[tuple]:
    """The audit's rows of actions that begin with ``kind``, oldest first."""
    con = db.connect(settings.database)
    try:
        rows = con.execute(
            "select action, email, target, result from audit where action like ? order by id",
            (kind + "%",),
        ).fetchall()
    finally:
        con.close()
    return [tuple(r) for r in rows]


def test_a_workspace_stopped_and_started_keeps_its_server_and_its_sessions(
    pinned, client, app, settings, monkeypatch
):
    from tests.test_terminals import Opened

    service, runtime = app.state.workspaces, app.state.workspaces.runtime
    made = made_workspace(client, ALICE, "alpha")
    name = rt.container_name(made["id"])
    signed_in(app, made, trusted=True)
    assert start(client, made).status_code == 200

    def named() -> dict | None:
        found = entry_of(client, made)
        server = found["server"]
        ready = found["state"] == "ready" and not found["starting"]
        return server if ready and server and server.get("session_name") else None

    session = until(named)["session_name"]
    opened = Opened(client, made["id"], 1)
    ends: list = []
    forward, real_end = app.state.forward, app.state.forward._end_from_thread

    def end_from_thread(which, result, workspace, person=None):
        ends.append((result, workspace))
        return real_end(which, result, workspace, person=person)

    monkeypatch.setattr(forward, "_end_from_thread", end_from_thread)
    assert service.run_in(service.get(made["id"]))(["true"]).code == 0  # opens its channel
    assert name in service.open_channels()

    r = power(client, made, "stop")
    assert r.status_code == 200, r.text
    shown = r.json()["workspace"]
    assert (shown["state"], shown["stoppable"], shown["startable"]) == ("halted", False, True)
    shown = entry_of(client, made)
    assert (shown["state"], shown["startable"], shown["server"]) == ("halted", True, None)
    assert shown["server_error"]["code"] == "workspace_stopped"
    assert not runtime.running(name)
    now = row(settings, made["id"])
    assert (now["state"], now["wanted"], now["claude_version"]) == ("halted", 1, None)
    assert row(settings, made["id"])["wanted"] == 1  # it still runs again at a Start
    opened.ended()
    assert opened.terminal.reason == "workspace_stopped"
    assert ("workspace_stopped", made["id"]) in ends
    assert name not in service.open_channels()

    approve(settings, SECOND)
    r = power(client, made, "start")
    assert r.status_code == 202, r.text
    assert until(named)["session_name"] == session
    assert record_of(settings, made) == SECOND  # the version approved now
    assert runtime.running(name)
    assert audited(settings)[-2:] == [
        ("workspace.stop", ALICE, "alpha", "ok"),
        ("workspace.start", ALICE, "alpha", "ok"),
    ]

    # An administrator stops anyone's workspace, as themselves.
    his = made_workspace(client, BOB, "beta")
    r = power(client, his, "stop")
    assert r.status_code == 200, r.text
    theirs = r.json()["workspace"]
    assert (theirs["state"], theirs["stoppable"], theirs["stopping"]) == ("halted", False, False)
    assert not {"log", "source", "error", "rebuild", "ports"} & set(theirs)  # the Everyone page's
    assert not runtime.running(rt.container_name(his["id"]))
    assert audited(settings)[-1] == ("workspace.stop", ALICE, f"{BOB}/beta", "ok")
    assert entry_of(client, his, BOB)["state"] == "halted"
