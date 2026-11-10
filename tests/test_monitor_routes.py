"""The Monitor page's routes (backend/rcman/routers/monitor.py): the snapshot, and the settings,
for administrators."""

import sqlite3

from rcman import monitor

from tests.conftest import ALICE, BOB, as_user, changing, made_workspace
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


def test_a_save_is_stored_answered_read_back_and_audited(client, settings):
    assert stored(settings) == []  # nothing is stored until a save
    r = client.get("/api/monitor/settings", headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"settings": DEFAULTS})
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
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"], last["email"]) == ("monitor.settings", "ok", ALICE)
    assert '"containers": true' in last["target"]


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


# --- Refresh ---------------------------------------------------------------------------


def snapshot(client) -> dict:
    r = client.get("/api/monitor", headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    return r.json()


def refresh(client) -> None:
    r = client.post("/api/monitor/refresh", headers=changing(ALICE))
    assert (r.status_code, r.json()) == (202, {"refreshing": True}), r.text


def disk_reads(runtime) -> int:
    return [c[0] for c in runtime.monitor_calls].count("disk_usage")


def test_a_refresh_runs_the_rounds_at_once_and_is_audited(client, settings, runtime):
    refresh(client)
    until(lambda: not snapshot(client)["refreshing"])
    assert disk_reads(runtime) == 1
    snap = snapshot(client)
    assert snap["usage"]["collected_at"] and snap["usage"]["error"] is None
    assert snap["disk"]["collected_at"] and snap["disk"]["error"] is None
    found = [(r["action"], r["result"], r["email"]) for r in audit_rows(settings)]
    assert [f for f in found if f[0] == "monitor.refresh"] == [("monitor.refresh", "ok", ALICE)]
