"""A workspace's server through the routes, on the host runtime with the fake claude."""

import sqlite3
import time

from rcman import db
from rcman.problems import FIELD_MESSAGES

from tests.conftest import ALICE, BOB, as_user, changing, fake_claude, made_workspace, signed_in


def workspaces(client, who=ALICE) -> list[dict]:
    return client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]


def server_of(client, workspace, who=ALICE):
    return next(w for w in workspaces(client, who) if w["id"] == workspace["id"])["server"]


def wait_state(client, workspace, state, who=ALICE):
    server = None
    for _ in range(100):
        server = server_of(client, workspace, who)
        if server and server["state"] == state:
            return server
        time.sleep(0.1)
    raise AssertionError(f"never {state}: {server}")


def post(client, verb, workspace, who=ALICE, **body):
    path = "/api/servers" + (f"/{verb}" if verb else "")
    return client.post(path, json={"workspace": workspace["id"], **body}, headers=changing(who))


def wanted(settings, workspace) -> tuple[int, int, str | None]:
    con = db.connect(settings.database)
    try:
        found = con.execute(
            "select server, wanted, prefix from workspaces where id = ?", (workspace["id"],)
        ).fetchone()
        return tuple(found)
    finally:
        con.close()


def test_a_start_answers_not_signed_in_until_claude_is(client, app):
    made = made_workspace(client, ALICE, "alpha")
    r = post(client, "", made)
    assert (r.status_code, r.json()["code"]) == (409, "claude_not_logged_in")
    assert server_of(client, made) is None
    signed_in(app, made)
    assert post(client, "", made).status_code == 200  # positive control


def test_the_lifecycle_records_what_should_be_running(client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    r = post(client, "", made)
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "trust_prompt"
    assert wanted(settings, made) == (1, 1, "alpha")  # named after the workspace by default
    r = post(client, "confirm", made, answer="y")
    assert r.json()["server"]["state"] in ("connected", "ready")
    ready = wait_state(client, made, "ready")
    assert ready["session_name"].startswith("alpha-")

    output = client.get(
        "/api/servers/output", params={"workspace": made["id"]}, headers=as_user(ALICE)
    )
    assert output.json()["screen"]

    assert post(client, "stop", made).json()["server"]["state"] == "stopped"
    assert wanted(settings, made) == (1, 0, "alpha")
    resumed = post(client, "resume", made).json()["server"]
    assert resumed["state"] in ("connected", "ready")
    assert wanted(settings, made)[:2] == (1, 1)
    post(client, "stop", made)
    assert post(client, "forget", made).status_code == 200
    assert server_of(client, made) is None
    assert wanted(settings, made)[:2] == (0, 0)


def test_the_prefix_set_names_the_sessions(client, app, settings):
    client.patch("/api/me/settings", json={"prefix": "mine"}, headers=changing(ALICE))
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made, trusted=True)
    post(client, "", made)
    assert wait_state(client, made, "ready")["session_name"].startswith("mine-")
    assert wanted(settings, made)[2] == "mine"


def test_conflicts_are_409(client, app):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    post(client, "", made)
    r = post(client, "", made)
    assert (r.status_code, r.json()["code"]) == (409, "already_running")


def test_a_workspace_that_is_not_ready_has_no_server(client, app):
    import threading

    release = threading.Event()
    runtime = app.state.workspaces.runtime
    real = runtime.create
    runtime.create = lambda spec: (release.wait(30), real(spec))
    try:
        made = client.post("/api/workspaces", json={"name": "slow"}, headers=changing(ALICE))
        r = post(client, "", made.json()["workspace"])
        assert (r.status_code, r.json()["code"]) == (409, "workspace_not_ready")
    finally:
        release.set()


def test_the_answer_is_validated(client):
    made = made_workspace(client, ALICE, "alpha")
    r = post(client, "confirm", made, answer="maybe")
    assert r.status_code == 422
    expected = FIELD_MESSAGES["literal_error"].format(expected="'y' or 'n'")
    assert r.json()["errors"] == [{"field": "answer", "message": expected}]


