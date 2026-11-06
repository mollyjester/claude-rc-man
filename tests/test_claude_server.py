"""A workspace's Claude Remote Control server: its tmux, its states and its verbs
(backend/rcman/claude_server.py), on the host runtime against real tmux and the fake
`claude`, through a real channel, as rcman runs them in a container."""

import contextlib
import dataclasses
import os
import signal
import subprocess
import threading
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
from tests.hang_guard import HANG_GUARD
from tests.test_screens import RECONNECTED_SCENARIOS
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


def refused(call, *args) -> cs.ServerError:
    with pytest.raises(cs.ServerError) as raised:
        call(*args)
    return raised.value


def within_the_guard(found, what: str):
    """``found()`` looked at until it gives something, ``what``, within the hang guard."""

    def looked() -> object:
        return found()

    looked.__qualname__ = what  # said where it does not come about
    return until(looked)


def wait_state(world, wanted, named=False):
    """The server once it is read as ``wanted``; with ``named``, once its session's name is
    read too, which a screen still being drawn may not show yet."""

    def found():
        server = world.server.status()
        if server and server["state"] == wanted and (server["session_name"] or not named):
            return server
        return None

    return within_the_guard(found, f"the state {wanted}")


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
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


class Signals:
    """The signals the server's stop sends, each with the fake time's mark as it was sent;
    with ``deliver`` False none reaches the server."""

    def __init__(self, server: cs.Server, fake: FakeTime, deliver: bool = True):
        self.sent: list[tuple[int, int]] = []
        self.told = threading.Condition()
        real = server.tmux.signal

        def signal_(pid, sig):
            with self.told:
                self.sent.append((sig, fake.mark()))
                self.told.notify_all()
            if deliver:
                real(pid, sig)

        server.tmux.signal = signal_

    def mark_of(self, n: int) -> int:
        """The mark at which the ``n``th signal was sent, once it has been."""
        with self.told:
            assert self.told.wait_for(lambda: len(self.sent) >= n, HANG_GUARD)
            return self.sent[n - 1][1]

    def kinds(self) -> list[int]:
        with self.told:
            return [sig for sig, _ in self.sent]


def stopped_past(world, fake: FakeTime, graces: int, deliver=True, before=None):
    """``world.server.stop()`` moved past its first ``graces`` waits: STOP_TIMEOUT for Ctrl+C,
    each moved once a look of the wait has begun, and so its deadline is taken; then
    END_GRACE_SECONDS after each signal, once a look begun after that signal is. ``before`` is
    called before the first move. What the stop returned or raised, and the signals sent."""
    signals = Signals(world.server, fake, deliver)
    start = fake.mark()
    stopping = Calling(world.server.stop)
    fake.wait_sleep_begun(after=start)  # it waits for the server to exit on Ctrl+C
    if before is not None:
        before()
    fake.advance(cs.STOP_TIMEOUT + cs.POLL_SECONDS)
    for n in range(1, graces):
        fake.wait_sleep_begun(after=signals.mark_of(n))
        fake.advance(cs.END_GRACE_SECONDS + cs.POLL_SECONDS)
    return stopping.result(), signals.kinds()


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
    ready = wait_state(world, "ready", named=True)
    screen = world.server.output()["screen"]
    assert any(line.endswith("· feature-x") for line in screen)
    assert not any("Spawn mode" in line for line in screen)
    assert world.server.stop()["server"]["state"] == "stopped"
    world.server.resume("pfx")
    assert wait_state(world, "ready", named=True)["session_name"] == ready["session_name"]


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


def test_an_answer_that_leaves_the_question_unchanged_is_not_accepted(world):
    """Twin of the above: a question that stays asked after both tries is refused."""
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


@pytest.mark.parametrize("short", [False, True])
def test_an_answer_waits_its_pace_and_types_nothing_before(world, fake_time, short):
    """On a `claude` that takes any answer at once, so that what holds the answer back is the
    server's pace and nothing of the fake's."""
    two_questions(world)
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    answers = typed(world.server, fake_time)
    fake_time.parking = paced
    confirming = Calling(world.server.confirm, "y")
    [pace] = fake_time.wait_parked(1, match=cs.ANSWER_PACE_SECONDS)
    assert answers == []
    if short:  # the twin: short of its pace, it has looked at the time again and typed nothing
        fake_time.advance(cs.ANSWER_PACE_SECONDS / 2)
        assert fake_time.wait_parked(1, match=cs.ANSWER_PACE_SECONDS) == [pace]
        assert answers == []
        fake_time.advance(cs.ANSWER_PACE_SECONDS / 2)
    else:
        fake_time.advance(cs.ANSWER_PACE_SECONDS)
    assert confirming.result()["server"]["state"] == "remote_control_prompt"
    assert pace.ended == "advanced"
    assert answers == [("y", pace.ordinal)]  # typed as the pace ended, no sleep between


