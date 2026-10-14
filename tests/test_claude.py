"""The Claude account's state, and the Claude Code versions routes: not there yet in
workspaces, each answers not_available_yet, its body checked as it will be. Signing in
and out: tests/test_accounts.py."""

from helpers import rules
from tests.conftest import ALICE, BOB, changing
from tests.test_git import area_routes, call


def test_every_versions_route_answers_not_available_yet(client, app):
    found = [
        (m, p) for m, p in area_routes(app, "/api/claude") if p.endswith(("versions", "upgrade"))
    ]
    assert len(found) == 2  # non-vacuity
    for method, path in found:
        r = call(client, method, path)
        assert (r.status_code, r.json()["code"]) == (501, "not_available_yet"), (method, path)


def test_the_versions_are_still_for_administrators_only(client):
    r = client.get("/api/claude/versions", headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_a_version_is_still_checked_by_its_rule(client):
    r = client.post("/api/claude/upgrade", json={"version": "latest"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "version", "message": rules.VERSION.sentence}]


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
