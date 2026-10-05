"""Output rcman does not recognise, written to the journal once per change."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from rcman import unrecognised

from devtools import fake_claude as fake
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_journal import guide_check_fails_on_a_drifted_table, guide_fields
from tests.test_servers import ALPHA, unrecognised_claude

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


def server(path, state, screen=SCREEN):
    found = {"path": path, "state": state}
    if state == "unrecognised":
        found["screen"] = screen
    return found


def listed(*servers):
    return (0, {"servers": list(servers)})


class Script:
    """An inner runner answering from a list, recording what it was given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, linux, args, stdin=None, timeout=None):
        self.calls.append((linux, list(args), stdin, timeout))
        return self.answers.pop(0)


def watched(settings, *answers):
    script = Script(*answers)
    return unrecognised.watched(script, unrecognised.Watch(settings)), script


def test_a_server_turning_unrecognised_is_logged_once(settings, journal):
    answer = listed(server("projects/a", "unrecognised"), server("projects/b", "ready"))
    run, _ = watched(settings, answer, answer, answer)
    for _ in range(3):
        assert run("alice", ["list"]) == answer
    [line] = lines_logged(journal)
    assert line["event"] == "unrecognised"
    assert (line["code"], line["verb"], line["path"]) == (
        "server_unrecognised",
        "list",
        "projects/a",
    )
    assert (line["linux"], line["lines"]) == ("alice", len(SCREEN))
    assert line["fingerprint"] == expected_fingerprint(SCREEN)
    assert "PLANTED-SCREEN-LINE" not in journal.text


def test_it_is_logged_again_after_leaving_the_state(settings, journal):
    run, _ = watched(
        settings,
        listed(server("projects/a", "unrecognised")),
        listed(server("projects/a", "ready")),
        listed(server("projects/a", "unrecognised")),
    )
    for _ in range(3):
        run("alice", ["list"])
    assert len(lines_logged(journal)) == 2


def test_a_list_without_the_server_forgets_it(settings, journal):
    run, _ = watched(
        settings,
        listed(server("projects/a", "unrecognised")),
        listed(),
        listed(server("projects/a", "unrecognised")),
    )
    for _ in range(3):
        run("alice", ["list"])
    assert len(lines_logged(journal)) == 2


def test_a_reconnected_server_is_not_written_and_ends_an_unrecognised_one(settings, journal):
    reconnected = {**server("projects/a", "reconnected"), "screen": SCREEN}
    run, _ = watched(
        settings,
        listed(reconnected),
        listed(server("projects/a", "unrecognised")),
        listed(reconnected),
        (0, {"server": server("projects/a", "unrecognised")}),
        (0, {"server": reconnected}),
        listed(server("projects/a", "unrecognised")),
    )
    run("alice", ["list"])
    assert lines_logged(journal) == []
    run("alice", ["list"])
    run("alice", ["list"])
    run("alice", ["start", "--prefix=p", "--", "projects/a"])
    run("alice", ["output", "--", "projects/a"])
    run("alice", ["list"])
    found = [(line["verb"], line["path"]) for line in lines_logged(journal)]
    assert found == [("list", "projects/a"), ("start", "projects/a"), ("list", "projects/a")]


def test_an_action_answer_counts_as_an_observation(settings, journal):
    run, _ = watched(
        settings,
        (0, {"server": server("projects/a", "unrecognised")}),
        listed(server("projects/a", "unrecognised")),
        (0, {"path": "projects/a", "server": server("projects/a", "ready")}),
        (0, {"path": "projects/a", "server": server("projects/a", "unrecognised")}),
    )
    run("alice", ["start", "--prefix=p", "--", "projects/a"])
    run("alice", ["list"])
    run("alice", ["output", "--", "projects/a"])
    run("alice", ["output", "--", "projects/a"])
    found = [(line["verb"], line["path"]) for line in lines_logged(journal)]
    assert found == [("start", "projects/a"), ("output", "projects/a")]


def failure(code, **detail):
    return (1, {"error": {"code": code, "message": "planted", **detail}})