def test_an_answer_claude_found_too_fast_is_given_again_after_its_pace(world, fake_time):
    log_in(world)
    world.binary.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = remote-control ] || exec {rec.FAKE_CLAUDE} "$@"\n'
        "printf 'Trust /x? [y/N] '\n"
        "read answer\n"
        # One write: the refusal and the question asked again are on the screen together.
        f"printf '{screens.TOO_FAST}\\nTrust /x? [y/N] '\n"
        "read answer\n"
        "printf 'Enable Remote Control? (y/n)\\n'\n"
        "read answer\n"
        "exit 1\n",
        encoding="utf-8",
    )
    assert world.server.start("pfx")["server"]["state"] == "trust_prompt"
    answers = typed(world.server, fake_time)
    fake_time.parking = paced
    confirming = Calling(world.server.confirm, "y")
    [first] = fake_time.wait_parked(1, match=cs.ANSWER_PACE_SECONDS)
    assert answers == []
    fake_time.advance(cs.ANSWER_PACE_SECONDS)
    [second] = fake_time.wait_parked(
        1, match=lambda w: w.seconds == cs.ANSWER_PACE_SECONDS and w.ordinal > first.ordinal
    )
    assert [text for text, _ in answers] == ["y"]  # once, before the second pace began
    assert answers[0][1] == first.ordinal  # typed as the first pace ended, no sleep between
    fake_time.advance(cs.ANSWER_PACE_SECONDS)
    assert confirming.result()["server"]["state"] == "remote_control_prompt"
    assert [text for text, _ in answers] == ["y", "y"]
    assert answers[1][1] == second.ordinal


@pytest.mark.parametrize("short", [False, True])
def test_a_start_that_never_leaves_starting_answers_timeout_past_its_deadline(
    world, fake_time, short
):
    log_in(world)
    world.binary.write_text(
        f'#!/bin/sh\n[ "$1" = remote-control ] || exec {rec.FAKE_CLAUDE} "$@"\n'
        f"exec sleep {3 * HANG_GUARD:g}\n",
        encoding="utf-8",
    )
    start = fake_time.mark()
    starting = Calling(world.server.start, "pfx")
    fake_time.wait_sleep_begun(after=start)  # a look begun: its deadline is taken
    fake_time.advance(cs.START_TIMEOUT if short else cs.START_TIMEOUT + cs.POLL_SECONDS)
    if short:  # the twin: exactly at its deadline, it looks on
        fake_time.wait_sleeps_returned(after=fake_time.mark(), n=2)
        assert starting.thread.is_alive()
        fake_time.advance(cs.POLL_SECONDS)
    error = starting.result()
    assert isinstance(error, cs.ServerError) and error.code == "timeout"
    assert error.detail["server"]["state"] == "starting"
    world.server.tmux.kill()


@pytest.mark.parametrize("short", [0, 1])
def test_an_unrecognised_screen_is_an_answer_once_it_has_lasted_its_grace(world, fake_time, short):
    log_in(world)
    world.binary.write_text(
        f'#!/bin/sh\n[ "$1" = remote-control ] || exec {rec.FAKE_CLAUDE} "$@"\n'
        f"echo a screen rcman does not know\nexec sleep {3 * HANG_GUARD:g}\n",
        encoding="utf-8",
    )
    starting = Calling(world.server.start, "pfx")
    wait_state(world, "unrecognised")
    # Two looks begun after it was seen: the second read it as unrecognised, and took its time.
    fake_time.wait_sleep_begun(after=fake_time.mark(), n=2)
    fake_time.advance(cs.UNRECOGNISED_GRACE_SECONDS - short)
    if short:  # the twin: a second short of its grace, it is no answer yet
        fake_time.wait_sleeps_returned(after=fake_time.mark(), n=2)
        assert starting.thread.is_alive()
        fake_time.advance(short)
    assert starting.result()["server"]["state"] == "unrecognised"
    world.server.tmux.kill()


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
        spawn = f"setsid sleep {3 * HANG_GUARD:g} </dev/null >/dev/null 2>&1 &\n"
    elif child:
        spawn = f"(trap '' HUP; exec sleep {3 * HANG_GUARD:g}) </dev/null >/dev/null 2>&1 &\n"
    else:
        spawn = ""
    if spawn:
        spawn = f'if [ "$1" = remote-control ]; then\n  {spawn}  echo $! > {pid_file}\nfi\n'
    write_claude(world.claude, before=f"{spawn}trap '' {' '.join(signals)}\n")
    return pid_file


def child_pid(pid_file: Path) -> int:
    def found():
        return pid_file.exists() and pid_file.read_text().strip()

    return int(within_the_guard(found, "the server's child"))


