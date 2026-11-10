"""The settings of port forwarding (backend/rcman/forwarding_settings.py) and their routes
(backend/rcman/routers/forwarding_settings.py), for administrators: the defaults, a save and
its audit. What a save that turns forwarding off ends is tests/test_forward.py's."""

import sqlite3

from rcman import forwarding_settings

from tests.conftest import ALICE, as_user, changing

PATH = "/api/forwarding/settings"
DEFAULTS = forwarding_settings.DEFAULTS.model_dump()


def stored(settings) -> list[dict]:
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from forwarding_settings")]
    finally:
        con.close()


def audit_rows(settings) -> list[dict]:
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def test_a_save_is_stored_in_seconds_answered_read_back_and_audited(client, settings):
    assert stored(settings) == []  # nothing is stored until a save
    r = client.get(PATH, headers=as_user(ALICE))
    assert (r.status_code, r.json()) == (200, {"settings": DEFAULTS})
    sent = {"enabled": True, "idle_minutes": 10, "absolute_hours": 2}
    r = client.put(PATH, json=sent, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert r.json() == {"settings": sent}
    assert client.get(PATH, headers=as_user(ALICE)).json() == {"settings": sent}
    client.put(PATH, json={**sent, "enabled": False}, headers=changing(ALICE))
    assert stored(settings) == [
        {"id": 1, "enabled": 0, "idle_seconds": 600, "absolute_seconds": 7200}
    ]
    store = client.app.state.forwarding_settings
    assert (store.settings().idle_seconds, store.settings().absolute_seconds) == (600, 7200)
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"], last["email"]) == ("forwarding.settings", "ok", ALICE)
    assert '"enabled": false' in last["target"] and '"idle_minutes": 10' in last["target"]