def test_administrators_see_every_persons_workspaces_and_servers(client, app):
    made = made_workspace(client, ALICE, "alpha")
    made_workspace(client, BOB, "beta")
    signed_in(app, made)
    post(client, "", made)
    r = client.get("/api/servers/everyone", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    people = {p["email"]: p for p in r.json()["people"]}
    assert set(people) == {ALICE, BOB, "carol@example.com"}
    [alpha] = people[ALICE]["workspaces"]
    assert alpha["name"] == "alpha" and alpha["server"]["state"] == "trust_prompt"
    assert set(alpha["server"]) == {"state", "session_name", "exit_code"}  # never its screen
    assert [(w["name"], w["server"]) for w in people[BOB]["workspaces"]] == [("beta", None)]
    assert people["carol@example.com"]["workspaces"] == []


def test_people_who_are_not_administrators_cannot_see_everyone(client):
    made_workspace(client, ALICE, "alpha")
    r = client.get("/api/servers/everyone", headers=as_user(BOB))
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")
    assert "alpha" not in r.text


def in_scenario(app, tmp_path, scenario):
    """Every claude the workspaces run from now on runs the fake's ``scenario``."""
    fake_claude(app.state.workspaces.runtime.claude, env=f"FAKE_CLAUDE_SCENARIO={scenario}")


def last_audit(settings):
    return (
        sqlite3.connect(settings.database)
        .execute("select action, target, result from audit")
        .fetchall()[-1]
    )


def test_an_action_ending_on_an_unrecognised_screen_is_audited_as_such(
    client, app, settings, tmp_path
):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    in_scenario(app, tmp_path, "unrecognised")
    r = post(client, "", made)
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "unrecognised"
    assert last_audit(settings) == ("server.start", "alpha", "unrecognised_screen")


def test_an_action_ending_reconnected_is_audited_ok(client, app, settings, tmp_path):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    in_scenario(app, tmp_path, "reconnected_without_status")
    r = post(client, "", made)
    assert r.json()["server"]["state"] == "reconnected"
    assert last_audit(settings) == ("server.start", "alpha", "ok")


def audit_rows(settings):
    return (
        sqlite3.connect(settings.database)
        .execute("select action, target, result from audit order by rowid")
        .fetchall()
    )


def at_the_remote_control_prompt(client, app, settings, tmp_path):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    in_scenario(app, tmp_path, "remote_control_prompt")
    r = post(client, "", made)
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "remote_control_prompt"
    return made


def test_a_start_at_the_remote_control_prompt_is_a_known_state_audited_ok(
    client, app, settings, tmp_path
):
    at_the_remote_control_prompt(client, app, settings, tmp_path)
    assert last_audit(settings) == ("server.start", "alpha", "ok")
    # The twin above is audited as unrecognised, so "ok" here is the state being known.
    assert "unrecognised_screen" not in {result for _, _, result in audit_rows(settings)}


def test_yes_at_the_remote_control_prompt_starts_the_server(client, app, settings, tmp_path):
    made = at_the_remote_control_prompt(client, app, settings, tmp_path)
    r = post(client, "confirm", made, answer="y")
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] in ("connected", "ready")
    assert wait_state(client, made, "ready")["session_name"].startswith("alpha-")
    assert last_audit(settings) == ("server.confirm", "alpha", "ok")
    assert post(client, "confirm", made, answer="y").status_code == 409  # no longer asked


def test_no_at_the_remote_control_prompt_ends_the_server_and_is_audited_ok(
    client, app, settings, tmp_path
):
    made = at_the_remote_control_prompt(client, app, settings, tmp_path)
    r = post(client, "confirm", made, answer="n")
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "exited"
    assert last_audit(settings) == ("server.confirm", "alpha", "ok")
    assert wanted(settings, made)[:2] == (1, 1)  # still wanted: a restore asks again
    assert post(client, "resume", made).json()["server"]["state"] == "remote_control_prompt"


def test_a_workspace_that_cannot_be_reached_is_listed_with_why(client, app):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    post(client, "", made)
    app.state.workspaces.runtime.stop(f"rcman-ws-{made['id']}")
    app.state.workspaces.close_channels()
    [found] = workspaces(client)
    assert found["server"] is None
    assert found["server_error"]["code"] == "workspace_failed"
    assert workspaces(client, BOB) == []
