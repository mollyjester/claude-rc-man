"""What administrators are warned about: servers on screens rcman does not recognise."""

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing, fake_claude, made_workspace, signed_in


def attention(client):
    return client.get("/api/attention", headers=as_user(ALICE))


def test_unrecognised_screens_are_named_without_their_text(client, app):
    made = made_workspace(client, BOB, "beta")
    signed_in(app, made)
    fake_claude(app.state.workspaces.runtime.claude, env="FAKE_CLAUDE_SCENARIO=unrecognised")
    started = client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(BOB))
    assert fake.UNRECOGNISED_SCREEN in started.text  # positive control: the screen exists
    r = attention(client)
    assert r.json()["unrecognised"] == [{"email": BOB, "workspace": "beta"}]
    assert fake.UNRECOGNISED_SCREEN not in r.text
