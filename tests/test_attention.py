"""What administrators are warned about: servers on screens rcman does not recognise."""

import json
import sqlite3

import pytest

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing, fake_claude, made_workspace, signed_in
from tests.test_screens import RECONNECTED_SCENARIOS


def attention(client, who=ALICE):
    return client.get("/api/attention", headers=as_user(who))


def started_in(client, app, scenario: str, who=ALICE, name="alpha"):
    made = made_workspace(client, who, name)
    signed_in(app, made)
    fake_claude(app.state.workspaces.runtime.claude, env=f"FAKE_CLAUDE_SCENARIO={scenario}")
    return client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(who))


def test_attention_is_for_administrators_only(client):
    r = attention(client, BOB)
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_nothing_to_report(client):
    made_workspace(client, BOB, "quiet")
    r = attention(client)
    assert (r.status_code, r.json()) == (200, {"unrecognised": []})


def test_unrecognised_screens_are_named_without_their_text(client, app, settings):
    started = started_in(client, app, "unrecognised", who=BOB, name="beta")
    assert fake.UNRECOGNISED_SCREEN in started.text  # positive control: the screen exists
    r = attention(client)
    assert r.json()["unrecognised"] == [{"email": BOB, "workspace": "beta"}]
    assert fake.UNRECOGNISED_SCREEN not in r.text
    con = sqlite3.connect(settings.database)
    stored = json.dumps([list(row) for row in con.execute("select * from audit")])
    assert "server.start" in stored and fake.UNRECOGNISED_SCREEN not in stored


@pytest.mark.parametrize("scenario", RECONNECTED_SCENARIOS)
def test_a_reconnected_server_is_not_a_warning(client, app, scenario):
    started = started_in(client, app, scenario)
    assert started.json()["server"]["state"] == "reconnected"  # positive control
    assert attention(client).json()["unrecognised"] == []


def test_a_server_at_the_remote_control_prompt_is_not_a_warning(client, app):
    started = started_in(client, app, "remote_control_prompt")
    assert started.json()["server"]["state"] == "remote_control_prompt"  # positive control
    assert attention(client).json() == {"unrecognised": []}
