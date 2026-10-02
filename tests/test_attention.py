"""What administrators are warned about: unrecognised screens, Claude Code versions."""

import json
import sqlite3

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_servers import ALPHA, unrecognised_claude

CLEAN_VERSIONS = {"approved": "2.1.0", "tested": "2.1.0", "users": []}


def attention(client, who=ALICE):
    return client.get("/api/attention", headers=as_user(who))


def test_attention_is_for_administrators_only(client):
    r = attention(client, BOB)
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_nothing_to_report(client, admin_runner):
    admin_runner.replies = [(0, CLEAN_VERSIONS)]
    r = attention(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"unrecognised": [], "versions_differ": [], "untested": False}
    assert admin_runner.calls == [["claude-status"]]


def test_unrecognised_screens_are_named_without_their_text(
    client, helper_homes, admin_runner, settings
):
    unrecognised_claude(helper_homes)
    started = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert fake.UNRECOGNISED_SCREEN in started.text  # positive control: the screen exists
    admin_runner.replies = [(0, CLEAN_VERSIONS)]
    r = attention(client)
    assert r.json()["unrecognised"] == [{"linux": "alice", "path": ALPHA["path"]}]
    assert fake.UNRECOGNISED_SCREEN not in r.text
    con = sqlite3.connect(settings.database)
    stored = json.dumps([list(row) for row in con.execute("select * from audit")])
    assert "server.start" in stored and fake.UNRECOGNISED_SCREEN not in stored


def test_versions_that_differ_and_an_untested_approval_are_reported(client, admin_runner):
    admin_runner.replies = [
        (
            0,
            {
                "approved": "2.2.0",
                "tested": "2.1.0",
                "users": [
                    {"linux": "alice", "installed": "2.2.0"},
                    {"linux": "bob", "installed": "2.1.0"},
                    {"linux": "carol", "installed": None},
                ],
            },
        )
    ]
    body = attention(client).json()
    assert body["versions_differ"] == ["bob", "carol"]
    assert body["untested"] is True


def test_attention_reuses_the_everyone_listing(client, admin_runner, monkeypatch):
    from rcman import everyone

    seen = []

    def listing(request):
        seen.append(request)
        return [
            {"linux": "alice", "email": ALICE, "servers": [{"path": "a", "state": "unrecognised"}]},
            {"linux": "bob", "email": BOB, "servers": [{"path": "b", "state": "ready"}]},
            {"linux": "carol", "email": "c", "error": {"code": "helper_timeout", "title": "t"}},
        ]

    monkeypatch.setattr(everyone, "everyone", listing)
    admin_runner.replies = [(0, CLEAN_VERSIONS)]
    assert attention(client).json()["unrecognised"] == [{"linux": "alice", "path": "a"}]
    assert len(seen) == 1
