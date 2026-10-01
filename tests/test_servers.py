"""Servers, through the real rc-helper and the fake claude, per user."""

import time

from tests.conftest import ALICE, BOB, as_user, changing

ALPHA = {"path": "projects/alice-project"}


def servers(client, who):
    return client.get("/api/servers", headers=as_user(who)).json()["servers"]


def wait_ready(client, who, path):
    for _ in range(100):
        s = next((x for x in servers(client, who) if x["path"] == path), None)
        if s and s["state"] == "ready":
            return s
        time.sleep(0.1)
    raise AssertionError(f"{path} never ready: {s}")


def test_lifecycle_uses_the_users_prefix(client):
    client.put("/api/me/settings", json={"prefix": "alpha"}, headers=changing(ALICE))
    r = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["server"]["state"] == "trust_prompt"
    r = client.post("/api/servers/confirm", json={**ALPHA, "answer": "y"}, headers=changing(ALICE))
    assert r.json()["server"]["state"] in ("connected", "ready")
    ready = wait_ready(client, ALICE, ALPHA["path"])
    assert ready["session_name"].startswith("alpha-")

    output = client.get("/api/servers/output", params=ALPHA, headers=as_user(ALICE)).json()
    assert output["screen"]

    assert (
        client.post("/api/servers/stop", json=ALPHA, headers=changing(ALICE)).json()["server"][
            "state"
        ]
        == "stopped"
    )
    resumed = client.post("/api/servers/resume", json=ALPHA, headers=changing(ALICE)).json()[
        "server"
    ]
    assert resumed["state"] in ("connected", "ready")
    client.post("/api/servers/stop", json=ALPHA, headers=changing(ALICE))
    assert (
        client.post("/api/servers/forget", json=ALPHA, headers=changing(ALICE)).status_code == 200
    )
    assert servers(client, ALICE) == []


def test_users_never_see_each_others_servers(client):
    client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert [s["path"] for s in servers(client, ALICE)] == [ALPHA["path"]]
    assert servers(client, BOB) == []
    r = client.post("/api/servers/stop", json=ALPHA, headers=changing(BOB))
    assert r.status_code == 404  # bob has no such folder


def test_conflicts_are_409(client):
    client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    r = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "already_running")


def test_answer_is_validated_by_the_server(client):
    r = client.post(
        "/api/servers/confirm", json={**ALPHA, "answer": "maybe"}, headers=changing(ALICE)
    )
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "answer"


def test_helper_detail_is_passed_on(client, helper_homes):
    from devtools import fake_claude as fake

    fake.write_state(helper_homes.homes["alice"] / ".claude-fake", logged_in=False)
    r = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "claude_not_logged_in")
