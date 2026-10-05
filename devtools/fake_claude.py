"""A fake `claude` that replays the recorded screens in ``tests/fakes/fixtures/``.

It implements only the invocations rcman makes, as a state machine whose every
screen is a recorded fixture with its placeholders filled in; any other
invocation fails loudly. Its state — whether it is logged in, which folders are
trusted, which session each folder last served — lives in ``fake-state.json``
under ``CLAUDE_CONFIG_DIR``, or under ``~/.claude-fake`` when that is unset, so
it never touches a real Claude configuration.

Setting ``FAKE_CLAUDE_SCENARIO=unrecognised`` makes ``remote-control`` and
``auth login`` show a synthetic screen that no recording contains, for testing
how unrecognised output is handled. ``FAKE_CLAUDE_VERSION`` replaces the
version it reports.

An answer to the folder-trust prompt that arrives sooner than
:data:`MIN_ANSWER_SECONDS` after the prompt is refused with the recorded
"too quickly" screen and asked again, as the real `claude` does. The real
threshold has not been measured exactly; an instant answer is refused and one
a fraction of a second later is accepted.

A server shows the recorded *connected* screen (no session yet) for
:data:`READY_SECONDS` — overridable with ``FAKE_CLAUDE_READY_SECONDS`` — then
the *ready* screen. Interrupting a server before it is ready has not been
recorded, so the fake finishes becoming ready before it handles the interrupt.

Signing in succeeds for a code listed in the state's ``accept_codes`` and fails
for any other, each with the recorded screens. Tests that need a logged-in fake
call :func:`write_state`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import signal
import socket
import sys
import termios
import time
from pathlib import Path

from devtools import record_claude as rec

STATE_FILE = "fake-state.json"
FAKE_ACCOUNT = {
    "email": "fake-user@example.com",
    "org_id": "00000000-0000-0000-0000-000000000000",
    "org_name": "Fake Org",
    "subscription_type": "max",
}
UNRECOGNISED_SCREEN = "fake claude: a synthetic screen that matches no recording"
CLEAR = "\x1b[H\x1b[2J"
MIN_ANSWER_SECONDS = 0.25
# How long a server shows the connected stage before its session is ready. Longer
# than the recorder's settling time, so a recording of the fake captures it.
READY_SECONDS = 2.0


def state_dir(env: dict[str, str]) -> str:
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME", "/"), ".claude-fake")


def write_state(
    config_dir: Path,
    logged_in: bool,
    trusted: list[str] | None = None,
    servers: dict[str, str] | None = None,
    accept_codes: list[str] | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "logged_in": logged_in,
        "trusted": trusted or [],
        "servers": servers or {},
        "accept_codes": accept_codes or [],
    }
    (config_dir / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")


def _load_state(directory: str) -> dict:
    path = Path(directory) / STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"logged_in": False, "trusted": [], "servers": {}}


def _save_state(directory: str, state: dict) -> None:
    Path(directory).mkdir(parents=True, exist_ok=True)
    (Path(directory) / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")


def _option(args: list[str], name: str) -> str | None:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def _branch(folder: str) -> str | None:
    """The branch of the repository ``folder`` is in, as its HEAD names it; None
    outside a repository."""
    for directory in (Path(folder), *Path(folder).parents):
        dot_git = directory / ".git"
        if dot_git.is_file():
            gitdir = dot_git.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
            dot_git = directory / gitdir
        if (dot_git / "HEAD").is_file():
            head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
            return head.removeprefix("ref: refs/heads/")
    return None


class Fake:
    def __init__(self, args: list[str], env: dict[str, str]):
        self.args = args
        self.env = env
        self.fixtures = rec.load_fixtures()
        self.version = env.get("FAKE_CLAUDE_VERSION") or rec.load_manifest()["claude_version"]
        self.cwd = os.getcwd()
        self.config_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(
            env.get("HOME", "/"), ".claude"
        )
        self.state_dir = state_dir(env)
        self.state = _load_state(self.state_dir)
        self.values = rec.placeholder_values(
            rec.Context(self.cwd, self.config_dir, self.version),
            environment_url=rec.ENVIRONMENT_URL_PREFIX
            + "env_fake"
            + hashlib.sha256(self.cwd.encode()).hexdigest()[:12],
            session_name="",
            **FAKE_ACCOUNT,
        )

    def show(self, name: str, prompt: bool = False) -> int | None:
        self.values["time"] = datetime.datetime.now().strftime("%H:%M:%S")
        text = rec.fill("\n".join(self.fixtures[name].lines), self.values)
        clear = CLEAR if sys.stdout.isatty() else ""
        sys.stdout.write(clear + text + ("" if prompt else "\n"))
        sys.stdout.flush()
        return self.fixtures[name].exit_code

    def read_line(self) -> str:
        if not sys.stdin.isatty():
            return sys.stdin.readline()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
        try:
            return sys.stdin.readline()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def paced_answer(self, too_fast_screen: str) -> str:
        """Read an answer, refusing and re-asking any that arrives too quickly."""
        while True:
            asked = time.monotonic()
            answer = self.read_line()
            if time.monotonic() - asked >= MIN_ANSWER_SECONDS:
                return answer
            self.show(too_fast_screen, prompt=True)

    def run(self) -> int:
        a = self.args
        if a == ["--version"]:
            return self.show("version")
        if a[:2] == ["auth", "status"]:
            return self.show(
                "auth_status_logged_in" if self.state["logged_in"] else "auth_status_logged_out"
            )
        if a == ["auth", "login", "--help"]:
            return self.show("auth_login_help")
        if a[:2] == ["auth", "login"]:
            return self.login()
        if a[:2] == ["auth", "logout"]:
            self.state["logged_in"] = False
            _save_state(self.state_dir, self.state)
            return self.show("auth_logout")
        if a[:1] == ["remote-control"]:
            return self.remote_control()
        return self.unsupported()

    def login(self) -> int:
        if self.env.get("FAKE_CLAUDE_SCENARIO") == "unrecognised":
            sys.stdout.write(CLEAR + UNRECOGNISED_SCREEN + "\n")
            sys.stdout.flush()
            self.wait_for_interrupt()
            return 1
        email = _option(self.args, "--email") or ""
        self.values["login_url"] = (
            rec.LOGIN_URL_PREFIX + "fake=1&login_hint=" + email.replace("@", "%40")
        )
        self.show("login_prompt", prompt=True)
        code = self.read_line().strip()
        if code not in self.state.get("accept_codes", []):
            return self.show("login_failed")
        self.state["logged_in"] = True
        _save_state(self.state_dir, self.state)
        self.show("login_succeeded", prompt=True)
        self.read_line()  # Claude Code waits for Enter
        return self.show("login_succeeded_done")

    def session_name(self) -> str:
        if existing := self.state["servers"].get(self.cwd):
            return existing
        if name := _option(self.args, "--name"):
            return name
        prefix = (
            _option(self.args, "--remote-control-session-name-prefix")
            or self.env.get("CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX")
            or socket.gethostname()
        )
        return f"{prefix}-fake-session"

    def remote_control(self) -> int:
        if not self.state["logged_in"]:
            return self.show("rc_logged_out")
        if self.env.get("FAKE_CLAUDE_SCENARIO") == "unrecognised":
            sys.stdout.write(CLEAR + UNRECOGNISED_SCREEN + "\n")
            sys.stdout.flush()
            self.wait_for_interrupt()
            return 1
        spawn = _option(self.args, "--spawn")
        if spawn not in (None, "same-dir"):
            return self.unsupported()
        branch = _branch(self.cwd)
        if branch is not None:
            self.values["branch"] = branch
        asks_spawn = branch is not None and spawn is None
        after_trust = self.cwd not in self.state["trusted"]
        if asks_spawn and not after_trust:
            return self.unsupported()  # whether Claude asks again has no recording
        if after_trust:
            self.show("git_trust_prompt" if branch is not None else "trust_prompt", prompt=True)
            if self.paced_answer("trust_answer_too_fast").strip().lower() != "y":
                return self.show("trust_declined")
            self.state["trusted"].append(self.cwd)
        if asks_spawn:
            if self.spawn_question() != "1":
                return self.unsupported()
            screens = ("spawn_connected_same_dir", "spawn_ready_same_dir", "spawn_stopped_same_dir")
        else:
            stage = "after_trust" if after_trust else "resumed"
            server = "git_server" if branch is not None else "server"
            screens = tuple(f"{server}_{s}_{stage}" for s in ("connected", "ready", "stopped"))
        self.values["session_name"] = self.session_name()
        self.state["servers"][self.cwd] = self.values["session_name"]
        _save_state(self.state_dir, self.state)
        connected, ready, stopped = screens
        interrupted: list[bool] = []
        signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
        self.show(connected)
        time.sleep(float(self.env.get("FAKE_CLAUDE_READY_SECONDS", READY_SECONDS)))
        self.show(ready)
        self.wait_for_interrupt(interrupted)
        return self.show(stopped)

    def spawn_question(self) -> str:
        """The spawn-mode answer; after Ctrl+C, which changes nothing on screen,
        every answer is ignored. SIGTERM is ignored throughout; only SIGKILL ends it."""
        interrupted: list[bool] = []

        def interrupt(*_) -> None:
            interrupted.append(True)
            self.show("spawn_prompt_interrupted", prompt=True)

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.show("spawn_prompt", prompt=True)
        answer = self.read_line()
        while interrupted:
            self.show("spawn_prompt_answered_after_interrupt", prompt=True)
            self.read_line()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return answer.strip()

    def unsupported(self) -> int:
        print(f"fake claude: unsupported invocation: {' '.join(self.args)}")
        return 2

    def wait_for_interrupt(self, interrupted: list[bool] | None = None) -> None:
        if interrupted is None:
            interrupted = []
            signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
        while not interrupted:
            signal.pause()


def main() -> int:
    code = Fake(sys.argv[1:], dict(os.environ)).run()
    return 0 if code is None else code
