"""Workspaces: made, listed and deleted (backend/rcman/workspaces.py and its routes), and
what rcman brings back when it starts, on the host runtime."""

import contextlib
import dataclasses
import threading
import time

import pytest
from rcman import db
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import (
    ALICE,
    BOB,
    MADE_WITHIN,
    as_user,
    changing,
    host_runtime,
    made_workspace,
    signed_in,
)


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
        release.wait(30)
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
    deadline = time.monotonic() + MADE_WITHIN
    while True:
        found = next(w for w in listed(client, who) if w["id"] == workspace_id)
        if found["state"] != "creating":
            return found
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_every_workspace_of_a_person_runs_in_their_slot(client, app):
    made = []
    runtime = app.state.workspaces.runtime
    real = runtime.create
    runtime.create = lambda spec: (made.append(spec), real(spec))
    made_workspace(client, ALICE, "one")
    made_workspace(client, ALICE, "two")
    made_workspace(client, BOB, "his")
    assert [(s.person, s.slot) for s in made] == [(1, 1), (1, 1), (2, 2)]


def test_people_see_only_their_own_workspaces(client):
    mine = made_workspace(client, ALICE, "mine")
    theirs = made_workspace(client, BOB, "mine")  # a name is a person's own
    assert [w["id"] for w in listed(client, ALICE)] == [mine["id"]]
    assert [w["id"] for w in listed(client, BOB)] == [theirs["id"]]


def test_a_name_taken_is_refused(client):
    made_workspace(client, ALICE, "twice")
    r = client.post("/api/workspaces", json={"name": "twice"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "workspace_name_taken")


@pytest.mark.parametrize("name", ["", "-x", "a b", "a/b", "x" * 33, "été"])
def test_a_name_the_rule_refuses_is_refused(client, name):
    from helpers import rules

    r = client.post("/api/workspaces", json={"name": name}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "name", "message": rules.WORKSPACE_NAME.sentence}]


def test_a_workspace_that_could_not_be_made_is_failed_and_can_be_deleted(client, app):
    runtime = app.state.workspaces.runtime

    def broken(spec):
        raise rt.RuntimeFailed("the image is not there")

    runtime.create = broken
    r = client.post("/api/workspaces", json={"name": "broken"}, headers=changing(ALICE))
    failed = made_workspace_ready(client, r.json()["workspace"]["id"])
    assert failed["state"] == "failed"
    assert "the image is not there" in failed["error"]
    r = client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert listed(client) == []


def git_answering(runtime, code: int, out: bytes, err: bytes = b""):
    """``runtime`` with what the workspace's ``git --version`` says replaced; every other
    command is run as before. The commands that asked are the returned list."""
    asked = []
    real = runtime.exec

    def exec_(name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT, cwd=None, keep=None, again=False):
        if argv and argv[-1] == "--version" and "git" in argv:
            asked.append(argv)
            return rt.Result(code, out, err)
        return real(name, argv, stdin, timeout, cwd, keep, again)

    runtime.exec = exec_
    return asked


def test_an_image_with_a_new_enough_git_makes_a_ready_workspace(client, app):
    asked = git_answering(app.state.workspaces.runtime, 0, b"git version 2.31.0\n")
    r = client.post("/api/workspaces", json={"name": "fine"}, headers=changing(ALICE))
    assert made_workspace_ready(client, r.json()["workspace"]["id"])["state"] == "ready"
    assert asked  # positive control for the refusals below: git was asked its version


def test_an_image_without_git_makes_a_failed_workspace_that_says_so(client, app):
    asked = git_answering(app.state.workspaces.runtime, 127, b"", b"timeout: can't execute 'git'")
    r = client.post("/api/workspaces", json={"name": "nogit"}, headers=changing(ALICE))
    failed = made_workspace_ready(client, r.json()["workspace"]["id"])
    assert asked and failed["state"] == "failed"
    assert "git is not installed" in failed["error"]
    assert (
        client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE)).status_code == 200
    )


