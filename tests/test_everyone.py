"""Everyone's workspaces and servers, for administrators (backend/rcman/everyone.py)."""

from types import SimpleNamespace

from rcman import everyone

from tests.conftest import ALICE, BOB, CAROL, changing, made_workspace, signed_in


def request_for(app):
    return SimpleNamespace(app=app)


def by_email(entries):
    return {e["email"]: e for e in entries}


def test_every_person_is_listed_with_their_workspaces_devices_and_servers(app, client):
    alpha = made_workspace(client, ALICE, "alpha")
    beta = made_workspace(client, BOB, "beta")
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
    assert set(shown) == {
        "id",
        "name",
        "device",
        "state",
        "server",
        "server_error",
        "claude_version",
        "stoppable",
        "stopping",
    }  # nothing more of it: not its source, its log or its error
    assert set(shown["server"]) == set(everyone.SERVER_FIELDS)
    assert "screen" not in str(shown) and "environment_url" not in str(shown)
    [other] = found[BOB]["workspaces"]
    assert (other["name"], other["server"]) == ("beta", None)
    assert (shown["device"], other["device"]) == (
        f"rcman-ws-{alpha['id']}",
        f"rcman-ws-{beta['id']}",
    )
    assert found[CAROL] == {"email": CAROL, "workspaces": []}
