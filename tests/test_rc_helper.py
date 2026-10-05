"""rc-helper: the per-user control script, driven through its real command line.

Tests run ``helpers/rc-helper`` as a subprocess with its test hooks pointing at
a temporary home, a private tmux socket and the fake `claude`. Screen
classification is held to every recorded fixture.
"""

import contextlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from devtools import fake_claude as fake
from devtools import record_claude as rec
from devtools import tmuxctl
from helpers import git_run
from helpers import rc_helper as h
from tests.conftest import tmux_sockets
from tests.github_server import UNREACHABLE
from tests.helper_env import helper_env

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "helpers" / "rc-helper"


# --- harness ----------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "projects" / "alpha").mkdir(parents=True)
    (home / "projects" / "beta" / "inner").mkdir(parents=True)
    (home / ".hidden").mkdir()
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_READY_SECONDS=0.3 exec {rec.FAKE_CLAUDE} "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    with tmux_sockets("rch-") as sock:
        yield helper_env(home, wrapper, sock / "rcman.sock", path=os.environ["PATH"])


def helper(env, *args, stdin=None):
    """rc-helper's exit status and answer; ``stdin`` is text, or bytes sent as they are."""
    if isinstance(stdin, str):
        stdin = stdin.encode()
    proc = subprocess.run(
        [str(HELPER), *args], env=env, input=stdin, capture_output=True, timeout=60
    )
    proc.stdout = proc.stdout.decode("utf-8", "replace")
    proc.stderr = proc.stderr.decode("utf-8", "replace")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"not JSON (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}")
    return proc.returncode, out


def ok(env, *args, stdin=None):
    code, out = helper(env, *args, stdin=stdin)
    assert code == 0, out
    return out


def error_code(env, *args, stdin=None):
    code, out = helper(env, *args, stdin=stdin)
    assert code == 1, out
    return out["error"]["code"]


def log_in(env):
    fake.write_state(Path(env["RC_HELPER_HOME"]) / ".claude-fake", logged_in=True)


def wait_state(env, path, wanted, tries=100):
    import time

    for _ in range(tries):
        server = next((s for s in ok(env, "list")["servers"] if s["path"] == path), None)
        if server and server["state"] == wanted:
            return server
        time.sleep(0.1)
    pytest.fail(f"{path} never reached {wanted}; last: {server}")


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
    "auth_logout": "rc-admin runs it and reads only the exit status",
    "auth_login_help": "the upgrade checks look for options in it",
    "auth_status_logged_in": "parsed as JSON by auth-status",
    "auth_status_logged_out": "parsed as JSON by auth-status",
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


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    (home / "file").write_text("x")
    (tmp_path / "outside").mkdir()
    (home / "escape").symlink_to(tmp_path / "outside")
    (home / "inner-link").symlink_to(home / "a")
    return home


@pytest.mark.parametrize(
    "rel,code",
    [
        ("", "path_is_home"),
        (".", "path_is_home"),
        ("a/..", "path_is_home"),
        ("/etc", "path_invalid"),
        ("a\0b", "path_invalid"),
        ("../x", "path_outside_home"),
        ("a/../../x", "path_outside_home"),
        ("escape", "path_outside_home"),
        ("missing", "path_not_found"),
        ("file", "path_not_directory"),
    ],
)
def test_folder_refusals(home, rel, code):
    with pytest.raises(h.HelperError) as err:
        h.resolve_folder(home, rel)
    assert err.value.code == code


def test_folder_inside_home_resolves(home):
    assert h.resolve_folder(home, "a/b") == (home / "a" / "b").resolve()


def test_symlink_inside_home_resolves_to_its_target(home):
    assert h.resolve_folder(home, "inner-link") == (home / "a").resolve()


def test_home_itself_is_allowed_only_for_listing(home):
    assert h.resolve_folder(home, "", allow_home=True) == home.resolve()


def test_the_home_folder_relative_to_itself_is_the_empty_path(home):
    assert h.relative(home, (home / "a" / "b").resolve()) == "a/b"  # positive control
    assert h.relative(home, home.resolve()) == ""


def test_the_home_folder_listed_as_dot_is_listed_as_the_home(env):
    assert ok(env, "tree", "--", ".")["path"] == ""
    assert ok(env, "tree", "--", "projects/..")["path"] == ""
    assert ok(env, "tree", "--", "projects")["path"] == "projects"  # positive control


def test_refusals_through_the_command_line(env):
    assert error_code(env, "start", "", "--prefix", "p") == "path_is_home"
    assert error_code(env, "start", "../x", "--prefix", "p") == "path_outside_home"
    assert error_code(env, "tree", "nope") == "path_not_found"


# --- tree -----------------------------------------------------------------------------


def test_tree_lists_subfolders_and_hides_dot_folders(env):
    out = ok(env, "tree")
    assert [e["path"] for e in out["entries"]] == ["projects"]
    assert out["entries"][0]["has_children"] is True


def test_tree_shows_dot_folders_on_request(env):
    paths = [e["path"] for e in ok(env, "tree", "--all")["entries"]]
    assert ".hidden" in paths


def test_tree_lists_a_subfolder(env):
    entries = ok(env, "tree", "projects")["entries"]
    assert [(e["path"], e["has_children"]) for e in entries] == [
        ("projects/alpha", False),
        ("projects/beta", True),
    ]


