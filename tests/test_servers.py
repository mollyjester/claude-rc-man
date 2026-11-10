"""A workspace's server through the routes, on the host runtime with the fake claude."""

import sqlite3

from rcman import db

from tests.conftest import ALICE, BOB, as_user, changing, fake_claude, made_workspace, signed_in
from tests.until import until


def server_of(client, workspace):
    listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]
    return next(w for w in listed if w["id"] == workspace["id"])["server"]


def wait_ready(client, workspace):
    """The server once it is read as ready with its session's name and its environment's URL.
    A ready screen is drawn line by line: it is read as ready from its Capacity line, before
    the name's line below it and the URL's below that are drawn, and the name's line is whole
    once the URL is read."""

    def reached():
        server = server_of(client, workspace)
        drawn = server and server["session_name"] and server["environment_url"]
        return server if drawn and server["state"] == "ready" else None

    return until(reached)


def post(client, verb, workspace, **body):
    path = "/api/servers" + (f"/{verb}" if verb else "")
    return client.post(path, json={"workspace": workspace["id"], **body}, headers=changing(ALICE))


def wanted(settings, workspace) -> tuple[int, int, str | None]:
    con = db.connect(settings.database)
    try:
        found = con.execute(
            "select server, wanted, prefix from workspaces where id = ?", (workspace["id"],)
        ).fetchone()
        return tuple(found)
    finally:
        con.close()


def test_the_lifecycle_records_what_should_be_running(client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    r = post(client, "", made)
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "trust_prompt"
    assert wanted(settings, made) == (1, 1, "alpha")  # named after the workspace by default
    r = post(client, "confirm", made, answer="y")
    assert r.json()["server"]["state"] in ("connected", "ready")
    ready = wait_ready(client, made)
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
    assert wait_ready(client, made)["session_name"].startswith("mine-")
    assert wanted(settings, made)[2] == "mine"


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


def in_scenario(app, scenario):
    """Every claude the workspaces run from now on runs the fake's ``scenario``."""
    fake_claude(app.state.workspaces.runtime.claude, env=f"FAKE_CLAUDE_SCENARIO={scenario}")


def last_audit(settings):
    return (
        sqlite3.connect(settings.database)
        .execute("select action, target, result from audit")
        .fetchall()[-1]
    )


def test_an_action_ending_on_an_unrecognised_screen_is_audited_as_such(client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    in_scenario(app, "unrecognised")
    r = post(client, "", made)
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "unrecognised"
    assert last_audit(settings) == ("server.start", "alpha", "unrecognised_screen")


def test_yes_at_the_remote_control_prompt_starts_the_server(client, app, settings):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    in_scenario(app, "remote_control_prompt")
    r = post(client, "", made)
    assert r.json()["server"]["state"] == "remote_control_prompt"
    assert last_audit(settings) == ("server.start", "alpha", "ok")  # a state rcman knows
    r = post(client, "confirm", made, answer="y")
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] in ("connected", "ready")
    assert wait_ready(client, made)["session_name"].startswith("alpha-")
    assert last_audit(settings) == ("server.confirm", "alpha", "ok")