def test_unrecognised_output_is_logged_once_until_the_same_call_works(settings, journal):
    git = failure("git_unrecognised_output", git=["PLANTED-GIT-LINE"])
    status = ["status", "--", "projects/a"]
    run, _ = watched(
        settings,
        git,
        git,
        git,  # the same call on another folder
        (0, {"branch": "main"}),  # the first call works
        git,
        failure("claude_unrecognised_output", screen=SCREEN),
    )
    run("alice", status)
    run("alice", status)
    run("alice", ["status", "--", "projects/b"])
    run("alice", status)
    run("alice", status)
    run("alice", ["auth-status"])
    found = [(line["code"], line["verb"], line["path"]) for line in lines_logged(journal)]
    assert found == [
        ("git_unrecognised_output", "status", "projects/a"),
        ("git_unrecognised_output", "status", "projects/b"),
        ("git_unrecognised_output", "status", "projects/a"),
        ("claude_unrecognised_output", "auth-status", None),
    ]
    assert lines_logged(journal)[0]["lines"] == 1
    assert lines_logged(journal)[-1]["fingerprint"] == expected_fingerprint(SCREEN)
    assert "PLANTED-GIT-LINE" not in journal.text


def test_the_failing_calls_remembered_are_bounded(settings, journal, monkeypatch):
    monkeypatch.setattr(unrecognised, "MAX_REMEMBERED", 2)
    git = failure("git_unrecognised_output", git=["x"])
    watch = unrecognised.Watch(settings)
    run = unrecognised.watched(Script(*[git] * 5), watch)
    for path in ("a", "b", "a", "c", "a"):
        run("alice", ["status", "--", path])
        assert len(watch.failing) <= 2, watch.failing
    # a and b remembered; a again is still remembered; c is past the bound, so all are
    # forgotten for it, and a is new again after.
    assert [line["path"] for line in lines_logged(journal)] == ["a", "b", "c", "a"]
    # At the bound, a remembered call failing with another code forgets nothing.
    journal.clear()
    claude = failure("claude_unrecognised_output", screen=["x"])
    watch = unrecognised.Watch(settings)
    run = unrecognised.watched(Script(git, git, claude, git), watch)
    for path in ("a", "b", "a", "b"):
        run("alice", ["status", "--", path])
        assert len(watch.failing) <= 2, watch.failing
    found = [(line["path"], line["code"]) for line in lines_logged(journal)]
    assert found == [
        ("a", "git_unrecognised_output"),
        ("b", "git_unrecognised_output"),
        ("a", "claude_unrecognised_output"),
    ]


def test_people_are_kept_apart(settings, journal):
    answer = listed(server("projects/a", "unrecognised"))
    run, _ = watched(settings, answer, answer, listed(), answer)
    run("alice", ["list"])
    run("bob", ["list"])
    run("bob", ["list"])  # bob's server is gone
    run("alice", ["list"])  # alice's is still remembered
    assert [line["linux"] for line in lines_logged(journal)] == ["alice", "bob"]


def test_other_failures_and_odd_answers_log_nothing_and_never_raise(settings, journal):
    odd = [
        failure("git_failed", git=["words"]),
        failure("claude_unrecognised_output", screen="not a list"),
        (1, {"error": "not an object"}),
        (1, {}),
        (0, None),
        (0, []),
        (0, {"servers": "not a list"}),
        (
            0,
            {"servers": [None, 1, {"state": "unrecognised"}, {"path": 5, "state": "unrecognised"}]},
        ),
        (0, {"server": "not an object"}),
        (0, {"server": {"state": "unrecognised"}}),
        (0, {"server": {"path": ["a"], "state": "unrecognised"}}),
    ]
    run, _ = watched(settings, *odd)
    calls = [["status", "--", "projects/a"], ["auth-status"], ["list"], ["list", "--"], []]
    for i, answer in enumerate(odd):
        assert run("alice", calls[i % len(calls)]) == answer
    logged = lines_logged(journal)
    # A screen that is not a list is still unrecognised output: logged, with no lines.
    assert [(line["code"], line["lines"]) for line in logged] == [("claude_unrecognised_output", 0)]
    journal.clear()
    watch = unrecognised.Watch(settings)

    def broken(*args):
        raise RuntimeError("planted")

    watch._observe = broken
    answer = listed(server("projects/a", "unrecognised"))
    assert unrecognised.watched(Script(answer), watch)("alice", ["list"]) == answer
    assert [r.levelname for r in journal.records if r.name == LOGGER] == ["ERROR"]


