"""A workspace's Claude Remote Control server: its tmux, its states and its verbs
(backend/rcman/claude_server.py), on the host runtime against real tmux and the fake
`claude`, through a real channel, as rcman runs them in a container."""

import contextlib
import dataclasses
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from rcman import claude_server as cs
from rcman import runtime as rt

from devtools import fake_claude as fake
from devtools import record_claude as rec
from devtools import tmuxctl
from devtools.held_folders import tmux_sockets
from helpers import screens
from tests.conftest import fake_claude
from tests.test_screens import RECONNECTED_SCENARIOS

REPO = Path(__file__).resolve().parent.parent
TMUX_CONF = REPO / "backend" / "rcman" / "tmux.conf"
NAME = rt.container_name(1)
# Short deadlines for a stop, so a test of what happens after them is quick.
QUICK_STOP = {"STOP_TIMEOUT": 1.0, "END_GRACE_SECONDS": 1.0}


@dataclass
class World:
    runtime: rt.HostRuntime
    layout: rt.Layout
    channel: object
    server: cs.Server
    claude: Path  # the store
    binary: Path  # the version in it that the server runs
    tmp_path: Path
    calls: list


def write_claude(root: Path, before: str = "", env: str = "FAKE_CLAUDE_READY_SECONDS=0.3") -> Path:
    """The store ``root`` with its one version running the fake, after ``before``."""
    return fake_claude(root, env=env, before=before)


@pytest.fixture
def world(tmp_path):
    claude = tmp_path / "claude"
    binary = write_claude(claude)
    root = tmp_path / "workspaces"
    root.mkdir()
    with tmux_sockets("rcs-") as sockets:
        runtime = rt.HostRuntime(root, sockets, claude, TMUX_CONF)
        runtime.create(rt.Spec(name=NAME, slot=1, workspace=1, person=1))
        channel = runtime.channel(NAME)
        calls = []

        def run(argv, timeout=cs.REQUEST_TIMEOUT):
            calls.append(list(argv))
            return channel.run(argv, timeout=timeout)

        layout = runtime.layout(NAME)
        try:
            yield World(
                runtime, layout, channel, cs.Server(layout, run, 1), claude, binary, tmp_path, calls
            )
        finally:
            channel.close()
            runtime.remove(NAME)


def log_in(world, trusted=False):
    folders = [world.layout.work] if trusted else []
    fake.write_state(Path(world.layout.config), logged_in=True, trusted=folders)


def refused(call, *args) -> cs.ServerError:
    with pytest.raises(cs.ServerError) as raised:
        call(*args)
    return raised.value


def wait_state(world, wanted, tries=100):
    server = None
    for _ in range(tries):
        server = world.server.status()
        if server and server["state"] == wanted:
            return server
        time.sleep(0.1)
    pytest.fail(f"never reached {wanted}; last: {server}")


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def quick_stop(monkeypatch):
    for name, value in QUICK_STOP.items():
        monkeypatch.setattr(cs, name, value)


# --- the lifecycle ---------------------------------------------------------------------


def test_no_server_until_one_is_started(world):
    assert world.server.status() is None
    for verb in (world.server.stop, world.server.forget, world.server.output):
        assert refused(verb).code == "unknown_server"
    assert refused(world.server.confirm, "y").code == "unknown_server"


def test_start_refuses_without_a_claude_sign_in(world):
    error = refused(world.server.start, "pfx")
    assert error.code == "claude_not_logged_in"
    assert world.server.status() is None  # nothing was started
    log_in(world)
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"  # positive control


