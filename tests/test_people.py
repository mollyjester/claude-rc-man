"""The people who may use rcman: their table (backend/rcman/people.py), the allowed list
oauth2-proxy reads, and the People page's routes (backend/rcman/routers/people.py)."""

import os
import stat

import pytest
from rcman import db, people
from rcman.limits import Usage
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


# --- limits: the routes ------------------------------------------------------------------

LIMITS = {"memory": "2G", "cpu": "1.5", "pids": "500"}
NO_LIMITS = {"memory": None, "cpu": None, "pids": None}


def limited(runtime) -> dict:
    """The slices that hold a limit: restore, as the app starts, sets every slice, an empty
    set resetting it."""
    return {name: held for name, held in runtime.limits.items() if held.limited}


def person_id(client, email=BOB) -> int:
    return next(p["id"] for p in listed(client) if p["email"] == email)


def audited(settings, count=1):
    import sqlite3

    con = sqlite3.connect(settings.database)
    try:
        found = con.execute(
            "select action, target, result, email from audit order by id"
        ).fetchall()
    finally:
        con.close()
    return found[-count:]


def test_the_list_shows_each_persons_stored_limits_and_asks_the_unit_manager_nothing(client, app):
    asked = []
    runtime = app.state.workspaces.runtime
    runtime.usage = lambda *a: asked.append(a) or Usage()
    assert all(p["limits"] == NO_LIMITS for p in listed(client))
    r = client.put(f"/api/people/{person_id(client)}/limits", json=LIMITS, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    asked.clear()  # setting a limit may read the use; listing never does
    found = {p["email"]: p["limits"] for p in listed(client)}
    assert found == {ALICE: NO_LIMITS, BOB: LIMITS, CAROL: NO_LIMITS}
    assert asked == []  # the list is the table's; the use is read by the limits route


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


def test_limits_left_null_are_no_limit_and_a_set_replaces_the_whole_of_the_last(client, settings):
    bob = person_id(client)
    client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    r = client.put(
        f"/api/people/{bob}/limits",
        json={"memory": "512M", "cpu": None, "pids": None},
        headers=changing(ALICE),
    )
    assert r.json() == {"limits": {"memory": "512M", "cpu": None, "pids": None}}
    assert audited(settings)[0][1] == f"{BOB} memory=512M cpu=none pids=none"
    r = client.put(f"/api/people/{bob}/limits", json=NO_LIMITS, headers=changing(ALICE))
    assert r.json() == {"limits": NO_LIMITS}
    assert next(p for p in listed(client) if p["id"] == bob)["limits"] == NO_LIMITS


def test_the_whole_set_is_sent_or_the_request_is_refused(client):
    bob = person_id(client)
    for left_out in LIMITS:
        body = {k: v for k, v in LIMITS.items() if k != left_out}
        r = client.put(f"/api/people/{bob}/limits", json=body, headers=changing(ALICE))
        assert r.status_code == 422, left_out
        assert r.json()["errors"] == [{"field": left_out, "message": "This is required."}]


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("memory", "lots", rules.MEMORY),
        ("memory", "2", rules.MEMORY),
        ("memory", "1.5G", rules.MEMORY),
        ("memory", "1M", rules.MEMORY),  # below the minimum
        ("memory", "0G", rules.MEMORY),
        ("cpu", "x", rules.CPU),
        ("cpu", "1.234", rules.CPU),
        ("cpu", "0.01", rules.CPU),  # below the minimum
        ("cpu", "0", rules.CPU),
        ("pids", "1.5", rules.PIDS),
        ("pids", "-1", rules.PIDS),
        ("pids", "1", rules.PIDS),  # below the minimum
        ("pids", "0", rules.PIDS),
    ],
)
def test_a_value_the_rule_or_the_minimum_refuses_is_refused_at_its_field_in_the_rules_words(
    client, settings, field, value, rule
):
    bob = person_id(client)
    r = client.put(
        f"/api/people/{bob}/limits", json={**LIMITS, field: value}, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert r.json()["errors"] == [{"field": field, "message": rule.sentence}]
    assert audited(settings) == [("person.limits", None, "validation_failed", ALICE)]
    assert next(p for p in listed(client) if p["id"] == bob)["limits"] == NO_LIMITS


def test_the_smallest_value_each_rule_allows_is_set(client):
    from rcman import limits

    bob = person_id(client)
    smallest = {
        "memory": limits.format_memory(rules.MIN_MEMORY_MIB),
        "cpu": limits.format_cpu(rules.MIN_CPU_PERCENT),
        "pids": str(rules.MIN_PIDS),
    }
    r = client.put(f"/api/people/{bob}/limits", json=smallest, headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"limits": smallest})


