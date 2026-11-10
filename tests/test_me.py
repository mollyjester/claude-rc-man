"""Who am I, and my settings."""

import re
from pathlib import Path

from tests.conftest import ALICE, BOB, as_user, changing

UNSET = {"prefix": "", "author_name": "", "author_email": "", "effective_author": None}


def settings_body():
    from rcman.routers.me import SettingsBody

    return SettingsBody


def patch(client, body):
    return client.patch("/api/me/settings", json=body, headers=changing(BOB))


def test_me_reports_identity_role_and_prefix(client):
    assert client.get("/api/me", headers=as_user(ALICE)).json() == {
        "email": ALICE,
        "admin": True,
        "prefix": None,
    }
    assert client.get("/api/me", headers=as_user(BOB)).json()["admin"] is False


def test_a_prefix_can_be_set_and_cleared_again(client):
    r = patch(client, {"prefix": "bobs-box"})
    assert r.json() == {**UNSET, "prefix": "bobs-box"}
    assert client.get("/api/me", headers=as_user(BOB)).json()["prefix"] == "bobs-box"
    assert client.get("/api/me", headers=as_user(ALICE)).json()["prefix"] is None  # per person
    assert patch(client, {"prefix": ""}).json() == UNSET  # each workspace's name again
    assert client.get("/api/me", headers=as_user(BOB)).json()["prefix"] is None


def test_the_commit_author_is_set_and_reported(client):
    r = patch(client, {"author_name": "Bob B", "author_email": "bob@work.example"})
    expected = {
        **UNSET,
        "author_name": "Bob B",
        "author_email": "bob@work.example",
        "effective_author": {"name": "Bob B", "email": "bob@work.example"},
    }
    assert (r.status_code, r.json()) == (200, expected)
    assert client.get("/api/me/settings", headers=as_user(BOB)).json() == expected
    assert client.get("/api/me/settings", headers=as_user(ALICE)).json()["author_name"] == ""


# --- the colleagues' guide to Settings ------------------------------------------------

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "settings.md"
HEADER = "| Setting | What it sets |"


def guide_fields(markdown):
    """The field each row of the guide's settings table names, as (`field`), in order."""
    lines = markdown.splitlines()
    if HEADER not in lines:
        return []
    found = []
    for line in lines[lines.index(HEADER) + 2 :]:
        if not line.startswith("|"):
            break
        found.append(re.search(r"\(`([a-z_]+)`\)", line.split("|")[1]).group(1))
    return found


def test_the_settings_guide_lists_exactly_the_settings_model():
    fields = list(settings_body().model_fields)
    assert fields  # non-vacuity
    assert guide_fields(GUIDE.read_text(encoding="utf-8")) == fields


def test_the_settings_guide_check_fails_on_a_drifted_guide():
    """Planted twin: a guide missing a row is caught."""
    fields = list(settings_body().model_fields)
    rows = [f"| **Label** (`{field}`) | what it sets |" for field in fields]
    right = "\n".join([HEADER, "|---|---|", *rows, ""])
    assert guide_fields(right) == fields
    missing = right.replace(rows[-1] + "\n", "")
    assert guide_fields(missing) != fields