def test_start_trust_confirm_ready_stop_resume_forget(world):
    log_in(world)
    started = world.server.start("pfx")
    assert started["server"]["state"] == "trust_prompt"
    assert started["server"]["workspace"] == 1

    confirmed = world.server.confirm("y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_state(world, "ready")
    assert ready["session_name"].startswith("pfx-")
    assert ready["environment_url"].startswith(rec.ENVIRONMENT_URL_PREFIX)

    assert refused(world.server.start, "pfx").code == "already_running"
    assert refused(world.server.resume, "pfx").code == "not_stopped"
    assert refused(world.server.forget).code == "still_running"

    stopped = world.server.stop()
    assert stopped["server"]["state"] == "stopped"
    assert refused(world.server.stop).code == "not_running"

    resumed = world.server.resume("pfx")
    assert resumed["server"]["state"] in ("connected", "ready")
    assert wait_state(world, "ready")["session_name"] == ready["session_name"]

    world.server.stop()
    assert world.server.forget() == {"forgotten": 1}
    assert world.server.status() is None


# Known to fail rarely, under load: see "Known flaky test" in project-knowledge/tests.md.
def test_a_server_in_a_git_repository_starts_stops_and_resumes(world):
    from tests.git_fixtures import fixture_git, make_repo

    log_in(world)
    repo = make_repo(Path(world.layout.work) / "repo")
    fixture_git(repo, "switch", "-q", "-c", "feature-x")
    for name in os.listdir(repo):
        os.rename(repo / name, Path(world.layout.work) / name)
    repo.rmdir()
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    world.server.confirm("y")
    ready = wait_state(world, "ready")
    screen = world.server.output()["screen"]
    assert any(line.endswith("· feature-x") for line in screen)
    assert not any("Spawn mode" in line for line in screen)
    assert world.server.stop()["server"]["state"] == "stopped"
    world.server.resume("pfx")
    assert wait_state(world, "ready")["session_name"] == ready["session_name"]


def test_confirm_refuses_when_nothing_is_asked(world):
    log_in(world, trusted=True)
    world.server.start("pfx")
    wait_state(world, "ready")
    assert refused(world.server.confirm, "y").code == "nothing_to_confirm"


def test_a_declined_trust_question_ends_the_server_and_start_starts_it_again(world):
    log_in(world)
    world.server.start("pfx")
    assert world.server.confirm("n")["server"]["state"] == "trust_declined"
    assert refused(world.server.resume, "pfx").code == "not_stopped"
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"


def test_a_server_that_died_can_be_resumed(world):
    log_in(world)
    world.server.start("pfx")
    world.server.confirm("y")
    ready = wait_state(world, "ready")
    _dead, pid = world.server.tmux.pane()
    os.kill(pid, signal.SIGKILL)
    died = wait_state(world, "exited")
    assert died["exit_code"] == 128 + signal.SIGKILL
    assert world.server.resume("pfx")["server"]["state"] in ("connected", "ready")
    assert wait_state(world, "ready")["session_name"] == ready["session_name"]


def test_a_server_whose_claude_finds_itself_signed_out_is_logged_out(world, tmp_path):
    log_in(world)
    signed_out = tmp_path / "signed-out"
    fake.write_state(signed_out, logged_in=False)
    write_claude(
        world.claude,
        before=f'if [ "$1" = remote-control ]; then export CLAUDE_CONFIG_DIR={signed_out}; fi\n',
    )
    world.server.start("pfx")
    wait_state(world, "logged_out")
    assert refused(world.server.resume, "pfx").code == "not_stopped"


def test_output_is_the_screen_and_the_server(world):
    log_in(world)
    world.server.start("pfx")
    out = world.server.output()
    assert any("[y/N]" in line for line in out["screen"])
    assert out["server"]["state"] == "trust_prompt" and out["workspace"] == 1


@pytest.mark.parametrize(
    "scenario,state",
    [*((name, "reconnected") for name in RECONNECTED_SCENARIOS), ("unrecognised", "unrecognised")],
)
def test_a_server_on_a_screen_without_a_status_is_reported_with_it_and_stops(
    world, scenario, state
):
    log_in(world)
    write_claude(world.claude, env=f"FAKE_CLAUDE_SCENARIO={scenario}")
    started = world.server.start("pfx")["server"]
    assert started["state"] == state
    assert (started["session_name"], started["environment_url"]) == (None, None)
    shown = world.server.output()
    assert shown["server"]["screen"] == shown["screen"]
    assert any("Reconnected" in line for line in shown["screen"]) == (state == "reconnected")
    if state == "unrecognised":
        assert fake.UNRECOGNISED_SCREEN in shown["screen"]
    stopped = world.server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("exited", 1)


# --- the one-time Remote Control question ----------------------------------------------


def at_the_remote_control_prompt(world):
    """A server of an account that has never used Remote Control, at its question."""
    log_in(world)
    write_claude(world.claude, env="FAKE_CLAUDE_SCENARIO=remote_control_prompt")
    started = world.server.start("pfx")["server"]
    assert started["state"] == "remote_control_prompt"  # positive control for what follows
    return started


def test_start_lands_on_the_remote_control_prompt_with_no_screen_to_report(world):
    started = at_the_remote_control_prompt(world)
    assert (started["session_name"], started["environment_url"]) == (None, None)
    assert "screen" not in started


def test_output_at_the_remote_control_prompt_carries_the_screen_and_the_state(world):
    at_the_remote_control_prompt(world)
    out = world.server.output()
    assert out["server"]["state"] == "remote_control_prompt" and out["workspace"] == 1
    assert out["screen"][-1] == "Enable Remote Control? (y/n)"
    assert any("Take this session with you" in line for line in out["screen"])


def test_yes_at_the_remote_control_prompt_starts_the_server(world):
    at_the_remote_control_prompt(world)
    confirmed = world.server.confirm("y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_state(world, "ready")
    assert ready["session_name"].startswith("pfx-")
    assert refused(world.server.confirm, "y").code == "nothing_to_confirm"  # no longer asked


def test_no_at_the_remote_control_prompt_ends_the_server_and_it_can_be_resumed(world):
    at_the_remote_control_prompt(world)
    ended = world.server.confirm("n")["server"]
    assert ended["state"] == "exited"
    assert ended["exit_code"] is not None
    assert refused(world.server.confirm, "n").code == "nothing_to_confirm"
    assert world.server.resume("pfx")["server"]["state"] == "remote_control_prompt"


def test_stop_at_the_remote_control_prompt_ends_the_server(world):
    at_the_remote_control_prompt(world)
    stopped = world.server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("exited", 1)


def two_questions(world):
    """A `claude` that asks the trust question and then the Remote Control question, as
    Claude Code does for an account that has never used Remote Control, and then ends."""
    log_in(world)
    world.binary.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = remote-control ] || exec {rec.FAKE_CLAUDE} "$@"\n'
        "printf 'Trust /x? [y/N] '\n"
        "read answer\n"
        "printf 'Enable Remote Control? (y/n)\\n'\n"
        "read answer\n"
        "exit 1\n",
        encoding="utf-8",
    )


