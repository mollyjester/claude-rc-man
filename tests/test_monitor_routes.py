"""The Monitor page's routes (backend/rcman/routers/monitor.py): the snapshot, and the settings,
for administrators."""

import sqlite3
import threading

import pytest
from rcman import db, monitor, people
from rcman import runtime as rt

from tests.conftest import ALICE, BOB, as_user, changing, made_workspace
from tests.hang_guard import HANG_GUARD
from tests.until import until

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
    sent = {**DEFAULTS, "usage_interval_seconds": 120, "containers": False, "storage": False}
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
    assert rows[0]["storage"] == 0


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


@pytest.mark.parametrize("name", monitor.SCOPES)
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
    [
        ("GET", "/api/monitor"),
        ("GET", "/api/monitor/settings"),
        ("PUT", "/api/monitor/settings"),
        ("POST", "/api/monitor/refresh"),
    ],
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
    assert set(snap) == {
        "scopes",
        "usage",
        "disk",
        "refreshing",
        "host",
        "storage",
        "people",
        "containers",
    }
    assert snap["scopes"] == {"host": True, "people": True, "containers": True, "storage": True}
    assert set(snap["storage"]) == {
        "images_bytes",
        "workspace_volumes_bytes",
        "other_volumes_bytes",
        "claude_versions_bytes",
        "total_bytes",
    }
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


def test_a_save_wakes_both_loops(app, settings):
    """The loops wait on the injected wait; a save sets both their events, from the request's
    thread, and each wait returns with its event set. The loops' wait for the restore is not
    one of them."""
    import threading

    from fastapi.testclient import TestClient

    made = app.state.monitor
    woken = []
    told = threading.Condition()
    real = made.wait

    async def recording(event, timeout):
        await real(event, timeout)
        if event.is_set() and event is not made._restored:
            with told:
                woken.append(event)
                told.notify_all()

    made.wait = recording
    with TestClient(app) as client:
        assert app.state.workspaces.restored.wait(HANG_GUARD)
        with told:
            before = len(woken)
        r = client.put(
            "/api/monitor/settings", json={**DEFAULTS, "host": False}, headers=changing(ALICE)
        )
        assert r.status_code == 200
        with told:
            both = told.wait_for(lambda: len({id(e) for e in woken[before:]}) >= 2, HANG_GUARD)
            assert both, woken
    assert {id(e) for e in woken[before:]} == {id(made.usage_wake), id(made.disk_wake)}


# --- Refresh ---------------------------------------------------------------------------

DAVE = "dave@example.com"


def snapshot(client) -> dict:
    r = client.get("/api/monitor", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    return r.json()


def refresh(client, who: str = ALICE):
    r = client.post("/api/monitor/refresh", headers=changing(who))
    assert (r.status_code, r.json()) == (202, {"refreshing": True}), r.text
    return r


def disk_reads(runtime) -> int:
    return [c[0] for c in runtime.monitor_calls].count("disk_usage")


def test_two_refreshes_while_a_disk_round_runs_start_one_disk_round(client, settings, runtime):
    con = db.connect(settings.database)
    try:
        people.add(con, DAVE, True)  # a second administrator
    finally:
        con.close()
    started, release = threading.Event(), threading.Event()
    real = runtime.disk_usage

    def held(cancel):
        started.set()
        release.wait(HANG_GUARD)
        return real(cancel)

    runtime.disk_usage = held
    refresh(client)
    assert started.wait(HANG_GUARD)  # the disk round runs, its read held
    assert snapshot(client)["refreshing"] is True
    refresh(client, DAVE)  # another administrator's press joins it
    refresh(client)
    release.set()
    until(lambda: not snapshot(client)["refreshing"])
    assert disk_reads(runtime) == 1
    snap = snapshot(client)
    assert snap["disk"]["collected_at"] and snap["disk"]["error"] is None
    found = [(r["action"], r["result"], r["email"]) for r in audit_rows(settings)]
    assert [f for f in found if f[0] == "monitor.refresh"] == [
        ("monitor.refresh", "ok", ALICE),
        ("monitor.refresh", "ok", DAVE),
        ("monitor.refresh", "ok", ALICE),
    ]


def test_a_refresh_that_fails_shows_the_rounds_error(client, runtime):
    said = "planted: podman system df failed"
    runtime.disk_failures = [rt.RuntimeFailed(said), rt.RuntimeFailed(said)]
    refresh(client)
    until(lambda: snapshot(client)["disk"]["error"])
    snap = snapshot(client)
    assert snap["disk"] == {"collected_at": None, "error": said}
    assert disk_reads(runtime) == 2  # the round's own retry, and no more
    until(lambda: not snapshot(client)["refreshing"])


def test_a_refresh_with_disk_off_runs_no_disk_round(client, runtime):
    made = client.app.state.monitor
    usage = []
    real = made.usage_round

    def counted():
        usage.append(1)
        real()

    made.usage_round = counted
    off = {**DEFAULTS, "people": False, "containers": False, "storage": False}
    assert client.put("/api/monitor/settings", json=off, headers=changing(ALICE)).status_code == 200
    before = len(usage)
    refresh(client)
    until(lambda: len(usage) > before and not snapshot(client)["refreshing"])  # the usage round ran
    assert disk_reads(runtime) == 0
    assert snapshot(client)["disk"]["collected_at"] is None
    # positive control: with Storage on, the same press runs the disk round
    on = {**off, "storage": True}
    assert client.put("/api/monitor/settings", json=on, headers=changing(ALICE)).status_code == 200
    refresh(client)
    until(lambda: snapshot(client)["disk"]["collected_at"])
    assert disk_reads(runtime) == 1
