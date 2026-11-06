"""Workspaces: made, listed and deleted (backend/rcman/workspaces.py and its routes), and
what rcman brings back when it starts, on the host runtime."""

import contextlib
import dataclasses
import os
import select
import threading
import time

import pytest
from rcman import channel as ch
from rcman import db
from rcman import runtime as rt
from rcman import workspaces as ws
from rcman.channel import ChannelBroken

from tests.conftest import (
    ALICE,
    BOB,
    MADE_WITHIN,
    as_user,
    changing,
    ended,
    host_runtime,
    made_workspace,
    signed_in,
)
from tests.hang_guard import HANG_GUARD
from tests.time_doubles import ControlledEvent, FakeTime


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


def test_a_channel_is_made_without_the_service_lock_held(service, settings, runtime, monkeypatch):
    """The host runtime's channel asks whether the container runs, and a runtime may take
    locks of its own to answer: the service lock is never held meanwhile."""

    def lock_free() -> bool:
        if not service._lock.acquire(blocking=False):  # a plain Lock: held by anyone
            return False
        service._lock.release()
        return True

    with service._lock:
        assert not lock_free()  # the control: held is seen as held
    real, seen = runtime.channel, []
    monkeypatch.setattr(runtime, "channel", lambda name: (seen.append(lock_free()), real(name))[1])
    workspace = insert(settings, 1, "free", "ready")
    runtime.create(spec(workspace))
    service.server(service.get(workspace)).status()
    assert seen == [True]


class Spare:
    """A channel that is never opened, and says whether it was closed."""

    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def mark_closed_if_idle(self, seconds: float) -> bool:
        return False


def held_at_making(service, monkeypatch) -> tuple[list, threading.Event, threading.Event]:
    """The runtime's channel made as a Spare, the first one held, once made, until ``go`` is
    set: what was made, the event set when the first is being made, and ``go``."""
    first_in, go = threading.Event(), threading.Event()
    made: list[Spare] = []

    def making(name):
        mine = Spare()
        made.append(mine)
        if len(made) == 1:
            first_in.set()
            assert go.wait(30)
        return mine

    monkeypatch.setattr(service.runtime, "channel", making)
    return made, first_in, go


def test_two_requests_racing_for_a_channel_get_one_and_the_spare_is_closed(service, monkeypatch):
    """The first request is still making its channel when the second makes and keeps its own:
    the first is given the second's, and closes the one it made."""
    made, first_in, go = held_at_making(service, monkeypatch)
    got: dict[str, object] = {}
    second_done = threading.Event()

    def second_request():
        got["second"] = service._channel("w")
        second_done.set()

    first = threading.Thread(target=lambda: got.setdefault("first", service._channel("w")))
    second = threading.Thread(target=second_request)
    first.start()
    try:
        assert first_in.wait(30)
        second.start()
        assert second_done.wait(30), "the second request waited on the first's making"
    finally:
        go.set()
        first.join(30)
        second.join(30)
    assert len(made) == 2
    assert got == {"first": made[1], "second": made[1]}
    assert service._channels == {"w": made[1]}
    assert made[0].closed and not made[1].closed


def test_a_channel_closed_while_it_was_being_made_is_not_put_in_and_its_request_is_refused(
    service, monkeypatch
):
    """A close while a request makes the workspace's channel (a deletion, a rebuild) wins: the
    channel made meanwhile is closed, not kept, and its request is refused as sent nowhere
    (ChannelClosed), which ``_channel_run`` asks once more. The next request makes one anew."""
    made, first_in, go = held_at_making(service, monkeypatch)
    failed: list = []

    def request():
        try:
            service._channel("w")
        except ch.ChannelClosed as error:
            failed.append(error)

    asking = threading.Thread(target=request)
    asking.start()
    try:
        assert first_in.wait(30)
        service.close_channel("w")
    finally:
        go.set()
        asking.join(30)
    assert len(failed) == 1 and made[0].closed
    assert service._channels == {}
    assert service._channel("w") is made[1]  # the control: a request afterwards gets one
    assert service._channels == {"w": made[1]} and not made[1].closed