def test_a_trust_answer_that_lands_on_the_remote_control_prompt_is_accepted(world):
    two_questions(world)
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    asked = world.server.confirm("y")["server"]
    assert asked["state"] == "remote_control_prompt"
    ended = world.server.confirm("n")["server"]
    assert (ended["state"], ended["exit_code"]) == ("exited", 1)


def test_an_answer_that_leaves_the_question_unchanged_is_not_accepted(world, monkeypatch):
    """Twin of the above: a question that stays asked after both tries is refused."""
    monkeypatch.setattr(cs, "START_TIMEOUT", 1.0)
    log_in(world)
    world.binary.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = remote-control ] || exec {rec.FAKE_CLAUDE} "$@"\n'
        "while :; do\n"
        "  printf 'Trust /x? [y/N] '\n"
        "  read answer\n"
        f"  printf '{screens.TOO_FAST}\\n'\n"
        "done\n",
        encoding="utf-8",
    )
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    error = refused(world.server.confirm, "y")
    assert error.code == "confirm_not_accepted"
    assert error.detail["server"]["state"] == "trust_prompt"


def test_answered_compares_with_the_question_asked(world):
    log_in(world)
    world.server.start("pfx")  # at the trust question: the screen has no "too quickly"
    moved = {"state": "remote_control_prompt"}
    assert world.server._answered(moved, "trust_prompt")
    assert not world.server._answered({"state": "trust_prompt"}, "trust_prompt")  # positive
    assert not world.server._answered({"state": "starting"}, "trust_prompt")
    assert not world.server._answered({"state": "starting"}, "remote_control_prompt")
    assert world.server._answered({"state": "ready"}, "remote_control_prompt")