def test_an_image_with_an_older_git_than_rcman_needs_makes_a_failed_workspace(client, app):
    from helpers import git_run

    minimum = ".".join(str(n) for n in git_run.GIT_MIN_VERSION)
    older = f"git version {git_run.GIT_MIN_VERSION[0]}.{git_run.GIT_MIN_VERSION[1] - 1}.9\n"
    git_answering(app.state.workspaces.runtime, 0, older.encode())
    r = client.post("/api/workspaces", json={"name": "old"}, headers=changing(ALICE))
    failed = made_workspace_ready(client, r.json()["workspace"]["id"])
    assert failed["state"] == "failed"
    assert older.split()[2].rsplit(".", 1)[0] in failed["error"] and minimum in failed["error"]


def test_an_image_whose_git_says_something_else_makes_a_failed_workspace(client, app):
    git_answering(app.state.workspaces.runtime, 0, b"hello\n")
    r = client.post("/api/workspaces", json={"name": "odd"}, headers=changing(ALICE))
    failed = made_workspace_ready(client, r.json()["workspace"]["id"])
    assert failed["state"] == "failed" and "git" in failed["error"]


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


def test_no_one_reaches_anothers_workspace(client):
    made = made_workspace(client, ALICE, "hers")
    for call in (
        lambda: client.delete(f"/api/workspaces/{made['id']}", headers=changing(BOB)),
        lambda: client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(BOB)),
        lambda: client.get(
            "/api/servers/output", params={"workspace": made["id"]}, headers=as_user(BOB)
        ),
    ):
        r = call()
        assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    assert [w["id"] for w in listed(client)] == [made["id"]]


def test_a_workspace_deleted_while_it_is_made_is_removed_once_it_is(client, app):
    release = threading.Event()
    runtime = app.state.workspaces.runtime
    real = runtime.create

    def slow(spec):
        release.wait(30)
        real(spec)

    runtime.create = slow
    r = client.post("/api/workspaces", json={"name": "brief"}, headers=changing(ALICE))
    made = r.json()["workspace"]
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert [w["state"] for w in listed(client)] in ([], ["deleting"])
    release.set()
    deadline = time.monotonic() + MADE_WITHIN
    while listed(client) or runtime.labelled():
        assert time.monotonic() < deadline, (listed(client), runtime.labelled())
        time.sleep(0.05)


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


def test_restore_finishes_what_was_being_made_and_deleted(service, settings, runtime):
    creating = insert(settings, 1, "creating", "creating")
    deleting = insert(settings, 1, "deleting", "deleting")
    runtime.create(spec(deleting))
    service.restore()
    assert row(settings, creating)["state"] == "ready"
    assert runtime.running(rt.container_name(creating))
    assert row(settings, deleting) is None
    assert not runtime.exists(rt.container_name(deleting))


def test_restore_makes_again_a_lost_workspace_and_starts_a_stopped_one(service, settings, runtime):
    lost = insert(settings, 1, "lost", "ready")
    stopped = insert(settings, 1, "stopped", "ready")
    runtime.create(spec(stopped))
    runtime.stop(rt.container_name(stopped))
    service.restore()
    assert runtime.running(rt.container_name(lost))
    assert runtime.running(rt.container_name(stopped))


def test_restore_removes_a_workspace_with_no_row(service, settings, runtime):
    kept = insert(settings, 1, "kept", "ready")
    runtime.create(spec(kept))
    runtime.create(spec(999))
    service.restore()
    assert list(runtime.labelled()) == [rt.container_name(kept)]


def server_of(service, workspace_id):
    return service.server_status(service.get(workspace_id))


def test_restore_starts_a_wanted_server_with_its_prefix_and_leaves_a_running_one(
    service, settings, runtime
):
    from pathlib import Path

    from devtools import fake_claude as fake
    from tests.e2e_server import record_sign_in

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


