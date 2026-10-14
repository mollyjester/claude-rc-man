"""What Claude Code shows, read as a server's state: held to every recorded fixture and
every screen observed on a live server (tests/fakes/)."""

import json

import pytest

from devtools import record_claude as rec
from helpers import screens as h

# --- classification against every recorded fixture ---------------------------------

SERVER_STATES = {
    "rc_logged_out": "logged_out",
    "trust_prompt": "trust_prompt",
    "trust_answer_too_fast": "trust_prompt",
    "trust_declined": "trust_declined",
    "server_connected_after_trust": "connected",
    "server_connected_resumed": "connected",
    "server_ready_after_trust": "ready",
    "server_ready_resumed": "ready",
    "server_stopped_after_trust": "stopped",
    "server_stopped_resumed": "stopped",
    "git_trust_prompt": "trust_prompt",
    "git_server_connected_after_trust": "connected",
    "git_server_connected_resumed": "connected",
    "git_server_ready_after_trust": "ready",
    "git_server_ready_resumed": "ready",
    "git_server_stopped_after_trust": "stopped",
    "git_server_stopped_resumed": "stopped",
    # rcman starts every server with --spawn same-dir, so Claude does not ask
    # this; shown all the same, it is no state rcman knows, and Stop ends it.
    "spawn_prompt": "unrecognised",
    "spawn_prompt_interrupted": "unrecognised",
    "spawn_prompt_answered_after_interrupt": "unrecognised",
    "spawn_prompt_killed": "exited",
    "spawn_connected_same_dir": "connected",
    "spawn_ready_same_dir": "ready",
    "spawn_stopped_same_dir": "stopped",
}
LOGIN_STATES = {
    "login_prompt": "awaiting_code",
    "login_failed": "failed",
    "login_succeeded": "succeeded",
    "login_succeeded_done": "succeeded",
}
NOT_READ_AS_SCREENS = {
    "version": "read by `claude --version` output, not a screen",
    "auth_logout": "nothing reads it: signing out comes back with the sign-in",
    "auth_login_help": "the upgrade checks look for options in it",
    "auth_status_logged_in": "parsed as JSON by the sign-in check (servers.py)",
    "auth_status_logged_out": "parsed as JSON by the sign-in check (servers.py)",
}
VALUES = {
    **rec.ANY_VALUES,
    "cwd": "/home/u/projects/x",
    "folder": "x",
    "login_url": rec.LOGIN_URL_PREFIX + "code=true&state=abc",
    "environment_url": rec.ENVIRONMENT_URL_PREFIX + "env_abc",
    "session_name": "pfx-quiet-otter",
    "time": "01:02:03",
    "branch": "feature-x",
}


def filled(name):
    fixture = rec.load_fixtures()[name]
    return rec.fill("\n".join(fixture.lines), VALUES).split("\n"), fixture.exit_code


def test_every_fixture_is_accounted_for_exactly_once():
    accounted = [*SERVER_STATES, *LOGIN_STATES, *NOT_READ_AS_SCREENS]
    assert len(accounted) == len(set(accounted))
    assert set(accounted) == set(rec.load_fixtures())


@pytest.mark.parametrize("name,state", sorted(SERVER_STATES.items()))
def test_server_screens_classify_as_recorded(name, state):
    lines, exit_code = filled(name)
    assert h.classify(lines, exit_code)["state"] == state


@pytest.mark.parametrize("name,state", sorted(LOGIN_STATES.items()))
def test_login_screens_classify_as_recorded(name, state):
    lines, exit_code = filled(name)
    assert h.classify_login(lines, exit_code)["state"] == state


def test_ready_screen_yields_url_and_session_name():
    info = h.classify(*filled("server_ready_resumed"))
    assert info["environment_url"] == VALUES["environment_url"]
    assert info["session_name"] == VALUES["session_name"]


def test_a_ready_screen_in_a_repository_yields_url_and_session_name():
    info = h.classify(*filled("git_server_ready_resumed"))
    assert info["environment_url"] == VALUES["environment_url"]
    assert info["session_name"] == VALUES["session_name"]
    assert any(line.endswith("· feature-x") for line in filled("git_server_ready_resumed")[0])


def test_the_spawn_mode_question_is_shown_as_it_is():
    lines, exit_code = filled("spawn_prompt")
    assert any("Spawn mode" in line for line in lines)  # the recording is the question
    info = h.classify(lines, exit_code)
    assert info["state"] == "unrecognised"
    assert info["screen"] == lines


def test_connected_screen_has_no_session_name():
    assert h.classify(*filled("server_connected_resumed"))["session_name"] is None


def test_login_prompt_yields_the_url():
    assert h.classify_login(*filled("login_prompt"))["url"] == VALUES["login_url"]


def test_an_unknown_live_screen_is_unrecognised_with_raw_output():
    info = h.classify(["something new and strange"], None)
    assert info["state"] == "unrecognised"
    assert info["screen"] == ["something new and strange"]


def test_an_unknown_exit_is_exited_with_its_code():
    info = h.classify(["crashed"], 3)
    assert (info["state"], info["exit_code"]) == ("exited", 3)


def test_an_empty_live_screen_is_starting():
    assert h.classify([], None)["state"] == "starting"


