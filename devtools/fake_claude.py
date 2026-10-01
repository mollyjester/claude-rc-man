"""A fake `claude` that replays the recorded screens in ``tests/fakes/fixtures/``.

It implements only the invocations rcman makes, as a state machine whose every
screen is a recorded fixture with its placeholders filled in; any other
invocation fails loudly. Its state — whether it is logged in, which folders are
trusted, which session each folder last served — lives in ``fake-state.json``
under ``CLAUDE_CONFIG_DIR``, or under ``~/.claude-fake`` when that is unset, so
it never touches a real Claude configuration.

Setting ``FAKE_CLAUDE_SCENARIO=unrecognised`` makes ``remote-control`` show a
synthetic screen that no recording contains, for testing how unrecognised
output is handled.

An answer to the folder-trust prompt that arrives sooner than
:data:`MIN_ANSWER_SECONDS` after the prompt is refused with the recorded
"too quickly" screen and asked again, as the real `claude` does. The real
threshold has not been measured exactly; an instant answer is refused and one
a fraction of a second later is accepted.

A server shows the recorded *connected* screen (no session yet) for
:data:`READY_SECONDS` — overridable with ``FAKE_CLAUDE_READY_SECONDS`` — then
the *ready* screen. Interrupting a server before it is ready has not been
recorded, so the fake finishes becoming ready before it handles the interrupt.

Logging in is not implemented: a successful sign-in has not been recorded, so
every submitted code is answered with the recorded failure. Tests that need a
logged-in fake call :func:`write_state`.
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
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    state = {"logged_in": logged_in, "trusted": trusted or [], "servers": servers or {}}
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


class Fake:
    def __init__(self, args: list[str], env: dict[str, str]):
        self.args = args
        self.env = env
        self.fixtures = rec.load_fixtures()
        self.version = rec.load_manifest()["claude_version"]
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
        if a[:2] == ["auth", "login"]:
            return self.login()
        if a[:2] == ["auth", "logout"]:
            self.state["logged_in"] = False
            _save_state(self.state_dir, self.state)
            return self.show("auth_logout")
        if a[:1] == ["remote-control"]:
            return self.remote_control()
        print(f"fake claude: unsupported invocation: {' '.join(a)}")
        return 2

    def login(self) -> int:
        email = _option(self.args, "--email") or ""
        self.values["login_url"] = (
            rec.LOGIN_URL_PREFIX + "fake=1&login_hint=" + email.replace("@", "%40")
        )
        self.show("login_prompt", prompt=True)
        self.read_line()
        return self.show("login_failed")

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
        after_trust = self.cwd not in self.state["trusted"]
        if after_trust:
            self.show("trust_prompt", prompt=True)
            if self.paced_answer("trust_answer_too_fast").strip().lower() != "y":
                return self.show("trust_declined")
            self.state["trusted"].append(self.cwd)
        self.values["session_name"] = self.session_name()
        self.state["servers"][self.cwd] = self.values["session_name"]
        _save_state(self.state_dir, self.state)
        stage = "after_trust" if after_trust else "resumed"
        interrupted: list[bool] = []
        signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
        self.show(f"server_connected_{stage}")
        time.sleep(float(self.env.get("FAKE_CLAUDE_READY_SECONDS", READY_SECONDS)))
        self.show(f"server_ready_{stage}")
        self.wait_for_interrupt(interrupted)
        return self.show(f"server_stopped_{stage}")

    def wait_for_interrupt(self, interrupted: list[bool] | None = None) -> None:
        if interrupted is None:
            interrupted = []
            signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
        while not interrupted:
            signal.pause()


def main() -> int:
    code = Fake(sys.argv[1:], dict(os.environ)).run()
    return 0 if code is None else code
