"""What Claude Code shows, read as a server's state: held to every recorded fixture and
every screen observed on a live server (tests/fakes/)."""

import json
import re

import pytest

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


def test_the_observed_remote_control_prompt_classifies_as_it():
    lines, exit_code = observed("remote_control_prompt")
    info = h.classify(lines, exit_code)
    assert info["state"] == "remote_control_prompt"
    assert (info["session_name"], info["environment_url"]) == (None, None)
    assert "screen" not in info
    assert info["exit_code"] is None
    # Planted twin: the same screen with its question taken away is no prompt.
    planted = lines[:-1]
    assert planted != lines
    assert h.classify(planted, exit_code)["state"] == "unrecognised"


def remote_control_prompt_variants(lines: list[str]) -> dict[str, list[str]]:
    """Screens like the observed Remote Control prompt that say something else."""
    question = lines[-1]
    return {
        "a line after the question": [*lines, "something else"],
        "a capital Y": [*lines[:-1], question.replace("y/n", "Y/n")],
        "no question mark": [*lines[:-1], question.replace("?", "")],
        "another word": [*lines[:-1], question.replace("Enable", "Disable")],
        "more words after it": [*lines[:-1], f"{question} now"],
        "words before it": [*lines[:-1], f"So: {question}"],
        "the trust question alone": lines[:-1],
    }


@pytest.mark.parametrize(
    "variant", sorted(remote_control_prompt_variants(observed("remote_control_prompt")[0]))
)
def test_screens_like_the_remote_control_prompt_stay_unrecognised(variant):
    lines, exit_code = observed("remote_control_prompt")
    assert h.classify(lines, exit_code)["state"] == "remote_control_prompt"  # positive control
    planted = remote_control_prompt_variants(lines)[variant]
    assert planted != lines
    info = h.classify(planted, exit_code)
    assert info["state"] == "unrecognised"
    assert info["screen"] == planted


@pytest.mark.parametrize("trailing", [[""], ["", ""], ["   "], ["", "  \t", ""]])
def test_blank_lines_after_the_remote_control_prompt_do_not_hide_it(trailing):
    lines, exit_code = observed("remote_control_prompt")
    planted = [*lines, *trailing]
    assert planted != lines
    assert h.classify(planted, exit_code)["state"] == "remote_control_prompt"


@pytest.mark.parametrize("code", [0, 1])
def test_a_remote_control_prompt_on_a_dead_pane_is_exited(code):
    lines, _ = observed("remote_control_prompt")
    assert h.classify(lines, None)["state"] == "remote_control_prompt"  # positive control
    info = h.classify(lines, code)
    assert (info["state"], info["exit_code"]) == ("exited", code)
    assert info["screen"] == lines


@pytest.mark.parametrize(
    "block,state",
    [("server_connected_resumed", "connected"), ("server_ready_resumed", "ready")],
)
def test_the_remote_control_prompt_above_a_status_block_is_that_block(block, state):
    prompt, exit_code = observed("remote_control_prompt")
    lines, _ = filled(block)
    assert h.classify(prompt, exit_code)["state"] == "remote_control_prompt"  # positive control
    assert h.classify([*prompt, *lines], exit_code)["state"] == state


def test_the_remote_control_prompt_as_the_last_line_of_a_ready_screen_is_the_prompt():
    """As for the trust question, the last line decides over a status block above it."""
    prompt, exit_code = observed("remote_control_prompt")
    lines, _ = filled("server_ready_resumed")
    assert h.classify(lines, exit_code)["state"] == "ready"  # positive control
    info = h.classify([*lines, prompt[-1]], exit_code)
    assert info["state"] == "remote_control_prompt"
    trust, _ = filled("trust_prompt")
    assert h.classify([*lines, trust[-1]], exit_code)["state"] == "trust_prompt"


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


@pytest.mark.parametrize("ready", ["server_ready_resumed", "git_server_ready_resumed"])
def test_a_capacity_line_still_wins_over_log_lines(ready):
    log = observed("reconnected_without_status")[0]
    lines, exit_code = filled(ready)
    for screen in ([*log, *lines], [*lines, log[-1]]):
        info = h.classify(screen, exit_code)
        assert (info["state"], info["session_name"]) == ("ready", VALUES["session_name"])
        assert h.classify(screen, 0)["state"] == "exited"
    assert h.classify(log, 1)["state"] == "exited"


