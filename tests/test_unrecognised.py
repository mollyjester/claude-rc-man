"""Output rcman does not recognise, written to the journal once per change."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from rcman import unrecognised

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing, fake_claude, made_workspace, signed_in
from tests.test_journal import guide_check_fails_on_a_drifted_table, guide_fields

REPO = Path(__file__).resolve().parent.parent
LOGGER = "rcman.unrecognised"
HEADING = "## Output rcman does not recognise"
SCREEN = ["PLANTED-SCREEN-LINE   ", "", "second line"]


@pytest.fixture
def journal(caplog):
    caplog.set_level("INFO", logger=LOGGER)
    return caplog


def lines_logged(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]


def expected_fingerprint(lines) -> str:
    """Worked out here, not by the code under test: the start of a sha256 of the lines,
    each without its trailing spaces."""
    text = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def server(state, screen=SCREEN):
    found = {"state": state}
    if state == "unrecognised":
        found["screen"] = screen
    return found


def test_a_server_turning_unrecognised_is_logged_once(journal):
    watch = unrecognised.Watch()
    for _ in range(3):
        watch.listed(ALICE, {"a": server("unrecognised"), "b": server("ready"), "c": None})
    [line] = lines_logged(journal)
    assert line["event"] == "unrecognised"
    assert (line["code"], line["verb"], line["workspace"]) == ("server_unrecognised", "list", "a")
    assert (line["email"], line["lines"]) == (ALICE, len(SCREEN))
    assert line["fingerprint"] == expected_fingerprint(SCREEN)
    assert "PLANTED-SCREEN-LINE" not in journal.text


def test_it_is_logged_again_after_leaving_the_state(journal):
    watch = unrecognised.Watch()
    for state in ("unrecognised", "ready", "unrecognised"):
        watch.listed(ALICE, {"a": server(state)})
    assert len(lines_logged(journal)) == 2


def test_a_listing_without_the_server_forgets_it(journal):
    watch = unrecognised.Watch()
    watch.listed(ALICE, {"a": server("unrecognised")})
    watch.listed(ALICE, {"a": None})
    watch.listed(ALICE, {"a": server("unrecognised")})
    assert len(lines_logged(journal)) == 2


def test_a_reconnected_server_is_not_written_and_ends_an_unrecognised_one(journal):
    watch = unrecognised.Watch()
    reconnected = {**server("reconnected"), "screen": SCREEN}
    watch.listed(ALICE, {"a": reconnected})
    assert lines_logged(journal) == []
    watch.listed(ALICE, {"a": server("unrecognised")})
    watch.listed(ALICE, {"a": reconnected})
    watch.seen(ALICE, "start", "a", server("unrecognised"))
    watch.seen(ALICE, "output", "a", reconnected)
    watch.listed(ALICE, {"a": server("unrecognised")})
    found = [(line["verb"], line["workspace"]) for line in lines_logged(journal)]
    assert found == [("list", "a"), ("start", "a"), ("list", "a")]


def test_an_action_answer_counts_as_an_observation(journal):
    watch = unrecognised.Watch()
    watch.seen(ALICE, "start", "a", server("unrecognised"))
    watch.listed(ALICE, {"a": server("unrecognised")})
    watch.seen(ALICE, "output", "a", server("ready"))
    watch.seen(ALICE, "output", "a", server("unrecognised"))
    found = [(line["verb"], line["workspace"]) for line in lines_logged(journal)]
    assert found == [("start", "a"), ("output", "a")]


def test_unrecognised_output_is_logged_once_until_the_same_call_works(journal):
    watch = unrecognised.Watch()
    code = "claude_unrecognised_output"
    watch.failed(ALICE, "start", "a", code, {"screen": SCREEN})
    watch.failed(ALICE, "start", "a", code, {"screen": SCREEN})
    watch.failed(ALICE, "start", "b", code, {"screen": SCREEN})  # another workspace
    watch.answered(ALICE, "start", "a")  # the first call works
    watch.failed(ALICE, "start", "a", code, {"screen": SCREEN})
    found = [(line["code"], line["verb"], line["workspace"]) for line in lines_logged(journal)]
    assert found == [(code, "start", "a"), (code, "start", "b"), (code, "start", "a")]
    assert lines_logged(journal)[0]["fingerprint"] == expected_fingerprint(SCREEN)


def test_the_failing_calls_remembered_are_bounded(journal, monkeypatch):
    monkeypatch.setattr(unrecognised, "MAX_REMEMBERED", 2)
    watch = unrecognised.Watch()
    for name in ("a", "b", "a", "c", "a"):
        watch.failed(ALICE, "start", name, "claude_unrecognised_output", {"screen": ["x"]})
        assert len(watch.failing) <= 2, watch.failing
    assert [line["workspace"] for line in lines_logged(journal)] == ["a", "b", "c", "a"]


def test_people_are_kept_apart(journal):
    watch = unrecognised.Watch()
    watch.listed(ALICE, {"a": server("unrecognised")})
    watch.listed(BOB, {"a": server("unrecognised")})
    watch.listed(BOB, {})
    watch.listed(ALICE, {"a": server("unrecognised")})
    assert [line["email"] for line in lines_logged(journal)] == [ALICE, BOB]


def test_other_failures_and_odd_answers_log_nothing_and_never_raise(journal):
    watch = unrecognised.Watch()
    watch.failed(ALICE, "start", "a", "already_running", {"screen": SCREEN})
    watch.failed(ALICE, "start", "a", "claude_unrecognised_output", "not an object")
    watch.listed(ALICE, "not a mapping")
    watch.listed(ALICE, {1: server("unrecognised"), "x": "not a server"})
    watch.seen(ALICE, "start", "a", "not an object")
    watch.seen(ALICE, "start", ["a"], server("unrecognised"))
    # A failure with no screen is still unrecognised output: logged, with no lines.
    assert [(line["code"], line["lines"]) for line in lines_logged(journal)] == [
        ("claude_unrecognised_output", 0)
    ]
    journal.clear()

    def broken(*args):
        raise RuntimeError("planted")

    watch._listed = broken
    watch.listed(ALICE, {"a": server("unrecognised")})
    assert [r.levelname for r in journal.records if r.name == LOGGER] == ["ERROR"]


# --- through the application ---------------------------------------------------------


def test_a_start_ending_on_an_unrecognised_screen_is_logged_once_without_it(
    client, app, settings, journal
):
    made = made_workspace(client, ALICE, "alpha")
    signed_in(app, made)
    fake_claude(app.state.workspaces.runtime.claude, env="FAKE_CLAUDE_SCENARIO=unrecognised")
    r = client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    screen = r.json()["server"]["screen"]
    assert fake.UNRECOGNISED_SCREEN in "\n".join(screen)  # positive control
    for _ in range(2):
        assert client.get("/api/workspaces", headers=as_user(ALICE)).status_code == 200
    [line] = lines_logged(journal)
    assert (line["code"], line["verb"], line["workspace"]) == (
        "server_unrecognised",
        "start",
        "alpha",
    )
    assert line["email"] == ALICE and line["fingerprint"] == expected_fingerprint(screen)
    assert fake.UNRECOGNISED_SCREEN not in journal.text
    last = sqlite3.connect(settings.database).execute("select result from audit").fetchall()[-1]
    assert last == ("unrecognised_screen",)


def test_an_administrators_warning_logs_the_screen_once(client, app, journal):
    made = made_workspace(client, BOB, "beta")
    signed_in(app, made)
    fake_claude(app.state.workspaces.runtime.claude, env="FAKE_CLAUDE_SCENARIO=unrecognised")
    service = app.state.workspaces
    out = service.start_server(service.get(made["id"]), "beta")  # outside any route
    assert out["server"]["state"] == "unrecognised"
    for _ in range(2):
        r = client.get("/api/attention", headers=as_user(ALICE))
        assert r.json()["unrecognised"] == [{"email": BOB, "workspace": "beta"}]
    [line] = lines_logged(journal)
    assert (line["email"], line["verb"], line["workspace"]) == (BOB, "list", "beta")
    assert line["fingerprint"] == expected_fingerprint(out["server"]["screen"])
    assert fake.UNRECOGNISED_SCREEN not in journal.text


# --- the guide -----------------------------------------------------------------------


def a_logged_line(caplog) -> dict:
    unrecognised.Watch().listed(ALICE, {"a": server("unrecognised")})
    [line] = lines_logged(caplog)
    return line


def test_the_journal_guide_lists_every_field_of_an_unrecognised_line(journal):
    guide = (REPO / "docs" / "journal.md").read_text(encoding="utf-8")
    assert sorted(guide_fields(guide, HEADING)) == sorted(a_logged_line(journal))


def test_the_unrecognised_guide_check_fails_on_a_drifted_table(journal):
    """Planted twin of the check above."""
    guide_check_fails_on_a_drifted_table(HEADING, set(a_logged_line(journal)))