def test_restore_waits_for_the_runtime(settings, tmp_path):
    from devtools.held_folders import tmux_sockets
    from tests.conftest import fake_claude

    root = tmp_path / "later"
    with tmux_sockets("rcw-") as sockets:
        fake_claude(tmp_path / "claude")
        runtime = rt.HostRuntime(root, sockets, tmp_path / "claude", tmp_path / "x")
        service = ws.Workspaces(settings, runtime)
        creating = insert(settings, 1, "creating", "creating")
        done = threading.Thread(target=service.restore)
        done.start()
        time.sleep(1.5)
        assert done.is_alive() and row(settings, creating)["state"] == "creating"
        root.mkdir()  # the runtime answers from now on
        done.join(60)
        assert not done.is_alive()
        assert row(settings, creating)["state"] == "ready"
        service.close()
        for name in runtime.labelled():
            runtime.remove(name)


def test_rcman_answers_while_it_restores(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    release = threading.Event()
    real = runtime.wait_ready
    runtime.wait_ready = lambda timeout: (release.wait(30), real(timeout))
    app = create_app(settings, runtime=runtime)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/workspaces", headers=as_user(ALICE)).status_code == 200
        assert not app.state.workspaces.restored.is_set()
        release.set()
        assert app.state.workspaces.restored.wait(30)


# --- channels --------------------------------------------------------------------------


def test_an_idle_channel_is_closed(service, settings, runtime, monkeypatch):
    workspace = insert(settings, 1, "idle", "ready")
    runtime.create(spec(workspace))
    service.server(service.get(workspace)).status()
    assert service.open_channels() == [rt.container_name(workspace)]
    monkeypatch.setattr(ws, "CHANNEL_IDLE_SECONDS", 0.1)
    time.sleep(0.2)
    service.close_idle()
    assert service.open_channels() == []


def test_a_workspace_with_no_server_is_listed_without_reaching_it(service, settings, runtime):
    workspace = insert(settings, 1, "quiet", "ready")
    runtime.create(spec(workspace))
    assert service.server_status(service.get(workspace)) is None
    assert service.open_channels() == []


def test_restore_finishes_deleting_a_person(service, settings, runtime):
    from rcman import people

    con = db.connect(settings.database)
    try:
        bob = people.by_email(con, "bob@example.com")
        people.mark_deleting(con, bob)
    finally:
        con.close()
    theirs = insert(settings, bob.id, "his", "deleting")
    runtime.create(spec(theirs, person=bob.id))
    service.restore()
    con = db.connect(settings.database)
    try:
        assert people.by_email(con, "bob@example.com") is None
        assert people.by_email(con, "alice@example.com") is not None  # positive control
    finally:
        con.close()
    assert not runtime.exists(rt.container_name(theirs))
    assert "bob@example.com" not in settings.allowed_emails.read_text()


def _person(settings, email="alice@example.com"):
    from rcman import people

    con = db.connect(settings.database)
    try:
        return people.by_email(con, email)
    finally:
        con.close()


def test_a_workspace_made_while_restore_runs_is_left_to_be_made(service, settings, runtime):
    """Restore neither removes nor makes again what a request's thread is making."""
    release, real_create, real_remove = threading.Event(), runtime.create, runtime.remove
    calls = []

    def create(spec):
        calls.append(("create", spec.name))
        release.wait(30)
        real_create(spec)

    def remove(name):
        calls.append(("remove", name))
        real_remove(name)

    runtime.create, runtime.remove = create, remove
    made = service.create(_person(settings), "racing")  # its own thread is making it
    restoring = threading.Thread(target=service.restore)
    restoring.start()
    restoring.join(3)  # restore has run past it, unless it waits on a create of its own
    release.set()
    restoring.join(60)
    service._making[made.id].join(30)
    name = rt.container_name(made.id)
    assert [c for c in calls if c[1] == name] == [("create", name)]
    assert service.get(made.id).state == ws.READY
    assert runtime.running(name)


def test_restore_never_removes_a_workspace_whose_row_appeared_meanwhile(service, settings, runtime):
    real = runtime.labelled
    made = {}

    def labelled():
        found = real()
        if not made:  # a workspace is made after restore read the table
            made["it"] = service.create(_person(settings), "late")
            service._making[made["it"].id].join(30)
            found = real()
        return found

    runtime.labelled = labelled
    service.restore()
    assert rt.container_name(made["it"].id) in real()
    assert service.get(made["it"].id).state == ws.READY


def test_the_last_workspace_of_a_person_being_deleted_finishes_their_deletion(
    service, settings, runtime
):
    from rcman import people

    bob = _person(settings, "bob@example.com")
    release, real = threading.Event(), runtime.create
    runtime.create = lambda spec: (release.wait(30), real(spec))
    made = service.create(bob, "last")
    con = db.connect(settings.database)
    try:
        people.mark_deleting(con, bob)
    finally:
        con.close()
    service.delete(service.get(made.id))
    release.set()
    service._making[made.id].join(30)
    assert service.get(made.id) is None
    assert _person(settings, "bob@example.com") is None
    assert _person(settings) is not None  # positive control


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_removal_that_fails_after_making_leaves_the_workspace_to_restore(
    service, settings, runtime, monkeypatch, caplog
):
    release, real = threading.Event(), runtime.create
    runtime.create = lambda spec: (release.wait(30), real(spec))
    made = service.create(_person(settings), "stuck")
    service.delete(service.get(made.id))

    def refusing(name):
        raise rt.RuntimeFailed("planted: it will not go")

    monkeypatch.setattr(runtime, "remove", refusing)
    release.set()
    service._making[made.id].join(30)
    assert service.get(made.id).state == ws.DELETING  # left for restore to finish
    assert "planted: it will not go" in caplog.text


def test_no_workspace_is_made_for_a_person_being_deleted(service, settings):
    from rcman import people
    from rcman.problems import Problem

    bob = _person(settings, "bob@example.com")
    con = db.connect(settings.database)
    try:
        people.mark_deleting(con, bob)
    finally:
        con.close()
    with pytest.raises(Problem) as refused:
        service.create(bob, "late")
    assert refused.value.code == "not_allowed"
    assert service.of_person(bob.id) == []


# Routes that name a workspace and are not a person's own: an administrator's, behind the
# People page. They answer anyone but an administrator as admin_only, and an administrator's
# workspace of another person's than the person in the path as not found
# (tests/test_people.py, test_a_workspace_that_is_another_persons_or_not_there_is_not_found).
ADMINISTRATORS_WORKSPACE_ROUTES = {("PUT", "/api/people/{person}/workspaces/{workspace}/limits")}


def test_the_routes_that_name_a_workspace_and_are_not_a_persons_own_are_administrators(app):
    from rcman import routes

    from tests.test_problems import request_fields

    named = {(f.method, f.path) for f in request_fields(app) if f.name == "workspace"}
    admin = {
        (method, c.path) for c in routes.api_routes(app) if routes.admin_only(c.route)
        for method in c.methods
    }  # fmt: skip
    assert ADMINISTRATORS_WORKSPACE_ROUTES  # non-vacuity
    assert ADMINISTRATORS_WORKSPACE_ROUTES <= named  # stale-checked: each is still a route
    assert ADMINISTRATORS_WORKSPACE_ROUTES <= admin  # and still behind the administrators' gate


def test_every_route_taking_a_workspace_answers_anothers_as_not_found(client, app):
    """Derived from the route table: each route with a workspace id, in its path, query or
    body, given alice's workspace by bob; but the administrators' own."""
    import copy

    from tests.conftest import CALLS, concrete
    from tests.test_problems import request_fields

    taking = sorted(
        {(f.method, f.path) for f in request_fields(app) if f.name == "workspace"}
        - ADMINISTRATORS_WORKSPACE_ROUTES
    )
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
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    assert record_of(settings, made) is None
    assert start(client, made).status_code == 200
    assert record_of(settings, made) == FIRST
    runtime = app.state.workspaces.runtime
    ran = pinned.read_text().split()
    assert ran and set(ran) == {str(runtime.claude / "versions" / FIRST)}  # not the link current


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


def test_a_start_that_is_refused_because_a_server_runs_leaves_the_record_alone(
    pinned, client, app, settings
):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    start(client, made)
    approve(settings, SECOND)
    r = start(client, made)
    assert (r.status_code, r.json()["code"]) == (409, "already_running")
    assert record_of(settings, made) == FIRST  # not overwritten by the approved version
    r = start(client, made, "resume")  # nothing stopped to resume
    assert r.json()["code"] == "not_stopped"
    assert record_of(settings, made) == FIRST


def test_a_resume_runs_the_approved_version(pinned, client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made, trusted=True)
    start(client, made)
    start(client, made, "stop")
    approve(settings, SECOND)
    assert start(client, made, "resume").status_code == 200
    assert record_of(settings, made) == SECOND


def test_a_stop_and_a_forget_clear_the_record(pinned, client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    start(client, made)
    assert record_of(settings, made) == FIRST
    start(client, made, "stop")
    assert record_of(settings, made) is None
    start(client, made, "resume")
    assert record_of(settings, made) == FIRST
    start(client, made, "stop")
    assert start(client, made, "forget").status_code == 200
    assert record_of(settings, made) is None


def test_a_server_that_would_not_stop_keeps_its_record(pinned, client, app, settings, monkeypatch):
    from rcman import claude_server as cs

    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    start(client, made)

    def timing_out(self):
        raise cs.ServerError("timeout")

    monkeypatch.setattr(cs.Server, "stop", timing_out)
    assert start(client, made, "stop").json()["code"] == "timeout"
    assert record_of(settings, made) == FIRST  # it is still running that version


def test_a_start_that_ends_with_the_server_not_running_clears_the_record(
    pinned, client, app, settings, tmp_path
):
    from devtools import fake_claude as fake
    from tests.conftest import fake_claude

    signed_out = tmp_path / "signed-out"
    fake.write_state(signed_out, logged_in=False)
    fake_claude(
        app.state.workspaces.runtime.claude,
        version=FIRST,
        before=f'if [ "$1" = remote-control ]; then export CLAUDE_CONFIG_DIR={signed_out}; fi\n',
    )
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    r = start(client, made)
    assert r.json()["server"]["state"] == "logged_out"
    assert row(settings, made["id"])["server"] == 1  # the ended server is still there to see
    assert record_of(settings, made) is None


def test_a_start_that_is_refused_before_a_server_exists_records_nothing(
    pinned, client, app, settings
):
    made = made_workspace(client, ALICE, "alpha")  # not signed in to Claude in the workspace
    from tests.e2e_server import record_sign_in

    record_sign_in(settings.database, 1)
    r = start(client, made)
    assert r.json()["code"] == "claude_not_logged_in"
    assert record_of(settings, made) is None


def test_a_start_with_the_approved_version_missing_from_the_store_is_not_installed(
    pinned, client, app, settings
):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    (app.state.workspaces.runtime.claude / "versions" / FIRST).unlink()
    r = start(client, made)
    assert (r.status_code, r.json()["code"]) == (409, "claude_not_installed")
    assert record_of(settings, made) is None
    assert app.state.workspaces.server_status(app.state.workspaces.get(made["id"])) is None


def test_the_server_is_started_through_the_layout_pinned_to_the_version(pinned, client, app):
    from rcman import claude_server as cs

    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    seen = []
    real = cs.Server.__init__

    def recording(self, layout, run, workspace):
        seen.append(layout.claude)
        real(self, layout, run, workspace)

    cs.Server.__init__ = recording
    try:
        start(client, made)
    finally:
        cs.Server.__init__ = real
    runtime = app.state.workspaces.runtime
    assert str(runtime.claude / "versions" / FIRST) in seen  # the one the start ran
    assert str(runtime.claude / "bin" / "claude") in seen  # the others read, never run it


def test_administrators_see_the_version_each_server_runs(pinned, client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    made_workspace(client, BOB, "beta")
    signed_in(app, made)
    start(client, made)
    people = {
        p["email"]: p
        for p in client.get("/api/servers/everyone", headers=as_user(ALICE)).json()["people"]
    }
    [alpha] = people[ALICE]["workspaces"]
    [beta] = people[BOB]["workspaces"]
    assert (alpha["claude_version"], beta["claude_version"]) == (FIRST, None)


def test_a_lifecycle_command_gets_the_autoupdater_turned_off(runtime):
    layout = runtime.layout(rt.container_name(1))
    env = ws.Workspaces._hook_env(layout)
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_a_lifecycle_command_has_gh_pointed_at_the_persons_git_folder_whatever_the_plan_says(
    runtime,
):
    base = runtime.layout(rt.container_name(1))
    layout = dataclasses.replace(base, env={"GH_CONFIG_DIR": "/elsewhere", "A": "b"})
    env = ws.Workspaces._hook_env(layout)
    assert env["A"] == "b"  # the plan's variables are there
    assert env["GH_CONFIG_DIR"] == f"{base.home}/{rt.GIT_HOME}" == rt.gh_config_dir(base)


def test_a_restore_runs_its_hooks_after_the_workspaces_and_past_one_that_fails(
    client, app, monkeypatch
):
    service = app.state.workspaces
    seen = []
    restore_all, finish = service._restore_all, service._finish_deleting_people
    monkeypatch.setattr(service, "_restore_all", lambda: (seen.append("workspaces"), restore_all()))
    monkeypatch.setattr(
        service, "_finish_deleting_people", lambda: (seen.append("people"), finish())
    )

    def broken():
        seen.append("broken")
        raise RuntimeError("planted")

    monkeypatch.setattr(service, "restore_hooks", [broken, lambda: seen.append("next")])
    service.restore()
    assert seen == ["workspaces", "broken", "next", "people"]


# --- a deletion from another process (rcman-admin) stops what is being done ---------------

# How soon work a deletion from another process stops must have ended: each held step below
# would otherwise run on for MADE_WITHIN, until its gate gives up.
STOPPED_WITHIN = 10.0
# How long the planted twin watches a clone that no watcher stops: a great many of the
# watcher's looks at the row, as quick_watch sets them.
UNWATCHED_FOR = 0.5


@pytest.fixture
def quick_watch(monkeypatch):
    monkeypatch.setattr(ws, "WORKING_POLL_SECONDS", 0.02)


@pytest.fixture
def recording(tmp_path):
    from tests.test_clone import Recording

    with host_runtime(tmp_path, Recording) as made:
        yield made


@pytest.fixture
def flow(tmp_path):
    from tests.test_devcontainer_flow import Flow

    with host_runtime(tmp_path, Flow) as made:
        yield made


@pytest.fixture
def publish(github_stand_in, tmp_path):
    """``publish(files)``: the repository the clone tests clone, with ``files`` (name -> text)
    committed in it."""
    from tests.git_fixtures import fixture_git, make_repo

    def publish(files: dict) -> None:
        repo = make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")
        for name, content in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(content)
        fixture_git(repo, "add", "-A")
        fixture_git(repo, "commit", "-q", "-m", "A devcontainer")
        fixture_git(repo, "push", "-q", "origin", "main")

    return publish


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


def gone_within(settings, workspace_id: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while row(settings, workspace_id) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def clone_deleted_elsewhere(settings, recording, within: float) -> tuple[bool, bool, str]:
    """A clone the service runs, its git held until its container is removed, and rcman-admin
    deleting the workspace: whether a watcher watched it, whether it was gone within
    ``within`` seconds, and its container's name."""
    from tests.test_clone import URL

    recording.gate = threading.Event()
    with two_processes(settings, recording) as (service, shell):
        try:
            made = service.create(_person(settings), None, URL)
            assert recording.cloning.wait(MADE_WITHIN)
            watched = bool(watchers(made.id))
            assert recording.removed == []  # positive control: nothing has removed it yet
            shell.delete(shell.get(made.id))  # marks it deleting: nothing of its own to stop
            assert shell._working == {}
            stopped = gone_within(settings, made.id, within)
        finally:
            recording.gate.set()
        assert gone_within(settings, made.id, MADE_WITHIN)
    return watched, stopped, made.container


def test_a_deletion_from_another_process_stops_a_clone_promptly(settings, recording, quick_watch):
    watched, stopped, name = clone_deleted_elsewhere(settings, recording, STOPPED_WITHIN)
    assert watched and stopped
    assert name in recording.removed and not recording.exists(name)


def test_the_planted_twin_without_the_watcher_a_clone_deleted_elsewhere_runs_on(
    settings, recording, quick_watch, monkeypatch
):
    monkeypatch.setattr(ws.Workspaces, "_watch", lambda self, *args: None)
    watched, stopped, name = clone_deleted_elsewhere(settings, recording, UNWATCHED_FOR)
    assert not watched  # the plant took effect: no watcher
    assert not stopped  # and the guard fires: git ran on, its workspace deleted
    assert not recording.exists(name)  # once let go, the service finished the deletion


def deleted_elsewhere(settings, flow, made_by, held) -> tuple[int, float]:
    """The service making a workspace until ``held`` is set, and rcman-admin deleting it then:
    the workspace, and how long after the deletion it was gone."""
    with two_processes(settings, flow) as (service, shell):
        made = made_by(service)
        assert held.wait(MADE_WITHIN)
        assert watchers(made)  # positive control: the service watches it
        began = time.monotonic()
        shell.delete(shell.get(made))
        assert gone_within(settings, made, STOPPED_WITHIN)
        took = time.monotonic() - began
    return made, took


def cloned_by(publish, files):
    from tests.test_clone import URL

    publish(files)
    return lambda service: service.create(_person(service.settings), None, URL).id


def test_a_deletion_from_another_process_stops_a_pull(settings, flow, publish, quick_watch):
    from tests.test_devcontainer_flow import IMAGE, config

    flow.pull_gate = threading.Event()
    try:
        made, took = deleted_elsewhere(settings, flow, cloned_by(publish, config()), flow.pulling)
    finally:
        flow.pull_gate.set()
    assert took < STOPPED_WITHIN
    assert flow.pulled == []  # the pull did not finish
    assert not [e for e in flow.events if e[0] == "create" and e[1] == IMAGE]
    assert flow.labelled() == {} and not (flow.root / rt.container_name(made)).exists()


def test_a_deletion_from_another_process_stops_a_build_and_removes_the_builder(
    settings, flow, publish, quick_watch
):
    from tests.test_devcontainer_flow import building

    flow.build_gate = threading.Event()
    try:
        made, took = deleted_elsewhere(
            settings, flow, cloned_by(publish, building()), flow.building
        )
    finally:
        flow.build_gate.set()
    assert took < STOPPED_WITHIN
    assert ("remove_build", made, False) in flow.events  # the watcher removed the builder
    assert flow.builders() == set() and flow.built_images() == {}
    assert not [e for e in flow.events if e[0] == "create" and e[1]]


def test_a_deletion_from_another_process_stops_a_lifecycle_command(
    settings, flow, publish, quick_watch
):
    from tests.test_devcontainer_flow import config

    flow.gate = threading.Event()
    try:
        made, took = deleted_elsewhere(
            settings, flow, cloned_by(publish, config(postCreateCommand="echo GATE")), flow.hooking
        )
    finally:
        flow.gate.set()
    assert took < STOPPED_WITHIN
    name = rt.container_name(made)
    assert ("remove", name) in flow.events
    assert flow.labelled() == {} and not (flow.root / name).exists()


def test_a_deletion_from_another_process_stops_a_restore_remaking_a_container(
    settings, flow, quick_watch
):
    from tests.test_devcontainer_flow import IMAGE, a_plan, ready_with_plan, restoring

    flow.pull_gate = threading.Event()
    with two_processes(settings, flow) as (service, shell):
        made = ready_with_plan(service, settings, flow, a_plan(), make_container=False)
        thread = restoring(service)
        try:
            assert flow.pulling.wait(MADE_WITHIN)
            assert watchers(made)  # positive control: the remaking is watched
            began = time.monotonic()
            shell.delete(shell.get(made))  # a ready one: rcman-admin removes it itself
            assert row(settings, made) is None
            thread.join(STOPPED_WITHIN)
            assert not thread.is_alive() and time.monotonic() - began < STOPPED_WITHIN
        finally:
            flow.pull_gate.set()
            thread.join(MADE_WITHIN)
    name = rt.container_name(made)
    assert flow.pulled == []  # the pull was stopped
    assert not [e for e in flow.events if e[0] == "create" and e[1] == IMAGE]
    assert name not in flow.labelled() and not (flow.root / name).exists()


def test_the_watcher_ends_with_the_work_and_leaves_no_thread(service, settings, quick_watch):
    made = insert(settings, 1, "watched", "creating")
    with service._working_on(made) as cancel:
        assert len(watchers(made)) == 1  # positive control: it is watched while worked on
        time.sleep(5 * ws.WORKING_POLL_SECONDS)  # it looks at the row a few times meanwhile
    assert watchers(made) == []
    assert not cancel.is_set()  # nothing was stopped: the row is not being deleted
    assert made not in service._working
    with pytest.raises(RuntimeError), service._working_on(made):
        assert len(watchers(made)) == 1
        raise RuntimeError("planted: the work failed")
    assert watchers(made) == []


def test_a_deletion_in_the_same_process_is_not_stopped_twice_by_the_watcher(
    settings, flow, publish, quick_watch
):
    from tests.test_devcontainer_flow import config

    flow.pull_gate = threading.Event()
    with two_processes(settings, flow) as (service, _):
        try:
            made = cloned_by(publish, config())(service)
            assert flow.pulling.wait(MADE_WITHIN)
            assert watchers(made)  # positive control: it is watched
            service.delete(service.get(made))
            assert gone_within(settings, made, STOPPED_WITHIN)
        finally:
            flow.pull_gate.set()
    assert flow.events.count(("remove_build", made, False)) == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_deleting_a_person_whose_workspace_its_own_making_could_not_remove_answers_at_once(
    service, settings, runtime, monkeypatch
):
    """Nothing is left to wait for: the thread that made it is done, and restore finishes it."""
    from rcman import problems
    from rcman.problems import Problem

    bob = _person(settings, BOB)
    release, real = threading.Event(), runtime.create
    runtime.create = lambda spec: (release.wait(MADE_WITHIN), real(spec))
    made = service.create(bob, "stuck")

    def refusing(name):
        raise rt.RuntimeFailed("planted: it will not go")

    monkeypatch.setattr(runtime, "remove", refusing)
    out: list = []

    def deleting():
        try:
            service.delete_person(bob)
            out.append(None)
        except Problem as refused:
            out.append(refused)

    thread = threading.Thread(target=deleting, daemon=True)
    thread.start()
    deadline = time.monotonic() + MADE_WITHIN
    while (row(settings, made.id) or {"state": None})["state"] != ws.DELETING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    release.set()
    thread.join(STOPPED_WITHIN)
    assert not thread.is_alive()
    assert out[0].code == "workspace_failed" and out[0].title == problems.PERSON_DELETION_LEFT
    assert row(settings, made.id)["state"] == ws.DELETING  # left for restore to finish