@pytest.mark.parametrize("prefix", ["", "-x", "a b", "a;b", "x" * 33])
def test_an_invalid_prefix_is_refused(world, prefix):
    log_in(world)
    assert refused(world.server.start, prefix).code == "prefix_invalid"
    assert world.server.status() is None


# --- stopping ------------------------------------------------------------------------


def ignoring(world, *signals, child=False, detached=False):
    """Servers started from now on ignore ``signals`` while Claude asks to trust the folder.

    With ``child``, each leaves a child running in its process group, which ignores
    SIGHUP, so only a signal to the whole group ends it; with ``detached``, a child in
    a session of its own, which no signal to the group reaches. Its pid is in the
    returned file."""
    pid_file = world.tmp_path / "child.pid"
    if detached:
        spawn = "setsid sleep 600 </dev/null >/dev/null 2>&1 &\n"
    elif child:
        spawn = "(trap '' HUP; exec sleep 600) </dev/null >/dev/null 2>&1 &\n"
    else:
        spawn = ""
    if spawn:
        spawn = f'if [ "$1" = remote-control ]; then\n  {spawn}  echo $! > {pid_file}\nfi\n'
    write_claude(world.claude, before=f"{spawn}trap '' {' '.join(signals)}\n")
    return pid_file


def child_pid(pid_file: Path) -> int:
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.1)
    pytest.fail("the server's child never started")


@pytest.mark.parametrize("ignored,ended_by", [(("INT",), "SIGTERM"), (("INT", "TERM"), "SIGKILL")])
def test_stop_ends_a_server_that_ignores_ctrl_c(world, monkeypatch, ignored, ended_by):
    quick_stop(monkeypatch)
    log_in(world)
    pid_file = ignoring(world, *ignored, child=True)
    world.server.start("pfx")
    pid = child_pid(pid_file)
    assert alive(pid)  # known positive for the check below
    try:
        began = time.monotonic()
        out = world.server.stop()
        took = time.monotonic() - began
        assert out["server"]["state"] == "exited"
        assert out["server"]["exit_code"] == 128 + getattr(signal, ended_by)
        assert took < QUICK_STOP["STOP_TIMEOUT"] + 2 * QUICK_STOP["END_GRACE_SECONDS"] + 5
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    kills = [
        c for c in world.calls if c[: len(world.layout.tool("kill"))] == world.layout.tool("kill")
    ]
    assert kills  # the signals went through the workspace's channel
    world.server.forget()
    assert world.server.status() is None


def test_a_stop_leaves_nothing_the_server_started_even_in_a_session_of_its_own(world, monkeypatch):
    """What a server started in a session of its own escapes a signal to its process
    group; the stop ends it as one of the server's own processes."""
    quick_stop(monkeypatch)
    log_in(world)
    pid_file = ignoring(world, "INT", detached=True)
    world.server.start("pfx")
    pid = child_pid(pid_file)
    assert alive(pid)
    try:
        assert os.getsid(pid) == pid  # the plant took effect: a session of its own
        world.server.stop()
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_server_that_stops_on_ctrl_c_is_not_signalled(world):
    log_in(world, trusted=True)
    world.server.start("pfx")
    wait_state(world, "ready")
    stopped = world.server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("stopped", 0)
    kill = world.layout.tool("kill")
    assert not [c for c in world.calls if c[: len(kill)] == kill and "-TERM" in c]


def test_stop_answers_timeout_when_it_cannot_end_a_server(world, monkeypatch):
    quick_stop(monkeypatch)
    log_in(world)
    ignoring(world, "INT")
    world.server.start("pfx")
    sent = []
    monkeypatch.setattr(cs.Tmux, "signal", lambda self, pid, sig: sent.append(sig))
    error = refused(world.server.stop)
    assert error.code == "timeout"
    assert error.detail["server"]["state"] == "trust_prompt"
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_a_signal_goes_only_to_the_process_the_pane_still_runs(world, monkeypatch):
    log_in(world)
    world.server.start("pfx")
    _dead, pid = world.server.tmux.pane()
    before = len(world.calls)
    world.server.tmux.signal(pid + 100000, signal.SIGTERM)  # not the pane's process
    kill = world.layout.tool("kill")
    assert not [c for c in world.calls[before:] if c[: len(kill)] == kill]
    world.server.tmux.signal(pid, signal.SIGTERM)  # known positive
    assert [c for c in world.calls[before:] if c[: len(kill)] == kill]


