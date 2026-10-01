"""Who am I, and my settings."""

import pytest

from helpers import rc_helper
from tests.conftest import ALICE, BOB, as_user, changing


def test_me_reports_identity_role_and_prefix(client):
    assert client.get("/api/me", headers=as_user(ALICE)).json() == {
        "email": ALICE,
        "linux": "alice",
        "admin": True,
        "prefix": "alice",
    }
    assert client.get("/api/me", headers=as_user(BOB)).json()["admin"] is False


def test_prefix_defaults_to_the_linux_user_and_can_be_changed(client):
    assert client.get("/api/me/settings", headers=as_user(BOB)).json() == {"prefix": "bob"}
    r = client.put("/api/me/settings", json={"prefix": "bobs-box"}, headers=changing(BOB))
    assert r.json() == {"prefix": "bobs-box"}
    assert client.get("/api/me", headers=as_user(BOB)).json()["prefix"] == "bobs-box"
    assert client.get("/api/me", headers=as_user(ALICE)).json()["prefix"] == "alice"  # per user


@pytest.mark.parametrize(
    "prefix", ["", "-x", "a b", "a;b", "x" * (rc_helper.MAX_PREFIX_LENGTH + 1)]
)
def test_invalid_prefix_is_refused_by_the_server(client, prefix):
    r = client.put("/api/me/settings", json={"prefix": prefix}, headers=changing(BOB))
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "prefix"


def test_the_prefix_rule_is_the_helpers_rule():
    from rcman.routers.me import SettingsBody

    pattern = SettingsBody.model_json_schema()["properties"]["prefix"]["pattern"]
    assert pattern.strip("^$") == rc_helper.PREFIX.pattern