def test_tree_skips_symlinks_leading_outside_home(env, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (Path(env["RC_HELPER_HOME"]) / "projects" / "out").symlink_to(tmp_path / "elsewhere")
    paths = [e["path"] for e in ok(env, "tree", "projects")["entries"]]
    assert "projects/out" not in paths


def test_tree_depth_is_bounded(env):
    assert error_code(env, "tree", "--depth", str(h.MAX_TREE_DEPTH + 1)) == "depth_invalid"
    nested = ok(env, "tree", "--depth", "2")["entries"]
    projects = next(e for e in nested if e["path"] == "projects")
    assert [c["path"] for c in projects["children"]] == ["projects/alpha", "projects/beta"]


# --- server lifecycle through the fake ---------------------------------------------


def test_start_refuses_without_a_claude_login(env):
    assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "claude_not_logged_in"


def test_start_trust_confirm_ready_stop_resume_forget(env):
    log_in(env)
    started = ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert started["server"]["state"] == "trust_prompt"

    confirmed = ok(env, "confirm", "projects/alpha", "--answer", "y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_state(env, "projects/alpha", "ready")
    assert ready["session_name"].startswith("pfx-")
    assert ready["environment_url"].startswith(rec.ENVIRONMENT_URL_PREFIX)

    assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "already_running"

    stopped = ok(env, "stop", "projects/alpha")
    assert stopped["server"]["state"] == "stopped"
    assert error_code(env, "stop", "projects/alpha") == "not_running"

    resumed = ok(env, "resume", "projects/alpha", "--prefix", "pfx")
    assert resumed["server"]["state"] in ("connected", "ready")
    again = wait_state(env, "projects/alpha", "ready")
    assert again["session_name"] == ready["session_name"]

    ok(env, "stop", "projects/alpha")
    ok(env, "forget", "projects/alpha")
    assert [s["path"] for s in ok(env, "list")["servers"]] == []


def git_folder(env, rel, branch="main"):
    """A repository in the home folder, on ``branch``."""
    from tests.git_fixtures import fixture_git, make_repo

    path = make_repo(Path(env["RC_HELPER_HOME"]) / rel)
    if branch != "main":
        fixture_git(path, "switch", "-q", "-c", branch)
    return path


def test_a_server_in_a_git_repository_starts_stops_and_resumes(env):
    log_in(env)
    git_folder(env, "projects/repo", branch="feature-x")
    started = ok(env, "start", "projects/repo", "--prefix", "pfx")
    assert started["server"]["state"] == "trust_prompt"

    confirmed = ok(env, "confirm", "projects/repo", "--answer", "y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_state(env, "projects/repo", "ready")
    assert ready["session_name"].startswith("pfx-")
    screen = ok(env, "output", "projects/repo")["screen"]
    assert any(line.endswith("· repo · feature-x") for line in screen)
    assert not any("Spawn mode" in line for line in screen)

    assert ok(env, "stop", "projects/repo")["server"]["state"] == "stopped"
    assert ok(env, "resume", "projects/repo", "--prefix", "pfx")["server"]["state"] in (
        "connected",
        "ready",
    )
    assert wait_state(env, "projects/repo", "ready")["session_name"] == ready["session_name"]
    ok(env, "stop", "projects/repo")
    ok(env, "forget", "projects/repo")
    assert ok(env, "list")["servers"] == []


def started_without_spawn(env):
    """Servers started from now on are started without --spawn, so that Claude
    asks the spawn-mode question in a repository."""
    Path(env["RC_HELPER_CLAUDE"]).write_text(
        "#!/bin/sh\n"
        "for a; do shift\n"
        '  if [ -n "$skip" ]; then skip=; continue; fi\n'
        '  if [ "$a" = --spawn ]; then skip=1; continue; fi\n'
        '  set -- "$@" "$a"\n'
        "done\n"
        f'exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )


# Short deadlines for a stop, so a test of what happens after them is quick.
QUICK_STOP = {"STOP_TIMEOUT": 1.0, "END_GRACE_SECONDS": 1.0}


def test_a_server_at_the_spawn_mode_question_is_ended_by_stop_and_can_be_forgotten(env):
    import signal

    log_in(env)
    git_folder(env, "projects/repo")
    started_without_spawn(env)
    ok(env, "start", "projects/repo", "--prefix", "pfx")
    asked = ok(env, "confirm", "projects/repo", "--answer", "y")["server"]
    assert asked["state"] == "unrecognised"
    assert any("Spawn mode" in line for line in asked["screen"])

    code, out = helper_with(env, QUICK_STOP, "stop", "--", "projects/repo", timeout=30)
    assert code == 0, out
    assert out["server"]["state"] == "exited"
    assert out["server"]["exit_code"] == 128 + signal.SIGKILL  # Ctrl+C and SIGTERM did not end it
    assert [s["state"] for s in ok(env, "list")["servers"]] == ["exited"]
    ok(env, "forget", "projects/repo")
    assert ok(env, "list")["servers"] == []


def ignoring(env, *signals, child=False):
    """Servers started from now on ignore ``signals`` while Claude asks to trust the folder.

    With ``child``, each also leaves a child of its own running, in its process
    group, whose process ID is in the returned file. The child ignores SIGHUP,
    which the session leader's death sends it, so only a signal to the whole
    group ends it.
    """
    pid_file = Path(env["RC_HELPER_HOME"]) / "child.pid"
    spawn = (
        'if [ "$1" = remote-control ]; then\n'
        "  (trap '' HUP; exec sleep 600) </dev/null >/dev/null 2>&1 &\n"
        f"  echo $! > {pid_file}\nfi\n"
        if child
        else ""
    )
    Path(env["RC_HELPER_CLAUDE"]).write_text(
        f"#!/bin/sh\n{spawn}trap '' {' '.join(signals)}\nexec {rec.FAKE_CLAUDE} \"$@\"\n",
        encoding="utf-8",
    )
    return pid_file


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


@pytest.mark.parametrize("ignored,ended_by", [(("INT",), "SIGTERM"), (("INT", "TERM"), "SIGKILL")])
def test_stop_ends_a_server_that_ignores_ctrl_c(env, ignored, ended_by):
    import signal
    import time

    log_in(env)
    child = ignoring(env, *ignored, child=True)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    pid = int(child.read_text())
    assert alive(pid)  # known positive for the check below
    try:
        began = time.monotonic()
        code, out = helper_with(env, QUICK_STOP, "stop", "--", "projects/alpha", timeout=30)
        took = time.monotonic() - began
        assert code == 0, out
        assert out["server"]["state"] == "exited"
        assert out["server"]["exit_code"] == 128 + getattr(signal, ended_by)
        assert took < QUICK_STOP["STOP_TIMEOUT"] + 2 * QUICK_STOP["END_GRACE_SECONDS"] + 5
        for _ in range(50):  # what the server started in its process group ends with it
            if not alive(pid):
                break
            time.sleep(0.1)
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    ok(env, "forget", "projects/alpha")
    assert ok(env, "list")["servers"] == []


def test_a_server_that_stops_on_ctrl_c_is_not_ended(env, monkeypatch):
    log_in(env)
    trusted(env, "projects/alpha")
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    wait_state(env, "projects/alpha", "ready")
    sent = []
    monkeypatch.setattr(h.Tmux, "signal", lambda self, session, pid, sig: sent.append(sig))
    stopped = in_process(env).stop("projects/alpha")["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("stopped", 0)
    assert sent == []


def test_stop_answers_timeout_when_it_cannot_end_a_server(env, monkeypatch):
    log_in(env)
    ignoring(env, "INT")
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    monkeypatch.setattr(h, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(h, "END_GRACE_SECONDS", 0.5)
    sent = []
    monkeypatch.setattr(h.Tmux, "signal", lambda self, session, pid, sig: sent.append(sig))
    error = refusal(in_process(env).stop, "projects/alpha")
    assert error.code == "timeout"
    assert error.detail["server"]["state"] == "trust_prompt"
    assert sent == [h.signal.SIGTERM, h.signal.SIGKILL]
    monkeypatch.undo()
    assert ok(env, "list")["servers"][0]["state"] == "trust_prompt"


def test_a_server_started_again_while_one_is_ended_is_left_alone(env, monkeypatch):
    """Stop and resume share the lock: a resume can respawn the pane between the
    SIGTERM and the SIGKILL of a stop, and the new server is not stop's to end."""
    import signal
    import sys
    import time

    log_in(env)
    ignoring(env, "INT", "TERM")
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    monkeypatch.setattr(h, "STOP_TIMEOUT", 0.5)
    monkeypatch.setattr(h, "END_GRACE_SECONDS", 0.5)
    helper = in_process(env)
    session = h.session_for("projects/alpha")
    folder = (Path(env["RC_HELPER_HOME"]) / "projects" / "alpha").resolve()
    real = h.Tmux.signal
    respawned = []

    def signal_then_respawn(self, session_, pid, sig):
        real(self, session_, pid, sig)
        if sig == signal.SIGTERM and not respawned:  # ignored; the server ends otherwise
            os.kill(pid, signal.SIGKILL)
            while helper.tmux.exit_status(session) is None:
                time.sleep(0.05)
            helper.tmux.respawn(session, folder, [sys.executable, "-c", "input()"])
            respawned.append(helper.tmux.pane(session)[1])

    monkeypatch.setattr(h.Tmux, "signal", signal_then_respawn)
    helper.stop("projects/alpha")
    assert respawned and respawned[0] != 0
    time.sleep(0.5)
    assert alive(respawned[0])
    assert helper.tmux.pane(session) == (False, respawned[0])


def test_a_signal_to_a_process_now_someone_elses_is_as_if_it_were_gone(env, monkeypatch):
    import signal

    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    tmux = in_process(env).tmux
    session = h.session_for("projects/alpha")
    dead, pid = tmux.pane(session)
    assert not dead and alive(pid)  # known positive

    def refused(*_args):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", refused)
    monkeypatch.setattr(os, "killpg", refused)
    tmux.signal(session, pid, signal.SIGTERM)
    tmux.signal(session, pid, signal.SIGKILL)


def test_a_signal_goes_only_to_the_process_the_pane_still_runs(env, monkeypatch):
    import signal

    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    tmux = in_process(env).tmux
    session = h.session_for("projects/alpha")
    _dead, pid = tmux.pane(session)
    sent = []
    monkeypatch.setattr(os, "kill", lambda p, sig: sent.append((p, sig)))
    tmux.signal(session, pid + 1, signal.SIGTERM)  # not the pane's process
    assert sent == []
    tmux.signal(session, pid, signal.SIGTERM)  # known positive
    assert sent == [(pid, signal.SIGTERM)]


def test_resume_needs_a_stopped_server(env, tmp_path):
    log_in(env)
    assert (
        error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "not_stopped"
    )  # missing

    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert (
        ok(env, "confirm", "projects/alpha", "--answer", "n")["server"]["state"] == "trust_declined"
    )
    assert error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "not_stopped"

    # A server whose claude finds itself signed out, though auth status said otherwise.
    signed_out = tmp_path / "signed-out"
    fake.write_state(signed_out, logged_in=False)
    wrapper = Path(env["RC_HELPER_CLAUDE"])
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = remote-control ]; then '
        f"export CLAUDE_CONFIG_DIR={signed_out}; fi\n"
        f'exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "logged_out")
    assert error_code(env, "resume", "projects/beta", "--prefix", "pfx") == "not_stopped"


def test_a_server_that_died_can_be_resumed(env):
    import signal

    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    ok(env, "confirm", "projects/alpha", "--answer", "y")
    ready = wait_state(env, "projects/alpha", "ready")
    tmux = ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"]]
    session = h.session_for("projects/alpha")
    pid = subprocess.run(
        [*tmux, "display-message", "-p", "-t", f"={session}:", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    os.kill(int(pid), signal.SIGKILL)
    died = wait_state(env, "projects/alpha", "exited")
    assert died["exit_code"] is not None

    resumed = ok(env, "resume", "projects/alpha", "--prefix", "pfx")
    assert resumed["server"]["state"] in ("connected", "ready")  # no trust question
    again = wait_state(env, "projects/alpha", "ready")
    assert again["session_name"] == ready["session_name"]


def test_a_server_whose_folder_is_gone_can_still_be_reached(env):
    import shutil

    log_in(env)
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    shutil.rmtree(Path(env["RC_HELPER_HOME"]) / "projects" / "beta")
    assert error_code(env, "start", "projects/beta/inner", "--prefix", "pfx") == "path_not_found"

    assert ok(env, "output", "projects/beta/inner")["server"]["state"] == "trust_prompt"
    assert ok(env, "stop", "projects/beta/inner")["server"]["exit_code"] is not None
    assert ok(env, "forget", "projects/beta/inner") == {"forgotten": "projects/beta/inner"}
    assert ok(env, "list")["servers"] == []


def test_a_folder_that_is_gone_is_still_confined_to_the_home(env, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (Path(env["RC_HELPER_HOME"]) / "projects" / "out").symlink_to(tmp_path / "elsewhere")
    for verb in (["stop"], ["forget"], ["output"], ["confirm", "--answer=y"]):
        assert error_code(env, *verb, "projects/gone") == "unknown_server"  # positive control
        assert error_code(env, *verb, "../gone") == "path_outside_home"
        assert error_code(env, *verb, "projects/out/gone") == "path_outside_home"
        assert error_code(env, *verb, "projects/gone/../..") == "path_is_home"
        assert error_code(env, *verb, "/gone") == "path_invalid"


def test_the_trust_question_of_a_server_whose_folder_is_gone_can_still_be_answered(env):
    import shutil

    log_in(env)
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    shutil.rmtree(Path(env["RC_HELPER_HOME"]) / "projects" / "beta")
    out = ok(env, "confirm", "projects/beta/inner", "--answer", "n")
    assert out["server"]["state"] == "trust_declined"


def test_forget_refuses_a_running_server(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert error_code(env, "forget", "projects/alpha") == "still_running"


def test_declining_trust_ends_the_server(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    out = ok(env, "confirm", "projects/alpha", "--answer", "n")
    assert out["server"]["state"] == "trust_declined"


def test_confirm_refuses_when_nothing_is_asked(env):
    log_in(env)
    fake.write_state(
        Path(env["RC_HELPER_HOME"]) / ".claude-fake",
        logged_in=True,
        trusted=[str(Path(env["RC_HELPER_HOME"]).resolve() / "projects" / "alpha")],
    )
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    wait_state(env, "projects/alpha", "ready")
    assert error_code(env, "confirm", "projects/alpha", "--answer", "y") == "nothing_to_confirm"


def test_list_reports_each_folder_separately(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    assert sorted(s["path"] for s in ok(env, "list")["servers"]) == [
        "projects/alpha",
        "projects/beta",
    ]


def test_output_returns_the_raw_screen(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    out = ok(env, "output", "projects/alpha")
    assert any("[y/N]" in line for line in out["screen"])


def test_unrecognised_screen_is_reported_with_its_output(env, tmp_path):
    log_in(env)
    wrapper = Path(env["RC_HELPER_CLAUDE"])
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_SCENARIO=unrecognised exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )
    out = ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert out["server"]["state"] == "unrecognised"
    assert fake.UNRECOGNISED_SCREEN in out["server"]["screen"]


def in_scenario(env, scenario):
    """Every claude rc-helper starts from now on runs the fake's ``scenario``."""
    Path(env["RC_HELPER_CLAUDE"]).write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_SCENARIO={scenario} exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "scenario,state",
    [("reconnected_without_status", "reconnected"), ("unrecognised", "unrecognised")],
)
def test_a_server_claude_shows_no_status_for_is_reported_reconnected_and_stops(
    env, scenario, state
):
    """The twin is the unrecognised scenario, run the same way."""
    log_in(env)
    in_scenario(env, scenario)
    started = ok(env, "start", "projects/alpha", "--prefix", "pfx")["server"]
    assert started["state"] == state
    assert (started["session_name"], started["environment_url"]) == (None, None)
    [listed] = ok(env, "list")["servers"]
    assert listed["state"] == state
    shown = ok(env, "output", "projects/alpha")
    assert shown["server"]["state"] == state
    assert shown["screen"] == listed["screen"] == shown["server"]["screen"]
    assert any("Reconnected" in line for line in shown["screen"]) == (state == "reconnected")
    stopped = ok(env, "stop", "projects/alpha")["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("exited", 1)


@pytest.mark.parametrize("prefix", ["", "-x", "a b", "a;b", "x" * (h.MAX_PREFIX_LENGTH + 1)])
def test_invalid_prefix_is_refused(env, prefix):
    assert error_code(env, "start", "projects/alpha", f"--prefix={prefix}") == "prefix_invalid"


# --- what the server is started with ---------------------------------------------


# --- the size of a server's window ----------------------------------------------------

# The size of the terminal attached to a server's tmux session: smaller than a server's.
TERMINAL = (80, 24)


def rcman_tmux(env, *args, conf=h.TMUX_CONF):
    return subprocess.run(
        ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"], "-f", str(conf), *args],
        capture_output=True,
        text=True,
    )


def window_size(env, session) -> tuple[int, int]:
    out = rcman_tmux(
        env, "display-message", "-p", "-t", f"={session}:", "#{window_width}x#{window_height}"
    )
    width, height = out.stdout.strip().split("x")
    return int(width), int(height)


@contextlib.contextmanager
def attached(env, session, tmp_path):
    """A terminal of TERMINAL's size attached to ``session``, as a person's tmux client."""
    import time

    with tmux_sockets("rct-") as outer, tmuxctl.Pane(outer, *TERMINAL) as terminal:
        terminal.run(
            ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"], "attach-session", "-t", f"={session}"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "TERM": "xterm-256color"},
        )
        # tmux sizes the window as it learns the client's size, so once it reports
        # the terminal's size any resize the terminal causes has been made.
        size = f"{TERMINAL[0]}x{TERMINAL[1]}"
        client = "#{client_width}x#{client_height}"
        for _ in range(100):
            clients = rcman_tmux(env, "list-clients", "-t", f"={session}", "-F", client)
            if clients.stdout.split() == [size]:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"no terminal of {size} attached; it shows:\n{terminal.text()}")
        yield


def resized(env, session) -> tuple[int, int]:
    """The window's size once a terminal's width has reached it, or failing that, after
    a timeout."""
    import time

    for _ in range(50):
        if (size := window_size(env, session))[0] == TERMINAL[0]:
            return size
        time.sleep(0.1)
    return window_size(env, session)


def failing_on(word: str):
    """A tmux runner that fails every call whose arguments include ``word``."""

    def runner(argv, **kwargs):
        if word in argv:
            return subprocess.CompletedProcess(argv, 1, "", f"planted failure on {word}")
        return subprocess.run(argv, **kwargs)

    return runner


def made_with_failing(env, word: str) -> list[tuple[str, str]]:
    """Make a server's session with a runner failing on ``word``; the sessions listed after."""
    cfg = h.config_from_env(env)
    session = h.session_for("projects/alpha")
    with pytest.raises(RuntimeError, match="planted failure"):
        h.Tmux(cfg, runner=failing_on(word)).new(
            session, cfg.home, ["sleep", "600"], {h.PATH_OPTION: "projects/alpha"}
        )
    assert h.Tmux(cfg).exists(session)  # the session was made all the same
    return h.Tmux(cfg).sessions()


def test_a_server_whose_window_cannot_be_pinned_is_still_listed(env):
    """Its folder is recorded before its window is pinned, so a failed pin never leaves
    a server running that list, stop and start cannot find."""
    assert made_with_failing(env, "window-size") == [
        (h.session_for("projects/alpha"), "projects/alpha")
    ]


def test_a_server_whose_folder_is_not_recorded_is_not_listed(env):
    """Twin: a session made without its folder recorded is lost to the list."""
    assert made_with_failing(env, h.PATH_OPTION) == []


def test_an_attached_terminal_does_not_resize_a_server(env, tmp_path):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    session = h.session_for("projects/alpha")
    with attached(env, session, tmp_path):
        assert window_size(env, session) == (h.WIDTH, h.HEIGHT)
    assert (
        rcman_tmux(env, "show-options", "-wv", "-t", f"={session}:", "window-size").stdout.strip()
        == "manual"
    )


def test_without_the_pin_an_attached_terminal_resizes_the_window(env, tmp_path):
    """Twin: a window made as rc-helper makes one, but not pinned, takes the terminal's size."""
    made = rcman_tmux(
        env,
        "new-session",
        "-d",
        "-s",
        "unpinned",
        "-x",
        str(h.WIDTH),
        "-y",
        str(h.HEIGHT),
        "sleep",
        "600",
    )
    assert made.returncode == 0, made.stderr
    assert window_size(env, "unpinned") == (h.WIDTH, h.HEIGHT)  # as made
    with attached(env, "unpinned", tmp_path):
        width, height = resized(env, "unpinned")
    assert width == TERMINAL[0] and height <= TERMINAL[1]


def test_a_server_started_again_in_an_old_session_is_pinned(env, tmp_path):
    """A session made before servers' windows were pinned, which a terminal has resized,
    is pinned when its server is started again, as resume starts one."""
    log_in(env)
    session = h.session_for("projects/alpha")
    made = rcman_tmux(
        env,
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        str(h.WIDTH),
        "-y",
        str(h.HEIGHT),
        "sh",
        "-c",
        "exit 3",
    )
    assert made.returncode == 0, made.stderr
    rcman_tmux(env, "set-option", "-t", f"={session}:", h.PATH_OPTION, "projects/alpha")
    assert wait_state(env, "projects/alpha", "exited")["exit_code"] == 3  # the old server ended
    with attached(env, session, tmp_path):
        assert resized(env, session)[0] == TERMINAL[0]  # positive control: it was resized
        assert (
            ok(env, "start", "projects/alpha", "--prefix", "pfx")["server"]["state"]
            == "trust_prompt"
        )
        assert window_size(env, session) == (h.WIDTH, h.HEIGHT)


def test_tmux_conf_sets_no_global_window_size(env, tmp_path):
    """A global ``window-size manual`` kills tmux 3.4's server at the next detached
    new-session, so servers' windows are pinned one by one instead."""
    made = rcman_tmux(
        env, "new-session", "-d", "-s", "probe", "-x", "10", "-y", "5", "sleep", "600"
    )
    assert made.returncode == 0, made.stderr
    assert rcman_tmux(env, "show-options", "-gv", "remain-on-exit").stdout.strip() == "on"  # read
    # tmux's own default, from a server that reads no configuration
    bare = Path(env["RC_HELPER_TMUX_SOCKET"]).parent / "bare.sock"
    default = subprocess.run(
        ["tmux", "-S", str(bare), "-f", "/dev/null", "start-server", ";"]
        + ["show-options", "-gwv", "window-size"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert default and default != "manual"
    assert rcman_tmux(env, "show-options", "-gwv", "window-size").stdout.strip() == default
    # Twin: the same configuration with a global window-size manual.
    planted = tmp_path / "tmux.conf"
    planted.write_text(h.TMUX_CONF.read_text(encoding="utf-8") + "set -g window-size manual\n")
    rcman_tmux(env, "kill-server")
    died = rcman_tmux(
        env, "new-session", "-d", "-s", "probe", "-x", "10", "-y", "5", "sleep", "600", conf=planted
    )
    assert died.returncode != 0
    assert "server exited unexpectedly" in died.stderr
    assert rcman_tmux(env, "has-session").returncode != 0


def test_server_command_is_fixed_and_shell_free(tmp_path):
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None, github=UNREACHABLE)
    argv = h.server_command(cfg, prefix="pfx")
    assert argv[0] == "/usr/bin/env" and argv[1] == "-i"
    command = argv[argv.index(str(tmp_path / "claude")) :]
    assert command == [
        str(tmp_path / "claude"),
        "remote-control",
        "--remote-control-session-name-prefix",
        "pfx",
        "--permission-mode",
        "default",
        "--spawn",
        "same-dir",
    ]


def test_the_server_is_started_as_the_recorder_records_one(tmp_path):
    """So the contract tests check every option rc-helper starts a server with."""
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None, github=UNREACHABLE)
    argv = h.server_command(cfg, prefix=rec.PROBE_PREFIX)
    assert argv[argv.index(str(tmp_path / "claude")) + 1 :] == rec.RC_ARGS


def test_server_environment_is_the_allowlist(tmp_path):
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None, github=UNREACHABLE)
    argv = h.server_command(cfg, prefix="pfx")
    assigned = [a.split("=", 1)[0] for a in argv[2:] if "=" in a and not a.startswith("/")]
    assert sorted(assigned) == sorted(h.child_env(cfg))
    assert h.child_env(cfg)["HOME"] == str(tmp_path)
    assert h.child_env(cfg)["DISABLE_AUTOUPDATER"] == "1"


def test_production_config_ignores_nothing_but_the_test_hooks():
    cfg = h.config_from_env({})
    import pwd

    assert cfg.home == Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert cfg.claude == cfg.home / ".local" / "bin" / "claude"
    assert cfg.tmux_socket is None  # the named socket h.TMUX_SOCKET_NAME
    assert cfg.github == h.GITHUB_ADDRESS == "https://github.com/"
    assert h.config_from_env({"RC_HELPER_GITHUB": UNREACHABLE}).github == UNREACHABLE
    assert h.config_from_env({"HOME": "/elsewhere"}).home == cfg.home  # $HOME is not trusted


def test_refuses_to_run_as_root(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    assert h.main(["list"], {}) == 1


# --- sign-in -------------------------------------------------------------------------


def test_auth_status_logged_out_and_in(env):
    assert ok(env, "auth-status") == {"logged_in": False}
    log_in(env)
    out = ok(env, "auth-status")
    assert out["logged_in"] is True
    assert out["email"] == fake.FAKE_ACCOUNT["email"]


def test_login_returns_the_sign_in_url(env):
    out = ok(env, "auth-login", "--email", "someone@example.com")
    assert out["url"].startswith(rec.LOGIN_URL_PREFIX)
    assert "someone%40example.com" in out["url"]


def test_login_code_is_read_from_stdin_and_never_echoed(env):
    ok(env, "auth-login", "--email", "someone@example.com")
    code, out = helper(env, "auth-code", stdin="SECRET-CODE-123\n")
    assert code == 1 and out["error"]["code"] == "login_failed"  # the fake never accepts
    assert "SECRET-CODE-123" not in json.dumps(out)


def test_login_code_never_reaches_any_argv(tmp_path):
    seen = []

    def runner(args, **kw):
        seen.append((list(args), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    cfg = h.Config(
        home=tmp_path, claude=tmp_path / "claude", tmux_socket=tmp_path / "s", github=UNREACHABLE
    )
    h.Tmux(cfg, runner=runner).paste_line("rc-login", "SECRET-CODE-123")
    assert seen, "nothing was run"
    assert not any("SECRET-CODE-123" in " ".join(args) for args, _ in seen)
    assert any(inp and "SECRET-CODE-123" in inp for _, inp in seen)


def test_auth_code_without_a_login_in_progress(env):
    assert error_code(env, "auth-code", stdin="x\n") == "no_login_in_progress"


@pytest.mark.parametrize("email", ["", "no-at", "a@b@c", "a b@c.d", "x@y;rm"])
def test_invalid_email_is_refused(env, email):
    assert error_code(env, "auth-login", "--email", email) == "email_invalid"


def rule_copy_problems(source: str, names=("EMAIL",)) -> list[str]:
    """What keeps a module's rules from being users_file's: not imported from it, or assigned."""
    import ast

    tree = ast.parse(source)
    problems = []
    for name in names:
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "helpers.users_file"
            and any(alias.name == name and alias.asname is None for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            problems.append(f"{name} is not imported from helpers.users_file")
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
            else []
        )
        assigned = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        for name in names:
            if name in assigned:
                problems.append(f"{name} is assigned on line {node.lineno}")
    return problems


def test_the_email_rule_is_the_users_files_rule():
    source = (REPO / "helpers" / "rc_helper.py").read_text(encoding="utf-8")
    assert "EMAIL" in source  # positive control: the module uses the rule
    assert rule_copy_problems(source) == []


def test_the_email_rule_check_catches_a_copy():
    """Planted twin: a module keeping its own copy of the rule is caught, imported or not."""
    right = "import re\nfrom helpers.users_file import EMAIL\n"
    assert rule_copy_problems(right) == []
    copy = 'EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+")\n'
    assert rule_copy_problems("import re\n" + copy) != []
    assert rule_copy_problems(right + copy) != []  # imported, then replaced
    renamed = right.replace("import EMAIL", "import EMAIL as ADDRESS")
    assert renamed != right and rule_copy_problems(renamed) != []


# --- the commit author's name -----------------------------------------------------

AUTHOR_NAMES_ACCEPTED = [
    "Bob B",
    "Сергей Лихарев",
    "O'Brien",
    "Bob Jr.",
    "(Bob)",
    "x",
    "x" * h.MAX_AUTHOR_NAME_LENGTH,
    f"a{chr(0x200C)}b",  # ZERO WIDTH NON-JOINER, see ALLOWED_FORMAT
    f"a{chr(0x200D)}b",  # ZERO WIDTH JOINER
]
AUTHOR_NAMES_REFUSED = [
    "",
    " ",
    " Bob",
    "Bob ",
    "a\nb",
    "a\rb",
    "a\tb",
    "a\x00b",
    "a\x85b",
    f"a{chr(0x2028)}b",  # LINE SEPARATOR
    f"a{chr(0x2029)}b",  # PARAGRAPH SEPARATOR
    "a<b",
    "a>b",
    f"a{chr(0x200B)}b",  # ZERO WIDTH SPACE
    f"a{chr(0x202E)}b",  # RIGHT-TO-LEFT OVERRIDE
    f"a{chr(0x2066)}b",  # LEFT-TO-RIGHT ISOLATE
    f"a{chr(0xFEFF)}b",  # ZERO WIDTH NO-BREAK SPACE
    f"a{chr(0x00AD)}b",  # SOFT HYPHEN
    f"a{chr(0x1D173)}b",  # MUSICAL SYMBOL BEGIN BEAM, beyond the BMP
    f"a{chr(0xE0020)}b",  # TAG SPACE
    f"a{chr(0x3164)}b",  # HANGUL FILLER
    chr(0x3164),
    chr(0x200C),  # ZWNJ and ZWJ: not alone, not at either end
    chr(0x200C) + chr(0x200D),
    chr(0x200C) + "Bob",
    "Bob" + chr(0x200D),
    "'Bob'",
    '"Bob"',
    "Bob;",
    "Bob\\",
    ",Bob",
    ":Bob",
    "x" * (h.MAX_AUTHOR_NAME_LENGTH + 1),
]
AUTHOR_NAMES = [(n, True) for n in AUTHOR_NAMES_ACCEPTED] + [
    (n, False) for n in AUTHOR_NAMES_REFUSED
]

# Format characters (Unicode category Cf) a name may hold, and why: Persian and
# Indic names are written with them.
ALLOWED_FORMAT = {0x200C: "ZERO WIDTH NON-JOINER", 0x200D: "ZERO WIDTH JOINER"}


def hangul_fillers() -> list[str]:
    """The Hangul fillers: letters by category, but they show as nothing."""
    import sys
    import unicodedata

    return [
        chr(c)
        for c in range(sys.maxunicode + 1)
        if {"HANGUL", "FILLER"} <= set(unicodedata.name(chr(c), "").split())
    ]


def format_characters() -> list[str]:
    """Every invisible format character this Python's Unicode database knows."""
    import sys
    import unicodedata

    return [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"]


@pytest.mark.parametrize(("name", "accepted"), AUTHOR_NAMES)
def test_author_name_rule(name, accepted):
    assert bool(h.AUTHOR_NAME.fullmatch(name)) is accepted


# The rules that refuse the same invisible characters: a commit author's name,
# and a folder's.
NAME_RULES = {"author name": "AUTHOR_NAME", "folder name": "FOLDER_NAME"}


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_every_format_character_is_refused_but_zwnj_and_zwj_inside_a_name(rule):
    import unicodedata

    rule = getattr(h, rule)
    found = format_characters()
    assert chr(0x200B) in found and chr(0xE0020) in found  # positive control
    for code, name in ALLOWED_FORMAT.items():  # each exemption is still what it is said to be
        assert chr(code) in found and unicodedata.name(chr(code)) == name
        assert rule.fullmatch(f"a{chr(code)}b")
    let_through = [
        (f"U+{ord(c):04X}", name)
        for c in found
        for name in (f"a{c}b", c, f"{c}b", f"a{c}")
        if rule.fullmatch(name) and (ord(c) not in ALLOWED_FORMAT or name != f"a{c}b")
    ]
    assert let_through == []  # the allowed ones only inside a name, never alone or at an end


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_hangul_fillers_are_refused_in_a_name(rule):
    rule = getattr(h, rule)
    found = hangul_fillers()
    assert chr(0x3164) in found  # positive control: HANGUL FILLER
    assert {code for first, last in h.HANGUL_FILLERS for code in range(first, last + 1)} == {
        ord(c) for c in found
    }
    for c in found:
        assert not rule.fullmatch(f"a{c}b"), f"U+{ord(c):04X}"
        assert not rule.fullmatch(c), f"U+{ord(c):04X}"


def recorded_by_git(tmp_path, name: str) -> str:
    """The author name real git records for ``name``."""
    proc = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": "bob@example.com",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rpartition(" <bob@example.com>")[0]


def test_git_records_every_accepted_name_as_given(tmp_path):
    assert recorded_by_git(tmp_path, ";Bob;") == "Bob"  # positive control: git does change some
    for name in AUTHOR_NAMES_ACCEPTED:
        assert h.AUTHOR_NAME.fullmatch(name), name
        assert recorded_by_git(tmp_path, name) == name


def test_names_git_would_trim_are_refused(tmp_path):
    """The printable characters git strips from the ends of a name are refused there."""
    printable = [chr(c) for c in range(0x21, 0x7F)]
    trimmed = [c for c in printable if recorded_by_git(tmp_path, f"{c}Bob{c}") == "Bob"]
    assert ";" in trimmed and "." not in trimmed  # positive control, and git keeps a dot
    for c in printable:
        refused = not h.AUTHOR_NAME.fullmatch(f"{c}Bob") and not h.AUTHOR_NAME.fullmatch(f"Bob{c}")
        assert refused is (c in trimmed), repr(c)


# --- folders: mkdir and delete ------------------------------------------------------


def in_process(env, runner=subprocess.run):
    """The helper in this process, for what the command line cannot plant."""
    cfg = h.config_from_env(env)
    return h.Helper(cfg, h.Tmux(cfg, runner=runner))


def refusal(call, *args) -> h.HelperError:
    with pytest.raises(h.HelperError) as err:
        call(*args)
    return err.value


def tmux_sessions(env) -> set[str]:
    out = subprocess.run(
        ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"], "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def trusted(env, *rels):
    home = Path(env["RC_HELPER_HOME"])
    fake.write_state(
        home / ".claude-fake", logged_in=True, trusted=[str(home.resolve() / r) for r in rels]
    )


def name_max(path) -> int:
    return os.pathconf(path, "PC_NAME_MAX")


@pytest.mark.parametrize(
    "rel,code",
    [
        ("", "path_is_home"),
        (".", "path_is_home"),
        ("a/..", "path_is_home"),
        ("a/b/../..", "path_is_home"),
        ("/etc", "path_invalid"),
        ("a\0b", "path_invalid"),
        ("a/b/..", "path_ends_in_dot"),
        ("a/b/.", "path_ends_in_dot"),
        ("x" * (h.MAX_PATH_LENGTH + 1), "path_invalid"),
        ("../x", "path_outside_home"),
        ("escape/x", "path_outside_home"),
        ("missing", "path_not_found"),
        ("a/missing", "path_not_found"),
        ("missing/b", "path_not_found"),
        ("file", "path_not_directory"),
    ],
)
def test_entry_refusals(home, rel, code):
    assert refusal(h.resolve_entry, home, rel).code == code


def test_an_entry_is_its_resolved_parent_and_its_own_name(home):
    entry = h.resolve_entry(home, "inner-link/b")
    try:
        assert (entry.parent, entry.name, entry.rel, entry.is_link) == (
            (home / "a").resolve(),
            "b",
            "a/b",
            False,
        )
        assert os.path.samestat(os.fstat(entry.parent_fd), os.stat(home / "a"))
    finally:
        os.close(entry.parent_fd)
    entry = h.resolve_entry(home, "inner-link")  # the link itself, never what it leads to
    try:
        assert (entry.parent, entry.name, entry.rel, entry.is_link) == (
            home.resolve(),
            "inner-link",
            "inner-link",
            True,
        )
    finally:
        os.close(entry.parent_fd)
    entry = h.resolve_entry(home, "escape")  # a link leading outside is still only a link
    os.close(entry.parent_fd)
    assert (entry.rel, entry.is_link) == ("escape", True)


def test_a_folder_is_opened_without_following_any_link(home, tmp_path):
    folder = h.resolve_folder(home, "a/b")
    fd = h.open_dir(home.resolve(), folder)
    try:
        assert os.path.samestat(os.fstat(fd), os.stat(folder))  # positive control
    finally:
        os.close(fd)
    (home / "a").rename(tmp_path / "outside" / "a")
    (home / "a").symlink_to(tmp_path / "outside" / "a")  # a/b now leads outside
    assert refusal(h.open_dir, home.resolve(), folder).code == "path_outside_home"


def test_mkdir_makes_a_folder_in_a_folder_and_in_the_home_folder(env):
    home = Path(env["RC_HELPER_HOME"])
    assert ok(env, "mkdir", "--name=new", "--", "projects") == {"created": "projects/new"}
    assert (home / "projects" / "new").is_dir()
    assert ok(env, "mkdir", "--name=top") == {"created": "top"}
    assert (home / "top").is_dir()
    entries = [e["path"] for e in ok(env, "tree", "projects")["entries"]]
    assert "projects/new" in entries


FOLDER_NAMES_ACCEPTED = [
    "new",
    "My Project",
    "проект",
    "a.b",
    "a.",
    "-x",
    "--name",
    "x",
    f"a{chr(0x200C)}b",  # ZERO WIDTH NON-JOINER, see ALLOWED_FORMAT
]
FOLDER_NAMES_REFUSED = [
    "",
    ".",
    "..",
    ".hidden",
    "a/b",
    "/",
    " a",
    "a ",
    " ",
    "a\tb",
    "a\nb",
    "a\x00b",
    "a\x7fb",
    "a\x85b",
    f"a{chr(0x2028)}b",  # LINE SEPARATOR
    f"a{chr(0x200B)}b",  # ZERO WIDTH SPACE
    f"a{chr(0x202E)}b",  # RIGHT-TO-LEFT OVERRIDE
    f"a{chr(0x3164)}b",  # HANGUL FILLER
    f"{chr(0x00A0)}a",  # NO-BREAK SPACE, white space at the start
    f"a{chr(0x3000)}",  # IDEOGRAPHIC SPACE, at the end
    chr(0x200C),
    chr(0x200D) + "a",
]
FOLDER_NAMES = [(n, True) for n in FOLDER_NAMES_ACCEPTED] + [
    (n, False) for n in FOLDER_NAMES_REFUSED
]


@pytest.mark.parametrize(("name", "accepted"), FOLDER_NAMES)
def test_folder_name_rule(name, accepted):
    assert bool(h.FOLDER_NAME.fullmatch(name)) is accepted


@pytest.mark.parametrize(("name", "accepted"), FOLDER_NAMES)
def test_mkdir_makes_exactly_the_folders_the_rule_accepts(env, name, accepted):
    home = Path(env["RC_HELPER_HOME"])
    before = set(os.listdir(home / "projects"))
    if accepted:
        assert ok(env, "mkdir", f"--name={name}", "--", "projects") == {
            "created": f"projects/{name}"
        }
        assert (home / "projects" / name).is_dir()
    elif "\0" in name:  # no command line can hold it
        assert refusal(in_process(env).mkdir, "projects", name).code == "name_invalid"
    else:
        assert error_code(env, "mkdir", f"--name={name}", "--", "projects") == "name_invalid"
    assert accepted or set(os.listdir(home / "projects")) == before


def test_a_name_longer_than_the_file_system_takes_is_refused(env):
    home = Path(env["RC_HELPER_HOME"])
    limit = name_max(home)
    fits = "п" * (limit // 2)  # two bytes each
    assert ok(env, "mkdir", f"--name={fits}", "--", "projects")["created"] == f"projects/{fits}"
    over = "п" * (limit // 2 + 1)
    assert len(over) <= limit  # in characters it would fit; in bytes it does not
    assert error_code(env, "mkdir", f"--name={over}", "--", "projects") == "name_invalid"


def test_a_folder_whose_path_would_be_too_long_is_refused(env):
    home = Path(env["RC_HELPER_HOME"])
    parts, length = [], 0
    while length < h.MAX_PATH_LENGTH - 100:
        parts.append("d" * 99)
        length += 100
    deep = "/".join(parts)
    (home / deep).mkdir(parents=True)
    fits = "n" * (h.MAX_PATH_LENGTH - len(deep) - 1)
    assert ok(env, "mkdir", f"--name={fits}", "--", deep)["created"] == f"{deep}/{fits}"
    over = fits + "n"
    assert error_code(env, "mkdir", f"--name={over}", "--", deep) == "folder_path_too_long"
    assert not (home / deep / over).exists()


def test_mkdir_refuses_a_name_that_is_there_already(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "file").write_text("x")
    (home / "projects" / "dangling").symlink_to(home / "nowhere")
    for name in ("alpha", "file", "dangling"):
        assert error_code(env, "mkdir", f"--name={name}", "--", "projects") == "folder_exists"
    assert (home / "projects" / "file").read_text() == "x"
    assert not (home / "nowhere").exists()


def test_mkdir_refuses_a_parent_outside_the_home_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (tmp_path / "outside").mkdir()
    (home / "escape").symlink_to(tmp_path / "outside")
    assert error_code(env, "mkdir", "--name=x", "--", "..") == "path_outside_home"
    assert error_code(env, "mkdir", "--name=x", "--", "escape") == "path_outside_home"
    assert error_code(env, "mkdir", "--name=x", "--", "/tmp") == "path_invalid"
    assert error_code(env, "mkdir", "--name=x", "--", "missing") == "path_not_found"
    assert list((tmp_path / "outside").iterdir()) == []
    assert not (tmp_path / "x").exists()


def test_delete_removes_a_folder_and_everything_in_it(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "beta" / "inner" / "deeper").mkdir()
    readonly = home / "projects" / "beta" / "inner" / "readonly"
    readonly.write_text("x")
    readonly.chmod(0o400)
    out = ok(env, "delete", "--", "projects/beta")
    assert out == {"deleted": "projects/beta", "link": False, "stopped": [], "forgotten": []}
    assert not (home / "projects" / "beta").exists()
    assert (home / "projects" / "alpha").is_dir()


@pytest.mark.parametrize("rel", ["", ".", "projects/..", "projects/alpha/../.."])
def test_delete_refuses_the_home_folder(env, rel):
    assert error_code(env, "delete", "--", rel) == "path_is_home"
    assert (Path(env["RC_HELPER_HOME"]) / "projects" / "alpha").is_dir()


def test_delete_refuses_anything_outside_the_home_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (tmp_path / "x").mkdir()
    (tmp_path / "outside" / "x").mkdir(parents=True)
    (home / "escape").symlink_to(tmp_path / "outside")
    assert error_code(env, "delete", "--", "../x") == "path_outside_home"
    assert error_code(env, "delete", "--", "escape/x") == "path_outside_home"
    assert error_code(env, "delete", "--", "projects/../../x") == "path_outside_home"
    assert error_code(env, "delete", "--", str(tmp_path / "x")) == "path_invalid"
    assert (tmp_path / "x").is_dir() and (tmp_path / "outside" / "x").is_dir()


def test_delete_refuses_a_dot_folder_and_anything_in_one(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / ".hidden" / "inside").mkdir()
    (home / "projects" / ".git").mkdir()
    (home / "dots").symlink_to(home / ".hidden")
    for rel in (".hidden", ".hidden/inside", "projects/.git", "dots/inside", ".hidden/../projects"):
        assert error_code(env, "delete", "--", rel) == "path_hidden", rel
    assert (home / ".hidden" / "inside").is_dir() and (home / "projects" / ".git").is_dir()
    assert ok(env, "delete", "--", "projects/alpha")["deleted"] == "projects/alpha"  # positive


def test_delete_removes_a_link_and_leaves_what_it_leads_to(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "alpha" / "kept").write_text("x")
    (home / "link").symlink_to(home / "projects" / "alpha")
    out = ok(env, "delete", "--", "link")
    assert (out["deleted"], out["link"]) == ("link", True)
    assert not (home / "link").is_symlink()
    assert (home / "projects" / "alpha" / "kept").read_text() == "x"
    # positive control: the real folder at the same place is deleted
    (home / "link").mkdir()
    assert ok(env, "delete", "--", "link")["link"] is False
    assert not (home / "link").exists()


def test_delete_removes_a_link_to_the_home_folder_and_nothing_else(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (home / "self").symlink_to(home)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "file").write_text("x")
    (home / "escape").symlink_to(tmp_path / "outside")
    assert ok(env, "delete", "--", "self")["link"] is True
    assert ok(env, "delete", "--", "escape")["link"] is True
    assert not (home / "self").is_symlink() and not (home / "escape").is_symlink()
    assert (home / "projects" / "alpha").is_dir()
    assert (tmp_path / "outside" / "file").read_text() == "x"


def test_delete_never_follows_a_link_inside_the_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    outside = tmp_path / "outside"
    (outside / "folder").mkdir(parents=True)
    (outside / "folder" / "file").write_text("x")
    (outside / "file").write_text("y")
    beta = home / "projects" / "beta"
    (beta / "to-folder").symlink_to(outside / "folder")
    (beta / "inner" / "to-file").symlink_to(outside / "file")
    (beta / "inner" / "real").write_text("z")
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"
    assert not (beta / "inner" / "real").exists()  # non-vacuity: what was really there is gone
    assert (outside / "folder" / "file").read_text() == "x"
    assert (outside / "file").read_text() == "y"


def test_the_system_python_resists_symlink_attacks_on_rmtree():
    proc = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import shutil; print(shutil.rmtree.avoids_symlink_attacks)",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "True", proc.stderr
    assert HELPER.read_text(encoding="utf-8").startswith("#!/usr/bin/python3 ")  # what it runs on


def test_delete_refuses_where_rmtree_does_not_resist_symlink_attacks(env, monkeypatch):
    import shutil

    home = Path(env["RC_HELPER_HOME"])
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    assert refusal(in_process(env).delete, "projects/beta").code == "delete_unsafe"
    assert (home / "projects" / "beta" / "inner").is_dir()
    monkeypatch.undo()
    assert in_process(env).delete("projects/beta")["deleted"] == "projects/beta"  # positive


def plant_a_mount(monkeypatch, folder: Path) -> None:
    """``folder`` looks like a mount point: another device, to anything that asks by its fd."""
    real_fstat, planted = os.fstat, os.stat(folder)

    def fstat(fd):
        result = real_fstat(fd)
        if os.path.samestat(result, planted):
            fields = list(result)
            fields[stat.ST_DEV] = result.st_dev + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "fstat", fstat)


def test_delete_refuses_a_folder_with_a_mount_point_inside(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    trusted(env, "projects/beta")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "ready")
    plant_a_mount(monkeypatch, home / "projects" / "beta" / "inner")
    assert refusal(in_process(env).delete, "projects/beta").code == "delete_mount_inside"
    assert (home / "projects" / "beta" / "inner").is_dir()
    assert wait_state(env, "projects/beta", "ready")  # not stopped
    monkeypatch.undo()
    out = in_process(env).delete("projects/beta")  # positive control
    assert out["stopped"] == ["projects/beta"] and not (home / "projects" / "beta").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_what_cannot_be_removed_is_reported_and_delete_again_carries_on(env):
    home = Path(env["RC_HELPER_HOME"])
    inner = home / "projects" / "beta" / "inner"
    (inner / "stuck").write_text("x")
    (home / "projects" / "beta" / "gone").write_text("y")
    inner.chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/beta")
        assert code == 1 and out["error"]["code"] == "delete_incomplete", out
        assert out["error"]["left"] == [
            "projects/beta",
            "projects/beta/inner",
            "projects/beta/inner/stuck",
        ]
        assert out["error"]["forgotten"] == []
        assert not (home / "projects" / "beta" / "gone").exists()  # the rest went
        assert stat.S_IMODE(inner.stat().st_mode) == 0o500  # no permission was changed
    finally:
        inner.chmod(0o700)
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"
    assert not (home / "projects" / "beta").exists()


def test_delete_refuses_a_path_that_goes_through_a_link(env):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    (home / "lnk").symlink_to(home / "projects")
    (home / "projects" / "to-beta").symlink_to(home / "projects" / "beta")
    for rel in ("lnk/alpha", "projects/to-beta/inner", "lnk/beta/inner"):
        assert error_code(env, "delete", "--", rel) == "path_through_link", rel
    assert (home / "projects" / "alpha").is_dir() and (
        home / "projects" / "beta" / "inner"
    ).is_dir()
    assert [s["state"] for s in ok(env, "list")["servers"]] == ["trust_prompt"]  # not stopped
    # positive control: the same folders, named where they are
    assert ok(env, "delete", "--", "projects/beta/inner")["deleted"] == "projects/beta/inner"
    assert ok(env, "delete", "--", "projects/./alpha")["forgotten"] == ["projects/alpha"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_a_link_that_cannot_be_removed_is_reported_as_nothing_deleted(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "lnk").symlink_to(home / "projects" / "alpha")
    (home / "projects").chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/lnk")
    finally:
        (home / "projects").chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert out["error"]["left"] == ["projects/lnk"]
    assert (home / "projects" / "lnk").is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_an_empty_folder_that_cannot_be_removed_is_reported_as_nothing_deleted(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "empty").mkdir()
    (home / "projects").chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/empty")
    finally:
        (home / "projects").chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert out["error"]["left"] == ["projects/empty"]
    assert (home / "projects" / "empty").is_dir()
    assert h.MESSAGES["delete_failed"] != h.MESSAGES["delete_incomplete"]


def test_a_path_back_into_the_home_through_its_own_name_is_no_link(env):
    home = Path(env["RC_HELPER_HOME"])
    out = ok(env, "delete", "--", f"../{home.name}/projects/beta")
    assert out["deleted"] == "projects/beta"
    assert not (home / "projects" / "beta").exists()


def test_delete_refuses_a_path_not_written_as_the_names_on_disk(env, monkeypatch):
    """As on a file system that ignores case: Beta reaches beta, whose server is projects/beta."""
    home = Path(env["RC_HELPER_HOME"])
    projects = (home / "projects").resolve()
    real_listdir = os.listdir

    def listdir(path="."):
        names = real_listdir(path)
        return (
            [("Beta" if n == "beta" else n) for n in names]
            if not isinstance(path, int) and Path(path) == projects
            else names
        )

    monkeypatch.setattr(os, "listdir", listdir)
    assert refusal(in_process(env).delete, "projects/beta").code == "path_name_differs"
    assert (home / "projects" / "beta" / "inner").is_dir()
    monkeypatch.undo()
    assert in_process(env).delete("projects/beta")["deleted"] == "projects/beta"  # positive


def listed_as(monkeypatch, folder: Path, name: str, shown: str) -> None:
    """``folder`` lists ``name`` as ``shown``, as a file system that ignores case can."""
    real_listdir, folder = os.listdir, folder.resolve()

    def listdir(path="."):
        names = real_listdir(path)
        return (
            [shown if n == name else n for n in names]
            if not isinstance(path, int) and Path(path) == folder
            else names
        )

    monkeypatch.setattr(os, "listdir", listdir)


def test_a_server_starts_and_a_folder_is_made_only_where_the_path_is_as_on_disk(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    listed_as(monkeypatch, home / "projects", "beta", "Beta")
    helper_ = in_process(env)
    for call, args in (
        (helper_.start, ("projects/beta", "pfx")),
        (helper_.resume, ("projects/beta", "pfx")),
        (helper_.mkdir, ("projects/beta", "x")),
    ):
        assert refusal(call, *args).code == "path_name_differs", call.__name__
    assert not (home / "projects" / "beta" / "x").exists()
    assert ok(env, "list")["servers"] == []
    assert helper_.mkdir("projects", "new") == {"created": "projects/new"}  # positive control
    assert helper_.start("projects/alpha", "pfx")["server"]["path"] == "projects/alpha"


def test_a_recorded_server_is_reached_however_its_folder_is_listed_now(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    listed_as(monkeypatch, home / "projects", "beta", "Beta")
    helper_ = in_process(env)
    assert helper_.output("projects/beta")["server"]["state"] == "trust_prompt"
    assert helper_.confirm("projects/beta", "n")["server"]["state"] == "trust_declined"
    assert helper_.forget("projects/beta") == {"forgotten": "projects/beta"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
@pytest.mark.parametrize("mode", [0o500, 0o000])
def test_a_folder_of_which_nothing_can_be_removed_is_reported_as_nothing_deleted(env, mode):
    home = Path(env["RC_HELPER_HOME"])
    locked = home / "projects" / "gamma" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    locked.chmod(mode)
    try:
        code, out = helper(env, "delete", "--", "projects/gamma")
    finally:
        locked.chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert (locked / "file").read_text() == "x"


def gamma_with_a_locked_folder(home: Path) -> Path:
    gamma = home / "projects" / "gamma"
    (gamma / "locked").mkdir(parents=True)
    (gamma / "locked" / "file").write_text("x")
    (gamma / "locked").chmod(0o500)
    return gamma


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_something_removed_while_something_else_appears_is_some_removed(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    gamma = gamma_with_a_locked_folder(home)
    (gamma / "a").write_text("yours")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        real_unlink(path, dir_fd=dir_fd)
        if path == "a":  # as it goes, something else appears beside it
            (gamma / "b").write_text("new")

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        error = refusal(in_process(env).delete, "projects/gamma")
    finally:
        monkeypatch.undo()
        (gamma / "locked").chmod(0o700)
    assert not (gamma / "a").exists() and (gamma / "b").exists()  # the probe took effect
    assert error.code == "delete_incomplete"


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_something_appearing_while_nothing_is_removed_is_nothing_removed(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    gamma = gamma_with_a_locked_folder(home)
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        (gamma / "added").write_text("new")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        error = refusal(in_process(env).delete, "projects/gamma")
    finally:
        monkeypatch.undo()
        (gamma / "locked").chmod(0o700)
    assert (gamma / "added").exists() and (gamma / "locked" / "file").exists()
    assert error.code == "delete_failed"


def test_the_report_of_what_is_left_is_capped(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    for i in range(h.DELETE_REPORT_LIMIT + 5):
        (home / "projects" / "beta" / f"f{i:03}").write_text("x")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if str(path).startswith("f"):
            raise PermissionError(13, "planted", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    error = refusal(in_process(env).delete, "projects/beta")
    assert error.code == "delete_incomplete"
    assert len(error.detail["left"]) == h.DELETE_REPORT_LIMIT
    assert error.detail["left"] == sorted(error.detail["left"])


def test_delete_stops_then_forgets_every_server_in_the_folder_and_below_it(env):
    home = Path(env["RC_HELPER_HOME"])
    for rel in ("projects/beta/done", "projects/beta2"):
        (home / rel).mkdir()
    log_in(env)
    trusted(env, "projects/beta", "projects/beta/done", "projects/beta2", "projects/alpha")
    for rel in ("projects/beta", "projects/beta/done", "projects/beta2", "projects/alpha"):
        ok(env, "start", rel, "--prefix", "pfx")
        wait_state(env, rel, "ready")
    ok(env, "stop", "projects/beta/done")
    # projects/beta/inner is not trusted: it waits at the trust question
    assert ok(env, "start", "projects/beta/inner", "--prefix", "pfx")["server"]["state"] == (
        "trust_prompt"
    )
    out = ok(env, "delete", "--", "projects/beta")
    assert out == {
        "deleted": "projects/beta",
        "link": False,
        "stopped": ["projects/beta", "projects/beta/inner"],
        "forgotten": ["projects/beta", "projects/beta/done", "projects/beta/inner"],
    }
    assert not (home / "projects" / "beta").exists()
    servers = {s["path"]: s["state"] for s in ok(env, "list")["servers"]}
    assert servers == {"projects/alpha": "ready", "projects/beta2": "ready"}
    assert (home / "projects" / "beta2").is_dir()


def test_delete_stops_everything_before_forgetting_anything_and_forgets_before_removing(env):
    home = Path(env["RC_HELPER_HOME"])
    beta = home / "projects" / "beta"
    trusted(env, "projects/beta", "projects/beta/inner")
    for rel in ("projects/beta", "projects/beta/inner"):
        ok(env, "start", rel, "--prefix", "pfx")
        wait_state(env, rel, "ready")
    steps = []

    def runner(args, **kwargs):
        if "send-keys" in args and "C-c" in args:
            steps.append(("interrupt", beta.exists()))
        if "kill-session" in args:
            steps.append(("forget", beta.exists()))
        return subprocess.run(args, **kwargs)

    in_process(env, runner).delete("projects/beta")
    assert steps == [("interrupt", True)] * 2 + [("forget", True)] * 2
    assert not beta.exists()


def helper_with(env, settings: dict, *args, timeout: float):
    """rc-helper's answer, run in its own process with some of its settings changed.

    A call that outlasts ``timeout`` fails the test, so one that would wait
    for ever is caught as a failure rather than a hang.
    """
    import sys

    program = (
        "import os, sys\nfrom helpers import rc_helper as h\n"
        + "".join(f"h.{name} = {value!r}\n" for name, value in settings.items())
        + "sys.exit(h.main(sys.argv[1:], dict(os.environ)))\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(REPO)!r})\n" + program,
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_delete_ends_a_server_that_ignores_ctrl_c(env):
    home = Path(env["RC_HELPER_HOME"])
    trusted(env, "projects/beta")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "ready")
    ignoring(env, "INT")
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    # a stop deadline of one second, so a delete that waits longer has missed it
    code, out = helper_with(env, QUICK_STOP, "delete", "--", "projects/beta", timeout=30)
    assert code == 0, out
    assert out["stopped"] == ["projects/beta", "projects/beta/inner"]
    assert out["forgotten"] == ["projects/beta", "projects/beta/inner"]
    assert not (home / "projects" / "beta").exists()
    assert ok(env, "list")["servers"] == []


def test_when_a_server_cannot_be_ended_nothing_is_forgotten_or_deleted(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    trusted(env, "projects/beta")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "ready")
    ignoring(env, "INT")
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    sessions = tmux_sessions(env)
    monkeypatch.setattr(h, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(h, "END_GRACE_SECONDS", 0.5)
    sent = []
    monkeypatch.setattr(h.Tmux, "signal", lambda self, session, pid, sig: sent.append(sig))
    error = refusal(in_process(env).delete, "projects/beta")
    assert error.code == "delete_stop_failed"
    assert sent == [h.signal.SIGTERM, h.signal.SIGKILL]  # it was ended, as stop ends one
    assert error.detail["server"]["path"] == "projects/beta/inner"
    assert any("[y/N]" in line for line in error.detail["server"]["screen"])
    assert error.detail["stopped"] == ["projects/beta"]
    monkeypatch.undo()
    assert tmux_sessions(env) == sessions  # nothing forgotten
    assert (home / "projects" / "beta" / "inner").is_dir()
    servers = {s["path"]: s["state"] for s in ok(env, "list")["servers"]}
    assert servers == {"projects/beta": "stopped", "projects/beta/inner": "trust_prompt"}


def test_when_a_server_cannot_be_forgotten_nothing_is_deleted(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    monkeypatch.setattr(h.Tmux, "kill", lambda self, session: None)  # kill-session does nothing
    error = refusal(in_process(env).delete, "projects/beta")
    assert error.code == "delete_forget_failed"
    assert error.detail["server"]["path"] == "projects/beta"
    assert error.detail["forgotten"] == []
    assert (home / "projects" / "beta" / "inner").is_dir()


def hold_the_lock(env, exclusive):
    import fcntl

    fd = os.open(Path(env["RC_HELPER_HOME"]).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return fd


def test_while_a_delete_runs_nothing_starts_and_no_folder_is_made(env):
    import time

    log_in(env)
    fd = hold_the_lock(env, exclusive=True)
    try:
        began = time.monotonic()
        assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "folder_busy"
        assert error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "folder_busy"
        assert error_code(env, "mkdir", "--name=new") == "folder_busy"
        assert error_code(env, "stop", "projects/alpha") == "folder_busy"
        assert error_code(env, "forget", "projects/alpha") == "folder_busy"
        assert error_code(env, "confirm", "--answer=y", "projects/alpha") == "folder_busy"
        assert error_code(env, "delete", "--", "projects/beta") == "folder_busy"
        assert time.monotonic() - began < 10  # refused at once, not after waiting
    finally:
        os.close(fd)
    assert ok(env, "start", "projects/alpha", "--prefix", "pfx")  # positive control
    assert ok(env, "mkdir", "--name=new")


def test_while_a_server_starts_or_a_folder_is_made_nothing_is_deleted(env):
    fd = hold_the_lock(env, exclusive=False)
    try:
        assert error_code(env, "delete", "--", "projects/beta") == "folder_busy"
        assert ok(env, "mkdir", "--name=new")  # these share it
    finally:
        os.close(fd)
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"  # positive


def test_delete_holds_the_lock_while_it_stops_servers(env, monkeypatch):
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    during = []
    real = h.Tmux.interrupt

    def interrupt(self, session):
        during.append(error_code(env, "start", "projects/alpha", "--prefix", "pfx"))
        real(self, session)

    monkeypatch.setattr(h.Tmux, "interrupt", interrupt)
    in_process(env).delete("projects/beta")
    assert during == ["folder_busy"]


def test_a_start_holds_the_lock_and_its_server_does_not(env, monkeypatch):
    log_in(env)
    during = []
    real = h.Tmux.new

    def new(self, *args):
        during.append(error_code(env, "delete", "--", "projects/beta"))
        return real(self, *args)

    monkeypatch.setattr(h.Tmux, "new", new)
    in_process(env).start("projects/alpha", "pfx")
    assert during == ["folder_busy"]
    monkeypatch.undo()
    # a server keeps running once started, and holds nothing: a delete goes ahead
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    assert ok(env, "delete", "--", "projects/beta")["forgotten"] == ["projects/beta"]
    assert [s["path"] for s in ok(env, "list")["servers"]] == ["projects/alpha"]


# --- the command-line surface ----------------------------------------------------


def test_every_error_code_has_a_message():
    used = set(
        re.findall(r'HelperError\(\s*"([a-z_]+)"', (REPO / "helpers" / "rc_helper.py").read_text())
    )
    assert used, "no error codes found"  # non-vacuity
    assert used <= set(h.MESSAGES)
    assert set(h.MESSAGES) <= used | {"internal_error", "usage"}


# What rc-helper is given that breaks each rule it reports, as an error code ->
# the command line, and what it reads on stdin, with the refused value in place
# of {}.
RULE_INVOCATIONS = {
    "prefix_invalid": (("start", "projects/alpha", "--prefix={}"), None),
    "name_invalid": (("mkdir", "--name={}", "--", "projects"), None),
    "email_invalid": (("auth-login", "--email={}"), None),
    "author_name_invalid": (
        ("git-commit", "--author-name={}", "--author-email=a@example.com", "--", "projects/alpha"),
        "A message",
    ),
    "message_invalid": (
        ("git-commit", "--author-name=A", "--author-email=a@example.com", "--", "projects/alpha"),
        "{}",
    ),
    "path_invalid": (("output", "--", "{}"), None),
    "branch_name_invalid": (("git-switch", "--branch={}", "--", "projects/alpha"), None),
    "token_invalid": (("github-token",), "{}"),
    "url_invalid": (("git-clone", "--url={}", "--", "projects"), None),
}
# Rules rc-helper holds without reporting them itself: subject -> why.
RULES_NOT_REPORTED = {
    "FOLDER_PATH": (
        "the backend checks it; rc-helper refuses every path that leads home, empty or not, "
        "as path_is_home"
    ),
}


def invoked(code: str, value: str) -> tuple[list[str], str | None]:
    """The command line and stdin that give rc-helper ``value`` for the rule of ``code``."""
    args, stdin = RULE_INVOCATIONS[code]
    return [part.format(value) for part in args], stdin and stdin.format(value)


# Values that break most rules, each one a command line can carry.
REFUSAL_CANDIDATES = ("\x01", " ", "", "/")


def refused_value(rule) -> str:
    """A value the rule refuses, from a few that break most rules."""
    for value in REFUSAL_CANDIDATES:
        if not rule.fullmatch(value):
            return value
    raise AssertionError(f"no candidate value breaks {rule.pattern!r}")


def test_a_rule_reported_by_code_is_reported_in_its_own_words():
    assert h.RULE_CODES  # non-vacuity
    for code, rule in h.RULE_CODES.items():
        assert h.MESSAGES[code] is rule.sentence, code


def test_every_rule_rc_helper_holds_is_reported_or_exempt():
    from tests.test_rules import held_rules

    held = held_rules(h)
    assert {"PREFIX", "FOLDER_NAME", "AUTHOR_NAME"} <= set(held)  # non-vacuity
    reported = list(h.RULE_CODES.values())
    for name in RULES_NOT_REPORTED:  # stale check: each exemption still needs to be one
        assert name in held, f"{name} is exempt but rc-helper no longer holds it"
        assert held[name] not in reported, f"{name} is exempt but has a code"
    for name, rule in held.items():
        assert name in RULES_NOT_REPORTED or rule in reported, f"{name} has no error code"


def test_each_rule_is_refused_through_the_command_line_with_its_sentence(env):
    assert set(RULE_INVOCATIONS) == set(h.RULE_CODES)
    log_in(env)
    for code, rule in h.RULE_CODES.items():
        value = refused_value(rule)
        args, stdin = invoked(code, value)
        status, out = helper(env, *args, stdin=stdin)
        assert status == 1 and out["error"]["code"] == code, (code, out)
        assert out["error"]["message"] == rule.sentence


def rule_code_problems(source: str, codes: dict) -> list[str]:
    """Where a rule's code is raised in a function that does not check that rule: the
    code would then say the rule's sentence about a value the rule accepts."""
    import ast

    names = {code: next(n for n, v in vars(h).items() if v is rule) for code, rule in codes.items()}
    found = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        text = ast.unparse(function)
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HelperError"):
                continue
            # The code may be chosen in the call: every constant it could be counts.
            given = {
                c.value
                for arg in node.args[:1]
                for c in ast.walk(arg)
                if isinstance(c, ast.Constant)
            }
            for code in sorted(given & set(names)):
                if f"{names[code]}.fullmatch" not in text:
                    found.append(f"{function.name} raises {code} without checking {names[code]}")
    return found


def test_a_rules_code_is_raised_only_where_the_rule_is_checked():
    source = (REPO / "helpers" / "rc_helper.py").read_text(encoding="utf-8")
    assert 'HelperError("path_invalid")' in source  # non-vacuity
    assert rule_code_problems(source, h.RULE_CODES) == []


def test_a_rules_code_raised_elsewhere_is_found():
    """Planted twin: path_invalid raised by a function that never checks PATH."""
    planted = (
        "def check(rel):\n"
        "    if not PATH.fullmatch(rel):\n"
        "        raise HelperError('path_invalid')\n"
        "def other(rel):\n"
        "    if rel.endswith('.'):\n"
        "        raise HelperError('path_is_home' if rel == '.' else 'path_invalid')\n"
    )
    assert rule_code_problems(planted, h.RULE_CODES) == [
        "other raises path_invalid without checking PATH"
    ]


def test_a_folder_path_is_a_path_that_is_not_the_home_and_says_so():
    assert h.FOLDER_PATH.fullmatch("projects") and not h.FOLDER_PATH.fullmatch("")
    for value in ("a", "/a", "a\x00b", "x" * (h.MAX_PATH_LENGTH + 1)):
        assert bool(h.FOLDER_PATH.fullmatch(value)) is bool(h.PATH.fullmatch(value)), value
    inside = h.MESSAGES["path_is_home"].removesuffix(".")
    assert h.FOLDER_PATH.sentence.startswith(inside)  # the home folder's own words


def test_a_path_ending_in_a_dot_is_refused_in_its_own_words(env):
    for rel in ("projects/beta/inner/..", "projects/alpha/."):
        status, out = helper(env, "delete", "--", rel)
        assert (status, out["error"]["code"]) == (1, "path_ends_in_dot"), rel
        assert out["error"]["message"] == h.MESSAGES["path_ends_in_dot"]
    assert (Path(env["RC_HELPER_HOME"]) / "projects" / "alpha").is_dir()


def test_rc_helper_and_the_backend_refuse_a_value_in_the_same_words(env, client):
    """Every field the backend checks against one of rc-helper's reported rules says
    what rc-helper says for the same value."""
    from tests.conftest import ALICE, CALLS, changing
    from tests.test_problems import request_fields, send

    log_in(env)
    fields = request_fields(client.app)
    for code, rule in h.RULE_CODES.items():
        value = refused_value(rule)
        args, stdin = invoked(code, value)
        said = helper(env, *args, stdin=stdin)[1]["error"]["message"]
        checked = [f for f in fields if f.schema.get("pattern") == rule.anchored]
        assert checked, f"the backend does not check {code}'s rule"
        for f in checked:
            response = send(client, f, value, CALLS, changing(ALICE))
            assert response.json()["errors"] == [{"field": f.name, "message": said}], f


def test_usage_errors_are_json_too(env):
    code, out = helper(env, "no-such-verb")
    assert code == 2 and out["error"]["code"] == "usage"


def test_documented_verbs_match_the_command_line():
    doc = (REPO / "project-knowledge" / "rc-helper.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`rc-helper ([a-z][a-z-]*)", doc))
    assert documented == set(h.VERBS)


def test_launcher_is_executable_and_runs_isolated():
    mode = HELPER.stat().st_mode
    assert mode & stat.S_IXUSR
    assert HELPER.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/python3 -IXutf8"


# --- sudoers template --------------------------------------------------------------


def test_sudoers_template_is_valid_and_narrow(tmp_path):
    template = (REPO / "helpers" / "sudoers.rc-helper").read_text(encoding="utf-8")
    rendered = template.replace("@HELPER@", "/opt/rcman/helpers/rc-helper").replace(
        "@APP_USER@", "rcman"
    )
    path = tmp_path / "sudoers"
    path.write_text(rendered, encoding="utf-8")
    check = subprocess.run(
        ["/usr/sbin/visudo", "-c", "-f", str(path)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr
    rules = [line for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    assert "rcman ALL=(%rcusers) NOPASSWD: /opt/rcman/helpers/rc-helper" in rules
    assert not any("env_keep" in line or "SETENV" in line for line in rules)
    assert any(
        line.startswith("Defaults!/opt/rcman/helpers/rc-helper") and "env_reset" in line
        for line in rules
    )


# --- sign-in success and login expiry ------------------------------------------------


def credentials(env, **oauth):
    path = Path(env["RC_HELPER_HOME"]) / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "AT-SECRET", "refreshToken": "RT-SECRET", **oauth}}
        )
    )


def test_an_accepted_code_connects_the_account(env):
    fake.write_state(
        Path(env["RC_HELPER_HOME"]) / ".claude-fake", logged_in=False, accept_codes=["GOOD"]
    )
    ok(env, "auth-login", "--email", "someone@example.com")
    out = ok(env, "auth-code", stdin="GOOD\n")
    assert out["logged_in"] is True
    assert ok(env, "auth-status")["logged_in"] is True


def test_auth_status_reports_when_the_login_expires(env):
    log_in(env)
    credentials(env, refreshTokenExpiresAt=1_900_000_000_000)
    out = ok(env, "auth-status")
    assert out["login_expires_at"] == "2030-03-17T17:46:40+00:00"
    assert "SECRET" not in json.dumps(out)


def test_login_expiry_is_unknown_without_the_field(env):
    log_in(env)
    assert ok(env, "auth-status")["login_expires_at"] is None  # no credentials file
    credentials(env)
    assert ok(env, "auth-status")["login_expires_at"] is None  # no expiry field
    path = Path(env["RC_HELPER_HOME"]) / ".claude" / ".credentials.json"
    path.write_text("not json")
    assert ok(env, "auth-status")["login_expires_at"] is None  # unreadable


# --- git ------------------------------------------------------------------------------

from tests.git_fixtures import (  # noqa: E402
    fixture_git,
    make_repo,
    push_branch_to_origin,
    write,
)

REPO_REL = "projects/repo"


@pytest.fixture
def repo(env, tmp_path):
    """A repository in the home folder, with an origin outside it holding main and feature."""
    origin = tmp_path / "origins" / "repo.git"
    return make_repo(Path(env["RC_HELPER_HOME"]) / REPO_REL, origin=origin)


def origin_of(repo: Path) -> Path:
    return Path(fixture_git(repo, "remote", "get-url", "origin").strip())


def status(env, rel=REPO_REL) -> dict:
    return ok(env, "git-status", "--", rel)


def listed(out: dict, which: str) -> list[tuple[str, str]]:
    return [(f["path"], f["change"]) for f in out[which]["files"]]


def stage(env, files, rel=REPO_REL, verb="git-stage"):
    return helper(env, verb, "--", rel, stdin=json.dumps(files))


def commit(env, message="A message", name="Ann Author", email="ann@example.com", rel=REPO_REL):
    args = ("git-commit", f"--author-name={name}", f"--author-email={email}", "--", rel)
    return helper(env, *args, stdin=message)


def branch_of(path: Path) -> str:
    return fixture_git(path, "branch", "--show-current").strip()


def test_tree_marks_repositories(env, repo, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    linked = home / "projects" / "linked"
    fixture_git(repo, "worktree", "add", "-q", str(linked))
    assert (linked / ".git").is_file()
    marked = {e["path"]: e["repository"] for e in ok(env, "tree", "projects")["entries"]}
    assert marked["projects/repo"] is True  # a .git folder
    assert marked["projects/linked"] is True  # a .git file
    assert marked["projects/alpha"] is False


def test_status_reports_the_branch_its_upstream_and_how_far_apart_they_are(env, repo):
    branch = status(env)["branch"]
    assert branch["name"] == "main" and branch["upstream"] == "origin/main"
    assert (branch["ahead"], branch["behind"]) == (0, 0)
    assert branch["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip()
    write(repo / "local.txt")
    fixture_git(repo, "add", ".")
    fixture_git(repo, "commit", "-q", "-m", "local")
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    fixture_git(repo, "fetch", "-q")
    branch = status(env)["branch"]
    assert (branch["ahead"], branch["behind"]) == (1, 1)


def test_status_of_a_detached_head_and_of_no_commits_yet(env, repo):
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert status(env)["branch"]["name"] is None
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "trunk")
    branch = status(env, "projects/fresh")["branch"]
    assert branch == {
        "name": "trunk",
        "commit": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }


def test_status_of_a_branch_whose_upstream_is_gone_names_it_with_no_ahead_or_behind(env, repo):
    fixture_git(repo, "switch", "-q", "-c", "feature", "--track", "origin/feature")
    branch = status(env)["branch"]
    assert branch["upstream"] == "origin/feature"
    assert (branch["ahead"], branch["behind"]) == (0, 0)  # positive control: counted while there
    fixture_git(origin_of(repo), "branch", "-D", "feature")
    ok(env, "git-fetch", "--", REPO_REL)  # rc-helper's own fetch prunes it
    assert "origin/feature" not in remote_names(env)  # the plant took effect
    branch = status(env)["branch"]
    assert branch["name"] == "feature" and branch["upstream"] == "origin/feature"
    assert (branch["ahead"], branch["behind"]) == (None, None)


def test_status_lists_each_kind_of_change_in_its_list(env, repo):
    write(repo / "both.txt")
    fixture_git(repo, "add", "both.txt")
    fixture_git(repo, "commit", "-q", "-m", "both")
    write(repo / "README.md", "changed\n")
    write(repo / "new.txt")
    fixture_git(repo, "add", "new.txt")
    write(repo / "folder" / "inside.txt")
    write(repo / "both.txt", "staged\n")
    fixture_git(repo, "add", "both.txt")
    write(repo / "both.txt", "and not\n")
    out = status(env)
    assert listed(out, "staged") == [("both.txt", "modified"), ("new.txt", "added")]
    assert sorted(listed(out, "not_staged")) == [
        ("README.md", "modified"),
        ("both.txt", "modified"),
        ("folder/", "untracked"),
    ]
    assert out["staged"]["more"] == 0 and out["path"] == REPO_REL


def test_a_rename_is_listed_with_where_it_came_from(env, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    [moved] = status(env)["staged"]["files"]
    assert moved == {"path": "MOVED.txt", "change": "renamed", "from": "README.md"}


def test_a_conflict_is_listed_as_unmerged(env, repo):
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "theirs\n")
    fixture_git(repo, "commit", "-q", "-am", "theirs")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "ours\n")
    fixture_git(repo, "commit", "-q", "-am", "ours")
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    merge = subprocess.run(
        ["git", "merge", "-q", "other"],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    assert merge.returncode != 0  # positive control: there is a conflict
    assert listed(status(env), "not_staged") == [("README.md", "unmerged")]


def test_a_submodule_is_marked(env, repo, tmp_path):
    from tests.git_fixtures import add_submodule

    add_submodule(repo, make_repo(tmp_path / "library"), "library")
    files = status(env)["staged"]["files"]
    assert {"path": "library", "change": "added", "submodule": True} in files
    assert {"path": ".gitmodules", "change": "added"} in files


NAMES = ["with space.txt", "with\nnewline.txt", "ünïcode.txt", "tab\there.txt"]


def test_names_with_spaces_and_newlines_round_trip_through_staging(env, repo):
    for name in NAMES:
        write(repo / name)
    out = status(env)
    assert sorted(path for path, _ in listed(out, "not_staged")) == sorted(NAMES)
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(NAMES))
    assert sorted(listed(status(env), "staged")) == sorted((n, "added") for n in NAMES)


def test_a_long_list_is_capped_and_says_how_many_more(env, repo):
    for i in range(h.MAX_LISTED_FILES + 3):
        write(repo / f"f{i:05}.txt")
    out = status(env)["not_staged"]
    assert len(out["files"]) == h.MAX_LISTED_FILES
    assert out["more"] == 3


def test_a_folder_that_is_not_a_repository_is_refused(env, repo):
    assert status(env)["path"] == REPO_REL  # positive control
    assert error_code(env, "git-status", "--", "projects/alpha") == "not_a_repository"
    assert error_code(env, "git-branches", "--", "projects/alpha") == "not_a_repository"


def test_a_folder_inside_a_repository_is_not_one(env, repo):
    write(repo / "inner" / "x.txt")
    (repo / "empty-git" / ".git").mkdir(parents=True)
    assert error_code(env, "git-status", "--", f"{REPO_REL}/inner") == "not_a_repository"
    assert error_code(env, "git-status", "--", f"{REPO_REL}/empty-git") == "not_a_repository"


def test_a_home_that_is_a_repository_makes_no_folder_one(env):
    home = Path(env["RC_HELPER_HOME"])
    fixture_git(home, "init", "-q")
    (home / "projects" / "alpha" / ".git").mkdir()  # not a repository, inside one
    assert error_code(env, "git-status", "--", "projects/alpha") == "not_a_repository"
    assert error_code(env, "git-status", "--", "projects/beta") == "not_a_repository"


def test_a_repository_whose_git_data_is_outside_the_home_is_refused(env, tmp_path):
    outside = make_repo(tmp_path / "outside-repo")
    linked = Path(env["RC_HELPER_HOME"]) / "projects" / "linked"
    fixture_git(outside, "worktree", "add", "-q", str(linked))
    assert error_code(env, "git-status", "--", "projects/linked") == "repository_outside_home"
    inside = make_repo(Path(env["RC_HELPER_HOME"]) / "projects" / "inside")
    fixture_git(inside, "worktree", "add", "-q", str(inside.parent / "linked-inside"))
    assert status(env, "projects/linked-inside")["branch"]["name"]  # positive control


def test_status_writes_nothing_into_the_repository(env, repo):
    import time

    index = repo / ".git" / "index"
    time.sleep(1.1)
    os.utime(repo / "README.md")  # the index's record of it is now stale
    before = index.stat().st_mtime_ns
    status(env)
    assert index.stat().st_mtime_ns == before
    fixture_git(repo, "status")  # positive control: git itself refreshes the index
    assert index.stat().st_mtime_ns != before


def test_stage_stages_only_the_files_given(env, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    write(repo / "README.md", "changed\n")
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["a.txt", "README.md"]))
    out = status(env)
    assert listed(out, "staged") == [("README.md", "modified"), ("a.txt", "added")]
    assert listed(out, "not_staged") == [("b.txt", "untracked")]


def test_unstage_unstages_only_the_files_given(env, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    fixture_git(repo, "add", ".")
    assert ok(env, "git-unstage", "--", REPO_REL, stdin=json.dumps(["a.txt"]))
    out = status(env)
    assert listed(out, "staged") == [("b.txt", "added")]
    assert listed(out, "not_staged") == [("a.txt", "untracked")]


def test_unstaging_a_rename_unstages_both_its_sides(env, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    assert ok(env, "git-unstage", "--", REPO_REL, stdin=json.dumps(["MOVED.txt"]))
    out = status(env)
    assert listed(out, "staged") == []
    assert sorted(listed(out, "not_staged")) == [
        ("MOVED.txt", "untracked"),
        ("README.md", "deleted"),
    ]


def test_unstage_works_before_the_first_commit(env):
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    write(fresh / "a.txt")
    fixture_git(fresh, "add", "a.txt")
    assert listed(status(env, "projects/fresh"), "staged") == [("a.txt", "added")]
    assert ok(env, "git-unstage", "--", "projects/fresh", stdin=json.dumps(["a.txt"]))
    assert listed(status(env, "projects/fresh"), "not_staged") == [("a.txt", "untracked")]


def test_a_deleted_file_and_an_untracked_folder_are_staged(env, repo):
    (repo / "README.md").unlink()
    write(repo / "folder" / "x.txt")
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["README.md", "folder/"]))
    assert listed(status(env), "staged") == [("README.md", "deleted"), ("folder/x.txt", "added")]


def test_names_are_taken_literally(env, repo):
    for name in ("*", "a", "ab", ":(glob)a*"):
        write(repo / name)
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["*", ":(glob)a*"]))
    assert sorted(listed(status(env), "staged")) == [("*", "added"), (":(glob)a*", "added")]


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../x", "file_outside_repository"),
        ("/etc/passwd", "file_outside_repository"),
        ("a/../../x", "file_outside_repository"),
        (".git/config", "file_outside_repository"),
        (".GIT/config", "file_outside_repository"),
        ("a//b", "file_invalid"),
        ("./a", "file_invalid"),
        ("", "file_invalid"),
        ("a\0b", "file_invalid"),
        ("x" * (h.MAX_PATH_LENGTH + 1), "file_invalid"),
    ],
)
def test_a_file_outside_the_repository_is_refused(env, repo, name, code):
    write(repo / "a")
    write(repo / "README.md", "changed\n")
    for verb in ("git-stage", "git-unstage"):
        status_code, out = stage(env, ["README.md", name], verb=verb)
        assert (status_code, out["error"]["code"]) == (1, code), out
    assert listed(status(env), "staged") == []  # nothing staged, not even README.md


def test_a_file_through_a_link_out_of_the_repository_is_refused(env, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "secret.txt")
    (repo / "out").symlink_to(elsewhere)
    assert error_code(env, "git-stage", "--", REPO_REL, stdin='["out/secret.txt"]') == (
        "file_outside_repository"
    )
    assert listed(status(env), "staged") == []
    assert (elsewhere / "secret.txt").read_text() == "content\n"
    assert not (elsewhere / ".git").exists()


def test_a_link_in_the_repository_is_staged_as_the_link(env, repo):
    (repo / "link").symlink_to("README.md")
    assert ok(env, "git-stage", "--", REPO_REL, stdin='["link"]')
    tree = fixture_git(repo, "ls-files", "-s", "link")
    assert tree.startswith("120000 ")  # a link, not the file it points at


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        '{"a": 1}',
        "[1]",
        '"a.txt"',
        "[]",
        json.dumps(["a"] * (h.MAX_FILES_PER_CALL + 1)),
    ],
)
def test_a_list_of_files_that_is_not_one_is_refused(env, repo, stdin):
    assert error_code(env, "git-stage", "--", REPO_REL, stdin=stdin) == "file_list_invalid"


def test_a_name_that_is_not_utf8_is_listed_but_cannot_be_staged(env, repo):
    os.close(os.open(bytes(repo) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY))
    write(repo / "plain.txt")
    files = status(env)["not_staged"]["files"]
    assert {"path": "plain.txt", "change": "untracked"} in files  # positive control
    odd = next(f for f in files if f["path"] != "plain.txt")
    assert odd["selectable"] is False and odd["path"].startswith("caf")
    assert "selectable" not in next(f for f in files if f["path"] == "plain.txt")
    assert (
        error_code(env, "git-stage", "--", REPO_REL, stdin='["caf\\udce9.txt"]') == "file_invalid"
    )
    assert listed(status(env), "staged") == []


def test_commit_records_the_message_from_stdin_and_the_author_given(env, repo):
    (Path(env["RC_HELPER_HOME"]) / ".gitconfig").write_text(
        "[user]\n\tname = Wrong Name\n\temail = wrong@example.com\n"
    )
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    message = "Add a.txt, ünïcode\n\nWith a body: --author-name=x; $(rm -rf ~)\n"
    code, out = commit(env, message)
    assert code == 0, out
    assert out == {
        "path": REPO_REL,
        "commit": fixture_git(repo, "rev-parse", "HEAD").strip(),
        "branch": "main",
    }
    shown = fixture_git(repo, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B")
    assert shown.startswith("Ann Author <ann@example.com>\nAnn Author <ann@example.com>\n")
    assert shown.split("\n", 2)[2].rstrip("\n") == message.rstrip("\n")
    assert listed(status(env), "staged") == []
    assert "Add a.txt" not in json.dumps(out)


def test_the_commit_message_never_reaches_any_argv(env, repo, tmp_path):
    seen = tmp_path / "seen"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"cat /proc/$PPID/cmdline > {seen}.git\n"
        f"helper=$(awk '{{print $4}}' /proc/$PPID/stat)\n"
        f"cat /proc/$helper/cmdline > {seen}.helper\n"
    )
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    assert commit(env, "SECRET-MESSAGE-42")[0] == 0
    git_argv = Path(f"{seen}.git").read_bytes()
    helper_argv = Path(f"{seen}.helper").read_bytes()
    assert b"commit" in git_argv and b"git-commit" in helper_argv  # positive controls
    assert b"SECRET-MESSAGE-42" not in git_argv + helper_argv
    assert "SECRET-MESSAGE-42" in fixture_git(repo, "log", "-1", "--format=%B")


def test_commit_is_refused_when_nothing_is_staged(env, repo):
    write(repo / "README.md", "changed but not staged\n")
    code, out = commit(env)
    assert (code, out["error"]["code"]) == (1, "nothing_staged")
    fixture_git(repo, "add", "README.md")
    assert commit(env)[0] == 0  # positive control


@pytest.mark.parametrize(
    ("message", "name", "email", "code"),
    [
        ("", "Ann", "a@example.com", "message_invalid"),
        (" \n\t\n", "Ann", "a@example.com", "message_invalid"),
        ("a\0b", "Ann", "a@example.com", "message_invalid"),
        ("x" * (h.MAX_COMMIT_MESSAGE_LENGTH + 1), "Ann", "a@example.com", "message_invalid"),
        (b"caf\xe9", "Ann", "a@example.com", "message_invalid"),
        ("Fine", "", "a@example.com", "author_name_invalid"),
        ("Fine", "Ann <x>", "a@example.com", "author_name_invalid"),
        ("Fine", "Ann", "not-an-address", "email_invalid"),
        ("Fine", "Ann", "", "email_invalid"),
    ],
)
def test_an_invalid_message_name_or_address_is_refused_in_its_rules_words(
    env, repo, message, name, email, code
):
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    status_code, out = commit(env, message, name, email)
    assert (status_code, out["error"]["code"]) == (1, code), out
    assert out["error"]["message"] == h.RULE_CODES[code].sentence
    assert listed(status(env), "staged") == [("a.txt", "added")]  # nothing committed


def test_a_message_as_long_as_allowed_is_committed(env, repo):
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    assert commit(env, "x" * h.MAX_COMMIT_MESSAGE_LENGTH)[0] == 0


def test_hooks_run_and_a_refusal_comes_back_in_their_words(env, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'No commits on Fridays.' >&2\nexit 1\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    code, out = commit(env)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert out["error"]["git"] == ["No commits on Fridays."] and out["error"]["git_more"] == 0
    assert out["error"]["message"] == h.MESSAGES["git_failed"]


def test_branches_lists_local_and_remote_tracking_branches(env, repo):
    fixture_git(repo, "remote", "set-head", "origin", "main")
    fixture_git(repo, "branch", "local-only")
    out = ok(env, "git-branches", "--", REPO_REL)
    assert out["local"] == {
        "branches": [
            {"name": "local-only", "current": False, "upstream": None},
            {"name": "main", "current": True, "upstream": "origin/main"},
        ],
        "more": 0,
    }
    assert out["remote"] == {
        "branches": [
            {"name": "origin/feature", "local": None, "taken_by": None},
            {"name": "origin/main", "local": "main", "taken_by": "main"},
        ],
        "more": 0,
    }


def remote_branch(env, name: str) -> dict:
    """A remote-tracking branch as git-branches lists it."""
    listed = ok(env, "git-branches", "--", REPO_REL)["remote"]["branches"]
    return next(b for b in listed if b["name"] == name)


def test_a_remote_branch_whose_name_a_local_branch_has_says_which_and_is_not_switched_to(env, repo):
    """git switch --track would make a local branch of the remote branch's name, and
    git refuses when one has it already, tracking something else."""
    assert remote_branch(env, "origin/feature") == {
        "name": "origin/feature",
        "local": None,
        "taken_by": None,
    }  # positive control: the name is free
    fixture_git(repo, "branch", "--no-track", "feature", "origin/main")
    assert remote_branch(env, "origin/feature") == {
        "name": "origin/feature",
        "local": None,
        "taken_by": "feature",
    }
    code, out = helper(env, "git-switch", "--branch=origin/feature", "--remote", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "branch_name_taken"), out
    assert out["error"]["message"] == h.MESSAGES["branch_name_taken"]
    assert branch_of(repo) == "main"
    assert ok(env, "git-switch", "--branch=feature", "--", REPO_REL)["branch"] == "feature"


def test_a_remote_with_a_slash_in_its_name_gives_the_name_git_gives(env, repo):
    """git switch --track names the local branch by what follows the first slash of the
    remote-tracking branch's name, whatever the remote is called: a/b/feature makes
    b/feature."""
    fixture_git(repo, "remote", "add", "a/b", str(origin_of(repo)))
    fixture_git(repo, "fetch", "-q", "a/b")
    fixture_git(repo, "branch", "--no-track", "feature", "origin/main")
    assert remote_branch(env, "a/b/feature")["taken_by"] is None  # feature is not its name
    out = ok(env, "git-switch", "--branch=a/b/feature", "--remote", "--", REPO_REL)
    assert out["branch"] == "b/feature"  # positive control: the name git gave it
    assert remote_branch(env, "a/b/feature")["local"] == "b/feature"
    fixture_git(repo, "switch", "-q", "main")
    fixture_git(repo, "branch", "-q", "-D", "b/feature")
    fixture_git(repo, "branch", "--no-track", "b/feature", "origin/main")
    assert remote_branch(env, "a/b/feature") == {
        "name": "a/b/feature",
        "local": None,
        "taken_by": "b/feature",
    }
    code = error_code(env, "git-switch", "--branch=a/b/feature", "--remote", "--", REPO_REL)
    assert code == "branch_name_taken"
    assert branch_of(repo) == "main"


def git_switch_track_refused(repo: Path, remote_branch_name: str) -> bool:
    """Whether real git refuses git switch --track for the remote branch; one it makes
    is switched away from and deleted again."""
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    proc = subprocess.run(
        ["git", "switch", "-q", "--track", remote_branch_name],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    if proc.returncode == 0:
        made = branch_of(repo)
        fixture_git(repo, "switch", "-q", "main")
        fixture_git(repo, "branch", "-q", "-D", made)
    return proc.returncode != 0


@pytest.mark.parametrize(
    ("local", "remote", "taken_by"),
    [
        ("feature/x", "origin/feature", "feature/x"),  # the new branch would be its folder
        ("topic", "origin/topic/x", "topic"),  # it would be in the new branch's place
        ("topic/x/y", "origin/topic/x", "topic/x/y"),
        ("feature-x", "origin/feature", None),  # positive controls: names beside each other
        ("feat", "origin/feature", None),
        ("topic/y", "origin/topic/x", None),
    ],
)
def test_a_local_branch_whose_name_git_cannot_keep_beside_the_new_ones_takes_it(
    env, repo, local, remote, taken_by
):
    """git keeps branches as paths, so a branch cannot be named as a folder of another:
    feature/x and feature cannot both be. Each case is first checked against git."""
    push_branch_to_origin(repo, origin_of(repo), "topic/x")
    fixture_git(repo, "fetch", "-q", "origin")
    fixture_git(repo, "branch", "--no-track", local, "origin/main")
    assert git_switch_track_refused(repo, remote) is (taken_by is not None)  # as git does
    assert remote_branch(env, remote) == {"name": remote, "local": None, "taken_by": taken_by}
    code, out = helper(env, "git-switch", f"--branch={remote}", "--remote", "--", REPO_REL)
    if taken_by is None:
        assert code == 0, out
    else:
        assert (code, out["error"]["code"]) == (1, "branch_name_taken"), out
        assert branch_of(repo) == "main"


def test_a_long_list_of_branches_is_capped(env, repo):
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    lines = "".join(f"create refs/heads/b{i:05} {head}\n" for i in range(h.MAX_LISTED_BRANCHES + 1))
    fixture_git(repo, "update-ref", "--stdin", input=lines.encode())
    local = ok(env, "git-branches", "--", REPO_REL)["local"]
    assert len(local["branches"]) == h.MAX_LISTED_BRANCHES
    assert local["more"] == 2  # the extra one, and main


def test_switch_to_a_local_branch(env, repo):
    fixture_git(repo, "branch", "other")
    assert ok(env, "git-switch", "--branch=other", "--", REPO_REL)["branch"] == "other"
    assert branch_of(repo) == "other"


def test_switch_to_a_remote_only_branch_makes_a_local_branch_tracking_it(env, repo):
    out = ok(env, "git-switch", "--branch=origin/feature", "--remote", "--", REPO_REL)
    assert out["branch"] == "feature"
    assert branch_of(repo) == "feature"
    assert fixture_git(repo, "config", "branch.feature.remote").strip() == "origin"
    assert fixture_git(repo, "config", "branch.feature.merge").strip() == "refs/heads/feature"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--branch=nope",), "branch_not_found"),
        (("--branch=feature",), "branch_not_found"),  # only on origin
        (("--branch=main", "--remote"), "branch_not_found"),
        (("--branch=origin/feature",), "branch_not_found"),
        (("--branch=-f",), "branch_name_invalid"),
        (("--branch=",), "branch_name_invalid"),
        ((f"--branch={'x' * (h.MAX_BRANCH_NAME_LENGTH + 1)}",), "branch_name_invalid"),
    ],
)
def test_switch_refuses_a_branch_that_is_not_there(env, repo, args, code):
    assert error_code(env, "git-switch", *args, "--", REPO_REL) == code
    assert branch_of(repo) == "main"


def test_a_switch_blocked_by_local_changes_passes_on_gits_words(env, repo):
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "on other\n")
    fixture_git(repo, "commit", "-q", "-am", "other")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "my unsaved work\n")
    code, out = helper(env, "git-switch", "--branch=other", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("would be overwritten" in line for line in out["error"]["git"])
    assert branch_of(repo) == "main"
    assert (repo / "README.md").read_text() == "my unsaved work\n"


def test_a_lock_held_on_the_index_passes_on_gits_words(env, repo):
    write(repo / "a.txt")
    (repo / ".git" / "index.lock").write_text("")
    code, out = stage(env, ["a.txt"])
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("index.lock" in line for line in out["error"]["git"])


def test_new_branch_makes_a_branch_and_switches_to_it(env, repo):
    out = ok(env, "git-branch-new", "--name=topic/new", "--", REPO_REL)
    assert out["branch"] == "topic/new"
    assert branch_of(repo) == "topic/new"
    assert fixture_git(repo, "rev-parse", "topic/new") == fixture_git(repo, "rev-parse", "main")


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("a..b", "branch_invalid"),
        ("@{-1}", "branch_invalid"),
        ("a b", "branch_invalid"),
        ("x.lock", "branch_invalid"),
        ("a~1", "branch_invalid"),
        ("HEAD", "branch_invalid"),
        ("-x", "branch_name_invalid"),
        ("", "branch_name_invalid"),
        ("x" * (h.MAX_BRANCH_NAME_LENGTH + 1), "branch_name_invalid"),
    ],
)
def test_new_branch_refuses_a_name_git_would_not_take_as_given(env, repo, name, code):
    fixture_git(repo, "switch", "-q", "-c", "previous")
    fixture_git(repo, "switch", "-q", "main")  # so that @{-1} names a branch
    assert error_code(env, "git-branch-new", f"--name={name}", "--", REPO_REL) == code
    assert branch_of(repo) == "main"


def test_new_branch_with_a_name_taken_passes_on_gits_words(env, repo):
    code, out = helper(env, "git-branch-new", "--name=main", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("already exists" in line for line in out["error"]["git"])


def test_new_branch_before_the_first_commit(env):
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    out = ok(env, "git-branch-new", "--name=start", "--", "projects/fresh")
    assert out["branch"] == "start"
    assert status(env, "projects/fresh")["branch"] == {
        "name": "start",
        "commit": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }


def remote_names(env) -> list[str]:
    return [b["name"] for b in ok(env, "git-branches", "--", REPO_REL)["remote"]["branches"]]


def test_fetch_adds_new_remote_branches_and_prunes_gone_ones(env, repo):
    push_branch_to_origin(repo, origin_of(repo), "later")
    fixture_git(origin_of(repo), "branch", "-D", "feature")
    before = remote_names(env)
    assert "origin/feature" in before and "origin/later" not in before  # positive control
    assert ok(env, "git-fetch", "--", REPO_REL)["path"] == REPO_REL
    assert remote_names(env) == ["origin/later", "origin/main"]
    assert branch_of(repo) == "main"
    assert listed(status(env), "not_staged") == []


@pytest.fixture
def asks_for_a_password():
    """An HTTP server that answers every request with a demand for a password."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="rcman test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
    server.shutdown()


def test_fetch_from_a_remote_that_needs_a_password_fails_without_asking(
    env, repo, asks_for_a_password
):
    import time

    fixture_git(repo, "remote", "set-url", "origin", asks_for_a_password)
    began = time.monotonic()
    code, out = helper(env, "git-fetch", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("terminal prompts disabled" in line for line in out["error"]["git"]), out
    assert time.monotonic() - began < h.GIT_NETWORK_TIMEOUT / 4


def test_git_runs_with_the_allowlisted_environment(env, repo, tmp_path):
    dumped = tmp_path / "env.json"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/usr/bin/python3\n"
        "import json, os\n"
        "helper = open(f'/proc/{os.getppid()}/stat').read().rsplit(')', 1)[1].split()[1]\n"
        "helper_env = open(f'/proc/{helper}/environ', 'rb').read().split(b'\\0')\n"
        f"json.dump({{'git': dict(os.environ), 'helper': [e.decode() for e in helper_env]}},"
        f" open({str(dumped)!r}, 'w'))\n"
    )
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    decoyed = {**env, "RCMAN_DECOY": "present"}
    assert helper(decoyed, *commit_args(), stdin="m")[0] == 0
    seen = json.loads(dumped.read_text())
    assert "RCMAN_DECOY=present" in seen["helper"]  # positive control: the helper had it
    assert "RCMAN_DECOY" not in seen["git"] and "RC_HELPER_HOME" not in seen["git"]
    expected = h.git_env(h.config_from_env(env))
    # git puts its own programs first on the PATH its hooks get
    exec_path = fixture_git(repo, "--exec-path").strip()
    assert seen["git"]["PATH"].startswith(f"{exec_path}:")
    seen["git"]["PATH"] = seen["git"]["PATH"].removeprefix(f"{exec_path}:")
    assert {k: v for k, v in seen["git"].items() if not k.startswith("GIT_")} == {
        k: v for k, v in expected.items() if not k.startswith("GIT_")
    }
    assert seen["git"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["git"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["git"]["PATH"].startswith(f"{env['RC_HELPER_HOME']}/.local/bin:")
    assert "GIT_CONFIG_NOSYSTEM" not in seen["git"]


def commit_args(rel=REPO_REL):
    return ("git-commit", "--author-name=Ann", "--author-email=ann@example.com", "--", rel)


def test_sigterm_ends_git_and_its_hooks(env, repo, tmp_path):
    import signal
    import time

    pid_file = tmp_path / "hook.pid"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    proc = subprocess.Popen(
        [str(HELPER), *commit_args()],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(b"m")
    proc.stdin.close()
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the hook never ran"
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    os.kill(pid, 0)  # positive control: the hook is running
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the hook {pid} outlived the helper")


IN_REPO = ("--", REPO_REL)
# Every verb that runs git: its command line and what it reads on stdin.
GIT_VERBS = [
    (("git-status", *IN_REPO), None),
    (("git-branches", *IN_REPO), None),
    (("git-stage", *IN_REPO), '["a.txt"]'),
    (("git-unstage", *IN_REPO), '["a.txt"]'),
    (("git-commit", "--author-name=Ann", "--author-email=ann@example.com", *IN_REPO), "m"),
    (("git-switch", "--branch=main", *IN_REPO), None),
    (("git-branch-new", "--name=new", *IN_REPO), None),
    (("git-fetch", *IN_REPO), None),
    (("git-push", *IN_REPO), None),
    (("github-token",), "a-token\n"),
    (("github-status",), None),
    (("github-forget",), None),
    (("git-clone", "--url=https://github.com/owner/nothing-here", "--", "projects"), None),
]


def test_every_git_verb_is_driven_here():
    git_verbs = {v for v in h.VERBS if v.startswith(("git-", "github-"))}
    assert {"git-status", "github-token"} <= git_verbs  # non-vacuity
    assert {args[0] for args, _ in GIT_VERBS} == git_verbs


@pytest.mark.parametrize(("args", "stdin"), GIT_VERBS, ids=[a[0] for a, _ in GIT_VERBS])
def test_git_verbs_are_refused_while_a_folder_is_being_deleted(env, repo, args, stdin):
    write(repo / "a.txt")
    fd = hold_the_lock(env, exclusive=True)
    try:
        assert error_code(env, *args, stdin=stdin) == "folder_busy"
    finally:
        os.close(fd)
    shared = hold_the_lock(env, exclusive=False)
    try:  # positive control: they share the lock with each other
        code, out = helper(env, *args, stdin=stdin)
        assert out.get("error", {}).get("code") != "folder_busy", out
    finally:
        os.close(shared)


def changes_table(markdown: str) -> list[str]:
    """The change codes in the first column of the table under "| Change |" in a guide."""
    lines = markdown.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("| Change |")), None)
    if start is None:
        return []
    codes = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        codes.append(re.fullmatch(r"\| `([a-z_]+)` \|.*", line).group(1))
    return codes


def test_the_git_guides_table_of_changes_is_every_change_rc_helper_reports():
    guide = (REPO / "docs" / "git.md").read_text(encoding="utf-8")
    assert changes_table(guide) == list(h.CHANGES)


def test_the_changes_table_check_fails_on_a_drifted_table():
    """Planted twins: a row missing, or one too many."""
    rows = [f"| `{c}` | x | y |" for c in h.CHANGES]
    right = "| Change | Shown as | Meaning |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    assert changes_table(right) == list(h.CHANGES)
    assert changes_table(right.replace(rows[0] + "\n", "")) != list(h.CHANGES)
    assert changes_table(right + "| `forged` | x | y |\n") != list(h.CHANGES)
    assert changes_table("nothing here") == []


def test_git_env_is_the_server_environment_cut_down_for_git(tmp_path):
    cfg = h.Config(
        home=tmp_path, claude=tmp_path / "claude", tmux_socket=None, github=h.GITHUB_ADDRESS
    )
    env = h.git_env(cfg)
    child = h.child_env(cfg)
    for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG"):
        assert env[key] == child[key]
    assert env["LC_ALL"] == "C.UTF-8"
    assert set(env) == {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
    }


# --- git's failures, each as rc-helper answers it -----------------------------------


def answer_of(env, capsys, *argv, stdin=""):
    """rc-helper's exit status and answer, run in this process so a test can change it."""
    import io

    capsys.readouterr()
    status = h.main(list(argv), env, stdin=io.TextIOWrapper(io.BytesIO(stdin.encode())))
    return status, json.loads(capsys.readouterr().out)


def test_a_git_past_its_deadline_is_git_timeout_and_is_ended(
    env, repo, tmp_path, monkeypatch, capsys
):
    import time

    pid_file = tmp_path / "hook.pid"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    monkeypatch.setattr(h, "GIT_TIMEOUT", 2.0)  # read when the verb is called
    began = time.monotonic()
    status, out = answer_of(env, capsys, *commit_args(), stdin="m")
    assert time.monotonic() - began < 10
    assert (status, out["error"]["code"]) == (1, "git_timeout"), out
    assert out["error"]["message"] == h.MESSAGES["git_timeout"]
    pid = int(pid_file.read_text())  # positive control: the hook ran
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the hook {pid} outlived its deadline")
    assert listed(status_of(env, capsys), "staged") == [("a.txt", "added")]  # nothing committed


def status_of(env, capsys) -> dict:
    status, out = answer_of(env, capsys, "git-status", "--", REPO_REL)
    assert status == 0, out
    return out


def test_a_fetch_has_its_own_deadline_read_when_it_is_called(env, repo, monkeypatch, capsys):
    import time

    hold = Path(env["RC_HELPER_HOME"]) / "slow-upload-pack"
    hold.write_text("#!/bin/sh\nexec sleep 60\n")
    hold.chmod(0o755)
    fixture_git(repo, "config", "remote.origin.uploadpack", str(hold))
    monkeypatch.setattr(h, "GIT_NETWORK_TIMEOUT", 2.0)
    began = time.monotonic()
    status, out = answer_of(env, capsys, "git-fetch", "--", REPO_REL)
    assert time.monotonic() - began < 10
    assert (status, out["error"]["code"]) == (1, "git_timeout"), out


def test_a_push_has_the_network_deadline_read_when_it_is_called(env, repo, monkeypatch, capsys):
    """A push whose remote never answers ends at GIT_NETWORK_TIMEOUT, not at GIT_TIMEOUT:
    the two are set apart here, the local one the shorter."""
    import time

    hold = Path(env["RC_HELPER_HOME"]) / "slow-receive-pack"
    hold.write_text("#!/bin/sh\nexec sleep 60\n")
    hold.chmod(0o755)
    fixture_git(repo, "config", "remote.origin.receivepack", str(hold))
    commit_in(repo, "more.txt")
    monkeypatch.setattr(h, "GIT_TIMEOUT", 1.0)
    monkeypatch.setattr(h, "GIT_NETWORK_TIMEOUT", 4.0)
    began = time.monotonic()
    status, out = answer_of(env, capsys, "git-push", "--", REPO_REL)
    took = time.monotonic() - began
    assert (status, out["error"]["code"]) == (1, "git_timeout"), out
    assert 3.5 < took < 10, took


def test_no_git_on_the_system_path_is_git_not_installed(env, repo, tmp_path, monkeypatch, capsys):
    from helpers import git_run

    assert status_of(env, capsys)["path"] == REPO_REL  # positive control
    monkeypatch.setattr(git_run, "GIT_SEARCH_PATH", str(tmp_path / "no-git-here"))
    status, out = answer_of(env, capsys, "git-status", "--", REPO_REL)
    assert (status, out["error"]["code"]) == (1, "git_not_installed"), out
    assert out["error"]["message"] == h.MESSAGES["git_not_installed"]


def test_a_status_record_git_is_not_known_to_write_is_git_unrecognised_output(
    env, repo, tmp_path, monkeypatch, capsys
):
    """A git that writes one record of an unknown kind among its status, through the verb."""
    from helpers import git_run

    real = git_run.find_git()
    wrapper_dir = tmp_path / "odd-git"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = status ] && { printf "z from a newer git\\0"; break; }; done\n'
        f'exec {real} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(git_run, "GIT_SEARCH_PATH", str(wrapper_dir))
    status, out = answer_of(env, capsys, "git-status", "--", REPO_REL)
    assert (status, out["error"]["code"]) == (1, "git_unrecognised_output"), out
    assert out["error"]["git"] == ["z from a newer git"]
    monkeypatch.setattr(git_run, "GIT_SEARCH_PATH", str(Path(real).parent))
    assert status_of(env, capsys)["path"] == REPO_REL  # positive control: real git is read


def test_tree_marks_only_what_the_changes_section_can_show(env, repo, tmp_path):
    """The marker follows the rule git-status keeps: a repository's own .git, with its git
    data in the home folder."""
    home = Path(env["RC_HELPER_HOME"])
    outside = make_repo(tmp_path / "outside-repo")
    fixture_git(outside, "worktree", "add", "-q", str(home / "projects" / "elsewhere"))
    (home / "projects" / "empty-git" / ".git").mkdir(parents=True)
    (home / "projects" / "linked-git").mkdir()
    (home / "projects" / "linked-git" / ".git").symlink_to(outside / ".git")
    (home / "projects" / "odd-file").mkdir()
    (home / "projects" / "odd-file" / ".git").write_text("not a gitdir line\n")
    marked = {e["path"]: e["repository"] for e in ok(env, "tree", "projects")["entries"]}
    assert marked["projects/repo"] is True  # positive control
    for rel in ("elsewhere", "empty-git", "linked-git", "odd-file"):
        assert marked[f"projects/{rel}"] is False, rel
        code = error_code(env, "git-status", "--", f"projects/{rel}")
        assert code in ("not_a_repository", "repository_outside_home", "git_failed"), (rel, code)


def test_status_says_when_a_merge_is_in_progress(env, repo):
    """Until the merge is committed, also once its conflicts are resolved and staged."""
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    assert status(env)["merging"] is False  # positive control's twin: no merge
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "theirs\n")
    fixture_git(repo, "commit", "-q", "-am", "theirs")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "ours\n")
    fixture_git(repo, "commit", "-q", "-am", "ours")
    merge = subprocess.run(
        ["git", "merge", "-q", "other"],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    assert merge.returncode != 0
    assert status(env)["merging"] is True
    write(repo / "README.md", "resolved\n")
    fixture_git(repo, "add", "README.md")
    resolved = status(env)
    assert listed(resolved, "not_staged") == []  # no unmerged entry is left to say so
    assert resolved["merging"] is True
    assert commit(env, "Merge other")[0] == 0
    assert status(env)["merging"] is False


def test_a_name_with_a_git_part_is_refused_as_written():
    """The written check alone: .git itself is a name in the repository's own folder."""
    with pytest.raises(h.HelperError) as err:
        h.check_file(Path("/nonexistent-repo"), ".git")
    assert err.value.code == "file_outside_repository"
    assert h.check_file(Path("/nonexistent-repo"), "git") == b"git"  # positive control


def test_a_name_whose_folder_resolves_into_the_git_folder_is_refused(tmp_path):
    """The resolved check alone: written without .git, the name's folder is .git."""
    repo = make_repo(tmp_path / "r").resolve()
    (repo / "meta").symlink_to(".git")
    (repo / "plain").mkdir()
    with pytest.raises(h.HelperError) as err:
        h.check_file(repo, "meta/config")
    assert err.value.code == "file_outside_repository"
    assert h.check_file(repo, "plain/config") == b"plain/config"  # positive control


# --- UTF-8 whatever the locale ------------------------------------------------------

LAUNCHERS = ["rc-helper", "rc-admin", "rcman-user"]


def test_every_launcher_runs_python_isolated_and_in_utf8_mode():
    found = sorted(
        p.name
        for p in (REPO / "helpers").iterdir()
        if p.is_file() and p.read_bytes().startswith(b"#!/usr/bin/python3")
    )
    assert found == sorted(LAUNCHERS)  # every launcher, found, not listed by hand
    for name in LAUNCHERS:
        first = (REPO / "helpers" / name).read_text(encoding="utf-8").splitlines()[0]
        assert first == "#!/usr/bin/python3 -IXutf8", name


def test_the_installer_runs_python_in_utf8_mode():
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    runs = [line for line in script.splitlines() if "/usr/bin/python3" in line]
    assert runs  # non-vacuity
    for line in runs:
        assert "/usr/bin/python3 -I -X utf8 " in line, line


@pytest.fixture
def latin1(env, tmp_path):
    """env with an ISO-8859-1 locale, built for the test with localedef."""
    locales = tmp_path / "locales"
    locales.mkdir()
    made = subprocess.run(
        ["localedef", "-i", "en_US", "-f", "ISO-8859-1", str(locales / "en_US.ISO-8859-1")],
        capture_output=True,
        text=True,
    )
    if made.returncode not in (0, 1) or not (locales / "en_US.ISO-8859-1").exists():
        pytest.skip(f"localedef could not build an ISO-8859-1 locale: {made.stderr.strip()}")
    latin = {**env, "LOCPATH": str(locales), "LC_ALL": "en_US.ISO-8859-1"}
    probe = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import locale; print(locale.getpreferredencoding(False))",
        ],
        env=latin,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "ISO-8859-1"  # the plant took effect
    return latin


def test_a_folder_name_is_listed_as_it_is_under_a_locale_that_is_not_utf8(latin1):
    (Path(latin1["RC_HELPER_HOME"]) / "projects" / "café").mkdir()
    names = [e["name"] for e in ok(latin1, "tree", "projects")["entries"]]
    assert "café" in names, names


def test_a_branch_name_is_taken_as_it_is_under_a_locale_that_is_not_utf8(latin1, repo):
    out = ok(latin1, "git-branch-new", "--name=topic/café", "--", REPO_REL)
    assert out["branch"] == "topic/café"
    assert branch_of(repo) == "topic/café"


def test_a_git_file_without_its_gitdir_prefix_is_not_a_repository(env, repo):
    """A .git file holding only a path, even one to a real git folder, is no gitdir file."""
    bare_path = Path(env["RC_HELPER_HOME"]) / "projects" / "bare-path"
    bare_path.mkdir()
    (bare_path / ".git").write_text(f"{repo / '.git'}\n")
    with_prefix = Path(env["RC_HELPER_HOME"]) / "projects" / "with-prefix"
    fixture_git(repo, "worktree", "add", "-q", str(with_prefix))
    assert (with_prefix / ".git").read_text().startswith("gitdir: ")  # the twin's form
    marked = {e["path"]: e["repository"] for e in ok(env, "tree", "projects")["entries"]}
    assert marked["projects/with-prefix"] is True  # positive control
    assert marked["projects/bare-path"] is False


def test_the_tree_never_lists_what_is_inside_a_git_folder(env, repo):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "peek").symlink_to(repo / ".git")
    (home / "projects" / "peek-inside").symlink_to(repo / ".git" / "refs")
    listed = [e["path"] for e in ok(env, "tree", "--all", "--", "projects")["entries"]]
    assert "projects/repo" in listed  # positive control: the repository itself is listed
    assert "projects/peek" not in listed and "projects/peek-inside" not in listed
    inside = [e["path"] for e in ok(env, "tree", "--all", "--", REPO_REL)["entries"]]
    assert f"{REPO_REL}/.git" not in inside


def test_a_git_verbs_deadline_must_name_a_constant_that_exists():
    h.git_verb("GIT_TIMEOUT")  # positive control
    with pytest.raises(NameError, match="NO_SUCH_TIMEOUT"):
        h.git_verb("NO_SUCH_TIMEOUT")


def test_git_env_points_github_at_the_hook_only_when_set(tmp_path):
    import dataclasses

    plain = h.Config(
        home=tmp_path, claude=tmp_path / "claude", tmux_socket=None, github=h.GITHUB_ADDRESS
    )
    assert plain.github == h.GITHUB_ADDRESS
    assert not any(key.startswith("GIT_CONFIG") for key in h.git_env(plain))
    hooked = h.git_env(dataclasses.replace(plain, github="http://127.0.0.1:9/"))
    assert {k: v for k, v in hooked.items() if k.startswith("GIT_CONFIG")} == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.http://127.0.0.1:9/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
    }


@pytest.fixture
def hooked(env, github):
    """``env`` with https://github.com/ pointed at the tests' GitHub."""
    return {**env, "RC_HELPER_GITHUB": github.url}


GH_REL = "projects/from-github"


@pytest.fixture
def on_github(env, github, tmp_path):
    """A repository in the home whose origin is https://github.com/owner/<unique>.git,
    served by the tests' GitHub; its bare repository there."""
    name = tmp_path.name
    bare = github.bare("owner", name)
    make_repo(
        Path(env["RC_HELPER_HOME"]) / GH_REL, origin=bare, url=github.github_url("owner", name)
    )
    return bare


def test_the_github_hook_sends_git_for_github_to_the_tests_server(env, hooked, on_github):
    repo = Path(env["RC_HELPER_HOME"]) / GH_REL
    assert fixture_git(repo, "remote", "get-url", "origin").startswith("https://github.com/")
    push_branch_to_origin(repo, on_github, "later")
    code, out = helper(env, "git-fetch", "--", GH_REL)  # unhooked: the unreachable address
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("127.0.0.1 port 1" in line for line in out["error"]["git"]), out
    ok(hooked, "git-fetch", "--", GH_REL)
    branches = ok(hooked, "git-branches", "--", GH_REL)["remote"]["branches"]
    assert "origin/later" in [b["name"] for b in branches]


# --- the GitHub token -----------------------------------------------------------------

TOKEN = "github_pat_rcman-test-0123456789"


def run_helper(env, *args, stdin=None) -> subprocess.CompletedProcess:
    """rc-helper's whole run: exit status, standard output and standard error."""
    return subprocess.run(
        [str(HELPER), *args], env=env, input=stdin, capture_output=True, text=True, timeout=60
    )


def set_token(env, token=TOKEN):
    return helper(env, "github-token", stdin=token + "\n")


def git_credentials(env) -> Path:
    return Path(env["RC_HELPER_HOME"]) / ".git-credentials"


def server_base(github) -> str:
    """The address the hook sends GitHub to, as a credential and its helper know it."""
    return github.url.removesuffix("/")


def stored_host(github) -> str:
    """The hook's host as git credential-store writes it, its colon percent-encoded."""
    return server_base(github).removeprefix("http://").replace(":", "%3a")


def global_config(env, *args) -> list[str]:
    """What the person's own ~/.gitconfig says, read with git outside rc-helper."""
    proc = subprocess.run(
        ["git", "config", "--file", str(Path(env["RC_HELPER_HOME"]) / ".gitconfig"), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.split("\n")[:-1]


def test_the_token_is_read_from_stdin_and_stored_for_github(hooked, github):
    proc = run_helper(hooked, "github-token", stdin=TOKEN + "\n")
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout) == {"token": True}
    assert TOKEN not in proc.stdout + proc.stderr
    stored = git_credentials(hooked)
    assert stored.read_text() == f"http://{h.GITHUB_USERNAME}:{TOKEN}@{stored_host(github)}\n"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert global_config(hooked, "--get-all", f"credential.{server_base(github)}.helper") == [
        "store"
    ]
    proc = run_helper(hooked, "github-status")
    assert json.loads(proc.stdout) == {"token": True} and TOKEN not in proc.stdout + proc.stderr


def test_without_the_hook_the_token_is_stored_for_github_com(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = h.Config(home=home, claude=tmp_path / "claude", tmux_socket=None, github=h.GITHUB_ADDRESS)
    assert cfg.github == h.GITHUB_ADDRESS
    helper_ = h.Helper(cfg)
    assert helper_.github_token(TOKEN.encode() + b"\n") == {"token": True}
    assert (home / ".git-credentials").read_text() == (
        f"https://{h.GITHUB_USERNAME}:{TOKEN}@github.com\n"
    )
    env = helper_env(home, tmp_path / "claude", tmp_path / "tmux.sock")
    assert global_config(env, "--get-all", "credential.https://github.com.helper") == ["store"]
    assert helper_.github_status() == {"token": True}
    assert helper_.github_forget() == {"token": False}
    assert helper_.github_status() == {"token": False}


def test_the_store_helper_is_set_once(hooked, github):
    key = f"credential.{server_base(github)}.helper"
    assert set_token(hooked)[0] == 0
    assert set_token(hooked, "another-token")[0] == 0
    assert global_config(hooked, "--get-all", key) == ["store"]


def test_a_store_helper_set_already_is_not_set_again(hooked, github):
    key = f"credential.{server_base(github)}.helper"
    gitconfig = Path(hooked["RC_HELPER_HOME"]) / ".gitconfig"
    gitconfig.write_text(f'[credential "{server_base(github)}"]\n\thelper = store\n')
    assert set_token(hooked)[0] == 0
    assert global_config(hooked, "--get-all", key) == ["store"]


def test_a_new_token_replaces_the_old(hooked, github):
    assert set_token(hooked, "first-token")[0] == 0
    assert set_token(hooked, "second-token")[0] == 0
    host = stored_host(github)
    assert (
        git_credentials(hooked).read_text() == f"http://{h.GITHUB_USERNAME}:second-token@{host}\n"
    )


def test_status_needs_both_the_token_and_its_helper(hooked, github):
    assert ok(hooked, "github-status") == {"token": False}  # neither
    assert set_token(hooked)[0] == 0
    assert ok(hooked, "github-status") == {"token": True}  # both
    key = f"credential.{server_base(github)}.helper"
    subprocess.run(
        ["git", "config", "--file", str(Path(hooked["RC_HELPER_HOME"]) / ".gitconfig"),
         "--unset-all", key],
        check=True,
    )  # fmt: skip
    assert ok(hooked, "github-status") == {"token": False}  # the token, but git would not use it
    assert set_token(hooked)[0] == 0
    assert ok(hooked, "github-forget") == {"token": False}
    assert global_config(hooked, "--get-all", key) == ["store"]  # the helper is left
    assert ok(hooked, "github-status") == {"token": False}  # the helper, but no token


def test_forget_removes_rcmans_token_only(hooked, github):
    host = server_base(github).removeprefix("http://")
    theirs = f"http://someone:their-own-token@{host}\n"
    git_credentials(hooked).write_text(theirs)
    git_credentials(hooked).chmod(0o600)
    assert set_token(hooked)[0] == 0
    assert TOKEN in git_credentials(hooked).read_text()  # positive control
    assert ok(hooked, "github-forget") == {"token": False}
    assert git_credentials(hooked).read_text() == theirs


# --- the guide's account of where the token is kept --------------------------------------

GIT_GUIDE = REPO / "docs" / "git.md"
# Files git's store helper and git config --global use in place of ~/.git-credentials
# and ~/.gitconfig when only they exist, made empty for the second layout.
XDG_FILES = (".config/git/credentials", ".config/git/config")


def files_in(home: Path) -> dict[str, str]:
    return {str(p.relative_to(home)): p.read_text() for p in home.rglob("*") if p.is_file()}


def config_settings(path: Path) -> list[tuple[str, str]]:
    """Every setting in a git configuration file, as (key, value), read with git."""
    listed = subprocess.run(
        ["git", "config", "--file", str(path), "--list", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [tuple(entry.split("\n", 1)) for entry in listed.split("\0") if entry]


def token_storage(tmp_path: Path, existing: tuple[str, ...] = ()) -> dict:
    """What github-token and github-forget change in a home holding the empty files
    ``existing``, for GitHub itself, as in production: the file the token is written
    to, the global configuration file that changes and its settings, and, once the
    token is forgotten, whether it is left and the settings left."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    for rel in existing:
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("")
    cfg = h.Config(home=home, claude=tmp_path / "claude", tmux_socket=None, github=h.GITHUB_ADDRESS)
    helper_ = h.Helper(cfg)
    before = files_in(home)
    helper_.github_token(TOKEN.encode() + b"\n")
    stored = files_in(home)
    changed = {rel for rel, text in stored.items() if before.get(rel) != text}
    [credentials] = [rel for rel in changed if TOKEN in stored[rel]]
    [config] = changed - {credentials}
    settings = config_settings(home / config)
    helper_.github_forget()
    return {
        "credentials": credentials,
        "config": config,
        "settings": settings,
        "token_left": TOKEN in files_in(home)[credentials],
        "settings_left": config_settings(home / config),
    }


def guide_token_bullets(text: str) -> dict[str, str]:
    """The bullets of the guide's GitHub token section by their bold lead, each with
    its lines joined."""
    section = re.search(r"^## GitHub token\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if section is None:
        return {}
    bullets = re.findall(r"^- \*\*(.+?)\*\*(.*?)(?=^\S|\Z)", section.group(1), re.M | re.S)
    return {lead: " ".join(body.split()) for lead, body in bullets}


def token_guide_problems(text: str, layouts: list[dict]) -> list[str]:
    """Where the guide's GitHub token section disagrees with what rc-helper does: its
    "Where it is kept." bullet names every file the token and the setting are written
    to, in each layout, and the setting's key and value; its "Removing it." bullet
    names the setting and says it stays exactly when it is left."""
    bullets = guide_token_bullets(text)
    kept, removing = bullets.get("Where it is kept."), bullets.get("Removing it.")
    if kept is None or removing is None:
        return [f"no Where it is kept. or Removing it. bullet in {sorted(bullets)}"]
    problems = []
    for layout in layouts:
        for rel in (layout["credentials"], layout["config"]):
            if f"`~/{rel}`" not in kept:
                problems.append(f"Where it is kept. does not name `~/{rel}`")
        for key, value in layout["settings"]:
            for word in (f"`{key}`", f"`{value}`"):
                if word not in kept:
                    problems.append(f"Where it is kept. does not name {word}")
            left = (key, value) in layout["settings_left"]
            if left != (f"`{key}`" in removing and "stays" in removing):
                problems.append(f"Removing it. does not say whether `{key}` stays")
    return problems


@pytest.fixture
def token_layouts(tmp_path) -> list[dict]:
    """token_storage in a home with neither file, and in one with only XDG_FILES."""
    layouts = [token_storage(tmp_path / "plain"), token_storage(tmp_path / "xdg", XDG_FILES)]
    # Positive controls: two layouts writing to different files, a setting written,
    # and the token gone once forgotten.
    assert layouts[0]["credentials"] != layouts[1]["credentials"]
    assert layouts[0]["config"] != layouts[1]["config"]
    assert all(layout["settings"] for layout in layouts)
    assert not any(layout["token_left"] for layout in layouts)
    return layouts


def test_the_guide_says_where_the_token_is_kept_and_what_removing_it_leaves(token_layouts):
    assert token_guide_problems(GIT_GUIDE.read_text(), token_layouts) == []


@pytest.mark.parametrize("plant", ["credentials file", "config file", "setting", "stays"])
def test_the_token_guide_check_fails_on_a_guide_that_says_otherwise(token_layouts, plant):
    """Planted twins: the guide with one thing it must say changed."""
    text = GIT_GUIDE.read_text()
    plain = token_layouts[0]
    key = plain["settings"][0][0]
    old, new = {
        "credentials file": (f"`~/{plain['credentials']}`", "`~/.planted-credentials`"),
        "config file": (f"`~/{plain['config']}`", "`~/.planted-config`"),
        "setting": (f"`{key}`", "`credential.planted.helper`"),
        "stays": ("stays", "goes"),
    }[plant]
    planted = text.replace(old, new)
    assert planted != text  # the plant took effect
    assert token_guide_problems(planted, token_layouts)


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "\n",
        "has space\n",
        "two\nlines\n",
        "tab\there\n",
        "carriage-return\r\n",
        "ünïcode\n",
        "too long",
        b"\xff\xfe-not-utf8\n",
    ],
)
def test_a_token_that_is_not_one_line_of_visible_characters_is_refused(hooked, stdin):
    if stdin == "too long":
        stdin = "x" * (h.MAX_GITHUB_TOKEN_LENGTH + 1) + "\n"
    code, out = helper(hooked, "github-token", stdin=stdin)
    assert (code, out["error"]["code"]) == (1, "token_invalid"), out
    assert out["error"]["message"] == h.GITHUB_TOKEN.sentence
    assert not git_credentials(hooked).exists()


def test_a_token_as_long_as_allowed_and_of_every_visible_character_is_stored(hooked):
    visible = "".join(map(chr, range(0x21, 0x7F)))
    assert set_token(hooked, visible)[0] == 0
    assert set_token(hooked, "x" * h.MAX_GITHUB_TOKEN_LENGTH)[0] == 0
    assert ok(hooked, "github-status") == {"token": True}


def test_the_token_rule_states_its_limit():
    assert str(h.MAX_GITHUB_TOKEN_LENGTH) in h.GITHUB_TOKEN.sentence


def push_as_the_person(env, repo: Path) -> subprocess.CompletedProcess:
    """A plain git push, as from a Claude session: the server's environment, no terminal,
    and the hook's address for GitHub, as rc-helper gives git."""
    cfg = h.config_from_env(env)
    hook = {k: v for k, v in h.git_env(cfg).items() if k.startswith("GIT_CONFIG")}
    assert hook  # positive control: the push goes to the tests' GitHub
    return subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/pushed"],
        cwd=repo,
        env={**h.child_env(cfg), **hook},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=60,
    )


def pushed(bare: Path) -> str | None:
    found = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", "refs/heads/pushed"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() or None


def test_a_plain_git_push_as_the_person_uses_the_stored_token(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = len(github.attempts)
    refused = push_as_the_person(hooked, repo)
    assert refused.returncode != 0 and pushed(on_github) is None
    assert ("x-access-token", True) not in github.attempts[before:]
    assert set_token(hooked, github.token)[0] == 0
    before = len(github.attempts)
    accepted = push_as_the_person(hooked, repo)
    assert accepted.returncode == 0, accepted.stderr
    assert pushed(on_github) == fixture_git(repo, "rev-parse", "HEAD").strip()
    assert set(github.attempts[before:]) == {(h.GITHUB_USERNAME, True)}


def test_rcmans_token_is_used_before_an_older_one(hooked, github, on_github):
    host = server_base(github).removeprefix("http://")
    git_credentials(hooked).write_text(f"http://older:not-the-token@{host}\n")
    git_credentials(hooked).chmod(0o600)
    assert set_token(hooked, github.token)[0] == 0
    before = len(github.attempts)
    proc = push_as_the_person(hooked, Path(hooked["RC_HELPER_HOME"]) / GH_REL)
    assert proc.returncode == 0, proc.stderr
    assert set(github.attempts[before:]) == {(h.GITHUB_USERNAME, True)}


def test_a_token_github_refuses_is_forgotten_by_git(hooked, github, on_github):
    assert set_token(hooked, "not-the-token")[0] == 0
    assert ok(hooked, "github-status") == {"token": True}
    proc = push_as_the_person(hooked, Path(hooked["RC_HELPER_HOME"]) / GH_REL)
    assert proc.returncode != 0 and "Authentication failed" in proc.stderr
    assert ok(hooked, "github-status") == {"token": False}


import time  # noqa: E402


def traced(monkeypatch, tmp_path) -> Path:
    """GIT_TRACE for every git rc-helper runs in this process: the file it writes to."""
    trace = tmp_path / "trace"
    plain = h.git_env
    monkeypatch.setattr(h, "git_env", lambda cfg: {**plain(cfg), "GIT_TRACE": str(trace)})
    return trace


def test_the_token_reaches_no_argv_git_traces(hooked, monkeypatch, tmp_path):
    trace = traced(monkeypatch, tmp_path)
    helper_ = in_process(hooked)
    helper_.github_token(TOKEN.encode() + b"\n")
    helper_.github_status()
    helper_.github_forget()
    text = trace.read_text()
    assert "credential-store" in text and "config" in text  # positive control
    assert TOKEN not in text


def test_a_push_reaches_github_with_the_token_in_no_argv(
    hooked, github, on_github, monkeypatch, tmp_path
):
    trace = traced(monkeypatch, tmp_path)
    helper_ = in_process(hooked)
    helper_.github_token(github.token.encode() + b"\n")
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    commit_in(repo, "pushed.txt")
    assert helper_.git_push(GH_REL, False)["upstream_set"] is False
    text = trace.read_text()
    assert "remote-http" in text and "push" in text  # positive control: it went over HTTP
    assert github.token not in text


def test_the_trace_check_sees_what_is_in_an_argv(hooked, monkeypatch, tmp_path):
    """Planted twin: a marker in a git argv is in the trace the check above reads."""
    trace = traced(monkeypatch, tmp_path)
    marker = "PLANTED-MARKER-IN-ARGV"
    home = Path(hooked["RC_HELPER_HOME"])
    git_run.Git(h.git_env(h.config_from_env(hooked))).run(
        home,
        ["config", "--file", str(tmp_path / "cfg"), "rcman.marker", marker],
        deadline=time.monotonic() + 30,
    )
    assert marker in trace.read_text()


# --- push ---------------------------------------------------------------------------


def commit_in(repo: Path, name: str) -> str:
    """A new commit in ``repo`` adding ``name``; its hash."""
    write(repo / name)
    fixture_git(repo, "add", name)
    fixture_git(repo, "commit", "-q", "-m", f"Add {name}")
    return fixture_git(repo, "rev-parse", "HEAD").strip()


def on_remote(bare: Path, branch: str) -> str | None:
    found = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() or None


def push(env, rel, *options):
    return helper(env, "git-push", *options, "--", rel)


def test_a_branch_with_no_upstream_is_pushed_and_set_to_track_it(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    assert set_token(hooked, github.token)[0] == 0
    fixture_git(repo, "switch", "-q", "-c", "topic")
    head = commit_in(repo, "topic.txt")
    code, out = push(hooked, GH_REL)
    assert code == 0, out
    assert out == {
        "path": GH_REL,
        "branch": "topic",
        "upstream": "origin/topic",
        "upstream_set": True,
    }
    assert on_remote(on_github, "topic") == head
    tracked = fixture_git(repo, "for-each-ref", "--format=%(upstream)", "refs/heads/topic")
    assert tracked.strip() == "refs/remotes/origin/topic"


def test_a_branch_with_an_upstream_is_pushed_there(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    assert set_token(hooked, github.token)[0] == 0
    head = commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL)
    assert code == 0, out
    assert out == {
        "path": GH_REL,
        "branch": "main",
        "upstream": "origin/main",
        "upstream_set": False,
    }
    assert on_remote(on_github, "main") == head


def test_a_push_to_github_is_refused_without_a_token(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = on_remote(on_github, "main")
    commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL)
    assert (code, out["error"]["code"]) == (1, "github_token_not_set"), out
    assert out["error"]["message"] == h.MESSAGES["github_token_not_set"]
    assert on_remote(on_github, "main") == before
    assert set_token(hooked, github.token)[0] == 0
    assert push(hooked, GH_REL)[0] == 0  # positive control: with the token it goes


def test_a_push_to_another_remote_needs_no_token(env, repo):
    head = commit_in(repo, "more.txt")
    assert ok(env, "github-status") == {"token": False}
    code, out = push(env, REPO_REL)
    assert code == 0, out
    assert out["upstream"] == "origin/main"
    assert on_remote(origin_of(repo), "main") == head


def test_a_token_github_refuses_comes_back_in_gits_words_without_it(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    wrong = "a-token-github-refuses"
    assert set_token(hooked, wrong)[0] == 0
    commit_in(repo, "more.txt")
    proc = run_helper(hooked, "git-push", "--", GH_REL)
    out = json.loads(proc.stdout)
    assert out["error"]["code"] == "git_failed", out
    assert any("Authentication failed" in line for line in out["error"]["git"]), out
    assert wrong not in proc.stdout + proc.stderr


def test_a_rejected_push_comes_back_in_gits_words(env, repo):
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    commit_in(repo, "local.txt")
    code, out = push(env, REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("rejected" in line for line in out["error"]["git"]), out


def test_a_branch_with_no_commit_yet_is_not_pushed_but_passes_the_check(env, repo):
    """Nothing to push before the first commit, which rc-helper says before git is
    asked; --check passes, as Commit & Push's commit makes that first commit."""
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "trunk")
    fixture_git(fresh, "remote", "add", "origin", str(origin_of(repo)))
    shown = ok(env, "git-status", "--", "projects/fresh")
    assert (shown["push_to_github"], shown["push_has_remote"]) == (False, True)
    code, out = push(env, "projects/fresh")
    assert (code, out["error"]["code"]) == (1, "no_commits_yet"), out
    assert out["error"]["message"] == h.MESSAGES["no_commits_yet"]
    assert ok(env, "git-push", "--check", "--", "projects/fresh") == {
        "path": "projects/fresh",
        "branch": "trunk",
    }
    head = commit_in(fresh, "first.txt")
    assert push(env, "projects/fresh")[0] == 0  # positive control: once committed, it goes
    assert on_remote(origin_of(repo), "trunk") == head


def test_a_branch_with_no_commit_yet_tracking_a_remote_of_another_name_has_a_remote(env, tmp_path):
    """A clone of an empty repository with -o: main tracks fork/main before its first
    commit, which git lists no ref of, and there is no origin."""
    bare = tmp_path / "empty.git"
    fixture_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    projects = Path(env["RC_HELPER_HOME"]) / "projects"
    fixture_git(projects, "clone", "-q", "-o", "fork", str(bare), "forked")
    shown = ok(env, "git-status", "--", "projects/forked")
    assert shown["branch"]["upstream"] == "fork/main"  # positive control: git says so
    assert shown["push_has_remote"] is True
    assert ok(env, "git-push", "--check", "--", "projects/forked") == {
        "path": "projects/forked",
        "branch": "main",
    }
    assert error_code(env, "git-push", "--", "projects/forked") == "no_commits_yet"
    head = commit_in(projects / "forked", "first.txt")
    assert push(env, "projects/forked")[0] == 0
    assert on_remote(bare, "main") == head


def test_a_branch_with_no_commit_yet_pushing_to_github_needs_the_token(hooked, github, tmp_path):
    """Before the first commit, where the branch pushes is read from its configuration:
    its push remote, here GitHub under a name other than origin."""
    name = f"unborn-{tmp_path.name}"
    github.bare("owner", name)
    fresh = Path(hooked["RC_HELPER_HOME"]) / "projects" / "unborn"
    fresh.mkdir(parents=True)
    fixture_git(fresh, "init", "-q", "-b", "trunk")
    fixture_git(fresh, "remote", "add", "hub", github.github_url("owner", name))
    fixture_git(fresh, "config", "branch.trunk.remote", "hub")
    fixture_git(fresh, "config", "branch.trunk.merge", "refs/heads/trunk")
    shown = ok(hooked, "git-status", "--", "projects/unborn")
    assert shown["branch"]["upstream"] == "hub/trunk"  # positive control
    assert (shown["push_has_remote"], shown["push_to_github"]) == (True, True)
    assert shown["github_token"] is False
    assert error_code(hooked, "git-push", "--check", "--", "projects/unborn") == (
        "github_token_not_set"
    )
    fixture_git(fresh, "config", "branch.trunk.pushRemote", "elsewhere")
    assert ok(hooked, "git-status", "--", "projects/unborn")["push_to_github"] is False


def test_a_detached_head_is_not_pushed(env, repo):
    commit_in(repo, "more.txt")
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    before = on_remote(origin_of(repo), "main")
    code, out = push(env, REPO_REL)
    assert (code, out["error"]["code"]) == (1, "detached_head"), out
    assert on_remote(origin_of(repo), "main") == before


def test_check_says_whether_a_push_could_start_and_pushes_nothing(env, hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = on_remote(on_github, "main")
    commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL, "--check")
    assert (code, out["error"]["code"]) == (1, "github_token_not_set"), out
    assert set_token(hooked, github.token)[0] == 0
    assert ok(hooked, "git-push", "--check", "--", GH_REL) == {"path": GH_REL, "branch": "main"}
    assert on_remote(on_github, "main") == before
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert error_code(hooked, "git-push", "--check", "--", GH_REL) == "detached_head"


def test_status_says_whether_a_push_goes_to_github(env, hooked, repo, on_github, tmp_path):
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is True
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is False
    gh_repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    fixture_git(gh_repo, "switch", "-q", "-c", "no-upstream")
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is True  # to origin
    fixture_git(gh_repo, "switch", "-q", "main")
    elsewhere = tmp_path / "elsewhere.git"
    fixture_git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    fixture_git(gh_repo, "remote", "set-url", "--push", "origin", str(elsewhere))
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is False
    assert ok(hooked, "git-push", "--check", "--", GH_REL)["branch"] == "main"  # no token asked


def test_a_push_remote_of_its_own_decides_where_a_branch_is_pushed(hooked, repo, on_github):
    """A branch tracking a remote that is not GitHub, with GitHub as its push remote."""
    gh_url = fixture_git(Path(hooked["RC_HELPER_HOME"]) / GH_REL, "remote", "get-url", "origin")
    fixture_git(repo, "remote", "add", "github", gh_url.strip())
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is False
    fixture_git(repo, "config", "branch.main.pushRemote", "github")
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is True
    assert error_code(hooked, "git-push", "--", REPO_REL) == "github_token_not_set"


def test_a_repository_with_no_remote_is_no_push_to_github(env):
    repo = make_repo(Path(env["RC_HELPER_HOME"]) / "projects" / "lonely")
    assert ok(env, "git-status", "--", "projects/lonely")["push_to_github"] is False
    commit_in(repo, "x.txt")
    assert error_code(env, "git-push", "--", "projects/lonely") == "no_push_remote"


def test_a_branch_with_no_upstream_and_no_origin_has_nowhere_to_push(env, tmp_path):
    """A branch with no upstream is pushed to origin: with no remote of that name,
    git push -u origin HEAD cannot work, and rc-helper says so before git is asked,
    in status and in a push, --check too."""
    repo = make_repo(Path(env["RC_HELPER_HOME"]) / "projects" / "lonely")
    commit_in(repo, "x.txt")
    shown = ok(env, "git-status", "--", "projects/lonely")
    assert shown["push_has_remote"] is False
    code, out = push(env, "projects/lonely")
    assert (code, out["error"]["code"]) == (1, "no_push_remote"), out
    assert out["error"]["message"] == h.MESSAGES["no_push_remote"]
    assert error_code(env, "git-push", "--check", "--", "projects/lonely") == "no_push_remote"
    # A branch tracking a remote of another name is pushed there: it has a remote.
    elsewhere = tmp_path / "elsewhere.git"
    fixture_git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    fixture_git(repo, "remote", "add", "elsewhere", str(elsewhere))
    fixture_git(repo, "push", "-q", "-u", "elsewhere", "main")
    assert ok(env, "git-status", "--", "projects/lonely")["push_has_remote"] is True
    head = commit_in(repo, "y.txt")
    assert push(env, "projects/lonely")[0] == 0
    assert on_remote(elsewhere, "main") == head
    # A branch with no upstream again: nowhere, until there is an origin.
    fixture_git(repo, "switch", "-q", "-c", "topic")
    assert ok(env, "git-status", "--", "projects/lonely")["push_has_remote"] is False
    fixture_git(repo, "remote", "rename", "elsewhere", "origin")
    assert ok(env, "git-status", "--", "projects/lonely")["push_has_remote"] is True
    assert ok(env, "git-push", "--", "projects/lonely")["upstream"] == "origin/topic"


def test_status_says_a_push_has_a_remote_where_it_goes_to_origin_or_an_upstream(env, repo):
    assert status(env)["push_has_remote"] is True  # main tracks origin/main
    fixture_git(repo, "switch", "-q", "-c", "no-upstream")
    assert status(env)["push_has_remote"] is True  # to origin, which there is


def test_with_no_branch_checked_out_a_push_has_a_remote_only_with_an_origin(env, repo):
    """No branch is a push to origin too, as a branch with no upstream is; git-push
    refuses it as detached_head first."""
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert status(env)["push_has_remote"] is True  # positive control: there is an origin
    fixture_git(repo, "remote", "remove", "origin")
    assert status(env)["push_has_remote"] is False
    assert error_code(env, "git-push", "--", REPO_REL) == "detached_head"


SESSION_DUMP = """#!/usr/bin/python3
import json, os
sid = os.getsid(0)
helper = open(f"/proc/{sid}/stat").read().rsplit(")", 1)[1].split()[1]
seen = []
for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
    try:
        if os.getsid(int(pid)) != sid and pid != helper:
            continue
        seen.append([open(f"/proc/{pid}/{part}", "rb").read().decode("utf-8", "replace")
                     for part in ("cmdline", "environ")])
    except OSError:
        pass
json.dump(seen, open(OUT, "w"))
"""


def test_while_a_push_runs_no_process_holds_the_token(hooked, github, on_github, tmp_path):
    """Every process of the push's session, and rc-helper itself, read from /proc by a
    pre-push hook while git's HTTP helper is talking to the server."""
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    dumped = tmp_path / "seen.json"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(SESSION_DUMP.replace("OUT", repr(str(dumped))))
    hook.chmod(0o755)
    assert set_token(hooked, github.token)[0] == 0
    commit_in(repo, "more.txt")
    assert push(hooked, GH_REL)[0] == 0
    seen = json.loads(dumped.read_text())
    commands = [cmdline for cmdline, _ in seen]
    assert any("remote-http" in c for c in commands), commands  # positive control
    assert any("git-push" in c for c in commands), commands  # rc-helper itself
    assert all(github.token not in cmdline + environ for cmdline, environ in seen)


# --- the token in what git is given ------------------------------------------------------


def started_gits(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every git rc-helper starts in this process: its argv and its environment, as
    git_run.Git hands them to Popen - global -c options included, which GIT_TRACE
    leaves out."""
    seen = []
    real = git_run.Git

    def popen(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return subprocess.Popen(argv, **kwargs)

    monkeypatch.setattr(git_run, "Git", lambda env, popen_=None: real(env, popen=popen))
    return seen


def test_the_token_is_in_no_argv_or_environment_git_is_started_with(
    hooked, github, on_github, monkeypatch
):
    seen = started_gits(monkeypatch)
    helper_ = in_process(hooked)
    helper_.github_token(github.token.encode() + b"\n")
    helper_.github_status()
    commit_in(Path(hooked["RC_HELPER_HOME"]) / GH_REL, "more.txt")
    helper_.git_push(GH_REL, False)
    helper_.github_forget()
    argvs = [argv for argv, _ in seen]
    assert any("credential-store" in argv for argv in argvs)  # positive controls
    assert any("push" in argv for argv in argvs)
    for argv, env in seen:
        assert not any(github.token in part for part in argv), argv
        assert not any(github.token in value for value in env.values()), argv


def test_the_argv_check_sees_a_global_option(hooked, monkeypatch, tmp_path):
    """Planted twin: a token given to git as a global -c option is in the argv recorded."""
    seen = started_gits(monkeypatch)
    marker = "PLANTED-TOKEN-IN-A-GLOBAL-OPTION"
    home = Path(hooked["RC_HELPER_HOME"])
    git_run.Git(h.git_env(h.config_from_env(hooked))).run(
        home, ["-c", f"rcman.t={marker}", "config", "rcman.t"], deadline=time.monotonic() + 30
    )
    assert any(marker in part for argv, _ in seen for part in argv)


def recorded_git_calls(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every call rc-helper's verbs make to git, with the options they make it with."""
    calls = []
    real = h.Helper._git

    def recording(self, repo, args, deadline, **kwargs):
        calls.append((list(args), kwargs))
        return real(self, repo, args, deadline, **kwargs)

    monkeypatch.setattr(h.Helper, "_git", recording)
    return calls


def test_what_git_answers_about_the_token_is_kept_out_of_refusals(hooked, monkeypatch):
    """The credential-store calls that are given or answer with the token keep git's
    standard output out of any refusal (secret_output)."""
    calls = recorded_git_calls(monkeypatch)
    helper_ = in_process(hooked)
    helper_.github_token(TOKEN.encode() + b"\n")
    helper_.github_status()
    secret = {
        tuple(args[:2]): kwargs.get("secret_output")
        for args, kwargs in calls
        if args[:2] in (["credential-store", "get"], ["credential-store", "store"])
    }
    assert secret == {("credential-store", "store"): True, ("credential-store", "get"): True}


def test_the_token_goes_where_gits_store_helper_keeps_credentials(hooked, github):
    """git's store helper writes to ~/.git-credentials, or to ~/.config/git/credentials
    when only that exists; rc-helper's git has no XDG_CONFIG_HOME, as the server's does
    not, so that is the place."""
    home = Path(hooked["RC_HELPER_HOME"])
    xdg = home / ".config" / "git" / "credentials"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("")
    xdg.chmod(0o600)
    assert set_token(hooked)[0] == 0
    assert TOKEN in xdg.read_text()
    assert not git_credentials(hooked).exists()
    assert ok(hooked, "github-status") == {"token": True}
    assert "XDG_CONFIG_HOME" not in h.git_env(h.config_from_env(hooked))


def test_a_github_address_naming_a_user_is_not_rcmans_to_push_with_its_token(
    hooked, github, on_github
):
    """https://someone@github.com/... makes git ask for that user's password, which rcman's
    token is not stored under, so rcman does not count it as GitHub. Nothing is pushed:
    the hook does not rewrite such an address, which would reach the real GitHub."""
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    url = fixture_git(repo, "remote", "get-url", "origin").strip()
    fixture_git(repo, "remote", "set-url", "origin", url.replace("https://", "https://someone@"))
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is False
    assert ok(hooked, "git-push", "--check", "--", GH_REL)["branch"] == "main"


def test_status_says_whether_a_token_is_stored_for_a_push_to_github(
    env, hooked, github, repo, on_github
):
    def token(rc_env, rel):
        return ok(rc_env, "git-status", "--", rel)["github_token"]

    assert token(hooked, GH_REL) is False
    assert token(hooked, REPO_REL) is None  # not a push to GitHub: not asked
    assert set_token(hooked, github.token)[0] == 0
    assert token(hooked, GH_REL) is True
    assert set_token(hooked, "a-token-github-refuses")[0] == 0
    commit_in(Path(hooked["RC_HELPER_HOME"]) / GH_REL, "more.txt")
    assert push(hooked, GH_REL)[1]["error"]["code"] == "git_failed"
    assert token(hooked, GH_REL) is False  # git forgot the token GitHub refused


# --- clone ----------------------------------------------------------------------------

import shutil  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402

from tests.git_fixtures import BODY  # noqa: E402

# Addresses GITHUB_URL accepts, each with the folder a clone of it makes.
GITHUB_URLS_ACCEPTED = [
    ("https://github.com/owner/repo", "repo"),
    ("https://github.com/owner/repo.git", "repo"),
    ("https://github.com/owner/repo/", "repo"),
    ("https://github.com/owner/repo.git/", "repo"),
    ("https://github.com/Owner-1/Mixed_Case.Repo", "Mixed_Case.Repo"),
    ("https://github.com/o/-r", "-r"),
    ("https://github.com/_o/_r", "_r"),
    ("https://github.com/o/r.git.git", "r.git"),
    ("https://github.com/o/git", "git"),
    ("https://github.com/o/r..x", "r..x"),
    ("https://github.com/0/9", "9"),
]
GITHUB_URLS_REFUSED = [
    "",
    "http://github.com/o/r",
    "HTTPS://github.com/o/r",
    "https://GitHub.com/o/r",
    "https://www.github.com/o/r",
    "https://github.com.example.com/o/r",
    "https://github.com:443/o/r",
    "https://user@github.com/o/r",
    "https://user:a-token@github.com/o/r",
    "https://github.com/o/r?x=1",
    "https://github.com/o/r#a",
    "https://github.com/o/r/tree/main",
    "https://github.com/o",
    "https://github.com/o/",
    "https://github.com//r",
    "https://github.com/o//r",
    "https://github.com/o/r//",
    "https://github.com/o/.github",
    "https://github.com/o/.git",
    "https://github.com/-o/r",
    "https://github.com/o.x/r",
    "https://github.com/o%2Fx/r",
    "https://github.com/o/%2e",
    "https://github.com/o/r%20x",
    "https://github.com/o/r x",
    "https://github.com/o/résumé",
    "https://github.com/o/r\n",
    "https://github.com/o/r\nhttps://github.com/o/s",
    "https://github.com/o/r\x00",
    " https://github.com/o/r",
    "https://github.com\\o\\r",
    "-https://github.com/o/r",
    "--upload-pack=x",
    "ext::sh -c x",
    "file:///etc",
    "git@github.com:o/r.git",
    "ssh://git@github.com/o/r.git",
]


@pytest.mark.parametrize(("url", "folder"), GITHUB_URLS_ACCEPTED)
def test_the_github_address_rule_accepts_a_repositorys_address(url, folder):
    assert h.GITHUB_URL.fullmatch(url)
    assert h.clone_folder_name(url) == folder


@pytest.mark.parametrize("url", GITHUB_URLS_REFUSED)
def test_the_github_address_rule_refuses_anything_else(url):
    assert not h.GITHUB_URL.fullmatch(url)


def test_the_longest_owner_and_name_are_accepted_and_no_longer():
    owner, name = "o" * h.MAX_GITHUB_OWNER_LENGTH, "r" * h.MAX_GITHUB_REPOSITORY_LENGTH
    for url in (f"https://github.com/{owner}/{name}", f"https://github.com/{owner}/{name}.git/"):
        assert h.GITHUB_URL.fullmatch(url), url
        assert h.clone_folder_name(url) == name
    assert not h.GITHUB_URL.fullmatch(f"https://github.com/{owner}o/{name}")
    assert not h.GITHUB_URL.fullmatch(f"https://github.com/{owner}/{name}r")


def test_the_folder_a_clone_makes_is_the_one_git_names(tmp_path):
    """Each accepted address, cloned with real git into a scratch folder, from a local
    repository it is pointed at by insteadOf, makes the folder clone_folder_name says."""
    source = make_repo(tmp_path / "source")
    for url, folder in GITHUB_URLS_ACCEPTED:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        fixture_git(scratch, "-c", f"url.{source}.insteadOf={url}", "clone", "-q", "--", url)
        assert os.listdir(scratch) == [h.clone_folder_name(url)] == [folder], url
        shutil.rmtree(scratch)


@pytest.fixture
def published(github, tmp_path):
    """A public repository on the tests' GitHub holding main and feature: its address,
    without .git."""
    name = f"pub-{tmp_path.name}"
    make_repo(tmp_path / "published", origin=github.bare("owner", name))
    return f"https://github.com/owner/{name}"


def clone(env, url, *parent):
    return helper(env, "git-clone", f"--url={url}", "--", *parent)


def folder_of(url: str) -> str:
    return url.removesuffix("/").rsplit("/", 1)[1].removesuffix(".git")


def asked_for(github, name: str, since: int) -> list[str]:
    """The tests' GitHub's requests for the repository ``name`` since ``since``."""
    return [p for _, p in github.requests[since:] if p.startswith(f"/owner/{name}.git/")]


@pytest.mark.parametrize("suffix", ["", ".git", "/", ".git/"])
def test_a_clone_makes_the_folder_git_names_holding_the_repository(
    hooked, github, published, suffix
):
    home = Path(hooked["RC_HELPER_HOME"])
    url, name = published + suffix, folder_of(published)
    before = len(github.requests)
    assert ok(hooked, "git-clone", f"--url={url}", "--", "projects") == {
        "created": f"projects/{name}"
    }
    assert asked_for(github, name, before)  # positive control: it came from the tests' GitHub
    clone_ = home / "projects" / name
    assert (clone_ / "README.md").read_text() == BODY
    assert fixture_git(clone_, "config", "remote.origin.url").strip() == url
    assert branch_of(clone_) == "main"
    marked = {e["path"]: e["repository"] for e in ok(hooked, "tree", "projects")["entries"]}
    assert marked[f"projects/{name}"] is True


def test_a_clone_without_a_parent_is_made_in_the_home_folder(hooked, published):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    assert ok(hooked, "git-clone", f"--url={published}") == {"created": name}
    assert (home / name / ".git").is_dir()


@pytest.mark.parametrize("kind", ["empty", "full", "file", "dangling"])
def test_a_clone_where_something_has_its_name_is_refused_and_leaves_it(
    hooked, github, published, kind
):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    there = home / "projects" / name
    if kind in ("empty", "full"):
        there.mkdir()
        if kind == "full":
            write(there / "mine.txt", "mine\n")
    elif kind == "file":
        write(there, "mine\n")
    else:
        there.symlink_to(home / "nowhere")
    inode = os.lstat(there).st_ino
    before = len(github.requests)
    code, out = clone(hooked, published, "projects")
    assert (code, out["error"]["code"]) == (1, "folder_exists"), out
    assert os.lstat(there).st_ino == inode
    assert asked_for(github, name, before) == []
    if kind == "full":
        assert os.listdir(there) == ["mine.txt"]
    elif kind == "empty":
        assert os.listdir(there) == []
    assert not (home / "nowhere").exists()
    assert ok(hooked, "git-clone", f"--url={published}") == {"created": name}  # positive control


def test_a_clone_git_refuses_leaves_nothing(hooked, github):
    home = Path(hooked["RC_HELPER_HOME"])
    before = set(os.listdir(home / "projects"))
    code, out = clone(hooked, "https://github.com/owner/no-such-repository", "projects")
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert out["error"]["git"] and "left" not in out["error"]
    assert set(os.listdir(home / "projects")) == before


def test_a_private_repository_is_cloned_with_the_stored_token_only(hooked, github, tmp_path):
    home = Path(hooked["RC_HELPER_HOME"])
    name = f"private-{tmp_path.name}"
    make_repo(tmp_path / "private", origin=github.bare("owner", name, private=True))
    url = f"https://github.com/owner/{name}"
    for token in (None, "a-token-github-refuses"):
        if token:
            assert set_token(hooked, token)[0] == 0
        code, out = clone(hooked, url, "projects")
        assert (code, out["error"]["code"]) == (1, "git_failed"), (token, out)
        assert out["error"]["git"] and "left" not in out["error"]
        assert not (home / "projects" / name).exists()
    assert set_token(hooked, github.token)[0] == 0
    assert ok(hooked, "git-clone", f"--url={url}", "--", "projects") == {
        "created": f"projects/{name}"
    }
    clone_ = home / "projects" / name
    assert (clone_ / "README.md").is_file()
    assert (h.GITHUB_USERNAME, True) in github.attempts  # positive control: the token was used
    for path in (clone_ / ".git").rglob("*"):
        if path.is_file():
            assert github.token.encode() not in path.read_bytes(), path


def test_the_token_is_in_no_argv_or_environment_of_a_private_clone(
    hooked, github, monkeypatch, tmp_path
):
    name = f"private-{tmp_path.name}"
    make_repo(tmp_path / "private", origin=github.bare("owner", name, private=True))
    seen = started_gits(monkeypatch)
    helper_ = in_process(hooked)
    helper_.github_token(github.token.encode() + b"\n")
    helper_.git_clone("projects", f"https://github.com/owner/{name}")
    assert any("clone" in argv for argv, _ in seen)  # positive control
    for argv, env in seen:
        assert not any(github.token in part for part in argv), argv
        assert not any(github.token in value for value in env.values()), argv


def persons_hook(env, name: str, script: str) -> None:
    """A hook of the person's own, from core.hooksPath in their ~/.gitconfig, which a
    clone runs: a new repository has no hooks of its own."""
    home = Path(env["RC_HELPER_HOME"])
    hooks = home / ".git-hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / name).write_text(script)
    (hooks / name).chmod(0o755)
    gitconfig = str(home / ".gitconfig")
    subprocess.run(["git", "config", "--file", gitconfig, "core.hooksPath", str(hooks)], check=True)


def gone(pid: int, within: float = 10) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_git_clones_the_address_into_the_folder_it_runs_in_the_one_made(
    hooked, published, tmp_path
):
    out = tmp_path / "argv.json"
    persons_hook(
        hooked,
        "post-checkout",
        "#!/usr/bin/python3\nimport json, os\n"
        "git = os.getppid()\n"
        "helper = open(f'/proc/{git}/stat').read().rsplit(')', 1)[1].split()[1]\n"
        "argv = lambda pid: open(f'/proc/{pid}/cmdline', 'rb').read().decode().split('\\0')[:-1]\n"
        "cwd = os.readlink(f'/proc/{git}/cwd')\n"
        f"json.dump({{'git': argv(git), 'helper': argv(helper), 'cwd': cwd}}, "
        f"open({str(out)!r}, 'w'))\n",
    )
    ok(hooked, "git-clone", f"--url={published}", "--", "projects")
    seen = json.loads(out.read_text())
    assert seen["git"] == [git_run.find_git(), "clone", "--", published, "."]
    assert seen["helper"][-4:] == ["git-clone", f"--url={published}", "--", "projects"]
    made = Path(hooked["RC_HELPER_HOME"]).resolve() / "projects" / folder_of(published)
    assert seen["cwd"] == str(made)


def test_a_clone_past_its_deadline_is_git_timeout_and_leaves_nothing(
    hooked, published, tmp_path, monkeypatch, capsys
):
    home = Path(hooked["RC_HELPER_HOME"])
    pid_file = tmp_path / "hook.pid"
    persons_hook(hooked, "post-checkout", f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    monkeypatch.setattr(h, "CLONE_TIMEOUT", 4.0)  # read when the verb is called
    monkeypatch.setattr(h, "CLONE_CLEANUP_SECONDS", 1.0)
    began = time.monotonic()
    status, out = answer_of(hooked, capsys, "git-clone", f"--url={published}", "--", "projects")
    assert time.monotonic() - began < 15
    assert (status, out["error"]["code"]) == (1, "git_timeout"), out
    assert "left" not in out["error"]
    assert gone(int(pid_file.read_text()))  # the hook ran, and was ended
    assert not (home / "projects" / folder_of(published)).exists()


def test_git_has_the_clones_deadline_less_the_time_to_clean_up(hooked, published, monkeypatch):
    deadlines = []
    real = h.Helper._git

    def recording(self, repo, args, deadline, **kwargs):
        deadlines.append((args[0], deadline - time.monotonic()))
        return real(self, repo, args, deadline, **kwargs)

    monkeypatch.setattr(h.Helper, "_git", recording)
    in_process(hooked).git_clone("projects", published)
    [(verb, left)] = deadlines
    assert verb == "clone"
    expected = h.CLONE_TIMEOUT - h.CLONE_CLEANUP_SECONDS
    assert expected - 5 < left <= expected


def test_sigterm_during_a_clone_ends_git_and_leaves_nothing(hooked, published, tmp_path):
    home = Path(hooked["RC_HELPER_HOME"])
    pid_file = tmp_path / "hook.pid"
    persons_hook(hooked, "post-checkout", f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    proc = subprocess.Popen(
        [str(HELPER), "git-clone", f"--url={published}", "--", "projects"],
        env=hooked,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the hook never ran"
        time.sleep(0.05)
    assert (home / "projects" / folder_of(published) / ".git").is_dir()  # positive control
    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=20)
    assert stdout == b""  # no answer: the backend answers helper_timeout
    assert gone(int(pid_file.read_text()))
    assert not (home / "projects" / folder_of(published)).exists()


def test_a_sigterm_while_a_failed_clone_is_removed_waits_for_the_removal(
    hooked, published, monkeypatch, capsys
):
    """The SIGTERM arrives as the clone a hook failed starts being removed; it takes
    effect once the removal is done."""
    home = Path(hooked["RC_HELPER_HOME"])
    persons_hook(hooked, "post-checkout", "#!/bin/sh\nexit 1\n")
    real = shutil.rmtree
    removing = []

    def rmtree(*args, **kwargs):
        removing.append(args)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        return real(*args, **kwargs)

    rmtree.avoids_symlink_attacks = real.avoids_symlink_attacks
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(SystemExit):
        answer_of(hooked, capsys, "git-clone", f"--url={published}", "--", "projects")
    assert removing  # positive control: the removal started
    assert not (home / "projects" / folder_of(published)).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_a_failed_clone_that_cannot_be_removed_is_reported_as_left(hooked, published):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    persons_hook(
        hooked, "post-checkout", "#!/bin/sh\nmkdir kept && touch kept/x && chmod 555 kept\nexit 1\n"
    )
    code, out = clone(hooked, published, "projects")
    kept = home / "projects" / name / "kept"
    try:
        assert (code, out["error"]["code"]) == (1, "git_failed"), out
        assert out["error"]["left"] == [f"projects/{name}"]
        assert (kept / "x").exists()
    finally:
        if kept.exists():
            kept.chmod(0o755)


def test_a_second_clone_of_the_same_repository_at_once_is_refused(
    hooked, github, published, tmp_path
):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    started, release = tmp_path / "started", tmp_path / "release"
    persons_hook(
        hooked,
        "post-checkout",
        f"#!/bin/sh\ntouch {started}\nwhile [ ! -e {release} ]; do sleep 0.05; done\n",
    )
    first = subprocess.Popen(
        [str(HELPER), "git-clone", f"--url={published}", "--", "projects"],
        env=hooked,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not started.exists():
            assert time.monotonic() < deadline, "the first clone never checked out"
            time.sleep(0.05)
        code, out = clone(hooked, published, "projects")
        assert (code, out["error"]["code"]) == (1, "folder_exists"), out
    finally:
        release.touch()
        stdout, _ = first.communicate(timeout=30)
    assert json.loads(stdout) == {"created": f"projects/{name}"}
    clone_ = home / "projects" / name
    assert (clone_ / "README.md").read_text() == BODY
    assert fixture_git(clone_, "status", "--porcelain") == ""


def test_a_clone_git_made_somewhere_else_is_refused_and_rcmans_folder_removed(
    hooked, published, monkeypatch, tmp_path
):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = h.Helper._git

    def misdirected(self, repo, args, deadline, **kwargs):
        return real(self, elsewhere if args[:1] == ["clone"] else repo, args, deadline, **kwargs)

    monkeypatch.setattr(h.Helper, "_git", misdirected)
    assert refusal(in_process(hooked).git_clone, "projects", published).code == "clone_missing"
    assert (elsewhere / ".git").is_dir()  # positive control: git did clone, there
    assert not (home / "projects" / name).exists()


def test_a_local_path_named_like_the_address_in_the_parent_changes_nothing(
    hooked, github, published
):
    """git runs in the empty folder made, so a path named like the address, such as a
    repository at https:/github.com/owner/<name> in the parent, is not where git looks:
    the clone comes from GitHub, and that repository is left as it is."""
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    twin = make_repo(home / "projects" / "https:" / "github.com" / "owner" / name)
    head = fixture_git(twin, "rev-parse", "HEAD")
    before = len(github.requests)
    assert ok(hooked, "git-clone", f"--url={published}", "--", "projects") == {
        "created": f"projects/{name}"
    }
    assert asked_for(github, name, before)
    assert fixture_git(twin, "rev-parse", "HEAD") == head


def test_a_clone_is_made_only_inside_the_home_folder(hooked, github, published, tmp_path):
    home = Path(hooked["RC_HELPER_HOME"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "projects" / "out").symlink_to(outside)
    write(home / "projects" / "a-file")
    before = len(github.requests)
    for parent, code in (
        ("projects/out", "path_outside_home"),
        ("..", "path_outside_home"),
        ("/etc", "path_invalid"),
        ("projects/a-file", "path_not_directory"),
        ("projects/none", "path_not_found"),
    ):
        assert clone(hooked, published, parent)[1]["error"]["code"] == code, parent
    assert os.listdir(outside) == []
    assert asked_for(github, folder_of(published), before) == []


def test_a_clone_is_made_only_where_the_path_is_as_on_disk(hooked, published, monkeypatch):
    home = Path(hooked["RC_HELPER_HOME"])
    listed_as(monkeypatch, home / "projects", "beta", "Beta")
    helper_ = in_process(hooked)
    assert refusal(helper_.git_clone, "projects/beta", published).code == "path_name_differs"
    assert os.listdir(home / "projects" / "beta") == ["inner"]
    assert helper_.git_clone("projects/alpha", published)["created"].startswith("projects/alpha/")


def test_an_address_is_refused_before_anything_else_is_looked_at(hooked, github, monkeypatch):
    before = len(github.requests)
    for parent in ("projects", "/etc", "../outside", "projects/none"):
        code, out = clone(hooked, "https://user:a-token@github.com/owner/r", parent)
        assert (code, out["error"]["code"]) == (1, "url_invalid"), (parent, out)
        assert "a-token" not in json.dumps(out)
    monkeypatch.setattr(h, "resolve_folder", lambda *a, **k: pytest.fail("a path was resolved"))
    error = refusal(in_process(hooked).git_clone, "projects", "file:///etc")
    assert error.code == "url_invalid"
    assert len(github.requests) == before


def test_a_clone_is_refused_where_a_failed_one_could_not_be_removed_safely(
    hooked, published, monkeypatch
):
    home = Path(hooked["RC_HELPER_HOME"])
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    assert refusal(in_process(hooked).git_clone, "projects", published).code == "clone_unsafe"
    assert not (home / "projects" / folder_of(published)).exists()


def test_a_clone_has_time_for_the_network_and_to_clean_up_before_rcman_gives_up():
    from rcman import helpers_client

    assert h.CLONE_TIMEOUT > helpers_client.HELPER_TIMEOUT
    assert 0 < h.CLONE_CLEANUP_SECONDS
    assert h.CLONE_TIMEOUT - h.CLONE_CLEANUP_SECONDS >= h.GIT_NETWORK_TIMEOUT


def test_a_clone_leaves_submodules_uncloned_even_where_the_person_asks_for_them(
    hooked, github, tmp_path
):
    """git clone -- <url> does not clone submodules, and the person's submodule.recurse
    does not make it: git's clone is the one command that setting does not reach."""
    from tests.git_fixtures import add_submodule

    home = Path(hooked["RC_HELPER_HOME"])
    sub, name = f"sub-{tmp_path.name}", f"super-{tmp_path.name}"
    make_repo(tmp_path / "sub", origin=github.bare("owner", sub))
    upper = make_repo(tmp_path / "super")
    add_submodule(upper, tmp_path / "sub", "lib")
    fixture_git(upper, "config", "-f", ".gitmodules", "submodule.lib.url", github_url_of(sub))
    fixture_git(upper, "commit", "-q", "-am", "Add lib")
    bare = github.bare("owner", name)
    fixture_git(upper, "push", "-q", str(bare), "main")
    gitconfig = str(home / ".gitconfig")
    subprocess.run(["git", "config", "--file", gitconfig, "submodule.recurse", "true"], check=True)
    ok(hooked, "git-clone", f"--url={github_url_of(name)}", "--", "projects")
    clone_ = home / "projects" / name
    assert (clone_ / ".gitmodules").is_file()  # positive control: it has a submodule
    assert os.listdir(clone_ / "lib") == []


def github_url_of(name: str) -> str:
    return f"https://github.com/owner/{name}"


def test_status_says_how_far_ahead_of_where_it_pushes_the_branch_is(env, repo, tmp_path):
    """Measured against the push destination (@{push}) when there is one, the upstream
    otherwise, and null with neither."""
    assert status(env)["push_ahead"] == 0  # main is where origin/main is
    commit_in(repo, "more.txt")
    assert status(env)["push_ahead"] == 1  # against the upstream
    mirror = tmp_path / "mirror.git"
    fixture_git(tmp_path, "init", "-q", "--bare", str(mirror))
    fixture_git(repo, "remote", "add", "mirror", str(mirror))
    fixture_git(repo, "push", "-q", "mirror", "HEAD~1:refs/heads/main")
    fixture_git(repo, "fetch", "-q", "mirror")
    fixture_git(repo, "push", "-q", "origin", "main")
    fixture_git(repo, "fetch", "-q", "origin")
    shown = status(env)
    assert shown["branch"]["ahead"] == 0  # the upstream has it all
    assert shown["push_ahead"] == 0  # no push remote of its own yet: the upstream
    fixture_git(repo, "config", "branch.main.pushRemote", "mirror")
    # git names no @{push} for push.default simple when it differs from the upstream,
    # so it is the upstream's count still; with current, @{push} is mirror/main.
    assert status(env)["push_ahead"] == 0
    fixture_git(repo, "config", "push.default", "current")
    assert status(env)["push_ahead"] == 1  # the push destination, mirror/main, lacks one
    fixture_git(repo, "switch", "-q", "-c", "no-upstream")
    fixture_git(repo, "config", "--unset", "branch.main.pushRemote")
    fixture_git(repo, "config", "--unset", "push.default")
    assert status(env)["push_ahead"] is None


@pytest.mark.parametrize("impostor", ["holding a file", "empty"])
def test_a_folder_put_where_the_clone_was_is_left_and_the_clone_emptied(
    hooked, published, impostor
):
    """A hook moves the folder rcman made away and puts another of the same name in
    its place, then fails the clone: the folder rcman made is emptied where it went,
    and reported as left; the other is not touched."""
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    make = f"mkdir {name} && touch {name}/mine" if impostor == "holding a file" else f"mkdir {name}"
    persons_hook(
        hooked, "post-checkout", f"#!/bin/sh\ncd .. && mv {name} {name}.moved && {make}\nexit 1\n"
    )
    code, out = clone(hooked, published, "projects")
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    there, moved = home / "projects" / name, home / "projects" / f"{name}.moved"
    assert there.is_dir()
    assert os.listdir(there) == (["mine"] if impostor == "holding a file" else [])
    assert os.listdir(moved) == []  # the clone's files, removed through its own descriptor
    assert out["error"]["left"] == [f"projects/{name}.moved"]


def test_a_parent_swapped_for_a_link_once_opened_does_not_take_the_clone_there(
    hooked, published, monkeypatch, tmp_path
):
    """Once the parent is open, a part of its path replaced by a link to outside the home
    folder does not move the clone: git runs in the folder that was opened."""
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    outside = tmp_path / "outside"
    outside.mkdir()
    alpha, kept = home / "projects" / "alpha", home / "projects" / "alpha-real"
    real = h.open_dir

    def swapped(root, folder):
        fd = real(root, folder)
        alpha.rename(kept)
        alpha.symlink_to(outside)
        return fd

    monkeypatch.setattr(h, "open_dir", swapped)
    try:
        answer = in_process(hooked).git_clone("projects/alpha", published)
    finally:
        assert os.listdir(outside) == []
    assert answer == {"created": f"projects/alpha/{name}"}
    assert (kept / name / ".git").is_dir()  # in the folder that was opened


def test_a_sigterm_as_the_folder_is_made_leaves_no_folder(hooked, published, monkeypatch, capsys):
    home = Path(hooked["RC_HELPER_HOME"])
    real = os.mkdir
    made = []

    def mkdir(*args, **kwargs):
        real(*args, **kwargs)
        made.append(args)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)

    monkeypatch.setattr(os, "mkdir", mkdir)
    with pytest.raises(SystemExit):
        answer_of(hooked, capsys, "git-clone", f"--url={published}", "--", "projects")
    assert made  # positive control: the SIGTERM came as the folder was made
    assert not (home / "projects" / folder_of(published)).exists()


def test_a_second_sigterm_does_not_cut_short_what_the_first_set_going(env, monkeypatch, capsys):
    """rc-helper ends on the first SIGTERM; one more, while it ends, is not taken."""
    main = threading.main_thread().ident
    finished = []

    def git_clone(self, rel, url):
        try:
            signal.pthread_kill(main, signal.SIGTERM)
            time.sleep(5)
        except SystemExit:
            signal.pthread_kill(main, signal.SIGTERM)
            time.sleep(0.2)
            finished.append(True)
            raise

    monkeypatch.setattr(h.Helper, "git_clone", git_clone)
    with pytest.raises(SystemExit):
        answer_of(env, capsys, "git-clone", "--url=https://github.com/owner/r", "--", "projects")
    assert finished


def test_a_link_put_at_the_new_folders_name_does_not_take_the_clone_there(
    hooked, published, monkeypatch, tmp_path
):
    """The folder made is moved away and a link to an empty folder outside the home put
    at its name before git runs: git clones into the folder made, where it went, and
    nothing outside the home; that folder is emptied again and reported as left."""
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = h._make_folder

    def swapped(fd, folder, created):
        made = real(fd, folder, created)
        os.rename(folder, f"{folder}.moved", src_dir_fd=fd, dst_dir_fd=fd)
        os.symlink(outside, folder, dir_fd=fd)
        return made

    monkeypatch.setattr(h, "_make_folder", swapped)
    error = refusal(in_process(hooked).git_clone, "projects", published)
    assert os.listdir(outside) == []
    assert error.code == "clone_missing"
    assert error.detail["left"] == [f"projects/{name}.moved"]
    assert os.listdir(home / "projects" / f"{name}.moved") == []
    assert (home / "projects" / name).is_symlink()  # not rcman's: left alone


def replaced_as_made(monkeypatch, name: str, by) -> None:
    """os.mkdir makes ``name``, which is then moved to <name>.moved and ``by(fd)`` puts
    something else at its name, before rc-helper can open it."""
    real = os.mkdir

    def mkdir(path, *args, dir_fd=None, **kwargs):
        real(path, *args, dir_fd=dir_fd, **kwargs)
        if path == name and dir_fd is not None:
            os.rename(name, f"{name}.moved", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            by(dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir)


def test_a_link_put_at_the_name_before_the_folder_is_opened_is_folder_exists(
    hooked, published, monkeypatch, tmp_path
):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)
    outside = tmp_path / "outside"
    outside.mkdir()
    replaced_as_made(monkeypatch, name, lambda fd: os.symlink(outside, name, dir_fd=fd))
    error = refusal(in_process(hooked).git_clone, "projects", published)
    assert error.code == "folder_exists"
    assert error.detail["left"] == [f"projects/{name}"]  # made there, and moved away
    assert os.listdir(outside) == []
    assert (home / "projects" / name).is_symlink()


def test_a_folder_put_at_the_name_before_it_is_opened_is_folder_exists(
    hooked, published, monkeypatch
):
    home = Path(hooked["RC_HELPER_HOME"])
    name = folder_of(published)

    real_mkdir = os.mkdir

    def full_folder(fd):
        real_mkdir(name, dir_fd=fd)
        os.close(os.open(f"{name}/mine", os.O_CREAT | os.O_WRONLY, dir_fd=fd))

    replaced_as_made(monkeypatch, name, full_folder)
    error = refusal(in_process(hooked).git_clone, "projects", published)
    assert error.code == "folder_exists"
    assert error.detail["left"] == [f"projects/{name}"]
    assert os.listdir(home / "projects" / name) == ["mine"]


def test_a_failed_clone_moved_to_a_name_ending_in_deleted_is_still_reported(hooked, published):
    """The folder's name, not whether it is gone, makes /proc show " (deleted)" after a
    path; what is left is known from the folder itself."""
    name = folder_of(published)
    persons_hook(
        hooked, "post-checkout", f'#!/bin/sh\ncd .. && mv {name} "{name} (deleted)"\nexit 1\n'
    )
    code, out = clone(hooked, published, "projects")
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert out["error"]["left"] == [f"projects/{name} (deleted)"]