def test_limits_of_no_one_are_not_found_and_are_not_applied(client, app, settings):
    r = client.put("/api/people/999/limits", json=LIMITS, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (404, "person_not_found")
    assert limited(app.state.workspaces.runtime) == {}
    assert audited(settings) == [("person.limits", None, "person_not_found", ALICE)]
    assert client.get("/api/people/999/limits", headers=as_user(ALICE)).status_code == 404


def test_a_limit_below_use_is_refused_in_a_problem_naming_it_and_audited_with_the_values(
    client, app, settings
):
    from rcman import limits

    bob = person_id(client)
    app.state.workspaces.runtime.used[limits.person_slice(bob)] = limits.Usage(3000 * 1024**2, 7)
    r = client.put(
        f"/api/people/{bob}/limits",
        json={**NO_LIMITS, "memory": "2G"},
        headers=changing(ALICE),
    )
    assert (r.status_code, r.json()["code"]) == (409, "limit_below_use")
    assert (r.json()["limit"], r.json()["used"]) == ("memory", "3000M")
    assert "3000M" in r.json()["title"]
    assert limited(app.state.workspaces.runtime) == {}
    assert audited(settings) == [
        ("person.limits", f"{BOB} memory=2G cpu=none pids=none", "limit_below_use", ALICE)
    ]


def test_limits_that_could_not_be_applied_are_a_problem_and_change_nothing(client, app):
    from rcman import runtime as rt

    bob = person_id(client)
    app.state.workspaces.runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    r = client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (502, "limits_not_applied")
    app.state.workspaces.runtime.limits_error = None
    assert next(p for p in listed(client) if p["id"] == bob)["limits"] == NO_LIMITS


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


# a workspace's


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


def test_a_workspace_that_is_another_persons_or_not_there_is_not_found(client, app, settings):
    hers = made_workspace(client, CAROL, "hers")
    bob = person_id(client)
    for workspace in (hers["id"], 999):
        r = client.put(
            f"/api/people/{bob}/workspaces/{workspace}/limits",
            json=LIMITS,
            headers=changing(ALICE),
        )
        assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    assert limited(app.state.workspaces.runtime) == {}
    assert audited(settings) == [("workspace.limits", None, "workspace_not_found", ALICE)]
    r = client.put("/api/people/999/workspaces/1/limits", json=LIMITS, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (404, "person_not_found")


def test_a_workspace_that_is_not_ready_can_be_given_limits(client, app, settings):
    import threading

    release = threading.Event()
    runtime = app.state.workspaces.runtime
    real = runtime.create
    runtime.create = lambda spec: (release.wait(30), real(spec))
    try:
        r = client.post("/api/workspaces", json={"name": "slow"}, headers=changing(BOB))
        workspace = r.json()["workspace"]
        assert workspace["state"] == "creating"
        r = client.put(
            f"/api/people/{person_id(client)}/workspaces/{workspace['id']}/limits",
            json=LIMITS,
            headers=changing(ALICE),
        )
        assert r.status_code == 200, r.text
    finally:
        release.set()
    made_workspace_ready = made_workspace  # the workspace is made with them applied
    assert made_workspace_ready and limited(runtime)


def test_a_workspace_limit_is_refused_in_the_same_fields_as_a_persons(client):
    mine = made_workspace(client, BOB, "mine")
    r = client.put(
        f"/api/people/{person_id(client)}/workspaces/{mine['id']}/limits",
        json={**LIMITS, "cpu": "many"},
        headers=changing(ALICE),
    )
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "cpu", "message": rules.CPU.sentence}]