def test_stdin_and_options_are_never_logged(settings, journal):
    run, script = watched(
        settings,
        failure("claude_unrecognised_output", screen=SCREEN),
        failure("git_unrecognised_output", git=["x"]),
    )
    run(
        "alice", ["auth-login", "--email=PLANTED-OPTION@example.com"], "PLANTED-SECRET\n", timeout=5
    )
    run("alice", ["clone", "--url=https://github.com/PLANTED-URL", "--", "projects"])
    assert script.calls[0] == (
        "alice",
        ["auth-login", "--email=PLANTED-OPTION@example.com"],
        "PLANTED-SECRET\n",
        5,
    )  # passed on as given
    assert len(lines_logged(journal)) == 2  # positive control
    for planted in ("PLANTED-SECRET", "PLANTED-OPTION", "PLANTED-URL"):
        assert planted not in journal.text


def test_the_line_names_the_person_and_survives_an_invalid_users_file(settings, journal):
    answer = listed(server("projects/a", "unrecognised"))
    run, _ = watched(settings, answer, answer)
    run("alice", ["list"])
    settings.users_file.write_text("not = [valid")
    run("bob", ["list"])
    assert [(line["linux"], line["email"]) for line in lines_logged(journal)] == [
        ("alice", ALICE),
        ("bob", None),
    ]


# --- through the application ---------------------------------------------------------


def test_a_start_ending_on_an_unrecognised_screen_is_logged_once_without_it(
    client, helper_homes, settings, journal
):
    unrecognised_claude(helper_homes)
    r = client.post("/api/servers", json=ALPHA, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    screen = r.json()["server"]["screen"]
    assert fake.UNRECOGNISED_SCREEN in "\n".join(screen)  # positive control
    for _ in range(2):
        assert client.get("/api/servers", headers=as_user(ALICE)).status_code == 200
    [line] = lines_logged(journal)
    assert (line["code"], line["verb"], line["path"]) == (
        "server_unrecognised",
        "start",
        ALPHA["path"],
    )
    assert line["email"] == ALICE and line["fingerprint"] == expected_fingerprint(screen)
    assert fake.UNRECOGNISED_SCREEN not in journal.text
    last = sqlite3.connect(settings.database).execute("select result from audit").fetchall()[-1]
    assert last == ("unrecognised_screen",)  # the audit trail is as it was


def test_an_administrators_warning_logs_the_screen_once(
    client, helper_homes, admin_runner, journal
):
    from tests.test_attention import CLEAN_VERSIONS

    admin_runner.replies = [(0, CLEAN_VERSIONS), (0, CLEAN_VERSIONS)]
    unrecognised_claude(helper_homes)
    # Started outside rcman, so that only the administrator's listing sees it.
    code, out = helper_homes("bob", ["start", "--prefix=bob", "--", "projects/bob-project"])
    assert (code, out["server"]["state"]) == (0, "unrecognised"), out
    for _ in range(2):
        r = client.get("/api/attention", headers=as_user(ALICE))
        assert r.json()["unrecognised"] == [{"linux": "bob", "path": "projects/bob-project"}]
    [line] = lines_logged(journal)
    assert (line["linux"], line["email"], line["verb"]) == ("bob", BOB, "list")
    assert line["fingerprint"] == expected_fingerprint(out["server"]["screen"])
    assert fake.UNRECOGNISED_SCREEN not in journal.text


def test_an_unrecognised_sign_in_screen_is_logged(client, helper_homes, journal):
    from tests.test_claude import logged_out

    unrecognised_claude(helper_homes)
    logged_out(helper_homes, "bob")
    r = client.post("/api/claude/login", json={}, headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (502, "claude_unrecognised_output")
    [line] = lines_logged(journal)
    assert (line["code"], line["verb"], line["path"]) == (
        "claude_unrecognised_output",
        "auth-login",
        None,
    )
    assert line["email"] == BOB and line["lines"] == len(r.json()["screen"])
    assert line["fingerprint"] == expected_fingerprint(r.json()["screen"])
    assert fake.UNRECOGNISED_SCREEN not in journal.text


# --- the guide -----------------------------------------------------------------------


def a_logged_line(settings, caplog) -> dict:
    run, _ = watched(settings, listed(server("projects/a", "unrecognised")))
    run("alice", ["list"])
    [line] = lines_logged(caplog)
    return line


def test_the_journal_guide_lists_every_field_of_an_unrecognised_line(settings, journal):
    guide = (REPO / "docs" / "journal.md").read_text(encoding="utf-8")
    expected = set(a_logged_line(settings, journal))
    assert sorted(guide_fields(guide, HEADING)) == sorted(expected)


def test_the_unrecognised_guide_check_fails_on_a_drifted_table(settings, journal):
    """Planted twin of the check above."""
    guide_check_fails_on_a_drifted_table(HEADING, set(a_logged_line(settings, journal)))
