"""Who am I, and my settings."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import rc_helper, users_file
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_rc_helper import AUTHOR_NAMES, HANGUL_FILLERS, format_characters

UNSET = {"prefix": "bob", "author_name": "", "author_email": "", "effective_author": None}


def settings_body():
    from rcman.routers.me import SettingsBody

    return SettingsBody


def patch(client, body, who=BOB):
    return client.patch("/api/me/settings", json=body, headers=changing(who))


def test_me_reports_identity_role_and_prefix(client):
    assert client.get("/api/me", headers=as_user(ALICE)).json() == {
        "email": ALICE,
        "linux": "alice",
        "admin": True,
        "prefix": "alice",
    }
    assert client.get("/api/me", headers=as_user(BOB)).json()["admin"] is False


def test_settings_are_their_defaults_until_set(client):
    assert client.get("/api/me/settings", headers=as_user(BOB)).json() == UNSET


def test_prefix_defaults_to_the_linux_user_and_can_be_changed(client):
    r = patch(client, {"prefix": "bobs-box"})
    assert r.json() == {**UNSET, "prefix": "bobs-box"}
    assert client.get("/api/me", headers=as_user(BOB)).json()["prefix"] == "bobs-box"
    assert client.get("/api/me", headers=as_user(ALICE)).json()["prefix"] == "alice"  # per user


@pytest.mark.parametrize(
    "prefix", ["", "-x", "a b", "a;b", "x" * (rc_helper.MAX_PREFIX_LENGTH + 1)]
)
def test_invalid_prefix_is_refused_by_the_server(client, prefix):
    r = patch(client, {"prefix": prefix})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "prefix"


def test_the_prefix_rule_is_the_helpers_rule():
    pattern = settings_body().model_json_schema()["properties"]["prefix"]["pattern"]
    assert pattern == f"^{rc_helper.PREFIX.pattern}$"


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


def test_an_empty_email_is_the_users_file_address(client):
    """Signed in with the address in other letters, the author's is the users file's."""
    r = patch(client, {"author_name": "Bob B", "author_email": ""}, who=BOB.upper())
    assert r.status_code == 200, r.text
    assert r.json()["author_email"] == ""
    assert r.json()["effective_author"] == {"name": "Bob B", "email": BOB}
    patch(client, {"author_email": "bob@work.example"})
    patch(client, {"author_email": ""})  # and setting it empty again goes back to it
    assert client.get("/api/me/settings", headers=as_user(BOB)).json()["effective_author"] == {
        "name": "Bob B",
        "email": BOB,
    }


def test_a_change_leaves_the_other_settings_alone(client):
    patch(client, {"prefix": "bobs-box"})
    patch(client, {"author_name": "Bob B", "author_email": "bob@work.example"})
    got = client.get("/api/me/settings", headers=as_user(BOB)).json()
    assert (got["prefix"], got["author_name"], got["author_email"]) == (
        "bobs-box",
        "Bob B",
        "bob@work.example",
    )
    patch(client, {"prefix": "other"})
    got = client.get("/api/me/settings", headers=as_user(BOB)).json()
    assert (got["prefix"], got["author_name"], got["author_email"]) == (
        "other",
        "Bob B",
        "bob@work.example",
    )


def test_an_author_set_first_leaves_the_prefix_its_default(client):
    r = patch(client, {"author_name": "Bob B"})
    assert r.status_code == 200, r.text
    assert r.json()["effective_author"] == {"name": "Bob B", "email": BOB}
    assert r.json()["prefix"] == "bob"
    assert client.get("/api/me", headers=as_user(BOB)).json()["prefix"] == "bob"


def stored(settings, email):
    import sqlite3

    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("select * from user_settings where email = ?", (email,)).fetchone())
    finally:
        con.close()


def test_an_empty_email_is_stored_as_unset(client, settings):
    patch(client, {"author_name": "Bob B", "author_email": "bob@work.example"})
    assert stored(settings, BOB)["author_email"] == "bob@work.example"  # positive control
    assert patch(client, {"author_email": ""}).status_code == 200
    assert stored(settings, BOB)["author_email"] is None


@pytest.mark.parametrize(("name", "accepted"), AUTHOR_NAMES)
def test_the_server_accepts_exactly_the_names_the_helpers_rule_does(client, name, accepted):
    assert bool(rc_helper.AUTHOR_NAME.fullmatch(name)) is accepted  # the helper's own verdict
    r = patch(client, {"author_name": name})
    if accepted:
        assert (r.status_code, r.json()["author_name"]) == (200, name)
    else:
        assert r.status_code == 422, name
        assert [e["field"] for e in r.json()["errors"]] == ["author_name"]


def test_the_server_refuses_exactly_the_format_characters_the_helpers_rule_does(client):
    found = format_characters() + [chr(c) for c in HANGUL_FILLERS]
    assert found  # non-vacuity
    disagree = []
    for c in found:
        name = f"a{c}b"
        status = patch(client, {"author_name": name}).status_code
        if (status == 200) is not bool(rc_helper.AUTHOR_NAME.fullmatch(name)):
            disagree.append((f"U+{ord(c):04X}", status))
    assert disagree == []


def test_the_author_rules_are_the_helpers_rules():
    properties = settings_body().model_json_schema()["properties"]
    assert properties["author_name"]["pattern"] == f"^{rc_helper.AUTHOR_NAME.pattern}$"
    assert properties["author_email"]["pattern"] == f"^(?:{users_file.EMAIL.pattern})?$"


@pytest.mark.parametrize("email", ["no-at", "a@b", "a b@c.d", "bob@example.com\n", " "])
def test_an_invalid_author_email_is_refused(client, email):
    r = patch(client, {"author_name": "Bob B", "author_email": email})
    assert r.status_code == 422
    assert [e["field"] for e in r.json()["errors"]] == ["author_email"]
    assert client.get("/api/me/settings", headers=as_user(BOB)).json() == UNSET  # nothing saved


@pytest.mark.parametrize("field", list(settings_body().model_fields))
def test_null_is_refused_for_every_setting(client, field):
    r = patch(client, {field: None})
    assert r.status_code == 422
    assert [e["field"] for e in r.json()["errors"]] == [field]


def test_a_change_with_no_settings_is_refused(client):
    r = patch(client, {})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert r.json()["errors"]


def test_author_of_is_what_the_settings_report(app, client):
    from rcman.identity import Identity
    from rcman.routers.me import Author, author_of

    request = SimpleNamespace(app=app)
    bob = Identity(email=BOB, linux="bob", admin=False)
    assert author_of(request, bob) is None
    patch(client, {"prefix": "bobs-box"})
    assert author_of(request, bob) is None  # a row with no name is still no author
    patch(client, {"author_name": "Bob B"})
    assert author_of(request, bob) == Author(name="Bob B", email=BOB)
    patch(client, {"author_email": "bob@work.example"})
    author = author_of(request, bob)
    assert author == Author(name="Bob B", email="bob@work.example")
    effective = client.get("/api/me/settings", headers=as_user(BOB)).json()["effective_author"]
    assert effective == {"name": author.name, "email": author.email}


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
    """Planted twin: a row missing, a field renamed or a row too many is caught."""
    fields = list(settings_body().model_fields)
    rows = [f"| **Label** (`{field}`) | what it sets |" for field in fields]
    right = "\n".join([HEADER, "|---|---|", *rows, ""])
    assert guide_fields(right) == fields
    missing = right.replace(rows[-1] + "\n", "")
    assert missing != right and guide_fields(missing) != fields
    renamed = right.replace(f"(`{fields[0]}`)", "(`renamed`)")
    assert renamed != right and guide_fields(renamed) != fields
    extra = right + "| **Extra** (`extra`) | what it sets |\n"
    assert guide_fields(extra) != fields
    assert guide_fields("") == []


def test_settings_are_changed_with_patch_only(client):
    r = client.put("/api/me/settings", json={"prefix": "x"}, headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (405, "method_not_allowed")