@pytest.mark.parametrize(
    "ignored,ended_by,graces", [(("INT",), "SIGTERM", 1), (("INT", "TERM"), "SIGKILL", 2)]
)
def test_stop_ends_a_server_that_ignores_ctrl_c(world, fake_time, ignored, ended_by, graces):
    log_in(world)
    pid_file = ignoring(world, *ignored, child=True)
    world.server.start("pfx")
    pid = child_pid(pid_file)
    assert alive(pid)  # known positive for the check below
    try:
        out, sent = stopped_past(world, fake_time, graces)
        assert out["server"]["state"] == "exited"
        assert out["server"]["exit_code"] == 128 + getattr(signal, ended_by)
        assert sent == [signal.SIGTERM, signal.SIGKILL][:graces]
        assert _gone(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    kills = [
        c for c in world.calls if c[: len(world.layout.tool("kill"))] == world.layout.tool("kill")
    ]
    assert kills  # the signals went through the workspace's channel
    world.server.forget()
    assert world.server.status() is None


@pytest.mark.parametrize("short", [False, True])
def test_a_stop_signals_a_server_that_ignores_ctrl_c_once_past_its_grace(world, fake_time, short):
    log_in(world)
    ignoring(world, "INT")
    world.server.start("pfx")
    signals = Signals(world.server, fake_time)
    start = fake_time.mark()
    stopping = Calling(world.server.stop)
    fake_time.wait_sleep_begun(after=start)  # a look begun: its deadline is taken
    fake_time.advance(cs.STOP_TIMEOUT if short else cs.STOP_TIMEOUT + cs.POLL_SECONDS)
    if short:  # the twin: exactly at its grace, a whole look after the move signals nothing
        fake_time.wait_sleeps_returned(after=fake_time.mark(), n=2)
        assert stopping.thread.is_alive() and signals.kinds() == []
        fake_time.advance(cs.POLL_SECONDS)
    out = stopping.result()["server"]
    assert (out["state"], out["exit_code"]) == ("exited", 128 + signal.SIGTERM)
    assert signals.kinds() == [signal.SIGTERM]


def test_a_stop_leaves_nothing_the_server_started_even_in_a_session_of_its_own(world, fake_time):
    """What a server started in a session of its own escapes a signal to its process
    group; the stop ends it as one of the server's own processes."""
    log_in(world)
    pid_file = ignoring(world, "INT", detached=True)
    world.server.start("pfx")
    pid = child_pid(pid_file)
    assert alive(pid)
    try:
        assert os.getsid(pid) == pid  # the plant took effect: a session of its own
        stopped_past(world, fake_time, 1)
        assert _gone(pid)
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


def test_stop_answers_timeout_when_it_cannot_end_a_server(world, fake_time):
    log_in(world)
    ignoring(world, "INT")
    world.server.start("pfx")
    error, sent = stopped_past(world, fake_time, 3, deliver=False)
    assert isinstance(error, cs.ServerError) and error.code == "timeout"
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
    ignores Ctrl+C there; the SIGTERM of the stop ends it. The confirm is on real time, the
    fake's pace for an answer being real; the stop on the test's."""
    from tests.git_fixtures import make_repo

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
    fake_time = FakeTime()
    monkeypatch.setattr(cs, "time", fake_time)
    out, sent = stopped_past(world, fake_time, 1)
    assert (out["server"]["state"], out["server"]["exit_code"]) == ("exited", 128 + signal.SIGTERM)
    assert sent == [signal.SIGTERM]


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

        def clients():
            listed = tmux(
                world, "list-clients", "-t", f"={session}", "-F", "#{client_width}x#{client_height}"
            )
            return listed.stdout.split() == [size]

        within_the_guard(clients, f"a terminal of {size} attached")
        yield


def resized(world, session=cs.SESSION) -> tuple[int, int]:
    """The window's size once it has taken the terminal's width."""

    def found():
        size = window_size(world, session)
        return size if size[0] == TERMINAL[0] else None

    return within_the_guard(found, "the terminal's width")


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

    def failing(argv, timeout=HANG_GUARD):
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
    return within_the_guard(lambda: not alive(pid), f"the end of {pid}")


def test_a_stop_ends_a_sessions_command_in_a_session_of_its_own(world, fake_time):
    """As seen with a real account: a session stays in the pane's process group, and the
    commands it runs are in a session of their own (setsid), whose parent the group's
    kill ends first, so they are collected before anything is signalled."""
    log_in(world)
    pid_file = world.tmp_path / "command.pid"
    session = (
        f"sh -c 'setsid sleep {3 * HANG_GUARD:g} </dev/null >/dev/null 2>&1 & "
        f"echo $! > {pid_file}; wait' "
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
        stopped_past(world, fake_time, 2)
        assert _gone(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_stop_ends_what_the_server_started_after_the_stop_began(world, monkeypatch):
    """A server may start a command while it is being stopped: what runs is collected
    again before each signal, while the server still holds it as a descendant. Its grace is
    passed only once the command it started on Ctrl+C has said who it is. Its start is on real
    time, its screen being one rcman does not recognise, which counts only after its grace; the
    stop on the test's."""
    log_in(world)
    pid_file = world.tmp_path / "late.pid"
    # The command writes its pid and its session's id, then becomes sleep.
    command = world.tmp_path / "command.sh"
    command.write_text(
        f"read -r a b c d e sid rest < /proc/$$/stat\necho $$ $sid > {pid_file}\n"
        f"exec sleep {3 * HANG_GUARD:g}\n"
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

    def told() -> None:
        within_the_guard(
            lambda: pid_file.exists() and len(pid_file.read_text().split()) == 2, "the command"
        )

    fake_time = FakeTime()
    monkeypatch.setattr(cs, "time", fake_time)
    try:
        stopped_past(world, fake_time, 1, before=told)
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
