"""Everyone's workspaces and servers, for administrators (backend/rcman/everyone.py)."""

from types import SimpleNamespace

from rcman import everyone

from tests.conftest import ALICE, BOB, CAROL, changing, made_workspace, signed_in


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