def test_what_holds_a_workspaces_channel_works_after_the_idle_closer_closed_it(
    service, settings, runtime, monkeypatch
):
    """The idle closer closes a channel that a terminal, a server, a repository or a read of
    the devcontainer.json may hold for longer: each of them still runs its next request."""
    workspace = insert(settings, 1, "held", "ready")
    runtime.create(spec(workspace))
    found = service.get(workspace)
    name = found.container
    server = service.server(found)
    holders = {
        "run_in": service.run_in(found),
        "server": server.run,
        "repository": service.repository(found).run,
        "devcontainer": service._channel_runner(name),
    }
    monkeypatch.setattr(ws, "CHANNEL_IDLE_SECONDS", 0.0)
    for what, run in holders.items():
        assert run(["true"], 10).code == 0, what
        assert service.open_channels() == [name], what  # the control: it opened one
        service.close_idle()
        assert service.open_channels() == [], what  # the control: and that one is closed
        assert run(["true"], 10).code == 0, what
    service.close_idle()
    assert server.status() is None  # a server's own requests too
    assert service.open_channels() == [name]
    service.close_idle()
    assert holders["devcontainer"](["true"], 10, cwd="/").code == 0


@pytest.mark.parametrize("closer", ["idle", "close"])
def test_a_request_whose_channel_is_closed_before_it_is_sent_runs_on_a_new_one(
    service, settings, runtime, monkeypatch, closer
):
    """A request finds the workspace's channel, and the idle closer, or a close, closes it
    before the request reaches it: nothing was sent, so it is run on the channel there is now."""
    workspace = insert(settings, 1, "raced", "ready")
    runtime.create(spec(workspace))
    name = service.get(workspace).container
    run = service.run_in(service.get(workspace))
    assert run(["true"], 10).code == 0  # opens the channel, which is idle from now
    [first] = [service._channels[name]]
    found, go = threading.Event(), threading.Event()
    lookup = service._channel

    def held_after_the_lookup(of):
        channel = lookup(of)
        if channel is first:
            found.set()
            assert go.wait(30)
        return channel

    monkeypatch.setattr(service, "_channel", held_after_the_lookup)
    monkeypatch.setattr(ws, "CHANNEL_IDLE_SECONDS", 0.0)
    answers: list = []
    asking = threading.Thread(target=lambda: answers.append(run(["echo", "ran"], 10)))
    asking.start()
    try:
        assert found.wait(30)
        if closer == "idle":
            service.close_idle()
        else:
            service.close_channel(name)
        assert service.open_channels() == []  # the control: the one it found is closed
    finally:
        go.set()
        asking.join(30)
    assert [a.out for a in answers] == [b"ran\n"]
    assert service.open_channels() == [name] and service._channels[name] is not first


def test_a_request_that_reaches_its_channel_as_the_idle_closer_closes_it_is_not_killed(
    service, settings, runtime, monkeypatch, tmp_path
):
    """A request found the channel before the idle closer chose it, and reaches the channel
    between that choice and the close: it is not killed in flight, but run, on the channel
    there is then. The channel that was idle is closed all the same."""
    workspace = insert(settings, 1, "window", "ready")
    runtime.create(spec(workspace))
    name = service.get(workspace).container
    run = service.run_in(service.get(workspace))
    assert run(["true"], 10).code == 0  # opens the channel, which is idle from now
    first = service._channels[name]
    files = first._files
    assert all(os.path.exists(f) for f in files)  # the control: the channel's files are there
    found, go = threading.Event(), threading.Event()
    lookup = service._channel

    def held_after_the_lookup(of):
        channel = lookup(of)
        if channel is first and not found.is_set():
            found.set()
            assert go.wait(30)
        return channel

    signal, done = tmp_path / "signal", tmp_path / "done"
    os.mkfifo(signal)
    os.mkfifo(done)
    reader = os.open(signal, os.O_RDWR)  # a writer of its own: no end-of-file meanwhile
    finisher = os.open(done, os.O_RDWR)
    close = first.close

    def closing():  # the idle closer's close of the channel it chose: the request comes first
        go.set()
        assert select.select([reader], [], [], 30)[0]  # the request's command is running
        close()

    first.close = closing
    monkeypatch.setattr(service, "_channel", held_after_the_lookup)
    monkeypatch.setattr(ws, "CHANNEL_IDLE_SECONDS", 0.0)
    answers: list = []
    failed: list = []

    def request():
        script = f"echo sent > {signal}; read line < {done}; echo ran"
        try:
            answers.append(run(["sh", "-c", script], 30))
        except ChannelBroken as error:
            failed.append(error)

    asking = threading.Thread(target=request)
    asking.start()
    try:
        assert found.wait(30)
        service.close_idle()
    finally:
        go.set()
        os.write(finisher, b"go\n")
        asking.join(30)
        os.close(reader)
        os.close(finisher)
    assert failed == [] and [a.out for a in answers] == [b"ran\n"]
    assert first.process is None  # the idle one was closed
    assert not any(os.path.exists(f) for f in files)  # and its files removed through its shell
    assert service.open_channels() == [name] and service._channels[name] is not first


