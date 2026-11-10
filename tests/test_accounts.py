"""Each person's Claude sign-in (backend/rcman/accounts.py and routers/claude.py): one
credentials file shared by every workspace of theirs, signing in through a short-lived
sign-in container, signing out of Claude and of rcman, on the host runtime with the
fake `claude`."""

import json
from pathlib import Path

import pytest
from rcman import db, people
from rcman import runtime as rt

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing, made_workspace
from tests.hang_guard import HANG_GUARD

URL_START = "https://claude.com/cai/oauth/authorize?"


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert app.state.workspaces.restored.wait(HANG_GUARD)
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


def start(client, workspace: dict):
    return client.post("/api/servers", json={"workspace": workspace["id"]}, headers=changing(ALICE))


def running(client, workspace: dict) -> bool:
    listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]
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


def test_the_code_reaches_claude_through_stdin_and_never_an_argv(client, app, monkeypatch):
    from rcman import channel

    argvs, stdins = [], []
    runtime = app.state.workspaces.runtime
    real_exec, real_run = runtime.exec, channel.Channel.run

    def exec_(name, argv, stdin=None, timeout=HANG_GUARD, cwd=None, keep=None, again=False):
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


# --- servers on the shared sign-in ----------------------------------------------------


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


# --- signing out of Claude --------------------------------------------------------------


def sign_out(client, keep: bool):
    return client.post("/api/claude/logout", json={"keep_sessions": keep}, headers=changing(ALICE))


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
    first, second = (made_workspace(client, ALICE, n) for n in ("one", "two"))
    connect(client)
    for workspace in (first, second):
        trusted(app, workspace)
    start(client, first)
    start(client, second)
    answer = sign_out(client, True).json()
    assert answer["signing_out"] is True and answer["connection"] != "not_connected"
    assert running(client, first) and running(client, second)  # nothing was stopped
    assert fake.signed_in(account_file(app, ALICE).parent)  # they keep the credentials
    client.post("/api/servers/stop", json={"workspace": first["id"]}, headers=changing(ALICE))
    assert fake.signed_in(account_file(app, ALICE).parent)  # one still runs
    client.post("/api/servers/stop", json={"workspace": second["id"]}, headers=changing(ALICE))
    assert account_file(app, ALICE).read_bytes() == b""
    found = client.get("/api/claude", headers=as_user(ALICE)).json()
    assert found["connection"] == "not_connected" and found["signing_out"] is False


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


def test_deleting_a_person_removes_their_sign_in(client, app):
    made_workspace(client, BOB)
    connect(client, BOB)
    bob = person(app, BOB)
    folder = app.state.workspaces.runtime._account_file(bob.id).parent
    assert folder.exists()  # positive control
    r = client.delete(f"/api/people/{bob.id}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert not folder.exists() and person(app, BOB) is None


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
