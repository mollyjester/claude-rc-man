"""A workspace's Claude Remote Control server: its tmux, its states and its verbs
(backend/rcman/claude_server.py), on the host runtime against real tmux and the fake
`claude`, through a real channel, as rcman runs them in a container."""

import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from rcman import claude_server as cs
from rcman import runtime as rt

from devtools import fake_claude as fake
from devtools import record_claude as rec
from devtools.held_folders import tmux_sockets
from tests.conftest import fake_claude
from tests.hang_guard import HANG_GUARD
from tests.time_doubles import FakeTime
from tests.until import until

REPO = Path(__file__).resolve().parent.parent
TMUX_CONF = REPO / "backend" / "rcman" / "tmux.conf"
NAME = rt.container_name(1)


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

        def run(argv, timeout=HANG_GUARD):
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


def within_the_guard(found, what: str):
    """``found()`` looked at until it gives something, ``what``, within the hang guard."""

    def looked() -> object:
        return found()

    looked.__qualname__ = what  # said where it does not come about
    return until(looked)


def wait_ready(world):
    """The server once it is read as ready with its session's name and its environment's URL.
    A ready screen is drawn line by line: it is read as ready from its Capacity line, before
    the name's line below it and the URL's below that are drawn, and the name's line is whole
    once the URL is read."""

    def found():
        server = world.server.status()
        drawn = server and server["session_name"] and server["environment_url"]
        return server if drawn and server["state"] == "ready" else None

    return within_the_guard(found, "the state ready")


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError, IndexError:
        return False
    return state != "Z"


@pytest.fixture
def fake_time(monkeypatch) -> FakeTime:
    """claude_server's time, which only the test moves (tests/time_doubles.py): the test calls
    the server itself, so every thread that reads it is the test's."""
    made = FakeTime()
    monkeypatch.setattr(cs, "time", made)
    return made


class Calling:
    """``call(*args)`` in a thread of the test's, with what it returned or raised."""

    def __init__(self, call, *args):
        self.out: list = []
        self.thread = threading.Thread(target=self._run, args=(call, *args), daemon=True)
        self.thread.start()

    def _run(self, call, *args):
        try:
            self.out.append(call(*args))
        except BaseException as error:  # noqa: BLE001 - kept for the test to look at
            self.out.append(error)

    def result(self):
        self.thread.join(HANG_GUARD)
        assert not self.thread.is_alive(), "the call did not end"
        return self.out[0]


# --- the lifecycle ---------------------------------------------------------------------


