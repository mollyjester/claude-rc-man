"""The settings of port forwarding (backend/rcman/forwarding_settings.py) and their routes
(backend/rcman/routers/forwarding_settings.py), for administrators: the defaults, the bounds,
who may read and save them, the audit, and the listeners a save that leaves forwarding off
calls."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from rcman import forwarding_settings

from tests.conftest import ALICE, BOB, ORIGIN, as_user, changing

PATH = "/api/forwarding/settings"
DEFAULTS = forwarding_settings.DEFAULTS.model_dump()
INTERVALS = ["idle_minutes", "absolute_hours"]


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


def bound(name: str, kind: str) -> int:
    meta = {
        type(m).__name__: m
        for m in forwarding_settings.ForwardingSettings.model_fields[name].metadata
    }
    return meta["Ge"].ge if kind == "ge" else meta["Le"].le


def test_the_defaults_are_off_five_idle_minutes_and_twelve_hours_in_all():
    assert forwarding_settings.DEFAULTS.enabled is False
    assert forwarding_settings.DEFAULTS.idle_seconds == 300
    assert forwarding_settings.DEFAULTS.absolute_seconds == 43200


def test_the_settings_are_the_defaults_with_no_row(client, settings):
    assert stored(settings) == []  # nothing is stored until a save
    r = client.get(PATH, headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    assert r.json() == {"settings": DEFAULTS}


def test_a_save_is_stored_in_seconds_answered_and_read_back(client, settings):
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


@pytest.mark.parametrize("name", INTERVALS)
def test_a_value_out_of_its_bounds_is_refused_under_its_field(client, settings, name):
    # the longest lifetime, so that the idle timeout is held to its own bounds alone
    base = {**DEFAULTS, "absolute_hours": bound("absolute_hours", "le")}
    for value in (bound(name, "ge"), bound(name, "le")):  # positive control: the bounds are taken
        r = client.put(PATH, json={**base, name: value}, headers=changing(ALICE))
        assert r.status_code == 200, r.text
    for value in (bound(name, "ge") - 1, bound(name, "le") + 1, "5", 1.5, None):
        r = client.put(PATH, json={**base, name: value}, headers=changing(ALICE))
        assert r.status_code == 422, (value, r.text)
        assert [e["field"] for e in r.json()["errors"]] == [name], value
    r = client.put(PATH, json={**base, name: bound(name, "ge") - 1}, headers=changing(ALICE))
    (error,) = r.json()["errors"]
    assert str(bound(name, "ge")) in error["message"]  # the bound, in the field's own unit
    # nothing refused was stored: the last save taken, at the upper bound, stays
    column, unit = {
        "idle_minutes": ("idle_seconds", 60),
        "absolute_hours": ("absolute_seconds", 3600),
    }[name]
    assert stored(settings)[0][column] == bound(name, "le") * unit


@pytest.mark.parametrize("value", [1, 0, "true", "yes", None])
def test_the_switch_takes_only_true_or_false(client, value):
    r = client.put(PATH, json={**DEFAULTS, "enabled": value}, headers=changing(ALICE))
    assert r.status_code == 422, r.text
    assert [e["field"] for e in r.json()["errors"]] == ["enabled"]


def test_a_save_must_send_every_setting(client):
    for name in DEFAULTS:
        sent = {k: v for k, v in DEFAULTS.items() if k != name}
        r = client.put(PATH, json=sent, headers=changing(ALICE))
        assert r.status_code == 422, name
        assert [e["field"] for e in r.json()["errors"]] == [name]


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_the_routes_are_for_administrators_only(client, method):
    r = client.request(method, PATH, headers=changing(BOB), json=DEFAULTS)
    assert (r.status_code, r.json()["code"]) == (403, "admin_only")


def test_a_save_needs_the_public_origin(client, settings):
    sent = {**DEFAULTS, "enabled": True}
    r = client.put(PATH, json=sent, headers=as_user(ALICE))  # no Origin
    assert (r.status_code, r.json()["code"]) == (403, "origin_refused")
    r = client.put(
        PATH, json=sent, headers={**as_user(ALICE), "Origin": "https://evil.example.com"}
    )
    assert (r.status_code, r.json()["code"]) == (403, "origin_refused")
    assert stored(settings) == []
    r = client.put(PATH, json=sent, headers={**as_user(ALICE), "Origin": ORIGIN})  # control
    assert r.status_code == 200, r.text


def test_a_save_is_audited_with_the_settings_saved(client, settings):
    sent = {**DEFAULTS, "enabled": True}
    client.put(PATH, json=sent, headers=changing(ALICE))
    client.put(PATH, json={**sent, "enabled": "no"}, headers=changing(ALICE))
    found = [(r["action"], r["result"], r["email"]) for r in audit_rows(settings)[-2:]]
    assert found == [
        ("forwarding.settings", "ok", ALICE),
        ("forwarding.settings", "validation_failed", ALICE),
    ]
    target = audit_rows(settings)[-2]["target"]
    assert '"enabled": true' in target and '"idle_minutes": 5' in target
    assert audit_rows(settings)[-1]["target"] is None


def test_a_save_that_leaves_forwarding_off_calls_each_listener_after_it_is_stored(client):
    store = client.app.state.forwarding_settings
    heard = []
    store.on_off(lambda: heard.append(("first", store.settings().enabled)))
    store.on_off(lambda: heard.append(("second", store.settings().enabled)))
    r = client.put(PATH, json={**DEFAULTS, "enabled": True}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert heard == []  # a save that turns it on calls none
    r = client.put(PATH, json={**DEFAULTS, "enabled": "off"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert heard == []  # nor does a refused one
    r = client.put(PATH, json={**DEFAULTS, "enabled": False}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    # each is called once, in order, once the settings say off
    assert heard == [("first", False), ("second", False)]


def test_a_listener_that_raises_fails_the_answer_after_every_listener_is_called(app, settings):
    """The settings stay stored, every listener after it is still called, and the audit row is
    the failure."""
    store = app.state.forwarding_settings
    heard = []

    def raising():
        heard.append("raising")
        raise RuntimeError("planted: the forward server did not stop")

    store.on_off(raising)
    store.on_off(lambda: heard.append("recording"))
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.put(PATH, json={**DEFAULTS, "enabled": True}, headers=changing(ALICE))
        assert r.status_code == 200, r.text  # positive control: a save that turns it on
        r = client.put(PATH, json=DEFAULTS, headers=changing(ALICE))
        assert (r.status_code, r.json()["code"]) == (500, "internal_error"), r.text
    assert heard == ["raising", "recording"]
    assert stored(settings)[0]["enabled"] == 0
    assert store.settings().enabled is False
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"]) == ("forwarding.settings", "internal_error")


@pytest.mark.parametrize(
    "idle_minutes, absolute_hours, taken",
    [(59, 1, True), (60, 1, False), (61, 1, False), (600, 12, True), (720, 12, False)],
)
def test_an_idle_timeout_as_long_as_the_lifetime_is_refused_at_the_idle_field(
    client, settings, idle_minutes, absolute_hours, taken
):
    sent = {**DEFAULTS, "idle_minutes": idle_minutes, "absolute_hours": absolute_hours}
    r = client.put(PATH, json=sent, headers=changing(ALICE))
    if taken:
        assert r.status_code == 200, r.text
        return
    assert r.status_code == 422, r.text
    (error,) = r.json()["errors"]
    assert error["field"] == "idle_minutes"
    assert error["message"] == forwarding_settings.IDLE_NOT_SHORTER
    assert stored(settings) == []  # nothing refused was stored
