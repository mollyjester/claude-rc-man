"""The Monitor page's routes (backend/rcman/routers/monitor.py): the snapshot, and the settings,
for administrators."""

import sqlite3

import pytest
from rcman import monitor

from tests.clocks import PhasedClock
from tests.conftest import ALICE, BOB, as_user, changing, made_workspace

DEFAULTS = monitor.DEFAULTS.model_dump()


def stored(settings) -> list[dict]:
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from monitor_settings")]
    finally:
        con.close()


def audit_rows(settings) -> list[dict]:
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def test_the_settings_are_the_defaults_with_no_row(client, settings):
    assert stored(settings) == []  # nothing is stored until a save
    r = client.get("/api/monitor/settings", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    assert r.json() == {"settings": DEFAULTS}


def test_a_save_is_stored_answered_and_read_back(client, settings):
    sent = {**DEFAULTS, "usage_interval_seconds": 120, "containers": False}
    r = client.put("/api/monitor/settings", json=sent, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert r.json() == {"settings": sent}
    assert client.get("/api/monitor/settings", headers=as_user(ALICE)).json() == {"settings": sent}
    again = {**sent, "containers": True}
    client.put("/api/monitor/settings", json=again, headers=changing(ALICE))
    rows = stored(settings)
    assert (
        len(rows) == 1 and rows[0]["containers"] == 1 and rows[0]["usage_interval_seconds"] == 120
    )


def bound(name: str, kind: str) -> int:
    meta = {type(m).__name__: m for m in monitor.MonitorSettings.model_fields[name].metadata}
    return meta["Ge"].ge if kind == "ge" else meta["Le"].le


@pytest.mark.parametrize("name", ["usage_interval_seconds", "disk_interval_seconds"])
def test_an_interval_out_of_its_bounds_is_refused_at_its_field(client, settings, name):
    for value in (bound(name, "ge"), bound(name, "le")):  # positive control: the bounds are taken
        r = client.put(
            "/api/monitor/settings", json={**DEFAULTS, name: value}, headers=changing(ALICE)
        )
        assert r.status_code == 200, r.text
    for value in (bound(name, "ge") - 1, bound(name, "le") + 1, "60", 60.5):
        r = client.put(
            "/api/monitor/settings", json={**DEFAULTS, name: value}, headers=changing(ALICE)
        )
        assert r.status_code == 422, (value, r.text)
        assert [e["field"] for e in r.json()["errors"]] == [name]
    assert stored(settings)[0][name] == bound(name, "le")  # nothing refused was stored


@pytest.mark.parametrize("name", ["host", "people", "containers"])
@pytest.mark.parametrize("value", [1, 0, "true", "yes", None])
def test_a_switch_takes_only_true_or_false(client, name, value):
    r = client.put("/api/monitor/settings", json={**DEFAULTS, name: value}, headers=changing(ALICE))
    assert r.status_code == 422, r.text
    assert [e["field"] for e in r.json()["errors"]] == [name]


def test_a_save_must_send_every_setting(client):
    for name in DEFAULTS:
        sent = {k: v for k, v in DEFAULTS.items() if k != name}
        r = client.put("/api/monitor/settings", json=sent, headers=changing(ALICE))
        assert r.status_code == 422, name
        assert [e["field"] for e in r.json()["errors"]] == [name]


def test_a_save_is_audited_with_the_settings_saved(client, settings):
    sent = {**DEFAULTS, "host": False}
    client.put("/api/monitor/settings", json=sent, headers=changing(ALICE))
    client.put("/api/monitor/settings", json={**sent, "host": "no"}, headers=changing(ALICE))
    found = [(r["action"], r["result"], r["email"]) for r in audit_rows(settings)[-2:]]
    assert found == [
        ("monitor.settings", "ok", ALICE),
        ("monitor.settings", "validation_failed", ALICE),
    ]
    target = audit_rows(settings)[-2]["target"]
    assert '"host": false' in target and '"usage_interval_seconds": 60' in target


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/api/monitor"), ("GET", "/api/monitor/settings"), ("PUT", "/api/monitor/settings")],
)
def test_the_routes_are_for_administrators_only(client, method, path):
    r = client.request(method, path, headers=changing(BOB), json=DEFAULTS)
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_the_snapshot_has_its_shape(client):
    made = made_workspace(client, BOB, "alpha")
    monitor_ = client.app.state.monitor
    monitor_.usage_round()
    r = client.get("/api/monitor", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    snap = r.json()
    assert set(snap) == {"scopes", "usage", "disk", "host", "people", "containers"}
    assert snap["scopes"] == {"host": True, "people": True, "containers": True}
    assert set(snap["usage"]) == {"collected_at", "error"} and snap["usage"]["collected_at"]
    assert snap["disk"] == {"collected_at": None, "error": None}
    assert set(snap["host"]) == {
        "cpu_percent",
        "cpus",
        "memory_used_bytes",
        "memory_total_bytes",
        "tasks",
        "disks",
    }
    assert set(snap["host"]["disks"][0]) == {
        "path",
        "roles",
        "size_bytes",
        "used_bytes",
        "available_bytes",
    }
    person = next(p for p in snap["people"] if p["email"] == BOB)
    assert set(person) == {
        "id",
        "email",
        "workspaces",
        "cpu_percent",
        "memory_bytes",
        "tasks",
        "disk_bytes",
    }
    (container,) = snap["containers"]
    assert container["id"] == made["id"] and container["name"] == "alpha"
    assert set(container) == {
        "id",
        "name",
        "owner",
        "state",
        "cpu_percent",
        "memory_bytes",
        "tasks",
        "disk_bytes",
    }
    assert container["owner"] == BOB


def test_a_scope_switched_off_reads_as_null_at_once(client):
    client.put("/api/monitor/settings", json={**DEFAULTS, "people": False}, headers=changing(ALICE))
    snap = client.get("/api/monitor", headers=as_user(ALICE)).json()
    assert snap["people"] is None and snap["scopes"]["people"] is False
    assert snap["containers"] == []  # positive control: another scope is still there


def test_a_save_wakes_both_loops(settings, runtime):
    """The loops wait on their events through the monitor's clock, which does not move; a save
    sets both events, from the request's thread, and each loop's wait returns and it waits
    again, to its interval."""
    from fastapi.testclient import TestClient
    from rcman.main import Clocks, create_app

    clock = PhasedClock()
    app = create_app(settings, runtime=runtime, clocks=Clocks(monitor=clock))
    with TestClient(app) as client:
        assert app.state.workspaces.restored.wait(30)
        clock.wait_parked(2)  # both loops wait
        before = len(clock.waits)
        r = client.put(
            "/api/monitor/settings", json={**DEFAULTS, "host": False}, headers=changing(ALICE)
        )
        assert r.status_code == 200
        clock.poke()
        clock.wait_parked(2)  # both woke, and wait again
        again = [seconds for method, seconds, _ in clock.waits[before:] if method == "await_event"]
    saved = monitor.DEFAULTS
    assert saved.usage_interval_seconds != saved.disk_interval_seconds  # each loop's is its own
    assert {saved.usage_interval_seconds, saved.disk_interval_seconds} <= set(again), again