# --- the startup text kept above the log, with both questions answered -----------------

ANSWERED = "remote_control_answered_reconnected"
RECONNECTED_SCENARIOS = sorted(n for n, s in OBSERVED_STATES.items() if s == "reconnected")
ANSWER = re.compile(r"(\[y/N\]|\(y/n\)) y$")
TIME = VALUES["time"]


def anchors(lines: list[str]) -> dict[str, int]:
    """Where the parts of the answered screen are, found by what the lines say."""
    return {
        "marker": next(i for i, x in enumerate(lines) if x.startswith("Remote Control starts")),
        "warning": next(i for i, x in enumerate(lines) if "configures hooks" in x),
        "trust": next(i for i, x in enumerate(lines) if x.startswith("Trust ")),
        "block": next(i for i, x in enumerate(lines) if x.startswith("Take this session")),
        "block_end": next(i for i, x in enumerate(lines) if x.startswith("control. Press")),
        "enable": next(i for i, x in enumerate(lines) if x.startswith("Enable Remote Control")),
        "log": next(i for i, x in enumerate(lines) if re.match(r"\[\d\d:\d\d:\d\d\] ", x)),
    }


def test_the_observed_answered_screen_classifies_as_reconnected():
    lines, exit_code = observed(ANSWERED)
    info = h.classify(lines, exit_code)
    assert info["state"] == "reconnected"
    assert (info["session_name"], info["environment_url"]) == (None, None)
    assert info["screen"] == lines
    at = anchors(lines)
    assert ANSWER.search(lines[at["trust"]]) and ANSWER.search(lines[at["enable"]])
    assert at["marker"] < at["trust"] < at["block"] < at["enable"] < at["log"]
    assert lines[-1].startswith("[") and "Reconnected after" in lines[-1]


def answered_variants(lines: list[str]) -> dict[str, list[str]]:
    """Screens like the observed answered one that say something else."""
    at = anchors(lines)
    trust, enable, last = lines[at["trust"]], lines[at["enable"]], lines[-1]
    log = lines[at["log"] :]
    stamp = f"[{TIME}]"
    prompt = observed("remote_control_prompt")[0]
    variants = {
        "a banner before the marker": ["A new version is available", *lines],
        "a line of other words after the first answer": [
            *lines[: at["trust"] + 1],
            "something else",
            *lines[at["trust"] + 1 :],
        ],
        "a line of other words among the logs": [*lines[:-1], "something else", last],
        "a line of other words after the second answer": [
            *lines[: at["enable"] + 1],
            "something else",
            *lines[at["enable"] + 1 :],
        ],
        "a session completed last": [*lines, f"{stamp} Session completed (1s) cse_x"],
        "a session completed in place of the reconnect": [
            *lines[:-1],
            f"{stamp} Session completed",
        ],
        "no reconnect last": lines[:-1],
        "a spawn question after the answers": [
            *lines[: at["enable"] + 1],
            "Choose [1/2] (default: 1):",
            *lines[at["enable"] + 1 :],
        ],
        "a spawn question last": [*lines, "Choose [1/2] (default: 1):"],
        "another question last": [*lines, "Continue? (y/n)"],
        "a doubled answer to the trust question": [
            *lines[: at["trust"]],
            "Trust a? [y/N] n? [y/N] y",
            *lines[at["trust"] + 1 :],
        ],
        "an unanswered question in the middle": [
            *lines[: at["enable"]],
            "Enable Remote Control? (y/n)",
            *lines[at["enable"] + 1 :],
        ],
        "an unanswered trust question in the middle": [
            *lines[: at["trust"]],
            trust.removesuffix(" y"),
            *lines[at["trust"] + 1 :],
        ],
        "a second start with an unanswered question": [*lines[:-1], *prompt, last],
        "the marker and no answered question": [*lines[: at["trust"]], *log],
        "a partial head at the top": lines[at["warning"] - 1 :],
    }
    for question, line in (("trust", trust), ("enable", enable)):
        row = at[question]
        stem = line.removesuffix(" y")
        for name, answered in {
            "n": f"{stem} n",
            "N": f"{stem} N",
            "Y": f"{stem} Y",
            "yes": f"{stem} yes",
            "y y": f"{stem} y y",
            "a double space": f"{stem}  y",
            "a tab": f"{stem}\ty",
            "a no-break space": f"{stem}\u00a0y",
            "no answer": stem,
        }.items():
            variants[f"{question} answered {name}"] = [
                *lines[:row],
                answered,
                *lines[row + 1 :],
            ]
        variants[f"{question} answered on the next line"] = [
            *lines[:row],
            stem,
            "y",
            *lines[row + 1 :],
        ]
    return variants