def test_a_request_whose_channel_is_closed_once_it_was_sent_fails_and_is_not_run_again(
    service, settings, runtime, tmp_path
):
    """The twin of the test above: a request that reached the shell may have run, so a close
    then fails it as broken, and it is not run again."""
    workspace = insert(settings, 1, "sent", "ready")
    runtime.create(spec(workspace))
    name = service.get(workspace).container
    run = service.run_in(service.get(workspace))
    count, signal = tmp_path / "count", tmp_path / "signal"
    os.mkfifo(signal)
    failed: list = []

    def request():
        script = f"echo x >> {count}; echo sent > {signal}; exec sleep 30"
        try:
            run(["sh", "-c", script], 60)
        except ChannelBroken as error:
            failed.append(error)

    asking = threading.Thread(target=request)
    asking.start()
    reader = os.open(signal, os.O_RDWR)  # a writer of its own: no end-of-file meanwhile
    try:
        assert select.select([reader], [], [], 30)[0]  # the command is running
        assert os.read(reader, 16) == b"sent\n"
        service.close_channel(name)
    finally:
        os.close(reader)
        asking.join(30)
    assert len(failed) == 1 and not isinstance(failed[0], ch.ChannelClosed)
    assert count.read_text() == "x\n"  # run once, not again
    assert service.open_channels() == []


def test_a_workspace_with_no_server_is_listed_without_reaching_it(service, settings, runtime):
    workspace = insert(settings, 1, "quiet", "ready")
    runtime.create(spec(workspace))
    assert service.server_status(service.get(workspace)) is None
    assert service.open_channels() == []


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
    ended(service, made.id)
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
            ended(service, made["it"].id)
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
    ended(service, made.id)
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
    ended(service, made.id)
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
def two_processes(settings, runtime, shell_closing=None):
    """The service and rcman-admin: two Workspaces on the same database and the same Podman,
    each with only its own threads and events, as two processes have. ``shell_closing``, a
    ControlledEvent, is rcman-admin's closing event where the test gives one, put in before any
    thread of its starts."""
    service = ws.Workspaces(settings, runtime)
    shell = ws.Workspaces(settings, runtime, prunes=False)
    if shell_closing is not None:
        shell._closing = shell_closing
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


def worked_on(service, workspace_id: int, body) -> list[BaseException]:
    """``body(cancel)`` run inside ``service._working_on(workspace_id)``, in a thread joined
    under HANG_GUARD: what it raised."""
    raised: list[BaseException] = []

    def run():
        try:
            with service._working_on(workspace_id) as cancel:
                body(cancel)
        except BaseException as failed:  # noqa: BLE001 - handed to the test
            raised.append(failed)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(HANG_GUARD)
    assert not thread.is_alive()  # the work's end ended the watcher's wait, and so the work
    return raised


