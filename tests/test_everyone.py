"""Everyone's workspaces and servers, for administrators (backend/rcman/everyone.py)."""

import time
from types import SimpleNamespace

from rcman import everyone

from tests.conftest import (
    ALICE,
    BOB,
    CAROL,
    MADE_WITHIN,
    as_user,
    changing,
    fake_claude,
    made_workspace,
    signed_in,
)


def request_for(app):
    return SimpleNamespace(app=app)


def by_email(entries):
    return {e["email"]: e for e in entries}


def test_every_person_is_listed_with_their_workspaces_and_servers(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    made_workspace(client, BOB, "beta")
    signed_in(app, alpha)
    client.post("/api/servers", json={"workspace": alpha["id"]}, headers=changing(ALICE))
    found = by_email(everyone.everyone(request_for(app)))
    assert set(found) == {ALICE, BOB, CAROL}
    [shown] = found[ALICE]["workspaces"]
    assert (shown["name"], shown["state"], shown["server"]["state"]) == (
        "alpha",
        "ready",
        "trust_prompt",
    )
    assert [(w["name"], w["server"]) for w in found[BOB]["workspaces"]] == [("beta", None)]
    assert found[CAROL] == {"email": CAROL, "workspaces": []}


def test_each_workspace_shows_the_device_name_claude_shows_for_its_server(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    beta = made_workspace(client, BOB, "beta")
    found = by_email(everyone.everyone(request_for(app)))
    [a] = found[ALICE]["workspaces"]
    [b] = found[BOB]["workspaces"]
    assert (a["device"], b["device"]) == (f"rcman-ws-{alpha['id']}", f"rcman-ws-{beta['id']}")
    assert a["device"] != b["device"]


def test_a_workspace_shows_only_its_id_name_device_state_server_and_version(app, client):
    url = "https://github.com/octo/widget"
    r = client.post("/api/git/clone", json={"url": f"{url}.git"}, headers=changing(ALICE))
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + MADE_WITHIN
    while True:
        [own] = client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]
        if own["state"] != "creating":
            break
        assert time.monotonic() < deadline, own
        time.sleep(0.05)
    # the person's own listing of the same workspace holds what is left out below
    assert own["state"] == "failed"  # no repository at the address, so it keeps its steps
    assert own["source_page"] == url and own["source"] == f"{url}.git"
    assert own["error"] and own["log"]
    [shown] = by_email(everyone.everyone(request_for(app)))[ALICE]["workspaces"]
    assert set(shown) == {
        "id",
        "name",
        "device",
        "state",
        "server",
        "server_error",
        "claude_version",
    }
    assert url not in str(shown) and own["log"] not in str(shown)


def test_a_server_shows_only_its_state_name_and_exit_code(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    signed_in(app, alpha)
    client.post("/api/servers", json={"workspace": alpha["id"]}, headers=changing(ALICE))
    [shown] = by_email(everyone.everyone(request_for(app)))[ALICE]["workspaces"]
    assert set(shown["server"]) == set(everyone.SERVER_FIELDS)
    assert "screen" not in str(shown) and "environment_url" not in str(shown)


def test_one_workspace_that_cannot_be_read_does_not_hide_the_others(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    beta = made_workspace(client, BOB, "beta")
    for made, who in ((alpha, ALICE), (beta, BOB)):
        signed_in(app, made)
        client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(who))
    app.state.workspaces.runtime.stop(f"rcman-ws-{alpha['id']}")
    app.state.workspaces.close_channels()
    found = by_email(everyone.everyone(request_for(app)))
    [broken] = found[ALICE]["workspaces"]
    assert broken["server"] is None and broken["server_error"]["code"] == "workspace_failed"
    [fine] = found[BOB]["workspaces"]
    assert fine["server"]["state"] == "trust_prompt"


def test_a_server_at_the_remote_control_prompt_is_listed_by_its_state_name(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    signed_in(app, alpha)
    fake_claude(
        app.state.workspaces.runtime.claude, env="FAKE_CLAUDE_SCENARIO=remote_control_prompt"
    )
    client.post("/api/servers", json={"workspace": alpha["id"]}, headers=changing(ALICE))
    [shown] = by_email(everyone.everyone(request_for(app)))[ALICE]["workspaces"]
    assert shown["server"]["state"] == "remote_control_prompt"
    assert set(shown["server"]) == set(everyone.SERVER_FIELDS)
    assert "screen" not in str(shown) and "environment_url" not in str(shown)