def test_a_server_at_the_spawn_mode_question_is_ended_by_stop(world, monkeypatch):
    """Claude asks the spawn mode in a repository when started without --spawn, and
    ignores Ctrl+C there; the SIGTERM of the stop ends it."""
    from tests.git_fixtures import make_repo

    quick_stop(monkeypatch)
    log_in(world)
    repo = make_repo(world.tmp_path / "repo")
    for name in os.listdir(repo):
        os.rename(repo / name, Path(world.layout.work) / name)
    world.binary.write_text(
        "#!/bin/sh\nfor a; do shift\n"
        '  if [ -n "$skip" ]; then skip=; continue; fi\n'
        '  if [ "$a" = --spawn ]; then skip=1; continue; fi\n'
        '  set -- "$@" "$a"\ndone\n'
        f'exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )
    world.server.start("pfx")
    asked = world.server.confirm("y")["server"]
    assert asked["state"] == "unrecognised"
    assert any("Spawn mode" in line for line in asked["screen"])
    out = world.server.stop()["server"]
    assert (out["state"], out["exit_code"]) == ("exited", 128 + signal.SIGTERM)


# --- what a server is started with ---------------------------------------------------


def test_the_server_command_is_fixed_and_shell_free(world):
    argv = cs.server_command(world.layout, "pfx")
    assert argv[: len(world.layout.tool("env")) + 1] == [*world.layout.tool("env"), "-i"]
    command = argv[argv.index(world.layout.claude) :]
    assert command == [
        world.layout.claude,
        "remote-control",
        "--remote-control-session-name-prefix",
        "pfx",
        "--permission-mode",
        "default",
        "--spawn",
        "same-dir",
    ]


def test_the_server_is_started_as_the_recorder_records_one(world):
    """So the contract tests check every option rcman starts a server with."""
    argv = cs.server_command(world.layout, rec.PROBE_PREFIX)
    assert argv[argv.index(world.layout.claude) + 1 :] == rec.RC_ARGS


def test_claudes_environment_is_the_allowlist(world):
    env = rt.child_env(world.layout)
    argv = cs.server_command(world.layout, "pfx")
    start = len(world.layout.tool("env")) + 1
    assigned = argv[start : argv.index(world.layout.claude)]
    assert [a.split("=", 1)[0] for a in assigned] == list(env)
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == world.layout.config
    assert env["HOME"] == world.layout.home
    assert env["SHELL"] == world.layout.shell


def test_the_server_runs_in_the_workspaces_folder_with_that_environment(world):
    log_in(world)
    world.server.start("pfx")
    _dead, pid = world.server.tmux.pane()
    environ = dict(
        item.split("=", 1)
        for item in Path(f"/proc/{pid}/environ").read_text().split("\0")
        if "=" in item
    )
    assert os.readlink(f"/proc/{pid}/cwd") == world.layout.work
    assert environ["CLAUDE_CONFIG_DIR"] == world.layout.config
    assert environ["DISABLE_AUTOUPDATER"] == "1"


# --- the size of a server's window ----------------------------------------------------

TERMINAL = (80, 24)


def tmux(world, *args, conf=TMUX_CONF):
    return subprocess.run(
        ["tmux", "-S", world.layout.tmux_socket, "-f", str(conf), *args],
        capture_output=True,
        text=True,
    )


def window_size(world, session=cs.SESSION) -> tuple[int, int]:
    out = tmux(
        world, "display-message", "-p", "-t", f"={session}:", "#{window_width}x#{window_height}"
    )
    width, height = out.stdout.strip().split("x")
    return int(width), int(height)


@contextlib.contextmanager
def attached(world, session=cs.SESSION):
    with tmux_sockets("rct-") as outer, tmuxctl.Pane(outer, *TERMINAL) as terminal:
        terminal.run(
            ["tmux", "-S", world.layout.tmux_socket, "attach-session", "-t", f"={session}"],
            cwd=world.tmp_path,
            env={"PATH": os.environ["PATH"], "TERM": "xterm-256color"},
        )
        size = f"{TERMINAL[0]}x{TERMINAL[1]}"
        for _ in range(100):
            clients = tmux(
                world, "list-clients", "-t", f"={session}", "-F", "#{client_width}x#{client_height}"
            )
            if clients.stdout.split() == [size]:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"no terminal of {size} attached; it shows:\n{terminal.text()}")
        yield


