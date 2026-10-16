"""The Claude account's state, and the Claude Code versions routes for administrators
(backend/rcman/routers/claude.py): the report, the rollout and the suggestion of the latest.
The rollout's own behaviour is tests/test_claude_versions.py's; signing in and out:
tests/test_accounts.py."""

import json

import pytest
from rcman import db

from helpers import claude_checks, rules
from tests import claude_download
from tests.conftest import ALICE, BOB, FAKE_VERSION, REPO, as_user, changing

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


def upgrade(client, version=NEW, who=ALICE):
    return client.post("/api/claude/upgrade", json={"version": version}, headers=changing(who))


def test_the_report_is_for_administrators_only(client):
    r = client.get("/api/claude/versions", headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")
    for path in ("/api/claude/latest",):
        r = client.get(path, headers=as_user(BOB))
        assert (r.status_code, r.json()["code"]) == (403, "admin_only"), path
    r = upgrade(client, who=BOB)
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_a_version_is_still_checked_by_its_rule(client):
    r = client.post("/api/claude/upgrade", json={"version": "latest"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "version", "message": rules.VERSION.sentence}]


def test_the_report_names_what_is_approved_installed_and_running(client, app):
    r = client.get("/api/claude/versions", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["approved"] == FAKE_VERSION and report["previous"] is None
    assert report["tested"] == (REPO / "helpers" / "claude-version").read_text().strip()
    assert report["installed"] == [
        {"version": FAKE_VERSION, "approved": True, "previous": False, "in_use": 0}
    ]
    assert report["drift"] == [] and report["upgrading"] is False


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


@pytest.mark.parametrize(
    "kind,make,version,status,code",
    [
        ("checksum", {"kind": "failing"}, NEW, 409, "claude_install_failed"),
        ("network", None, NEW, 502, "claude_install_failed"),
        ("no_such_version", None, "9.9.9", 409, "claude_install_failed"),
        ("no_build", {"platform": "plan9-mips"}, NEW, 409, "claude_install_failed"),
    ],
)
def test_a_version_that_cannot_be_had_says_why_and_the_audit_row_holds_the_code_only(
    client, settings, releases, kind, make, version, status, code
):
    from rcman.problems import CLAUDE_INSTALL_MESSAGES

    if make is not None:
        claude_download.release(releases, NEW, **make)
    if kind == "network":
        with claude_download.served(releases) as gone:
            pass
        client.app.state.settings.claude_download = gone  # nothing answers there any more
    r = upgrade(client, version)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert r.json()["kind"] == kind and r.json()["title"] == CLAUDE_INSTALL_MESSAGES[kind]
    last = rows(settings)[-1]
    assert (last["target"], last["result"]) == (version, code)


def test_a_second_rollout_at_once_is_a_conflict(client, app, releases):
    import threading

    claude_download.release(releases, NEW)
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        release.wait(30)
        return {}

    app.state.workspaces.runtime.canary_script = slow
    first = threading.Thread(target=lambda: upgrade(client))
    first.start()
    try:
        assert entered.wait(30)
        r = upgrade(client, "2.1.0", who=ALICE)
        assert (r.status_code, r.json()["code"]) == (409, "claude_upgrade_busy")
    finally:
        release.set()
        first.join(60)


def test_the_latest_version_is_what_the_release_site_names_and_nothing_is_installed(
    client, releases, settings
):
    claude_download.release(releases, "2.5.0")
    r = client.get("/api/claude/latest", headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"latest": "2.5.0"})
    assert sorted(p.name for p in (settings.claude_store / "versions").iterdir()) == [FAKE_VERSION]
    assert rows(settings) == []  # a read changes nothing, so nothing is audited


def test_no_latest_version_is_nothing_to_suggest_and_not_a_failure(client, releases):
    r = client.get("/api/claude/latest", headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"latest": None})
    with claude_download.served(releases) as gone:
        pass
    client.app.state.settings.claude_download = gone
    r = client.get("/api/claude/latest", headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"latest": None})


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


def test_the_warning_setting_is_a_positive_number_of_days():
    import pytest
    from pydantic import ValidationError
    from rcman.settings import Settings

    assert Settings(public_origin="https://x.example.com").login_warning_days >= 1
    with pytest.raises(ValidationError):
        Settings(public_origin="https://x.example.com", login_warning_days=0)