def test_the_watcher_ends_with_the_work_and_leaves_no_thread(service, settings, monkeypatch):
    # Its look is never due within the test: only the work's end ends the watcher's wait.
    monkeypatch.setattr(ws, "WORKING_POLL_SECONDS", 2 * HANG_GUARD)
    made = insert(settings, 1, "watched", "creating")
    seen: list = []

    def work(cancel):
        seen.append((len(watchers(made)), cancel))

    assert worked_on(service, made, work) == []
    [(watching, cancel)] = seen
    assert watching == 1  # positive control: it is watched while worked on
    assert watchers(made) == []
    assert not cancel.is_set()  # nothing was stopped: the row is not being deleted
    assert made not in service._working

    def failing(cancel):
        seen.append(len(watchers(made)))
        raise RuntimeError("planted: the work failed")

    [raised] = worked_on(service, made, failing)
    assert isinstance(raised, RuntimeError) and seen[-1] == 1
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


# --- a person's deletion waits for what makes their workspaces -------------------------------


@pytest.fixture
def fake(monkeypatch):
    """The workspaces module's time, frozen unless the test moves it: only a person's deletion,
    in the test's own thread, reads it here."""
    made = FakeTime()
    monkeypatch.setattr(ws, "time", made)
    return made


@pytest.fixture
def looks(monkeypatch):
    """The first service's closing event a ControlledEvent: a person's deletion's look at rows
    another process makes (``WORKING_POLL_SECONDS``) waits on it until the test ends it."""
    from tests.conftest import into_the_first_service

    made = ControlledEvent()
    into_the_first_service(monkeypatch, "_closing", made)
    return made


@pytest.fixture
def timed(settings, runtime, changed, looks, fake):
    """The service with the test's condition (``changed``), closing event (``looks``) and time
    (``fake``). The two doubles tell their records on one condition, before any thread of the
    service's starts, so that a test can wait for a wait on either (``deletion_waits``)."""
    looks._told = changed._told
    made = ws.Workspaces(settings, runtime)
    assert made._changed is changed and made._closing is looks
    yield made
    made.close()


def held_create(runtime) -> threading.Event:
    """Every container's making held until the event is set."""
    held, real = threading.Event(), runtime.create
    runtime.create = lambda spec: (held.wait(HANG_GUARD), real(spec))
    return held


def deleting_person(service, person) -> tuple[threading.Thread, list]:
    """``delete_person`` in a thread of its own: the thread, and what it ended in. A service whose
    closing event is a ControlledEvent has it told once the deletion answered (``answered``)."""
    from rcman.problems import Problem

    out: list = []

    def run():
        try:
            service.delete_person(person)
            out.append(None)
        except Problem as refused:
            out.append(refused)
        finally:
            if isinstance(service._closing, ControlledEvent):
                with service._closing._told:
                    service._closing._told.notify_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def joined(thread: threading.Thread) -> None:
    thread.join(HANG_GUARD)
    assert not thread.is_alive()


def waits_of(double, thread) -> list:
    """The waits recorded on ``double`` by ``thread``."""
    return [w for w in double.waits if w.thread is thread]


def deletion_waits(thread: threading.Thread, changed, looks) -> None:
    """Until the deletion in ``thread`` waits: for its making thread, on ``changed``, or for a
    look at the rows, on ``looks`` (``timed``'s doubles, which tell on one condition)."""
    changed._until(
        lambda: waits_of(changed, thread) or waits_of(looks, thread), "the deletion's wait", None
    )


def answered(thread: threading.Thread, out: list, looks) -> None:
    """Until the deletion in ``thread`` has answered, or has looked at the rows once (a wait on
    ``looks``): it must have answered without looking."""
    looks._until(lambda: out or waits_of(looks, thread), "the deletion's answer or a look", None)
    assert waits_of(looks, thread) == []  # it did not look at the rows
    joined(thread)


def left(out) -> bool:
    from rcman import problems

    return out[0].code == "workspace_failed" and out[0].title == problems.PERSON_DELETION_LEFT