def resized(world, session=cs.SESSION) -> tuple[int, int]:
    for _ in range(50):
        if (size := window_size(world, session))[0] == TERMINAL[0]:
            return size
        time.sleep(0.1)
    return window_size(world, session)


def test_an_attached_terminal_does_not_resize_a_server(world):
    log_in(world)
    world.server.start("pfx")
    with attached(world):
        assert window_size(world) == (screens.WIDTH, screens.HEIGHT)
    pinned = tmux(world, "show-options", "-wv", "-t", f"={cs.SESSION}:", "window-size")
    assert pinned.stdout.strip() == "manual"


def test_without_the_pin_an_attached_terminal_resizes_the_window(world):
    """Twin: a window made as rcman makes one, but not pinned, takes the terminal's size."""
    made = tmux(world, "new-session", "-d", "-s", "unpinned", "-x", str(screens.WIDTH),
                "-y", str(screens.HEIGHT), "sleep", "600")  # fmt: skip
    assert made.returncode == 0, made.stderr
    with attached(world, "unpinned"):
        width, height = resized(world, "unpinned")
    assert width == TERMINAL[0] and height <= TERMINAL[1]


def test_a_server_started_again_in_an_old_session_is_pinned(world):
    log_in(world)
    made = tmux(world, "new-session", "-d", "-s", cs.SESSION, "-x", str(screens.WIDTH),
                "-y", str(screens.HEIGHT), "sh", "-c", "exit 3")  # fmt: skip
    assert made.returncode == 0, made.stderr
    assert wait_state(world, "exited")["exit_code"] == 3
    with attached(world):
        assert resized(world)[0] == TERMINAL[0]  # positive control: it was resized
        assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
        assert window_size(world) == (screens.WIDTH, screens.HEIGHT)


def test_a_server_whose_window_cannot_be_pinned_is_still_found(world):
    log_in(world)
    real = world.server.tmux.run_argv

    def failing(argv, timeout=cs.REQUEST_TIMEOUT):
        if "window-size" in argv:
            return rt.Result(1, b"", b"planted failure on window-size")
        return real(argv, timeout=timeout)

    world.server.tmux.run_argv = failing
    with pytest.raises(cs.TmuxFailed, match="planted failure"):
        world.server.start("pfx")
    world.server.tmux.run_argv = real
    assert world.server.status() is not None  # found all the same
    wait_state(world, "trust_prompt")
    world.server.stop()


