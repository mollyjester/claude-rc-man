"""Connecting a Claude account, its expiry, and Claude Code versions."""

import datetime as dt
import json
import logging
import sqlite3

import pytest

from devtools import fake_claude as fake
from devtools import record_claude as rec
from tests.conftest import ALICE, BOB, as_user, changing


def logged_out(helper_homes, linux, accept=()):
    fake.write_state(
        helper_homes.homes[linux] / ".claude-fake", logged_in=False, accept_codes=list(accept)
    )


def expires_in(helper_homes, linux, days):
    at = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    path = helper_homes.homes[linux] / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": int(at.timestamp() * 1000)}})
    )


# --- connection state -----------------------------------------------------------------


def test_not_connected(client, helper_homes):
    logged_out(helper_homes, "bob")
    assert client.get("/api/claude", headers=as_user(BOB)).json() == {
        "connection": "not_connected",
        "account": None,
        "login_expires_at": None,
    }


def test_connected_reports_the_account(client, helper_homes):
    expires_in(helper_homes, "bob", 20)
    out = client.get("/api/claude", headers=as_user(BOB)).json()
    assert out["connection"] == "connected"
    assert out["account"]["email"] == fake.FAKE_ACCOUNT["email"]
    assert out["login_expires_at"]


@pytest.mark.parametrize("days,state", [(20, "connected"), (2, "expiring"), (-1, "expired")])
def test_expiry_states_follow_the_warning_setting(client, helper_homes, settings, days, state):
    assert settings.login_warning_days > 2
    expires_in(helper_homes, "bob", days)
    assert client.get("/api/claude", headers=as_user(BOB)).json()["connection"] == state


def test_unknown_expiry_is_connected_without_a_date(client):
    out = client.get("/api/claude", headers=as_user(BOB)).json()
    assert (out["connection"], out["login_expires_at"]) == ("connected", None)


# --- connecting ----------------------------------------------------------------------


def test_connect_flow(client, helper_homes):
    logged_out(helper_homes, "bob", accept=["GOOD-CODE"])
    url = client.post("/api/claude/login", json={}, headers=changing(BOB)).json()["url"]
    assert url.startswith(rec.LOGIN_URL_PREFIX)
    assert "bob%40example.com" in url  # the Google address unless another is given
    out = client.post("/api/claude/code", json={"code": "GOOD-CODE"}, headers=changing(BOB)).json()
    assert out["connection"] == "connected"


def test_another_claude_address_can_be_given(client, helper_homes):
    logged_out(helper_homes, "bob")
    url = client.post(
        "/api/claude/login", json={"email": "work@example.org"}, headers=changing(BOB)
    ).json()["url"]
    assert "work%40example.org" in url


def test_a_wrong_code_is_a_problem(client, helper_homes):
    logged_out(helper_homes, "bob", accept=["GOOD-CODE"])
    client.post("/api/claude/login", json={}, headers=changing(BOB))
    r = client.post("/api/claude/code", json={"code": "BAD-CODE"}, headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (409, "login_failed")


def test_the_code_is_never_stored_or_logged(client, helper_homes, settings, caplog):
    caplog.set_level(logging.DEBUG)
    logged_out(helper_homes, "bob", accept=["SECRET-CODE-42"])
    client.post("/api/claude/login", json={}, headers=changing(BOB))
    client.post("/api/claude/code", json={"code": "SECRET-CODE-42"}, headers=changing(BOB))
    rows = sqlite3.connect(settings.database).execute("select * from audit").fetchall()
    assert rows  # non-vacuity: the connect was audited
    assert "SECRET-CODE-42" not in json.dumps(rows)
    assert "SECRET-CODE-42" not in caplog.text


def test_an_unrecognised_sign_in_screen_is_shown_and_audited(client, helper_homes, settings):
    wrapper = helper_homes.homes["bob"].parent.parent / "claude"
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_SCENARIO=unrecognised exec {rec.FAKE_CLAUDE} "$@"\n'
    )
    logged_out(helper_homes, "bob")
    r = client.post("/api/claude/login", json={}, headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (502, "claude_unrecognised_output")
    assert fake.UNRECOGNISED_SCREEN in r.json()["screen"]
    last = (
        sqlite3.connect(settings.database)
        .execute("select action, result from audit")
        .fetchall()[-1]
    )
    assert last == ("claude.login", "claude_unrecognised_output")


def test_code_and_email_are_validated(client):
    assert (
        client.post("/api/claude/code", json={"code": ""}, headers=changing(BOB)).status_code == 422
    )
    assert (
        client.post("/api/claude/login", json={"email": "nope"}, headers=changing(BOB)).status_code
        == 422
    )


# --- versions (administrators) -------------------------------------------------------


def test_versions_flag_who_differs_and_untested_approval(client, admin_runner):
    admin_runner.replies = [
        (
            0,
            {
                "approved": "2.2.0",
                "tested": "2.1.0",
                "users": [
                    {"linux": "alice", "installed": "2.2.0"},
                    {"linux": "bob", "installed": "2.1.0"},
                ],
            },
        )
    ]
    out = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert admin_runner.calls == [["claude-status"]]
    assert out["differs"] == ["bob"]
    assert out["untested"] is True


def test_upgrade_goes_through_rc_admin(client, admin_runner):
    admin_runner.replies = [
        (0, {"approved": "2.2.0", "tested": "2.1.0", "users": {"alice": "installed"}})
    ]
    r = client.post("/api/claude/upgrade", json={"version": "2.2.0"}, headers=changing(ALICE))
    assert r.status_code == 200
    assert admin_runner.calls == [["claude-upgrade", "2.2.0"]]


def test_a_failed_upgrade_is_a_problem_naming_the_checks(client, admin_runner):
    error = {"code": "claude_checks_failed", "message": "x", "failed": ["sign_in_screen"]}
    admin_runner.replies = [(1, {"error": error})]
    r = client.post("/api/claude/upgrade", json={"version": "2.2.0"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"], r.json()["failed"]) == (
        409,
        "claude_checks_failed",
        ["sign_in_screen"],
    )


def test_versions_are_for_administrators(client, admin_runner):
    assert client.get("/api/claude/versions", headers=as_user(BOB)).status_code == 403
    assert (
        client.post(
            "/api/claude/upgrade", json={"version": "2.2.0"}, headers=changing(BOB)
        ).status_code
        == 403
    )
    assert admin_runner.calls == []


def test_upgrade_version_is_validated(client, admin_runner):
    r = client.post("/api/claude/upgrade", json={"version": "latest"}, headers=changing(ALICE))
    assert r.status_code == 422 and admin_runner.calls == []