def test_deleting_a_person_waits_for_their_making_thread_on_one_wait_and_no_poll(
    timed, changed, looks, settings, runtime
):
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "own")
    thread, out = deleting_person(timed, bob)
    [waiting] = changed.wait_waiting(1, match=thread)
    assert waiting.seconds == ws.MADE_WITHIN_SECONDS
    assert thread.is_alive()
    held.set()
    joined(thread)
    assert out == [None] and row(settings, made.id) is None
    assert _person(settings, BOB) is None
    assert [w.ended for w in waits_of(changed, thread)] == ["predicate"]  # its thread's end
    assert waits_of(looks, thread) == []  # no look at the rows


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_deleting_a_person_whose_workspace_its_own_making_could_not_remove_answers_at_once(
    timed, changed, looks, settings, runtime, monkeypatch
):
    """Nothing is left to wait for: the thread that made it is done, and restore finishes it."""
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "stuck")

    def refusing(name):
        raise rt.RuntimeFailed("planted: it will not go")

    thread, out = deleting_person(timed, bob)
    deletion_waits(thread, changed, looks)
    monkeypatch.setattr(runtime, "remove", refusing)
    held.set()
    answered(thread, out, looks)
    assert left(out)
    assert row(settings, made.id)["state"] == ws.DELETING  # left for restore to finish


def test_deleting_a_person_whose_making_thread_raised_at_its_end_answers_at_once(
    timed, changed, looks, settings, runtime, monkeypatch
):
    import sqlite3

    caught: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: caught.append(args.exc_value))
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "raising")

    def failing(workspace):
        raise sqlite3.OperationalError("planted: the database is locked")

    thread, out = deleting_person(timed, bob)
    deletion_waits(thread, changed, looks)
    monkeypatch.setattr(timed, "_remove", failing)  # what its final look calls
    held.set()
    answered(thread, out, looks)
    assert [type(e) for e in caught] == [sqlite3.OperationalError]
    assert left(out) and row(settings, made.id) is not None
    with timed._changed:
        assert made.id in timed._ended_here


def test_deleting_a_person_whose_workspace_a_thread_here_left_creating_answers_at_once(
    timed, looks, settings, monkeypatch
):
    """A making thread of this process ended and left the row creating: no thread makes it, and
    it is not another process's either, so it is not looked at again."""
    import sqlite3

    caught: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: caught.append(args.exc_value))
    bob = _person(settings, BOB)
    real = timed._set

    def failing(workspace_id, **columns):
        if columns.get("state") == ws.READY:
            raise sqlite3.OperationalError("planted: the database is locked")
        real(workspace_id, **columns)

    monkeypatch.setattr(timed, "_set", failing)
    made = timed.create(bob, "unfinished")
    ended(timed, made.id, HANG_GUARD)
    assert [type(e) for e in caught] == [sqlite3.OperationalError]
    assert row(settings, made.id)["state"] == ws.CREATING  # positive: the row it left
    assert not timed._being_made(made.id)
    thread, out = deleting_person(timed, bob)
    answered(thread, out, looks)
    assert left(out)


def test_deleting_a_person_gives_up_on_their_making_thread_after_its_deadline(
    timed, changed, settings, runtime
):
    """The wait's timeout is the whole deadline, exactly (the time is frozen): run out, the
    deletion answers that work is left. Its twin a second short is given up: the condition's
    timeout never runs out by itself, so its seconds stand in."""
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "slow")
    thread, out = deleting_person(timed, bob)
    try:
        [waiting] = changed.wait_waiting(1, match=thread)
        assert waiting.seconds == ws.MADE_WITHIN_SECONDS
        assert thread.is_alive() and out == []  # it never runs out by itself
        changed.expire(thread)
        joined(thread)
        assert left(out)
    finally:
        held.set()
    ended(timed, made.id, HANG_GUARD)


def look(closing, n: int = 1) -> None:
    """Until rcman-admin's deletion waits for its next look at the rows."""
    closing.wait_waiting(n, match=ws.WORKING_POLL_SECONDS)


def test_rcman_admin_deleting_a_person_polls_a_workspace_the_service_makes_until_it_is_gone(
    settings, runtime
):
    shell_closing = ControlledEvent()
    bob = _person(settings, BOB)
    held = held_create(runtime)
    with two_processes(settings, runtime, shell_closing) as (service, shell):
        try:
            made = service.create(bob, "theirs")
            thread, out = deleting_person(shell, bob)
            look(shell_closing)
            assert thread.is_alive()
        finally:
            held.set()
        ended(service, made.id, HANG_GUARD)  # the service removed it, as it was deleting
        assert row(settings, made.id) is None
        assert thread.is_alive() and out == []  # positive: it has not looked again yet
        shell_closing.expire(ws.WORKING_POLL_SECONDS)
        joined(thread)
        assert out == [None]