def test_tmux_conf_sets_no_global_window_size(world, tmp_path):
    """A global ``window-size manual`` kills tmux 3.4's server at the next detached
    new-session, so servers' windows are pinned one by one instead."""
    made = tmux(world, "new-session", "-d", "-s", "probe", "-x", "10", "-y", "5", "sleep", "600")
    assert made.returncode == 0, made.stderr
    assert tmux(world, "show-options", "-gv", "remain-on-exit").stdout.strip() == "on"
    bare = Path(world.layout.tmux_socket).parent / "bare.sock"
    default = subprocess.run(
        ["tmux", "-S", str(bare), "-f", "/dev/null", "start-server", ";"]
        + ["show-options", "-gwv", "window-size"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert default and default != "manual"
    assert tmux(world, "show-options", "-gwv", "window-size").stdout.strip() == default
    planted = tmp_path / "tmux.conf"
    planted.write_text(TMUX_CONF.read_text(encoding="utf-8") + "set -g window-size manual\n")
    tmux(world, "kill-server")
    died = tmux(world, "new-session", "-d", "-s", "probe", "-x", "10", "-y", "5", "sleep", "600",
                conf=planted)  # fmt: skip
    assert died.returncode != 0
    assert "server exited unexpectedly" in died.stderr


# --- signed in ----------------------------------------------------------------------


def test_signed_in_is_what_claude_says(world):
    assert world.server.signed_in() is False
    log_in(world)
    assert world.server.signed_in() is True


def test_an_answer_of_claude_that_is_not_json_is_unrecognised_output(world):
    world.binary.write_text("#!/bin/sh\necho not json\n", encoding="utf-8")
    error = refused(world.server.signed_in)
    assert error.code == "claude_unrecognised_output"
    assert error.detail["screen"] == ["not json"]


def test_no_claude_in_the_workspace_is_claude_not_installed(world):
    world.binary.unlink()
    assert refused(world.server.start, "pfx").code == "claude_not_installed"


def _gone(pid: int) -> bool:
    for _ in range(50):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_a_stop_ends_a_sessions_command_in_a_session_of_its_own(world, monkeypatch):
    """As seen with a real account: a session stays in the pane's process group, and the
    commands it runs are in a session of their own (setsid), whose parent the group's
    kill ends first, so they are collected before anything is signalled."""
    quick_stop(monkeypatch)
    log_in(world)
    pid_file = world.tmp_path / "command.pid"
    session = (
        f"sh -c 'setsid sleep 600 </dev/null >/dev/null 2>&1 & echo $! > {pid_file}; wait' "
        "</dev/null >/dev/null 2>&1 &\n"
    )
    write_claude(
        world.claude,
        before=f"if [ \"$1\" = remote-control ]; then\n  {session}fi\ntrap '' INT TERM\n",
    )
    world.server.start("pfx")
    pid = child_pid(pid_file)
    try:
        assert os.getsid(pid) == pid  # the plant took effect: a session of its own
        world.server.stop()
        assert _gone(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_stop_ends_what_the_server_started_after_the_stop_began(world, monkeypatch):
    """A server may start a command while it is being stopped: what runs is collected
    again before each signal, while the server still holds it as a descendant."""
    quick_stop(monkeypatch)
    log_in(world)
    pid_file = world.tmp_path / "late.pid"
    # The command writes its pid and its session's id, then becomes sleep.
    command = world.tmp_path / "command.sh"
    command.write_text(
        f"read -r a b c d e sid rest < /proc/$$/stat\necho $$ $sid > {pid_file}\nexec sleep 600\n"
    )
    quiet = "</dev/null >/dev/null 2>&1"
    late = f'sh -c "setsid sh {command} {quiet} & wait" {quiet} &'
    write_claude(
        world.claude,
        before=(
            'if [ "$1" = remote-control ]; then\n'
            f"  trap '{late}' INT\n"
            "  echo a server that starts a command on Ctrl+C\n"
            "  while :; do sleep 1; done\n"
            "fi\n"
        ),
    )
    world.server.start("pfx")
    try:
        world.server.stop()
        for _ in range(50):
            if pid_file.exists() and len(pid_file.read_text().split()) == 2:
                break
            time.sleep(0.1)
        pid, sid = (int(x) for x in pid_file.read_text().split())
        assert sid == pid  # the plant took effect: a session of its own
        assert _gone(pid)
    finally:
        found = pid_file.read_text().split() if pid_file.exists() else []
        if found and alive(int(found[0])):
            os.kill(int(found[0]), signal.SIGKILL)


class EndsAfterTheFirstRead:
    """A tmux whose server ends, having written its last words, right after the first thing
    it is asked: what is read first is what it looked like before, what second, after."""

    def __init__(self):
        self.asked = 0

    def _ended(self) -> bool:
        self.asked += 1
        return self.asked > 1

    def screen(self):
        return ["Unable to determine your organization"] if self._ended() else []

    def exit_status(self):
        return 1 if self._ended() else None


def test_a_server_that_ends_between_the_two_reads_is_never_an_exit_with_a_screen_from_before(world):
    """The status of a server is its screen and its exit status, read one after the other:
    one that ends between them must not be reported as ended with the screen it had before
    it wrote its last words, as happens when the screen is read first."""
    world.server.tmux = EndsAfterTheFirstRead()
    info = world.server._server()
    shown = "\n".join(info.get("screen", []))
    assert info["exit_code"] is None or "Unable to determine" in shown, info


# --- a server runs the exact file of the version it is pinned to --------------------------------


def test_a_layout_pinned_to_a_version_names_that_versions_file_and_nothing_else_changes(world):
    pinned = world.layout.pinned("1.2.3")
    assert pinned.claude == str(world.claude / "versions" / "1.2.3")
    assert world.layout.claude == str(world.claude / "bin" / "claude")  # the layout is not changed
    assert dataclasses.replace(pinned, claude=world.layout.claude) == world.layout


def test_the_command_of_a_server_pinned_to_a_version_runs_that_file(world):
    argv = cs.server_command(world.layout.pinned("1.2.3"), "pfx")
    assert str(world.claude / "versions" / "1.2.3") in argv
    assert world.layout.claude not in argv


def test_a_server_pinned_to_a_version_asks_and_starts_that_exact_file(world):
    ran = world.tmp_path / "ran"
    other = fake_claude(world.claude, version="1.1.0", before=f'echo "other $0" >> {ran}\n')
    first = fake_claude(world.claude, before=f'echo "first $0" >> {ran}\n')  # current again
    log_in(world)
    assert world.server.signed_in() is True  # positive control: unpinned, it is the current one
    assert ran.read_text().splitlines() == [f"first {world.layout.claude}"]
    ran.unlink()
    pinned = cs.Server(world.layout.pinned("1.1.0"), world.server.run, 1)
    assert pinned.signed_in() is True  # auth status
    pinned.start("pfx")
    lines = ran.read_text().splitlines()
    assert len(lines) >= 2  # auth status, and the server
    assert set(lines) == {f"other {other}"}
    assert str(first) not in ran.read_text()


def test_the_pinned_version_must_look_like_a_version(world):
    for bad in ("../x", "1.2", "latest", "1.2.3/../../x", ""):
        with pytest.raises(ValueError):
            world.layout.pinned(bad)


# --- every use of the path of Claude goes through the command builder or the pinned layout ------


def claude_path_uses(sources: dict[str, str]) -> list[tuple[str, str]]:
    """Where a file's code reads an attribute called ``claude`` - the path of Claude in a
    layout, or the store a host runtime holds - as (file, the function, class first, it is
    in; "" at module level)."""
    import ast

    found = []

    def walk(name, node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = [*scope, child.name]
            if isinstance(child, ast.Attribute) and child.attr == "claude":
                found.append((name, ".".join(scope)))
            walk(name, child, inner)

    for name, source in sources.items():
        walk(name, ast.parse(source), [])
    return found


# Where the path is read, and why: the one builder of a command
# and the host runtime that holds the store the path starts from.
ALLOWED_CLAUDE_PATH_USES = {
    ("claude_server.py", "claude_command"): "the one builder of a command that runs Claude",
    ("runtime.py", "HostRuntime.__init__"): "the store the host runtime holds",
    ("runtime.py", "HostRuntime.layout"): "the store the host runtime's layouts start from",
}


def backend_sources() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((REPO / "backend" / "rcman").rglob("*.py"))}


def test_the_path_of_claude_is_read_only_where_a_command_is_built_or_a_version_pinned():
    sources = backend_sources()
    uses = claude_path_uses(sources)
    assert uses  # non-vacuity: the scan finds the uses there are
    stray = sorted(set(uses) - set(ALLOWED_CLAUDE_PATH_USES))
    assert stray == []
    # an allowance whose use is gone is stale
    assert set(ALLOWED_CLAUDE_PATH_USES) <= set(uses)


def test_the_guard_of_the_path_of_claude_finds_a_planted_use():
    planted = {
        "claude_server.py": "def claude_command(layout):\n    return [layout.claude]\n",
        "planted.py": "def start(layout):\n    return [layout.claude, 'remote-control']\n",
        "also_planted.py": "class A:\n    def b(self):\n        return self.layout.claude\n",
    }
    uses = claude_path_uses(planted)
    assert uses == [
        ("claude_server.py", "claude_command"),
        ("planted.py", "start"),
        ("also_planted.py", "A.b"),
    ]
    assert sorted(set(uses) - set(ALLOWED_CLAUDE_PATH_USES)) == [
        ("also_planted.py", "A.b"),
        ("planted.py", "start"),
    ]
