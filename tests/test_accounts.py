"""Each person's Claude sign-in (backend/rcman/accounts.py and routers/claude.py): one
credentials file shared by every workspace of theirs, signing in through a short-lived
sign-in container, signing out of Claude and of rcman, on the host runtime with the
fake `claude`."""

import json
import os
import time
from pathlib import Path

import pytest
from rcman import db, people
from rcman import runtime as rt
from rcman.problems import Problem

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, CAROL, as_user, changing, made_workspace

URL_START = "https://claude.com/cai/oauth/authorize?"


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert app.state.workspaces.restored.wait(30)
        yield c


def person(app, email: str) -> people.Person:
    con = db.connect(app.state.settings.database)
    try:
        return people.by_email(con, email)
    finally:
        con.close()


def account_file(app, email: str) -> Path:
    runtime = app.state.workspaces.runtime
    found = person(app, email)
    runtime.ensure_account(found.id, found.slot)
    return runtime._account_file(found.id)


def connect(client, who: str = ALICE, email: str | None = None) -> dict:
    r = client.post(
        "/api/claude/login", json={"email": email} if email else {}, headers=changing(who)
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith(URL_START)
    r = client.post("/api/claude/code", json={"code": fake.ACCEPTED_CODE}, headers=changing(who))
    assert r.status_code == 200, r.text
    return r.json()


def trusted(app, workspace: dict) -> None:
    layout = app.state.workspaces.runtime.layout(rt.container_name(workspace["id"]))
    (Path(layout.config) / fake.STATE_FILE).write_text(
        json.dumps({"trusted": [layout.work], "servers": {}})
    )


def start(client, workspace: dict, who: str = ALICE):
    return client.post("/api/servers", json={"workspace": workspace["id"]}, headers=changing(who))


def running(client, workspace: dict, who: str = ALICE) -> bool:
    listed = client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
    server = next(w for w in listed if w["id"] == workspace["id"])["server"]
    return (
        server is not None
        and server.get("exit_code") is None
        and server["state"]
        in (
            "connected",
            "ready",
        )
    )


def sign_ins_left(app) -> list[str]:
    root = app.state.workspaces.runtime.root
    return [p.name for p in root.iterdir() if p.name.startswith("rcman-sign-in-")]


def audited(app, action: str) -> list[tuple]:
    con = db.connect(app.state.settings.database)
    try:
        return con.execute(
            "select email, target, result from audit where action = ?", (action,)
        ).fetchall()
    finally:
        con.close()


# --- signing in -------------------------------------------------------------------------


def test_before_a_sign_in_the_account_is_not_connected_and_its_file_is_empty(client, app):
    r = client.get("/api/claude", headers=as_user(ALICE))
    assert r.status_code == 200
    assert r.json()["connection"] == "not_connected" and r.json()["signing_out"] is False
    assert account_file(app, ALICE).read_bytes() == b""


def test_a_sign_in_connects_the_account_and_leaves_no_sign_in_container(client, app):
    answer = connect(client, email="work@example.org")
    assert answer["connection"] == "connected"
    assert answer["account"]["email"] == "work@example.org"
    assert answer["login_expires_at"]  # from the fake's refreshTokenExpiresAt
    assert fake.signed_in(account_file(app, ALICE).parent)
    assert sign_ins_left(app) == []
    assert client.get("/api/claude", headers=as_user(ALICE)).json()["connection"] == "connected"
    assert client.get("/api/claude", headers=as_user(BOB)).json()["connection"] == "not_connected"
    assert [(e, t) for e, t, _ in audited(app, "claude.login")] == [(ALICE, "work@example.org")]
    assert [r for _, _, r in audited(app, "claude.connect")] == ["ok"]


def test_a_code_not_accepted_fails_and_ends_the_sign_in(client, app):
    client.post("/api/claude/login", json={}, headers=changing(ALICE))
    r = client.post("/api/claude/code", json={"code": "wrong#code"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "login_failed")
    assert sign_ins_left(app) == []
    assert client.get("/api/claude", headers=as_user(ALICE)).json()["connection"] == "not_connected"


def test_a_code_with_no_sign_in_waiting_is_refused(client):
    r = client.post("/api/claude/code", json={"code": fake.ACCEPTED_CODE}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "no_login_in_progress")


def test_the_code_reaches_claude_through_stdin_and_never_an_argv(client, app, monkeypatch):
    from rcman import channel

    argvs, stdins = [], []
    runtime = app.state.workspaces.runtime
    real_exec, real_run = runtime.exec, channel.Channel.run

    def exec_(name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT, cwd=None, keep=None, again=False):
        argvs.append(argv)
        stdins.append(stdin)
        return real_exec(name, argv, stdin, timeout, cwd, keep, again)

    def run(self, argv, *args, **kwargs):
        argvs.append(argv)
        return real_run(self, argv, *args, **kwargs)

    monkeypatch.setattr(runtime, "exec", exec_)
    monkeypatch.setattr(channel.Channel, "run", run)
    connect(client)
    assert any(fake.ACCEPTED_CODE.encode() in (s or b"") for s in stdins)  # positive control
    assert not [a for a in argvs if any(fake.ACCEPTED_CODE in part for part in a)]


def test_the_expiry_is_the_refresh_tokens_and_none_without_one(client, app):
    connect(client)
    path = account_file(app, ALICE)
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["refreshTokenExpiresAt"] = 4102444800000  # 2100-01-01
    path.write_text(json.dumps(data))
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["login_expires_at"].startswith("2100-01-01")
    del data["claudeAiOauth"]["refreshTokenExpiresAt"]
    path.write_text(json.dumps(data))
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "connected" and found["login_expires_at"] is None


# --- servers on the shared sign-in ----------------------------------------------------


def test_a_start_before_a_sign_in_says_to_connect_on_the_claude_page(client, app):
    work = made_workspace(client, ALICE)
    r = start(client, work)
    assert (r.status_code, r.json()["code"]) == (409, "claude_not_logged_in")
    assert "Claude page" in r.json()["title"]


def test_workspaces_made_before_and_after_a_sign_in_both_start(client, app):
    before = made_workspace(client, ALICE, "before")
    connect(client)
    after = made_workspace(client, ALICE, "after")
    for workspace in (before, after):
        trusted(app, workspace)
        r = start(client, workspace)
        assert r.status_code == 200, r.text
        assert r.json()["server"]["state"] in ("connected", "ready"), r.json()


def test_oauth_account_is_seeded_before_the_server_starts_keeping_every_other_key(client, app):
    work = made_workspace(client, ALICE)
    connect(client, email="work@example.org")
    layout = app.state.workspaces.runtime.layout(rt.container_name(work["id"]))
    config = Path(layout.config) / fake.CONFIG_FILE
    config.write_text(json.dumps({"numStartups": 7, "oauthAccount": {"emailAddress": "old"}}))
    trusted(app, work)
    assert start(client, work).status_code == 200
    seeded = json.loads(config.read_text())
    assert seeded["numStartups"] == 7
    assert seeded["oauthAccount"]["emailAddress"] == "work@example.org"
    assert (config.stat().st_mode & 0o777) == 0o600


def test_a_lost_read_of_the_claude_config_does_not_lose_its_keys(client, app, monkeypatch):
    """The read of ``.claude.json`` changes nothing, so it is one the runtime runs once more
    should it lose what the read wrote (Runtime.exec): an answer of nothing would be taken for
    an empty file, and every other key overwritten."""
    work = made_workspace(client, ALICE)
    connect(client, email="work@example.org")
    runtime = app.state.workspaces.runtime
    layout = runtime.layout(rt.container_name(work["id"]))
    config = Path(layout.config) / fake.CONFIG_FILE
    config.write_text(json.dumps({"numStartups": 7}))
    real = runtime.exec

    def exec_(name, argv, stdin=None, *args, again=False, **kwargs):
        if stdin is None and argv[-1] == str(config) and not again:
            return rt.Result(0, b"", b"")  # what was read lost, as only a rerun recovers
        return real(name, argv, stdin, *args, again=again, **kwargs)

    monkeypatch.setattr(runtime, "exec", exec_)
    trusted(app, work)
    assert start(client, work).status_code == 200
    seeded = json.loads(config.read_text())
    assert seeded["numStartups"] == 7
    assert seeded["oauthAccount"]["emailAddress"] == "work@example.org"


def test_connecting_a_different_account_stops_nothing(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    assert start(client, work).status_code == 200
    assert running(client, work)  # positive control
    connect(client, email="other@example.org")
    assert running(client, work)


# --- signing out of Claude --------------------------------------------------------------


def sign_out(client, keep: bool | None, who: str = ALICE):
    body = {} if keep is None else {"keep_sessions": keep}
    return client.post("/api/claude/logout", json=body, headers=changing(who))


def test_signing_out_with_nothing_running_empties_the_file_in_place_at_once(client, app):
    connect(client)
    path = account_file(app, ALICE)
    inode = os.stat(path).st_ino
    r = sign_out(client, None)
    assert r.status_code == 200, r.text
    assert r.json()["connection"] == "not_connected" and r.json()["signing_out"] is False
    assert path.read_bytes() == b"" and os.stat(path).st_ino == inode
    assert sign_ins_left(app) == []
    assert [(e, r) for e, _, r in audited(app, "claude.disconnect")] == [(ALICE, "ok")]


def test_signing_out_without_keeping_sessions_stops_the_running_servers_first(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    assert running(client, work)
    assert sign_out(client, False).json()["connection"] == "not_connected"
    assert not running(client, work)
    assert account_file(app, ALICE).read_bytes() == b""


def test_signing_out_keeping_sessions_waits_for_the_last_running_server(client, app):
    first, second, idle = (made_workspace(client, ALICE, n) for n in ("one", "two", "idle"))
    connect(client)
    for workspace in (first, second, idle):
        trusted(app, workspace)
    start(client, first)
    start(client, second)
    answer = sign_out(client, True).json()
    assert answer["signing_out"] is True and answer["connection"] != "not_connected"
    assert running(client, first) and running(client, second)  # nothing was stopped
    assert fake.signed_in(account_file(app, ALICE).parent)  # they keep the credentials
    r = start(client, idle)
    assert (r.status_code, r.json()["code"]) == (409, "claude_signing_out")
    client.post("/api/servers/stop", json={"workspace": first["id"]}, headers=changing(ALICE))
    assert fake.signed_in(account_file(app, ALICE).parent)  # one still runs
    client.post("/api/servers/stop", json={"workspace": second["id"]}, headers=changing(ALICE))
    assert account_file(app, ALICE).read_bytes() == b""
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "not_connected" and found["signing_out"] is False


def test_a_sign_out_left_waiting_ends_when_the_page_finds_nothing_running(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)
    app.state.workspaces.runtime.stop(rt.container_name(work["id"]))  # its server ended
    app.state.workspaces.runtime.start(rt.container_name(work["id"]))
    app.state.workspaces.close_channels()
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "not_connected" and found["signing_out"] is False


def test_a_new_sign_in_ends_a_sign_out_left_waiting(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)
    assert connect(client)["signing_out"] is False
    client.post("/api/servers/stop", json={"workspace": work["id"]}, headers=changing(ALICE))
    assert fake.signed_in(account_file(app, ALICE).parent)


# --- signing out of rcman -----------------------------------------------------------------


def test_signing_out_of_rcman_keeps_sessions_running_by_default(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    r = client.post("/api/me/sign-out", json={}, headers=changing(ALICE))
    assert r.status_code == 200 and r.json() == {"sign_out_url": "/oauth2/sign_out"}
    assert running(client, work)
    assert [(e, r) for e, _, r in audited(app, "me.sign_out")] == [(ALICE, "ok")]


def test_signing_out_of_rcman_without_keeping_sessions_stops_them(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    r = client.post("/api/me/sign-out", json={"keep_sessions": False}, headers=changing(ALICE))
    assert r.status_code == 200
    assert not running(client, work)
    assert fake.signed_in(account_file(app, ALICE).parent)  # still connected to Claude


# --- deleting a person ------------------------------------------------------------------


def test_deleting_a_person_removes_their_sign_in_before_their_row(client, app):
    made_workspace(client, BOB)
    connect(client, BOB)
    bob = person(app, BOB)
    folder = app.state.workspaces.runtime._account_file(bob.id).parent
    assert folder.exists()  # positive control
    r = client.delete(f"/api/people/{bob.id}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert not folder.exists() and person(app, BOB) is None


def test_a_sign_in_left_by_a_failed_removal_never_reaches_the_next_person(client, app, monkeypatch):
    """A slot is given again: a leftover file would sign the next one in."""
    connect(client, CAROL)
    carol = person(app, CAROL)
    runtime = app.state.workspaces.runtime

    def refused(person_id):
        raise rt.RuntimeFailed("planted: the volume could not be removed")

    monkeypatch.setattr(runtime, "remove_account", refused)
    r = client.delete(f"/api/people/{carol.id}", headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    assert person(app, CAROL).state == people.DELETING  # the row waits for the file
    assert fake.signed_in(runtime._account_file(carol.id).parent)  # the planted leftover
    monkeypatch.undo()
    app.state.workspaces.restore()  # what rcman's next start does
    assert person(app, CAROL) is None
    con = db.connect(app.state.settings.database)
    try:
        again = people.add(con, "dave@example.com", False)
    finally:
        con.close()
    assert again.slot == carol.slot and again.id > carol.id  # the slot again, never the id
    assert runtime.read_account(again.id, again.slot) == b""


# --- the sign-in that disappeared -------------------------------------------------------


def test_a_connected_account_whose_file_was_emptied_is_reported_as_signed_out_unexpectedly(
    client, app
):
    """Two workspaces of one person refresh the shared file with Claude's own writes, which
    can collide and leave it empty or torn; SQLite still says the account is connected."""
    connect(client)
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "connected" and found["signed_out_unexpectedly"] is False
    account_file(app, ALICE).write_bytes(b"")
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "not_connected" and found["account"] is None
    assert found["signed_out_unexpectedly"] is True


@pytest.mark.parametrize("content", [b"{", b'{"claudeAiOauth": 5}', b"[]", b"\xff\xfe", b"null"])
def test_a_file_that_holds_no_sign_in_is_the_same_while_the_account_is_connected(
    client, app, content
):
    connect(client)
    account_file(app, ALICE).write_bytes(content)
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert (found["connection"], found["signed_out_unexpectedly"]) == ("not_connected", True)


def test_nothing_is_unexpected_before_a_sign_in_after_a_sign_out_or_while_one_waits(client, app):
    assert (
        client.get("/api/claude", headers=as_user(ALICE)).json()["signed_out_unexpectedly"] is False
    )
    connect(client)
    answer = sign_out(client, False).json()
    assert answer["connection"] == "not_connected" and answer["signed_out_unexpectedly"] is False
    assert (
        client.get("/api/claude", headers=as_user(ALICE)).json()["signed_out_unexpectedly"] is False
    )
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    waiting = sign_out(client, True).json()  # keeps sessions: the file stays until they stop
    assert waiting["signing_out"] is True and waiting["signed_out_unexpectedly"] is False
    account_file(app, ALICE).write_bytes(b"")  # emptied while the sign-out waits: expected
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["signing_out"] is True and found["signed_out_unexpectedly"] is False


def test_what_a_credentials_file_holds_is_one_of_three_states():
    from rcman.runtime import credentials_state

    signed_in = b'{"claudeAiOauth": {"accessToken": "x"}}'
    assert credentials_state(signed_in) == "signed_in"
    assert credentials_state(b"") == credentials_state(b" \n") == "empty"
    for torn in (b"{", b"{}", b'{"claudeAiOauth": null}', b"[]", b"\xff", signed_in[:20]):
        assert credentials_state(torn) == "unreadable", torn


# --- a person being deleted is let into nothing of theirs ------------------------------


def being_deleted(app, email: str) -> people.Person:
    """The person as a request that began before the deletion still holds them, and the row as
    the deletion left it."""
    held = person(app, email)
    con = db.connect(app.state.settings.database)
    try:
        people.mark_deleting(con, held)
    finally:
        con.close()
    return held


def test_the_status_of_a_person_being_deleted_does_not_make_their_volume_again(client, app):
    bob = being_deleted(app, BOB)
    runtime = app.state.workspaces.runtime
    runtime.remove_account(bob.id)  # as the deletion does: the restore wrote gh's files there
    assert not runtime._account_file(bob.id).parent.exists()  # the control: no volume now
    with pytest.raises(Problem) as refused:
        app.state.accounts.status(bob)
    assert refused.value.code == "not_allowed"
    assert not runtime._account_file(bob.id).parent.exists()
    # and the control the other way: an active person's status makes it, as it always has
    app.state.accounts.status(person(app, CAROL))
    assert runtime._account_file(person(app, CAROL).id).parent.exists()


def test_a_status_that_waits_for_the_deletion_of_the_person_does_not_make_their_volume_again(
    client, app
):
    """The read makes the volume if it is not there: it is taken under the person's lock, and
    the person is looked at again once the lock is had."""
    bob = person(app, BOB)
    runtime = app.state.workspaces.runtime
    app.state.accounts.status(bob)  # the control: an active person's volume is there
    folder = runtime._account_file(bob.id).parent
    assert folder.exists()
    lock = app.state.workspaces.person_lock(bob.id)
    lock.acquire()
    try:
        reading, out = in_thread(app.state.accounts.status, bob)
        reading.join(1.0)
        assert reading.is_alive()  # it waits for the lock, which a deletion holds
        being_deleted(app, BOB)
        shutil_rmtree(folder)
    finally:
        lock.release()
    reading.join(30)
    assert len(out) == 1 and isinstance(out[0], Problem) and out[0].code == "not_allowed"
    assert not folder.exists()


def test_a_sign_in_begun_or_finished_for_a_person_being_deleted_is_refused_and_leaves_nothing(
    client, app
):
    bob = person(app, BOB)
    accounts = app.state.accounts
    accounts.begin(bob, "bob@example.org")  # the control: an active person begins one
    assert sign_ins_left(app)
    accounts.close()
    assert sign_ins_left(app) == []
    held = being_deleted(app, BOB)
    folder = app.state.workspaces.runtime._account_file(held.id).parent
    shutil_rmtree(folder)
    with pytest.raises(Problem) as refused:
        accounts.begin(held, "bob@example.org")
    assert refused.value.code == "not_allowed"
    with pytest.raises(Problem) as refused:
        accounts.finish(held, fake.ACCEPTED_CODE)
    assert refused.value.code == "not_allowed"
    assert sign_ins_left(app) == [] and not folder.exists()


def shutil_rmtree(folder: Path) -> None:
    import shutil

    shutil.rmtree(folder, ignore_errors=True)


def test_a_sign_in_that_was_begun_ends_with_the_deletion_of_the_person(client, app):
    bob = person(app, BOB)
    app.state.accounts.begin(bob, "bob@example.org")
    assert sign_ins_left(app) and bob.id in app.state.accounts._sign_ins
    r = client.delete(f"/api/people/{bob.id}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert sign_ins_left(app) == [] and bob.id not in app.state.accounts._sign_ins


def test_deleting_a_person_waits_for_what_holds_their_lock(client, app):
    bob = person(app, BOB)
    lock = app.state.workspaces.person_lock(bob.id)
    lock.acquire()
    try:
        thread, out = in_thread(app.state.workspaces.delete_person, bob)
        thread.join(1.0)
        assert thread.is_alive() and person(app, BOB) is not None  # held back by the lock
    finally:
        lock.release()
    thread.join(30)
    assert not thread.is_alive() and out == [None] and person(app, BOB) is None


def test_the_status_is_given_even_when_finishing_a_sign_out_keeps_failing(client, app, monkeypatch):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)  # waits for the server
    app.state.workspaces.runtime.stop(rt.container_name(work["id"]))  # so it could be settled
    app.state.workspaces.runtime.start(rt.container_name(work["id"]))
    app.state.workspaces.close_channels()

    def broken(*args, **kwargs):
        raise rt.RuntimeFailed("planted: the sign-out container cannot be made")

    monkeypatch.setattr(app.state.workspaces.runtime, "start_sign_in", broken)
    r = client.get("/api/claude", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["signing_out"] is True  # still waiting: said as it is


# --- races and their order ------------------------------------------------------------

import threading  # noqa: E402

from rcman import accounts as acc  # noqa: E402
from rcman import claude_server  # noqa: E402
from rcman import workspaces as ws  # noqa: E402

from helpers.screens import LOGIN_SUCCEEDED  # noqa: E402


def in_thread(call, *args) -> tuple[threading.Thread, list]:
    out: list = []

    def run():
        try:
            out.append(call(*args))
        except Exception as error:  # noqa: BLE001 - the test reads it
            out.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize("keep", [True, False])
def test_a_start_in_progress_does_not_escape_a_sign_out(client, app, monkeypatch, keep):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    entered, release = threading.Event(), threading.Event()
    real = claude_server.Server.start

    def held(self, prefix):
        entered.set()
        assert release.wait(30)
        return real(self, prefix)

    monkeypatch.setattr(claude_server.Server, "start", held)
    workspaces = app.state.workspaces
    starting, _ = in_thread(workspaces.start_server, workspaces.get(work["id"]), "pfx")
    assert entered.wait(10)
    signing_out, _ = in_thread(app.state.accounts.sign_out, person(app, ALICE), keep)
    signing_out.join(0.5)
    assert signing_out.is_alive()  # it waits for the start to finish
    release.set()
    starting.join(30)
    signing_out.join(30)
    if keep:  # the server started first, and keeps the account until it stops
        assert running(client, work)
        assert fake.signed_in(account_file(app, ALICE).parent)
    else:  # the server started first, and was stopped by the sign-out
        assert not running(client, work)
        assert account_file(app, ALICE).read_bytes() == b""


@pytest.fixture
def quick_periodic_checks(monkeypatch):
    """rcman's periodic checks every fraction of a second. The service reads the interval
    when it starts, so a test takes this before ``client``, which starts it."""
    monkeypatch.setattr(ws, "IDLE_CHECK_SECONDS", 0.2)


def test_a_server_that_ends_by_itself_finishes_a_waiting_sign_out(
    quick_periodic_checks, client, app
):
    """Nothing but rcman's own periodic checks: no stop, and no Claude page read."""
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)
    name = rt.container_name(work["id"])
    app.state.workspaces.close_channels()
    app.state.workspaces.runtime.stop(name)  # its server ends
    app.state.workspaces.runtime.start(name)
    deadline = time.monotonic() + 30
    while fake.signed_in(account_file(app, ALICE).parent):
        assert time.monotonic() < deadline, "the sign-out never finished"
        time.sleep(0.2)
    assert account_file(app, ALICE).read_bytes() == b""


def test_a_sign_in_begun_while_a_sign_out_waits_is_not_ended_by_it(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)
    r = client.post("/api/claude/login", json={}, headers=changing(ALICE))
    assert r.status_code == 200
    client.post("/api/servers/stop", json={"workspace": work["id"]}, headers=changing(ALICE))
    assert sign_ins_left(app)  # the sign-in still waits for its code
    r = client.post("/api/claude/code", json={"code": fake.ACCEPTED_CODE}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["connection"] == "connected" and r.json()["signing_out"] is False


def test_a_code_too_late_for_the_sign_in_container_is_refused(client, app, monkeypatch):
    """The container ends itself CODE_WAIT_SECONDS after it starts; a code that would
    reach it too close to that is refused rather than lost."""
    monkeypatch.setattr(acc, "CODE_WAIT_SECONDS", 2 * acc.LOGIN_TIMEOUT)
    client.post("/api/claude/login", json={}, headers=changing(ALICE))
    r = client.post("/api/claude/code", json={"code": fake.ACCEPTED_CODE}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "no_login_in_progress")
    assert sign_ins_left(app) == []


def test_a_stop_succeeds_whatever_finishing_a_sign_out_does(client, app):
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)

    def broken(person_id):
        raise RuntimeError("planted: settling failed")

    app.state.workspaces.on_stopped = broken
    r = client.post("/api/servers/stop", json={"workspace": work["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/workspaces/{work['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text


def test_two_sign_ins_at_once_leave_one_which_connects(client, app):
    accounts, alice = app.state.accounts, person(app, ALICE)
    first, a = in_thread(accounts.begin, alice, ALICE)
    second, b = in_thread(accounts.begin, alice, ALICE)
    first.join(60)
    second.join(60)
    assert [type(x) for x in a + b] == [dict, dict], a + b
    assert len(sign_ins_left(app)) == 1  # the later replaced the earlier
    r = client.post("/api/claude/code", json={"code": fake.ACCEPTED_CODE}, headers=changing(ALICE))
    assert r.json()["connection"] == "connected"
    assert sign_ins_left(app) == []


def test_a_start_seeds_the_account_a_sign_in_finishing_meanwhile_leaves(client, app, monkeypatch):
    work = made_workspace(client, ALICE)
    connect(client, email="old@example.org")
    trusted(app, work)
    accounts = app.state.accounts
    accounts.begin(person(app, ALICE), "new@example.org")
    entered, release = threading.Event(), threading.Event()
    real = acc.Accounts._give_code

    def held(self, sign_in, code):
        entered.set()
        assert release.wait(30)
        return real(self, sign_in, code)

    monkeypatch.setattr(acc.Accounts, "_give_code", held)
    finishing, _ = in_thread(accounts.finish, person(app, ALICE), fake.ACCEPTED_CODE)
    assert entered.wait(10)
    workspaces = app.state.workspaces
    starting, answer = in_thread(workspaces.start_server, workspaces.get(work["id"]), "pfx")
    starting.join(0.5)
    assert starting.is_alive()  # the start waits for the sign-in
    release.set()
    finishing.join(60)
    starting.join(60)
    assert isinstance(answer[0], dict), answer
    layout = workspaces.runtime.layout(rt.container_name(work["id"]))
    seeded = json.loads((Path(layout.config) / fake.CONFIG_FILE).read_text())
    assert seeded["oauthAccount"]["emailAddress"] == "new@example.org"


def test_the_sweep_leaves_a_person_whose_lock_is_held_for_its_next_round(client, app):
    """A sign-in holds the person's lock for up to LOGIN_TIMEOUT; the periodic sweep,
    which also closes everyone's idle channels, does not wait for it."""
    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    start(client, work)
    sign_out(client, True)
    client.post("/api/servers/stop", json={"workspace": work["id"]}, headers=changing(ALICE))
    workspaces = app.state.workspaces
    con = db.connect(app.state.settings.database)
    try:
        people.set_claude_account(con, person(app, ALICE).id, {"oauthAccount": {}}, True)
    finally:
        con.close()
    held, release = threading.Event(), threading.Event()

    def hold():
        with workspaces.person_lock(person(app, ALICE).id):
            held.set()
            release.wait(30)

    holder, _ = in_thread(hold)
    assert held.wait(10)
    try:
        sweeping, _ = in_thread(workspaces.settle_signing_out)
        sweeping.join(2)
        assert not sweeping.is_alive()  # it did not wait for the lock
        assert people.claude_account(
            db.connect(app.state.settings.database), person(app, ALICE).id
        )[1]
    finally:
        release.set()
        holder.join(30)
    workspaces.settle_signing_out()  # the next round finishes it
    assert account_file(app, ALICE).read_bytes() == b""


def test_a_workspace_not_seeded_is_refused_by_claude_and_a_seeded_one_starts(
    client, app, monkeypatch
):
    """Claude's Remote Control needs ``oauthAccount``, which the credentials file does not
    hold (tests/fakes/fixtures/rc_no_oauth_account.txt, as seen with a real account)."""
    from rcman.workspaces import Workspaces

    work = made_workspace(client, ALICE)
    connect(client)
    trusted(app, work)
    monkeypatch.setattr(Workspaces, "_seed", lambda self, workspace, account: None)
    r = start(client, work)
    assert r.status_code == 200, r.text
    server = r.json()["server"]
    assert (server["state"], server["exit_code"]) == ("exited", 1), server
    assert "Unable to determine your organization" in "\n".join(server["screen"])
    monkeypatch.undo()
    r = start(client, work)
    assert r.status_code == 200 and r.json()["server"]["state"] in ("connected", "ready"), r.text


class LoginEndingAfterTheFirstRead:
    """A tmux whose login prints its success line and exits right after the first thing it
    is asked: what is read first is what it looked like before, what second, after."""

    def __init__(self):
        self.asked = 0

    def _ended(self) -> bool:
        self.asked += 1
        return self.asked > 1

    def screen(self):
        return [LOGIN_SUCCEEDED] if self._ended() else []

    def exit_status(self):
        return 0 if self._ended() else None


def test_a_login_that_ends_between_the_two_reads_is_never_an_exit_with_a_screen_from_before():
    """The state of a login is its screen and its exit status, read one after the other: one
    that ends between them must not be read as exited with the screen it had before its
    success line, as happens when the screen is read first."""
    from types import SimpleNamespace

    sign_in = SimpleNamespace(tmux=LoginEndingAfterTheFirstRead())
    found = acc.Accounts.__new__(acc.Accounts)._wait(sign_in, lambda state: True)
    assert found["state"] == "succeeded", found