@pytest.mark.parametrize("variant", sorted(answered_variants(observed(ANSWERED)[0])))
def test_screens_like_the_answered_one_stay_unrecognised(variant):
    lines, exit_code = observed(ANSWERED)
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    planted = answered_variants(lines)[variant]
    assert planted != lines
    info = h.classify(planted, exit_code)
    assert info["state"] == "unrecognised"
    assert info["screen"] == planted


def test_the_answered_variants_include_every_kind_of_answer():
    """Non-vacuity: the table holds the same refused answers for each question, and some."""
    names = answered_variants(observed(ANSWERED)[0])
    kinds = {
        question: {n.removeprefix(f"{question} answered ") for n in names if n.startswith(question)}
        for question in ("trust", "enable")
    }
    assert kinds["trust"] == kinds["enable"]
    assert len(kinds["trust"]) > 1


@pytest.mark.parametrize("wording", ["retry [3/5]", "ask (y/n)", "Trust x? [y/N]"])
def test_a_log_line_holding_a_choice_is_a_log_line(wording):
    """A log line is Claude's own output: the check for unanswered questions is for the
    other lines. This holds on a log alone and under the startup text."""
    lines, exit_code = observed(ANSWERED)
    log = f"[{TIME}] {wording}"
    alone = [log, lines[-1]]
    assert h.classify([lines[-1]], None)["state"] == "reconnected"  # positive control
    assert h.classify(alone, exit_code)["state"] == "reconnected"
    under = [*lines[:-1], log, lines[-1]]
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    assert h.classify(under, exit_code)["state"] == "reconnected"
    # Planted twin: the same words on a line that is no log line are a question.
    assert h.classify([wording, lines[-1]], exit_code)["state"] == "unrecognised"
    assert h.classify([*lines[:-1], wording, lines[-1]], exit_code)["state"] == "unrecognised"


def test_a_prompt_without_a_choice_is_free_wording_only_before_the_first_answer():
    """Wording before the first answered question is free, as the hooks warning varies
    with the folder. After it Claude has moved on, so a line that is none of the known
    ones is not the screen of a server that went on."""
    lines, exit_code = observed(ANSWERED)
    at = anchors(lines)
    prompt = "Press Enter to continue"
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    before = [*lines[: at["trust"]], prompt, *lines[at["trust"] :]]
    after = [*lines[: at["trust"] + 1], prompt, *lines[at["trust"] + 1 :]]
    assert prompt in before and prompt in after  # the plants took effect
    assert h.classify(before, exit_code)["state"] == "reconnected"
    assert h.classify(after, exit_code)["state"] == "unrecognised"


def accepted_variants(lines: list[str]) -> dict[str, list[str]]:
    """Screens like the observed answered one that say less, or other wording, and are
    still the same state."""
    at = anchors(lines)
    saved = next(x for x in observed("remote_control_prompt")[0] if x.startswith("Couldn't save"))
    log = lines[at["log"] :]
    return {
        "no Take-this-session block": [*lines[: at["block"]], *lines[at["block_end"] + 1 :]],
        "no Enable question": [*lines[: at["trust"] + 1], "", *log],
        "blank lines": [x for line in lines for x in (line, "", "  ")],
        "blank lines first": ["", "  ", *lines],
        "another warning": [
            *lines[: at["warning"]],
            "  ⚠ Something else about this folder",
            *lines[at["warning"] + 1 :],
        ],
        "no warning": [*lines[: at["warning"]], *lines[at["warning"] + 2 :]],
        "the config note": [*lines[: at["trust"] + 1], saved, *lines[at["trust"] + 1 :]],
        "cut at the first answer": lines[at["trust"] :],
        "cut at the second answer": lines[at["enable"] :],
        "cut inside the block": lines[at["block_end"] - 1 :],
        "cut after the answers": log,
        "more logs": [*lines[:-1], f"[{TIME}] Connecting", lines[-1]],
        "trailing spaces": [f"{x}   " for x in lines],
    }