def test_start_trust_confirm_ready_stop_resume_forget(world):
    log_in(world)
    started = world.server.start("pfx")
    assert started["server"]["state"] == "trust_prompt"
    assert started["server"]["workspace"] == 1

    confirmed = world.server.confirm("y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_ready(world)
    assert ready["session_name"].startswith("pfx-")
    assert ready["environment_url"].startswith(rec.ENVIRONMENT_URL_PREFIX)

    stopped = world.server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("stopped", 0)
    kill = world.layout.tool("kill")
    assert not [c for c in world.calls if c[: len(kill)] == kill]  # Ctrl+C stopped it

    resumed = world.server.resume("pfx")
    assert resumed["server"]["state"] in ("connected", "ready")
    assert wait_ready(world)["session_name"] == ready["session_name"]

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
    ready = wait_ready(world)
    screen = world.server.output()["screen"]
    assert any(line.endswith("· feature-x") for line in screen)
    assert not any("Spawn mode" in line for line in screen)
    assert world.server.stop()["server"]["state"] == "stopped"
    world.server.resume("pfx")
    assert wait_ready(world)["session_name"] == ready["session_name"]


def test_a_server_on_a_screen_rcman_does_not_know_is_reported_with_it_and_stops(world):
    log_in(world)
    write_claude(world.claude, env="FAKE_CLAUDE_SCENARIO=unrecognised")
    started = world.server.start("pfx")["server"]
    assert started["state"] == "unrecognised"
    assert (started["session_name"], started["environment_url"]) == (None, None)
    shown = world.server.output()
    assert shown["server"]["screen"] == shown["screen"]
    assert fake.UNRECOGNISED_SCREEN in shown["screen"]
    stopped = world.server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("exited", 1)


def test_yes_at_the_remote_control_prompt_starts_the_server(world):
    """The question of an account that has never used Remote Control."""
    log_in(world)
    write_claude(world.claude, env="FAKE_CLAUDE_SCENARIO=remote_control_prompt")
    started = world.server.start("pfx")["server"]
    assert started["state"] == "remote_control_prompt" and "screen" not in started
    out = world.server.output()
    assert out["server"]["state"] == "remote_control_prompt" and out["workspace"] == 1
    assert out["screen"][-1] == "Enable Remote Control? (y/n)"
    confirmed = world.server.confirm("y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    assert wait_ready(world)["session_name"].startswith("pfx-")


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


def typed(server: cs.Server, fake: FakeTime) -> list[tuple[str, int]]:
    """Each answer the server types from here on, with the fake time's mark as it was typed."""
    answers: list[tuple[str, int]] = []
    real = server.tmux.type

    def type_(text):
        answers.append((text, fake.mark()))
        real(text)

    server.tmux.type = type_
    return answers


def paced(seconds: float) -> bool:
    """Only the answer's pace parks: the waits for the screen after it poll."""
    return seconds == cs.ANSWER_PACE_SECONDS


def test_an_answer_waits_its_pace_and_types_nothing_before(world, fake_time):
    """On a `claude` that takes any answer at once, so that what holds the answer back is the
    server's pace and nothing of the fake's."""
    two_questions(world)
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    answers = typed(world.server, fake_time)
    fake_time.parking = paced
    confirming = Calling(world.server.confirm, "y")
    [pace] = fake_time.wait_parked(1, match=cs.ANSWER_PACE_SECONDS)
    assert answers == []
    fake_time.advance(cs.ANSWER_PACE_SECONDS)
    assert confirming.result()["server"]["state"] == "remote_control_prompt"
    assert pace.ended == "advanced"
    assert answers == [("y", pace.ordinal)]  # typed as the pace ended, no sleep between


# --- stopping ------------------------------------------------------------------------


def ignoring_ctrl_c(world) -> Path:
    """Servers started from now on ignore Ctrl+C while Claude asks to trust the folder, and
    each leaves a child running in its process group, which ignores SIGHUP, so only a signal
    to the whole group ends it. Its pid is in the returned file."""
    pid_file = world.tmp_path / "child.pid"
    spawn = f"(trap '' HUP; exec sleep {3 * HANG_GUARD:g}) </dev/null >/dev/null 2>&1 &\n"
    write_claude(
        world.claude,
        before=f'if [ "$1" = remote-control ]; then\n  {spawn}  echo $! > {pid_file}\nfi\n'
        "trap '' INT\n",
    )
    return pid_file


def test_stop_ends_a_server_that_ignores_ctrl_c_and_its_process_group_past_its_grace(
    world, fake_time
):
    log_in(world)
    pid_file = ignoring_ctrl_c(world)
    world.server.start("pfx")
    pid = int(
        within_the_guard(lambda: pid_file.exists() and pid_file.read_text().strip(), "the child")
    )
    assert alive(pid)  # known positive for the check below
    sent = []
    real = world.server.tmux.signal

    def signal_(target, sig):
        sent.append(sig)
        real(target, sig)

    world.server.tmux.signal = signal_
    try:
        start = fake_time.mark()
        stopping = Calling(world.server.stop)
        fake_time.wait_sleep_begun(after=start)  # it waits for the server to exit on Ctrl+C
        fake_time.advance(cs.STOP_TIMEOUT + cs.POLL_SECONDS)
        out = stopping.result()["server"]
        assert (out["state"], out["exit_code"]) == ("exited", 128 + signal.SIGTERM)
        assert sent == [signal.SIGTERM]
        assert within_the_guard(lambda: not alive(pid), f"the end of {pid}")
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    kill = world.layout.tool("kill")
    assert [c for c in world.calls if c[: len(kill)] == kill]  # through the workspace's channel
    world.server.forget()
    assert world.server.status() is None


# --- what a server is started with ---------------------------------------------------


def test_the_server_command_is_fixed_shell_free_and_its_environment_the_allowlist(world):
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
    env = rt.child_env(world.layout)
    assigned = argv[len(world.layout.tool("env")) + 1 : argv.index(world.layout.claude)]
    assert [a.split("=", 1)[0] for a in assigned] == list(env)
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == world.layout.config
    assert env["HOME"] == world.layout.home
    assert env["SHELL"] == world.layout.shell


def test_the_server_is_started_as_the_recorder_records_one(world):
    """So the contract tests check every option rcman starts a server with."""
    argv = cs.server_command(world.layout, rec.PROBE_PREFIX)
    assert argv[argv.index(world.layout.claude) + 1 :] == rec.RC_ARGS


def test_signed_in_is_what_claude_says(world):
    assert world.server.signed_in() is False
    log_in(world)
    assert world.server.signed_in() is True


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