def test_a_workspace_limit_below_what_the_workspace_uses_is_refused(client, app):
    from rcman import limits

    mine = made_workspace(client, BOB, "mine")
    bob = person_id(client)
    app.state.workspaces.runtime.used[limits.workspace_slice(bob, mine["id"])] = limits.Usage(
        0, 300
    )
    r = client.put(
        f"/api/people/{bob}/workspaces/{mine['id']}/limits",
        json={**NO_LIMITS, "pids": "100"},
        headers=changing(ALICE),
    )
    assert (r.status_code, r.json()["code"]) == (409, "limit_below_use")
    assert (r.json()["limit"], r.json()["used"]) == ("pids", "300")


def test_limits_are_for_administrators_only_and_a_person_sees_nothing_of_theirs(client):
    mine = made_workspace(client, BOB, "mine")
    bob = person_id(client)
    for method, path, body in (
        ("GET", f"/api/people/{bob}/limits", None),
        ("PUT", f"/api/people/{bob}/limits", LIMITS),
        ("PUT", f"/api/people/{bob}/workspaces/{mine['id']}/limits", LIMITS),
    ):
        kwargs = {"json": body} if body else {}
        r = client.request(method, path, headers=changing(BOB), **kwargs)
        assert (r.status_code, r.json()["code"]) == (403, "admin_only"), path
    client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    assert "limit" not in str(client.get("/api/workspaces", headers=as_user(BOB)).json())
    assert "limit" not in str(client.get("/api/me", headers=as_user(BOB)).json())
    assert "limit" not in str(client.get("/api/me/settings", headers=as_user(BOB)).json())


def test_deleting_a_person_drops_their_limits(client, app):
    from rcman import limits

    mine = made_workspace(client, BOB, "mine")
    bob = person_id(client)
    client.put(f"/api/people/{bob}/limits", json=LIMITS, headers=changing(ALICE))
    client.put(
        f"/api/people/{bob}/workspaces/{mine['id']}/limits", json=LIMITS, headers=changing(ALICE)
    )
    runtime = app.state.workspaces.runtime
    assert len(limited(runtime)) == 2  # positive control
    assert client.delete(f"/api/people/{bob}", headers=changing(ALICE)).status_code == 200
    assert limited(runtime) == {}
    assert runtime.dropped[-1] == limits.person_slice(bob)


def test_the_last_slot_given_is_the_last_a_person_gets(con):
    assert people.add(con, "a@x.io", False, last_slot=2).slot == 1
    assert people.add(con, "b@x.io", False, last_slot=2).slot == 2
    assert refused(lambda *a: people.add(*a, last_slot=2), con, "c@x.io", False) == "people_full"
    assert people.add(con, "c@x.io", False).slot == 3  # the control: the product's last slot


def test_the_route_gives_no_slot_at_or_above_the_apps_builder_slot(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    from tests.conftest import PEOPLE

    full = create_app(settings, runtime=runtime, builder_slot=len(PEOPLE) + 1)
    assert full.state.workspaces.builder_slot == len(PEOPLE) + 1
    with TestClient(full) as c:
        r = c.post("/api/people", json={"email": "e@x.io"}, headers=changing(ALICE))
        assert (r.status_code, r.json()["code"]) == (409, "people_full"), r.json()
    room = create_app(settings, runtime=runtime)  # the control: the product's builder slot
    with TestClient(room) as c:
        r = c.post("/api/people", json={"email": "e@x.io"}, headers=changing(ALICE))
        assert r.status_code == 200 and r.json()["person"]["email"] == "e@x.io", r.json()
