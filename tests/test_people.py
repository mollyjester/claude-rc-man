"""The people who may use rcman: their table (backend/rcman/people.py), the allowed list
oauth2-proxy reads, and the People page's routes (backend/rcman/routers/people.py)."""

import os
import stat

import pytest
from rcman import db, people
from rcman.problems import Problem
from rcman.runtime import container_name

from helpers import rules
from tests.conftest import ALICE, BOB, CAROL, as_user, changing, made_workspace


@pytest.fixture
def con(tmp_path):
    path = tmp_path / "rcman.db"
    db.migrate(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def refused(call, *args) -> str:
    with pytest.raises(Problem) as raised:
        call(*args)
    return raised.value.code


# --- the table ---------------------------------------------------------------------


def test_people_get_the_lowest_free_slot(con):
    assert [people.add(con, e, False).slot for e in ("a@x.io", "b@x.io", "c@x.io")] == [1, 2, 3]
    people.remove(con, people.by_email(con, "b@x.io"))
    assert people.add(con, "d@x.io", False).slot == 2  # a freed slot is used again


def test_an_address_is_kept_in_lowercase_and_is_one_persons_in_any_case(con):
    added = people.add(con, "Dave@Example.COM", True)
    assert (added.email, added.admin, added.state) == ("dave@example.com", True, "active")
    assert refused(people.add, con, "DAVE@example.com", False) == "email_taken"
    assert people.by_email(con, "dAvE@example.com") == added


def test_there_is_room_for_max_people_and_no_more(con, monkeypatch):
    monkeypatch.setattr(people, "MAX_PEOPLE", 2)
    people.add(con, "a@x.io", False)
    people.add(con, "b@x.io", False)
    assert refused(people.add, con, "c@x.io", False) == "people_full"
    assert people.by_email(con, "c@x.io") is None


def test_the_builders_slot_is_above_every_persons_and_no_person_is_given_it(con):
    assert people.BUILDER_SLOT == people.MAX_PEOPLE + 1
    slots = [people.add(con, f"p{n}@x.io", False).slot for n in range(people.MAX_PEOPLE)]
    assert max(slots) == people.MAX_PEOPLE and len(set(slots)) == people.MAX_PEOPLE  # all given
    assert people.BUILDER_SLOT not in slots and 0 not in slots
    assert refused(people.add, con, "one-more@x.io", False) == "people_full"


@pytest.mark.parametrize("email", ["nope", "-x@example.com", "a b@example.com", ""])
def test_an_address_the_rule_refuses_is_refused(con, email):
    assert refused(people.add, con, email, False) == "email_invalid"
    assert people.everyone(con) == []


def test_the_slots_fit_the_subordinate_range_the_installer_gives(con):
    """Every slot's uid range lies inside rcman's subordinate ids (installer/install.py)."""
    from rcman.runtime import RANGE

    from installer import install as ins

    assert ins.SUBORDINATE_COUNT >= (people.MAX_PEOPLE + 1) * RANGE


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


def test_the_allowed_list_is_replaced_whole_by_a_rename(con, tmp_path):
    allowed = tmp_path / "allowed-emails"
    people.add(con, "alice@example.com", True)
    people.write_allowed(con, allowed)
    before = allowed.stat().st_ino
    people.add(con, "bob@example.com", False)
    people.write_allowed(con, allowed)
    assert allowed.stat().st_ino != before  # a new file took its name
    assert sorted(os.listdir(tmp_path)) == [
        "allowed-emails",
        "rcman.db",
        "rcman.db-shm",
        "rcman.db-wal",
    ]


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


def test_an_address_taken_is_refused(client):
    r = client.post("/api/people", json={"email": BOB.upper()}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "email_taken")


def test_an_address_is_checked_by_its_rule(client):
    r = client.post("/api/people", json={"email": "-x@example.com"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "email", "message": rules.EMAIL.sentence}]


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


def test_anyone_may_be_deleted_the_last_administrator_and_oneself_too(client):
    alice = next(p for p in listed(client) if p["email"] == ALICE)
    r = client.delete(f"/api/people/{alice['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=as_user(ALICE)).status_code == 403


def test_deleting_no_one_is_not_found(client):
    r = client.delete("/api/people/999", headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (404, "person_not_found")


def test_people_are_for_administrators_only(client):
    assert client.get("/api/people", headers=as_user(BOB)).status_code == 403
    r = client.post("/api/people", json={"email": "d@example.com"}, headers=changing(BOB))
    assert r.status_code == 403
    assert client.get("/api/me", headers=as_user("d@example.com")).status_code == 403


def test_a_person_whose_workspace_cannot_be_removed_stays_let_in_nowhere(
    client, app, settings, monkeypatch
):
    from rcman import runtime as rt

    made_workspace(client, BOB, "stuck")
    runtime = app.state.workspaces.runtime

    def refusing(name):
        raise rt.RuntimeFailed("planted: it will not go")

    monkeypatch.setattr(runtime, "remove", refusing)
    bob = next(p for p in listed(client) if p["email"] == BOB)
    r = client.delete(f"/api/people/{bob['id']}", headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 403
    assert BOB not in settings.allowed_emails.read_text()
    assert [p["state"] for p in listed(client) if p["email"] == BOB] == ["deleting"]