# --- classification against every screen observed on a live server -------------------

# Screens no recording can bring about, seen on a live server (rec.load_observed).
OBSERVED_STATES = {
    # Claude stopped drawing its status block after a Remote Control error, and then
    # reconnected; it draws the block again on its own tick, if at all.
    "reconnected_without_status": "reconnected",
}


def observed(name):
    screen = rec.load_observed()[name]
    return rec.fill("\n".join(screen.lines), VALUES).split("\n"), screen.exit_code


def unaccounted(observed_names) -> list[str]:
    """The observed screens OBSERVED_STATES does not account for, and those it accounts
    for that are not observed."""
    return sorted(set(observed_names) ^ set(OBSERVED_STATES))


def test_every_observed_screen_is_accounted_for_exactly_once():
    names = rec.load_observed()
    assert names, "no observed screens at all"
    assert unaccounted(names) == []
    assert set(OBSERVED_STATES).isdisjoint(rec.load_fixtures())


def test_an_observed_screen_not_accounted_for_is_found(tmp_path):
    """Planted twin: an extra observed screen fails the accounting."""
    import shutil

    copy = tmp_path / "observed"
    shutil.copytree(rec.OBSERVED, copy)
    manifest = json.loads((copy / rec.MANIFEST).read_text(encoding="utf-8"))
    [(name, entry)] = list(manifest["screens"].items())[:1]
    manifest["screens"]["planted_screen"] = entry
    (copy / rec.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copy(copy / f"{name}.txt", copy / "planted_screen.txt")
    assert "planted_screen" in rec.load_observed(copy)  # the plant took effect
    assert unaccounted(rec.load_observed(copy)) == ["planted_screen"]


@pytest.mark.parametrize("name,state", sorted(OBSERVED_STATES.items()))
def test_observed_screens_classify_as_observed(name, state):
    assert h.classify(*observed(name))["state"] == state


ERROR_LINE = 1  # the observed reconnected screen's error, above its last line


def test_the_observed_reconnected_screen_classifies_as_reconnected():
    lines, exit_code = observed("reconnected_without_status")
    info = h.classify(lines, exit_code)
    assert info["state"] == "reconnected"
    assert (info["session_name"], info["environment_url"]) == (None, None)
    assert info["screen"] == lines
    # Planted twin: ending on its error rather than on the reconnect, it is unrecognised.
    assert "Error" in lines[ERROR_LINE] and "Reconnected" in lines[-1]
    planted = [*lines[:-1], lines[ERROR_LINE]]
    assert h.classify(planted, exit_code)["state"] == "unrecognised"


def reconnected_variants(lines: list[str]) -> dict[str, list[str]]:
    """Screens like the observed reconnected one that say less than it does."""
    log, reconnected = lines[ERROR_LINE], lines[-1]
    stamp = reconnected[: reconnected.index("]") + 1]
    return {
        "ending on an error": [*lines, log],
        "a log line after the reconnect": [*lines, f"{stamp} Connecting"],
        "a line that is no log line after the reconnect": [*lines, "something else"],
        "a line that is no log line before it": ["something else", *lines],
        "an untimed reconnect": [*lines[:-1], reconnected.removeprefix(stamp).strip()],
        "a reconnect saying more": [*lines[:-1], f"{reconnected}, then lost again"],
        "a reconnect with no time it took": [*lines[:-1], f"{stamp} Reconnected"],
        "a reconnect after other words": [
            *lines[:-1],
            f"{stamp} X {reconnected.removeprefix(stamp).strip()}",
        ],
        "a bracketed line that is no clock time": ["[note] something", *lines],
        "a clock time alone": [*lines[:-1], stamp, reconnected],
    }


@pytest.mark.parametrize(
    "variant", sorted(reconnected_variants(observed("reconnected_without_status")[0]))
)
def test_screens_like_the_reconnected_one_stay_unrecognised(variant):
    lines, exit_code = observed("reconnected_without_status")
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    planted = reconnected_variants(lines)[variant]
    assert planted != lines
    info = h.classify(planted, exit_code)
    assert info["state"] == "unrecognised"
    assert info["screen"] == planted


def test_a_reconnected_screen_names_no_environment_even_when_a_log_line_does():
    lines, exit_code = observed("reconnected_without_status")
    url = VALUES["environment_url"]
    planted = [*lines[:-1], f"{lines[-1].split(']')[0]}] Open {url}", lines[-1]]
    assert url in "\n".join(planted)  # the plant took effect
    info = h.classify(planted, exit_code)
    assert info["state"] == "reconnected"
    assert (info["environment_url"], info["session_name"]) == (None, None)


@pytest.mark.parametrize("ready", ["server_ready_resumed", "git_server_ready_resumed"])
def test_a_capacity_line_still_wins_over_log_lines(ready):
    log = observed("reconnected_without_status")[0]
    lines, exit_code = filled(ready)
    for screen in ([*log, *lines], [*lines, log[-1]]):
        info = h.classify(screen, exit_code)
        assert (info["state"], info["session_name"]) == ("ready", VALUES["session_name"])
        assert h.classify(screen, 0)["state"] == "exited"
    assert h.classify(log, 1)["state"] == "exited"


# --- paths ----------------------------------------------------------------------------
