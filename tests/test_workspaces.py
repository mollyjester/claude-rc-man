"""Workspaces: made, listed and deleted (backend/rcman/workspaces.py and its routes), and
what rcman brings back when it starts, on the host runtime."""

import threading
import time

import pytest
from rcman import db
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import ALICE, BOB, MADE_WITHIN, as_user, changing, made_workspace, signed_in


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

    wanted = insert(settings, 1, "wanted", "ready", wanted=1, server=1, prefix="pfx")
    running = insert(settings, 1, "running", "ready", wanted=1, server=1, prefix="pfx")
    unwanted = insert(settings, 1, "unwanted", "ready", wanted=0, server=0)
    for workspace_id in (wanted, running, unwanted):
        runtime.create(spec(workspace_id))
        layout = runtime.layout(rt.container_name(workspace_id))
        fake.write_state(Path(layout.config), logged_in=True, trusted=[layout.work])
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
        runtime = rt.HostRuntime(root, sockets, fake_claude(tmp_path / "claude"), tmp_path / "x")
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


def test_every_route_taking_a_workspace_answers_anothers_as_not_found(client, app):
    """Derived from the route table: each route with a workspace id, in its path, query or
    body, given alice's workspace by bob."""
    import copy

    from tests.conftest import CALLS, concrete
    from tests.test_problems import request_fields

    taking = sorted({(f.method, f.path) for f in request_fields(app) if f.name == "workspace"})
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