@pytest.mark.parametrize("variant", sorted(accepted_variants(observed(ANSWERED)[0])))
def test_screens_like_the_answered_one_that_say_less_are_still_reconnected(variant):
    lines, exit_code = observed(ANSWERED)
    planted = accepted_variants(lines)[variant]
    assert planted != lines
    info = h.classify(planted, exit_code)
    assert info["state"] == "reconnected"
    assert (info["session_name"], info["environment_url"]) == (None, None)
    assert info["screen"] == planted


def test_a_screen_with_an_unanswered_question_in_place_of_an_answer_is_not_this_state():
    """The planted twin of the variants above, from the accepted side: the cut form with
    its answer taken away is no longer accepted."""
    lines, exit_code = observed(ANSWERED)
    cut = lines[anchors(lines)["trust"] :]
    assert h.classify(cut, exit_code)["state"] == "reconnected"
    unanswered = [cut[0].removesuffix(" y"), *cut[1:]]
    assert unanswered != cut
    assert h.classify(unanswered, exit_code)["state"] == "unrecognised"


def test_an_unanswered_question_last_is_still_that_question():
    lines, exit_code = observed(ANSWERED)
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    enable = lines[anchors(lines)["enable"]].removesuffix(" y")
    trust = lines[anchors(lines)["trust"]].removesuffix(" y")
    assert h.classify([*lines, enable], exit_code)["state"] == "remote_control_prompt"
    assert h.classify([*lines, trust], exit_code)["state"] == "trust_prompt"


def test_the_answered_screen_names_no_environment_even_when_the_head_does():
    lines, exit_code = observed(ANSWERED)
    url = VALUES["environment_url"]
    planted = [*lines[:1], f"  {url}", *lines[1:]]
    assert url in "\n".join(planted)  # the plant took effect
    info = h.classify(planted, exit_code)
    assert info["state"] == "reconnected"
    assert (info["environment_url"], info["session_name"]) == (None, None)


@pytest.mark.parametrize("code", [0, 1])
def test_the_answered_screen_on_a_dead_pane_is_exited(code):
    lines, _ = observed(ANSWERED)
    assert h.classify(lines, None)["state"] == "reconnected"  # positive control
    info = h.classify(lines, code)
    assert (info["state"], info["exit_code"]) == ("exited", code)
    assert info["screen"] == lines


@pytest.mark.parametrize(
    "block,state",
    [("server_connected_resumed", "connected"), ("server_ready_resumed", "ready")],
)
def test_a_status_block_under_the_answered_screen_is_that_block(block, state):
    lines, exit_code = observed(ANSWERED)
    status, _ = filled(block)
    assert h.classify(lines, exit_code)["state"] == "reconnected"  # positive control
    info = h.classify([*lines, *status], exit_code)
    assert info["state"] == state
    if state == "ready":
        assert info["session_name"] == VALUES["session_name"]
    else:
        assert info["session_name"] is None


@pytest.mark.parametrize(
    "duration,accepted",
    [
        ("3s", True),
        ("13s", True),
        ("1m 3s", True),
        ("1h 2m 3s", True),
        ("1m", False),
        ("s", False),
        ("1h", False),
        ("1h 3s", True),
        ("1m  3s", False),
        ("３s", False),
    ],
)
def test_the_reconnect_names_how_long_it_took(duration, accepted):
    lines = [f"[{TIME}] Reconnected after {duration}"]
    assert h.classify([f"[{TIME}] Reconnected after 3s"], None)["state"] == "reconnected"
    assert (h.classify(lines, None)["state"] == "reconnected") is accepted


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


# --- paths ----------------------------------------------------------------------------