@pytest.mark.parametrize("short", [0, 1])
def test_rcman_admin_deleting_a_person_gives_up_on_a_row_nothing_makes_after_its_deadline(
    settings, runtime, monkeypatch, short
):
    fake = FakeTime()
    monkeypatch.setattr(ws, "time", fake)  # read only by the deletion, the test's thread
    shell_closing = ControlledEvent()
    bob = _person(settings, BOB)
    orphan = insert(settings, bob.id, "orphan", ws.CREATING)
    with two_processes(settings, runtime, shell_closing) as (_, shell):
        thread, out = deleting_person(shell, bob)
        look(shell_closing)
        fake.advance(ws.MADE_WITHIN_SECONDS - short)
        if short:  # the twin: a second short, it looks on
            for _ in range(2):
                shell_closing.expire(ws.WORKING_POLL_SECONDS)
                look(shell_closing)
            assert thread.is_alive() and out == []
            fake.advance(short)
        shell_closing.expire(ws.WORKING_POLL_SECONDS)
        joined(thread)
        assert left(out)
        assert row(settings, orphan)["state"] == ws.DELETING


def test_closing_ends_a_persons_deletion_waiting_for_their_making_thread_at_once(
    timed, changed, settings, runtime
):
    bob = _person(settings, BOB)
    held = held_create(runtime)
    made = timed.create(bob, "own")
    thread, out = deleting_person(timed, bob)
    changed.wait_waiting(1, match=thread)
    closing = threading.Thread(target=timed.close, daemon=True)
    closing.start()
    try:
        joined(thread)  # with the making thread still held
        assert left(out)
    finally:
        held.set()
    joined(closing)
    ended(timed, made.id, HANG_GUARD)


def test_closing_ends_rcman_admins_deletion_waiting_for_its_next_look_at_once(settings, runtime):
    shell_closing = ControlledEvent()
    bob = _person(settings, BOB)
    insert(settings, bob.id, "orphan", ws.CREATING)
    with two_processes(settings, runtime, shell_closing) as (_, shell):
        thread, out = deleting_person(shell, bob)
        look(shell_closing)
        shell.close()
        joined(thread)
        assert left(out)
        assert [w.ended for w in waits_of(shell_closing, thread)] == ["set"]


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


def test_close_gives_up_after_its_deadline_and_says_what_is_left(
    on_changed, changed, settings, runtime, caplog
):
    timed = on_changed
    held = held_create(runtime)
    made = timed.create(_person(settings), "slow")
    closing = threading.Thread(target=timed.close, daemon=True)
    closing.start()
    try:
        [waiting] = changed.wait_waiting(1, match=closing)
        assert waiting.seconds == ws.CLOSE_WITHIN_SECONDS  # its deadline is the bound's
        assert closing.is_alive()  # the deadline never runs out by itself
        changed.expire(closing)
        joined(closing)
        said = [r.getMessage() for r in caplog.records if r.name == "rcman.workspaces"]
        assert [m for m in said if "did not end" in m and str(made.id) in m], said
    finally:
        held.set()
    ended(timed, made.id, HANG_GUARD)


def test_close_with_nothing_running_returns_at_once(on_changed, changed):
    on_changed.close()
    assert changed.waits == []  # it did not wait at all


def test_no_put_back_is_begun_once_rcman_is_closing(timed, settings, monkeypatch):
    left_ = insert(settings, 1, "left", ws.READY)
    con = db.connect(settings.database)
    try:
        con.execute("update workspaces set rebuild_state = ? where id = ?", ("switching", left_))
        con.commit()
    finally:
        con.close()
    begun: list = []
    monkeypatch.setattr(timed, "_restore_task", lambda w, **k: begun.append(w.id))
    timed.restored.set()
    assert timed._left_in_switch(timed.get(left_))  # positive control: it would be put back
    timed.close()
    timed.settle_left_over()
    with timed._changed:
        assert timed._putting_back == {}
    assert begun == []


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
