"""What administrators are warned about: unrecognised screens, Claude Code versions."""

import json
import sqlite3

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_servers import ALPHA, reconnected_claude, unrecognised_claude

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
    assert r.json() == {
        "unrecognised": [],
        "versions_differ": [],
        "untested": False,
        "versions_error": None,
    }
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


def test_a_reconnected_server_is_not_a_warning(client, helper_homes, admin_runner):
    reconnected_claude(helper_homes)
    started = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert started.json()["server"]["state"] == "reconnected"  # positive control
    admin_runner.replies = [(0, CLEAN_VERSIONS)]
    r = attention(client)
    assert r.status_code == 200, r.text
    assert r.json()["unrecognised"] == []


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


def test_a_failing_version_report_still_names_the_unrecognised_screens(
    client, admin_runner, monkeypatch
):
    from rcman import everyone

    monkeypatch.setattr(
        everyone,
        "everyone",
        lambda request: [
            {"linux": "alice", "email": ALICE, "servers": [{"path": "a", "state": "unrecognised"}]}
        ],
    )
    admin_runner.replies = [
        (1, {"error": {"code": "internal_error", "message": "rc-admin broke."}})
    ]
    r = attention(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unrecognised"] == [{"linux": "alice", "path": "a"}]
    assert body["versions_error"] == {"code": "internal_error", "title": "rc-admin broke."}
    assert body["versions_differ"] == [] and body["untested"] is False


def test_nothing_failing_means_no_version_error(client, admin_runner):
    admin_runner.replies = [(0, CLEAN_VERSIONS)]
    assert attention(client).json()["versions_error"] is None


def test_the_listing_and_the_version_report_run_side_by_side(client, admin_runner, monkeypatch):
    """The listing waits for the version report to start, which it would not if run after it."""
    import threading

    from rcman import everyone

    started = threading.Event()

    def listing(request):
        assert started.wait(5), "the version report did not run alongside the listing"
        return []

    def admin(args, timeout=None):
        started.set()
        return 0, CLEAN_VERSIONS

    monkeypatch.setattr(everyone, "everyone", listing)
    client.app.state.admin = admin
    r = attention(client)
    assert r.status_code == 200, r.text


def test_a_slow_version_report_is_a_timeout_not_a_wait(client, monkeypatch):
    import threading
    import time

    from rcman import everyone

    monkeypatch.setattr(everyone, "ATTENTION_VERSIONS_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(everyone, "everyone", lambda request: [])
    seen, release = {}, threading.Event()

    def admin(args, timeout=None):
        seen["timeout"] = timeout
        release.wait(5)  # a stand-in that does not honour its timeout
        return 0, CLEAN_VERSIONS

    client.app.state.admin = admin
    started = time.monotonic()
    try:
        r = attention(client)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert seen["timeout"] == 0.5  # the version report's own timeout reaches rc-admin
    assert elapsed < 0.5 + 1.5
    assert r.status_code == 200 and r.json()["versions_error"]["code"] == "helper_timeout"


def test_a_failing_listing_does_not_wait_for_the_version_report(client, monkeypatch):
    import threading
    import time

    from rcman import everyone
    from rcman.problems import Problem

    def listing(request):
        raise Problem("users_file_invalid")

    release = threading.Event()
    monkeypatch.setattr(everyone, "everyone", listing)
    client.app.state.admin = lambda args, timeout=None: (release.wait(5), (0, CLEAN_VERSIONS))[1]
    started = time.monotonic()
    try:
        r = attention(client)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert (r.status_code, r.json()["code"]) == (500, "users_file_invalid")
    assert elapsed < 1.5
