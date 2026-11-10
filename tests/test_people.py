"""The people who may use rcman: their table (backend/rcman/people.py), the allowed list
oauth2-proxy reads, and the People page's routes (backend/rcman/routers/people.py)."""

import stat

import pytest
from rcman import db, people
from rcman.runtime import container_name

from tests.conftest import ALICE, BOB, CAROL, as_user, changing, made_workspace


@pytest.fixture
def con(tmp_path):
    path = tmp_path / "rcman.db"
    db.migrate(path)
    connection = db.connect(path)
    yield connection
    connection.close()


# --- the table ---------------------------------------------------------------------


def test_people_get_the_lowest_free_slot(con):
    assert [people.add(con, e, False).slot for e in ("a@x.io", "b@x.io", "c@x.io")] == [1, 2, 3]
    people.remove(con, people.by_email(con, "b@x.io"))
    assert people.add(con, "d@x.io", False).slot == 2  # a freed slot is used again


# --- the allowed list -----------------------------------------------------------------


def test_the_allowed_list_is_every_active_persons_address(con, tmp_path):
    people.add(con, "Bob@Example.com", False)
    people.add(con, "alice@example.com", True)
    gone = people.add(con, "carol@example.com", False)
    people.mark_deleting(con, gone)
    allowed = tmp_path / "allowed-emails"
    people.write_allowed(con, allowed)
    assert allowed.read_text() == "alice@example.com\nbob@example.com\n"
    assert stat.S_IMODE(allowed.stat().st_mode) == 0o640


# --- the routes ------------------------------------------------------------------------


def listed(client) -> list[dict]:
    return client.get("/api/people", headers=as_user(ALICE)).json()["people"]


def test_administrators_list_everyone(client):
    found = listed(client)
    assert [(p["email"], p["admin"]) for p in found] == [
        (ALICE, True),
        (BOB, False),
        (CAROL, False),
    ]
    assert all({"id", "email", "admin", "workspaces"} <= set(p) for p in found)


def test_adding_a_person_lets_them_in_at_once(client, settings):
    assert client.get("/api/me", headers=as_user("dave@example.com")).status_code == 403
    r = client.post(
        "/api/people", json={"email": "Dave@Example.com", "admin": True}, headers=changing(ALICE)
    )
    assert r.status_code == 200, r.text
    assert r.json()["person"]["email"] == "dave@example.com"
    assert client.get("/api/me", headers=as_user("dave@example.com")).json()["admin"] is True
    assert "dave@example.com\n" in settings.allowed_emails.read_text()


def test_deleting_a_person_deletes_their_workspaces_and_settings(client, app, settings):
    made_workspace(client, BOB, "one")
    made_workspace(client, BOB, "two")
    kept = made_workspace(client, CAROL, "hers")
    client.patch("/api/me/settings", json={"prefix": "bobs"}, headers=changing(BOB))
    runtime = app.state.workspaces.runtime
    assert len(runtime.labelled()) == 3  # positive control
    bob = next(p for p in listed(client) if p["email"] == BOB)
    assert bob["workspaces"] == 2

    r = client.delete(f"/api/people/{bob['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert list(runtime.labelled()) == [container_name(kept["id"])]
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 403
    assert BOB not in settings.allowed_emails.read_text()
    con = db.connect(settings.database)
    try:
        assert (
            con.execute("select count(*) from user_settings where email = ?", (BOB,)).fetchone()[0]
            == 0
        )
    finally:
        con.close()


# --- limits: the routes ------------------------------------------------------------------

LIMITS = {"memory": "2G", "cpu": "1.5", "pids": "500"}
NO_LIMITS = {"memory": None, "cpu": None, "pids": None}


def limited(runtime) -> dict:
    """The slices that hold a limit: restore, as the app starts, sets every slice, an empty
    set resetting it."""
    return {name: held for name, held in runtime.limits.items() if held.limited}


def person_id(client) -> int:
    return next(p["id"] for p in listed(client) if p["email"] == BOB)


def audited(settings):
    import sqlite3

    con = sqlite3.connect(settings.database)
    try:
        found = con.execute(
            "select action, target, result, email from audit order by id"
        ).fetchall()
    finally:
        con.close()
    return found[-1:]


def test_a_persons_limits_are_set_applied_and_audited_with_the_values(client, app, settings):
    bob = person_id(client)
    r = client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"limits": LIMITS})
    from rcman import limits

    runtime = app.state.workspaces.runtime
    assert limited(runtime) == {limits.person_slice(bob): limits.parse(*LIMITS.values())}
    assert audited(settings) == [
        ("person.limits", f"{BOB} memory=2G cpu=1.5 pids=500", "ok", ALICE)
    ]


def test_the_limits_route_reports_the_limits_and_the_use_of_the_person_and_their_workspaces(
    client, app
):
    from rcman import limits

    mine = made_workspace(client, BOB, "mine")
    made_workspace(client, CAROL, "hers")
    bob = person_id(client)
    runtime = app.state.workspaces.runtime
    runtime.used[limits.person_slice(bob)] = limits.Usage(2 * 1024**3, 9)
    client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    client.put(
        f"/api/people/{bob}/workspaces/{mine['id']}/limits",
        json={**NO_LIMITS, "pids": "80"},
        headers=changing(ALICE),
    )
    r = client.get(f"/api/people/{bob}/limits", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["person"] == {
        "limits": LIMITS,
        "usage": {"memory": "2G", "memory_bytes": 2 * 1024**3, "tasks": 9},
    }
    [workspace] = r.json()["workspaces"]  # only Bob's
    assert workspace["id"] == mine["id"] and workspace["name"] == "mine"
    assert workspace["state"] == "ready"
    assert workspace["limits"] == {**NO_LIMITS, "pids": "80"}
    assert workspace["usage"] == {"memory": "0M", "memory_bytes": 0, "tasks": 0}


def test_a_workspaces_limits_are_set_on_its_own_slice_and_audited_with_its_owner_and_name(
    client, app, settings
):
    from rcman import limits

    mine = made_workspace(client, BOB, "mine")
    bob = person_id(client)
    r = client.put(
        f"/api/people/{bob}/workspaces/{mine['id']}/limits", json=LIMITS, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()) == (200, {"limits": LIMITS})
    runtime = app.state.workspaces.runtime
    assert limited(runtime) == {
        limits.workspace_slice(bob, mine["id"]): limits.parse(*LIMITS.values())
    }
    assert audited(settings) == [
        ("workspace.limits", f"{BOB}/mine memory=2G cpu=1.5 pids=500", "ok", ALICE)
    ]


def test_a_workspace_that_is_another_persons_is_not_found(client, app):
    hers = made_workspace(client, CAROL, "hers")
    bob = person_id(client)
    r = client.put(
        f"/api/people/{bob}/workspaces/{hers['id']}/limits", json=LIMITS, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    assert limited(app.state.workspaces.runtime) == {}
