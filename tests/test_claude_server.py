"""A workspace's Claude Remote Control server: its tmux, its states and its verbs
(backend/rcman/claude_server.py), on the host runtime against real tmux and the fake
`claude`, through a real channel, as rcman runs them in a container."""

import contextlib
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
    claude: Path
    tmp_path: Path
    calls: list


def write_claude(path: Path, before: str = "", env: str = "FAKE_CLAUDE_READY_SECONDS=0.3") -> None:
    path.write_text(f'#!/bin/sh\n{before}{env} exec {rec.FAKE_CLAUDE} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path):
    claude = tmp_path / "claude"
    write_claude(claude)
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
                runtime, layout, channel, cs.Server(layout, run, 1), claude, tmp_path, calls
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
    [("reconnected_without_status", "reconnected"), ("unrecognised", "unrecognised")],
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
    ignores Ctrl+C and SIGTERM there; only the SIGKILL ends it."""
    from tests.git_fixtures import make_repo

    quick_stop(monkeypatch)
    log_in(world)
    repo = make_repo(world.tmp_path / "repo")
    for name in os.listdir(repo):
        os.rename(repo / name, Path(world.layout.work) / name)
    world.claude.write_text(
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
    assert (out["state"], out["exit_code"]) == ("exited", 128 + signal.SIGKILL)


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
    env = cs.child_env(world.layout)
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
    world.claude.write_text("#!/bin/sh\necho not json\n", encoding="utf-8")
    error = refused(world.server.signed_in)
    assert error.code == "claude_unrecognised_output"
    assert error.detail["screen"] == ["not json"]


def test_no_claude_in_the_workspace_is_claude_not_installed(world):
    world.claude.unlink()
    assert refused(world.server.start, "pfx").code == "claude_not_installed"
