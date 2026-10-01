"""The audit trail: recording, coverage of every change, retention."""

import datetime as dt
import re
import sqlite3
from pathlib import Path

from fastapi.routing import APIRoute
from rcman import audit, db, routes

from tests.conftest import ALICE, BOB, changing

REPO = Path(__file__).resolve().parent.parent
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def rows(settings):
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("select * from audit order by id")]


# --- coverage: structural ---------------------------------------------------------


def changing_routes(app):
    return [c for c in routes.api_routes(app) if c.methods & CHANGING]


def test_every_changing_route_is_audited_or_exempt_with_a_reason(app):
    routes = changing_routes(app)
    assert routes  # non-vacuity
    unaccounted = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if audit.action_of(r.route) is None and (r.path, *sorted(r.methods)) not in audit.EXEMPT
    ]
    assert unaccounted == []


def test_exemptions_are_stale_checked_and_documented(app):
    present = {(r.path, *sorted(r.methods)) for r in changing_routes(app)}
    assert set(audit.EXEMPT) <= present
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    for reason in audit.EXEMPT.values():
        assert reason in guide


def test_every_action_is_listed_in_the_guide(app):
    actions = {audit.action_of(r.route) for r in changing_routes(app)} - {None}
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    listed = set(
        re.findall(r"^\| `([a-z]+\.[a-z_]+)` \|", guide, re.MULTILINE)
    )  # the actions table
    assert actions and actions == listed


def test_a_route_without_a_recorder_is_detected():
    """Planted twin: a changing route with no recorder is reported."""
    from fastapi import FastAPI

    planted = FastAPI()

    @planted.post("/planted")
    def handler():
        return {}

    route = next(r for r in planted.routes if isinstance(r, APIRoute))
    assert audit.action_of(route) is None


# --- coverage: behavioural -----------------------------------------------------------


def test_driving_every_audited_route_writes_its_row(app, client, settings, admin_runner):
    calls = {
        ("POST", "/api/servers"): {"json": {"path": "projects/alice-project"}},
        ("POST", "/api/servers/resume"): {"json": {"path": "projects/alice-project"}},
        ("POST", "/api/servers/stop"): {"json": {"path": "projects/alice-project"}},
        ("POST", "/api/servers/forget"): {"json": {"path": "projects/alice-project"}},
        ("POST", "/api/servers/confirm"): {
            "json": {"path": "projects/alice-project", "answer": "y"}
        },
        ("PUT", "/api/me/settings"): {"json": {"prefix": "alice"}},
        ("POST", "/api/users"): {"json": {"linux": "dave", "email": "dave@example.com"}},
        ("DELETE", "/api/users/{linux}"): {"params": {"home": "keep"}},
        ("POST", "/api/claude/login"): {"json": {}},
        ("POST", "/api/claude/code"): {"json": {"code": "x"}},
        ("POST", "/api/claude/upgrade"): {"json": {"version": "2.2.0"}},
    }
    audited = {
        (sorted(r.methods)[0], r.path): audit.action_of(r.route)
        for r in changing_routes(app)
        if audit.action_of(r.route)
    }
    assert set(calls) == set(audited)
    admin_runner.replies = [
        (0, {"user": {}, "created": True}),
        (0, {"deleted": "bob", "home_deleted": False}),
        (0, {"approved": "2.2.0", "tested": "2.2.0", "users": {}}),
    ]
    for (method, path), kwargs in calls.items():
        before = len(rows(settings))
        client.request(method, path.replace("{linux}", "bob"), headers=changing(ALICE), **kwargs)
        after = rows(settings)
        assert len(after) == before + 1, (method, path)
        assert after[-1]["action"] == audited[(method, path)]
        assert after[-1]["email"] == ALICE and after[-1]["linux"] == "alice"


def test_a_refused_change_is_recorded_with_its_error_code(client, settings):
    client.post(
        "/api/servers/stop", json={"path": "projects/alice-project"}, headers=changing(ALICE)
    )
    last = rows(settings)[-1]
    assert (last["action"], last["result"]) == ("server.stop", "unknown_server")
    assert last["target"] == "projects/alice-project"


def test_a_successful_change_is_recorded_as_ok(client, settings):
    client.put("/api/me/settings", json={"prefix": "pfx"}, headers=changing(BOB))
    last = rows(settings)[-1]
    assert (last["action"], last["result"], last["linux"]) == ("settings.update", "ok", "bob")


def test_audit_events_are_logged_as_json(client, caplog):
    import json

    caplog.set_level("INFO", logger="rcman.audit")
    client.put("/api/me/settings", json={"prefix": "pfx"}, headers=changing(BOB))
    event = json.loads(caplog.records[-1].getMessage())
    assert event["action"] == "settings.update" and event["email"] == BOB


# --- retention ------------------------------------------------------------------------


def seed(path, ages_days, now):
    db.migrate(path)
    con = db.connect(path)
    for age in ages_days:
        at = (now - dt.timedelta(days=age)).isoformat()
        con.execute(
            "insert into audit (at, email, linux, action, target, result) "
            "values (?, 'e', 'l', 'a', 't', 'ok')",
            (at,),
        )
    con.commit()
    return con


def test_retention_deletes_rows_older_than_the_limit(tmp_path):
    now = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    con = seed(tmp_path / "a.db", [1, 5, 10, 40], now)
    deleted = audit.apply_retention(con, now=now, days=7, max_rows=100)
    assert deleted == 2
    assert con.execute("select count(*) from audit").fetchone()[0] == 2


def test_retention_caps_the_row_count_keeping_the_newest(tmp_path):
    now = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    con = seed(tmp_path / "a.db", [4, 3, 2, 1], now)
    audit.apply_retention(con, now=now, days=30, max_rows=2)
    ages = sorted(
        (now - dt.datetime.fromisoformat(r[0])).days for r in con.execute("select at from audit")
    )
    assert ages == [1, 2]


def test_retention_runs_when_the_app_starts(settings, helper_homes, admin_runner):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    seed(settings.database, [settings.audit_retention_days + 1], dt.datetime.now(dt.UTC)).close()
    with TestClient(create_app(settings, helper=helper_homes, admin=admin_runner)):
        pass
    assert rows(settings) == []
