"""The Claude account's state, and the Claude Code versions routes for administrators
(backend/rcman/routers/claude.py): the report, the rollout and the suggestion of the latest.
The rollout's own behaviour is tests/test_claude_versions.py's; signing in and out:
tests/test_accounts.py."""

import json

import pytest
from rcman import db

from helpers import claude_checks
from tests import claude_download
from tests.conftest import ALICE, FAKE_VERSION, REPO, as_user, changing

NEW = "2.0.0"


@pytest.fixture
def releases(tmp_path, settings, client):
    """A release site of the test's own, served; the app uses it from now on."""
    folder = tmp_path / "releases"
    folder.mkdir()
    with claude_download.served(folder) as url:
        settings.claude_download = url
        yield folder


def rows(settings) -> list[dict]:
    con = db.connect(settings.database)
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def upgrade(client):
    return client.post("/api/claude/upgrade", json={"version": NEW}, headers=changing(ALICE))


def test_the_report_names_what_is_approved_installed_and_running(client, app):
    r = client.get("/api/claude/versions", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["approved"] == FAKE_VERSION and report["previous"] is None
    assert report["tested"] == (REPO / "helpers" / "claude-version").read_text().strip()
    assert report["installed"] == [
        {"version": FAKE_VERSION, "approved": True, "previous": False, "in_use": 0, "failed": False}
    ]
    assert report["drift"] == [] and report["upgrading"] is False
    assert report["failed"] == []


def test_a_rollout_answers_with_the_report_and_is_audited_with_the_version_as_target(
    client, app, settings, releases
):
    claude_download.release(releases, NEW)
    app.state.workspaces.runtime.canary_script = lambda *args: {}
    r = upgrade(client)
    assert r.status_code == 200, r.text
    assert (r.json()["approved"], r.json()["previous"]) == (NEW, FAKE_VERSION)
    last = rows(settings)[-1]
    assert (last["action"], last["target"], last["result"], last["email"]) == (
        "claude.upgrade",
        NEW,
        "ok",
        ALICE,
    )
    after = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert after["approved"] == NEW


def test_a_version_that_fails_the_checks_is_a_conflict_with_the_checks_and_their_reports(
    client, app, settings, releases, caplog
):
    claude_download.release(releases, NEW, kind="broken")
    r = upgrade(client)
    assert (r.status_code, r.json()["code"]) == (409, "claude_checks_failed")
    body = r.json()
    names = sorted(name for name, _ in claude_checks.CHECKS)
    assert body["failed"] == names and set(body["report"]) == set(names)
    assert body["report"]["version"]["lines"]
    # the report is for the administrator who asked, and nowhere else
    last = rows(settings)[-1]
    assert (last["action"], last["target"], last["result"]) == (
        "claude.upgrade",
        NEW,
        "claude_checks_failed",
    )
    report_text = " ".join(body["report"]["version"]["lines"])
    assert report_text not in json.dumps(last) and report_text not in caplog.text
    after = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert after["approved"] == FAKE_VERSION


def mark(client, reason):
    sent = {"version": NEW, "reason": reason}
    return client.put("/api/claude/failures", json=sent, headers=changing(ALICE))


def unmark(client):
    return client.delete(f"/api/claude/failures/{NEW}", headers=changing(ALICE))


def test_a_mark_and_its_removal_answer_the_report_and_are_audited(client, settings):
    r = mark(client, reason="  it hangs  ")
    assert r.status_code == 200, r.text
    [failed] = r.json()["failed"]
    assert {k: failed[k] for k in ("version", "by", "reason")} == {
        "version": NEW,
        "by": ALICE,
        "reason": "it hangs",
    }
    assert r.json()["approved"] == FAKE_VERSION  # the report, whole
    assert client.get("/api/claude/versions", headers=as_user(ALICE)).json()["failed"] == [failed]
    r = unmark(client)
    assert r.status_code == 200, r.text
    assert r.json()["failed"] == [] and r.json()["approved"] == FAKE_VERSION
    recorded = [(r["action"], r["target"], r["result"], r["email"]) for r in rows(settings)[-2:]]
    assert recorded == [
        ("claude.mark_failed", '{"version": "2.0.0", "reason": "it hangs"}', "ok", ALICE),
        ("claude.clear_failed", NEW, "ok", ALICE),
    ]


def test_the_latest_version_is_what_the_release_site_names_and_nothing_is_installed(
    client, releases, settings
):
    claude_download.release(releases, "2.5.0")
    r = client.get("/api/claude/latest", headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"latest": "2.5.0"})
    assert sorted(p.name for p in (settings.claude_store / "versions").iterdir()) == [FAKE_VERSION]
    assert rows(settings) == []  # a read changes nothing, so nothing is audited


# --- the account's state, as signing in will report it --------------------------------

NOW = __import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").UTC)


def status_expiring_in(days: float | None) -> dict:
    import datetime as dt

    expires = None if days is None else (NOW + dt.timedelta(days=days)).isoformat()
    return {"logged_in": True, "email": "a@example.com", "login_expires_at": expires}


def test_the_account_states_follow_the_sign_ins_expiry_and_the_warning_setting():
    from rcman.routers.claude import connection

    assert connection({"logged_in": False}, 3, NOW)["connection"] == "not_connected"
    for days, state in ((10, "connected"), (2, "expiring"), (-1, "expired"), (None, "connected")):
        assert connection(status_expiring_in(days), 3, NOW)["connection"] == state, days
    assert connection(status_expiring_in(2), 1, NOW)["connection"] == "connected"
