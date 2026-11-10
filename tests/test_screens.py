"""What Claude Code shows, read as a server's state: held to every recorded fixture and
every screen observed on a live server (tests/fakes/)."""

import json
import re

from devtools import record_claude as rec
from helpers import screens as h

# --- classification against every recorded fixture ---------------------------------

SERVER_STATES = {
    "rc_logged_out": "logged_out",
    # rcman seeds oauthAccount before every start, so Claude does not refuse this way;
    # shown all the same, the server has ended.
    "rc_no_oauth_account": "exited",
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


def unaccounted_fixtures(names) -> list[str]:
    """The fixtures the tables above do not account for, those they account for that are not
    recorded, and those they account for more than once."""
    accounted = [*SERVER_STATES, *LOGIN_STATES, *NOT_READ_AS_SCREENS]
    twice = sorted({name for name in accounted if accounted.count(name) > 1})
    return sorted(set(names) ^ set(accounted)) + twice


def test_every_fixture_is_accounted_for_exactly_once():
    names = rec.load_fixtures()
    assert names, "no fixtures at all"
    assert unaccounted_fixtures(names) == []


def test_a_fixture_not_accounted_for_is_found():
    """Planted twin: a recorded fixture that no table names fails the accounting."""
    assert unaccounted_fixtures([*rec.load_fixtures(), "planted_capture"]) == ["planted_capture"]


def test_a_ready_screen_yields_its_url_and_session_name_and_a_sign_in_its_url():
    info = h.classify(*filled("server_ready_resumed"))
    assert info["environment_url"] == VALUES["environment_url"]
    assert info["session_name"] == VALUES["session_name"]
    assert h.classify(*filled("server_connected_resumed"))["session_name"] is None
    assert h.classify_login(*filled("login_prompt"))["url"] == VALUES["login_url"]


def test_a_screen_rcman_does_not_know_is_unrecognised_with_its_lines():
    info = h.classify(["something new and strange"], None)
    assert info["state"] == "unrecognised"
    assert info["screen"] == ["something new and strange"]


# --- classification against every screen observed on a live server -------------------

# Screens no recording can bring about, seen on a live server (rec.load_observed).
OBSERVED_STATES = {
    # Claude stopped drawing its status block after a Remote Control error, and then
    # reconnected; it draws the block again on its own tick, if at all.
    "reconnected_without_status": "reconnected",
    # The same after both startup questions were answered, which rcman's `y` does: the
    # screen still holds the startup text, with the answers, above the log lines.
    "remote_control_answered_reconnected": "reconnected",
    # Claude's one-time confirmation of an account that has never used Remote Control,
    # asked after the folder-trust question.
    "remote_control_prompt": "remote_control_prompt",
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


def test_every_recorded_and_observed_screen_reads_as_its_state():
    wrong = {}
    for name, state in SERVER_STATES.items():
        if (found := h.classify(*filled(name))["state"]) != state:
            wrong[name] = found
    for name, state in LOGIN_STATES.items():
        if (found := h.classify_login(*filled(name))["state"]) != state:
            wrong[name] = found
    for name, state in OBSERVED_STATES.items():
        if (found := h.classify(*observed(name))["state"]) != state:
            wrong[name] = found
    assert wrong == {}


ASKS_YES_OR_NO = re.compile(r"(\[y/N\]|\(y/n\))$")


def asking_states(screens: dict[str, tuple[list[str], int | None]]) -> set[str]:
    """The states of the still running screens whose last line asks for a y or an n."""
    found = set()
    for lines, exit_code in screens.values():
        nonempty = [line for line in lines if line.strip()]
        if exit_code is None and nonempty and ASKS_YES_OR_NO.search(nonempty[-1].strip()):
            found.add(h.classify(lines, exit_code)["state"])
    return found


def every_server_screen() -> dict[str, tuple[list[str], int | None]]:
    return {
        **{name: filled(name) for name in SERVER_STATES},
        **{name: observed(name) for name in OBSERVED_STATES},
    }


def test_the_questions_are_the_states_of_the_screens_that_ask_for_a_y_or_an_n():
    assert asking_states(every_server_screen()) == set(h.QUESTIONS)


def test_a_state_asking_for_a_y_or_an_n_that_is_not_a_question_is_found():
    """Planted twin: a screen ending in a question that no state of QUESTIONS is."""
    screens = every_server_screen()
    assert asking_states(screens) == set(h.QUESTIONS)  # positive control
    screens["planted"] = (["Continue? (y/n)"], None)
    assert h.classify(*screens["planted"])["state"] == "unrecognised"  # the plant took effect
    assert asking_states(screens) == {*h.QUESTIONS, "unrecognised"}
    assert asking_states({}) == set()


# --- a reconnect appended to a screen that is not the reconnected one -------------------

ANSWERED = "remote_control_answered_reconnected"
RECONNECTED_SCENARIOS = sorted(n for n, s in OBSERVED_STATES.items() if s == "reconnected")
TIME = VALUES["time"]


def reconnect_appended(screens: dict[str, tuple[list[str], int | None]]) -> dict[str, str]:
    """The state of each still running screen with a reconnect appended."""
    return {
        name: h.classify([*lines, f"[{TIME}] Reconnected after 1s"], exit_code)["state"]
        for name, (lines, exit_code) in screens.items()
        if exit_code is None
    }


def live_screens() -> dict[str, tuple[list[str], int | None]]:
    return {
        **{f"fixture {name}": filled(name) for name in rec.load_fixtures()},
        **{f"observed {name}": observed(name) for name in rec.load_observed()},
    }


def wrongly_reconnected(states: dict[str, str]) -> list[str]:
    """The screens a reconnect appended turns into the reconnected state that are not one
    of the observed reconnected screens."""
    allowed = {f"observed {name}" for name in RECONNECTED_SCENARIOS}
    return sorted(
        name for name, state in states.items() if state == "reconnected" and name not in allowed
    )


def test_a_reconnect_appended_to_any_other_live_screen_is_not_reconnected():
    states = reconnect_appended(live_screens())
    assert len(states) == sum(1 for _, code in live_screens().values() if code is None) > 20
    for name in RECONNECTED_SCENARIOS:  # positive control: the allowed ones are found
        assert states[f"observed {name}"] == "reconnected"
    assert wrongly_reconnected(states) == []
    for name, state in states.items():  # a status block still decides
        if name.startswith("fixture") and any("Capacity:" in x for x in live_screens()[name][0]):
            assert state in ("connected", "ready"), name


def test_a_screen_wrongly_reconnected_is_found():
    """Planted twin: an answered screen whose answered question is of another kind."""
    screens = live_screens()
    assert wrongly_reconnected(reconnect_appended(screens)) == []  # positive control
    lines, exit_code = observed(ANSWERED)
    screens["planted"] = (lines, exit_code)
    states = reconnect_appended(screens)
    assert states["planted"] == "reconnected"  # the plant took effect
    assert wrongly_reconnected(states) == ["planted"]
    assert wrongly_reconnected({}) == []
