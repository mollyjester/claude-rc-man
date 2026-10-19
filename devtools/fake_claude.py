"""A fake `claude` that replays the recorded screens in ``tests/fakes/fixtures/``.

It implements only the invocations rcman makes, as a state machine whose every
screen is a recorded fixture, or a screen observed on a live server, with its
placeholders filled in; any other invocation fails loudly. Its configuration folder
is ``CLAUDE_CONFIG_DIR``, or ``~/.claude-fake`` when that is unset, so it never
touches a real Claude configuration. Whether it is signed in is its credentials
file there, CREDENTIALS, as the real one's is: signed in while it holds
``claudeAiOauth``, and signed out when it is empty, ``{}`` or not there. Which
folders are trusted, and which session each folder last served, is
``fake-state.json`` there.

The credentials file is written as the real one writes it, by rename
(:func:`write_credentials`), and in place, truncated first, when the rename is
refused with EBUSY, as it is over a file bound into a workspace; a link is followed,
for the host runtime's links to a person's one file. ``auth logout`` removes it,
and leaves one it cannot remove (EBUSY). A sign-in also writes ``oauthAccount``
into ``.claude.json`` there, keeping every other key.

Setting ``FAKE_CLAUDE_SCENARIO=unrecognised`` makes ``remote-control`` and
``auth login`` show a synthetic screen that no recording contains, for testing
how unrecognised output is handled. ``FAKE_CLAUDE_SCENARIO=reconnected_without_status``
makes ``remote-control`` show the screen of that name in ``tests/fakes/observed/``,
seen on a live server (:func:`record_claude.load_observed`), and so does every
observed screen's name; what follows Ctrl+C there was not seen, so it ends as
the ``unrecognised`` scenario does. In both,
Ctrl+C ends it with exit status 1. ``FAKE_CLAUDE_VERSION`` replaces the version it
reports.

``FAKE_CLAUDE_SCENARIO=remote_control_prompt`` is the one observed screen that takes
an answer, so the fake reads one (:meth:`Fake.remote_control_prompt`): ``y`` goes on to
the connected and ready screens, as after the folder-trust question. Only the
screen was seen. Not seen, so unobserved: the screens after ``y``, the screen and exit status
after ``n`` (the fake exits 1, and so does anything but ``y``), Ctrl+C at the prompt (it
exits 1), and how soon after the question an answer may come (the fake takes any).
Nothing shows it unless asked: the default scenario never does.

An answer to the folder-trust prompt that arrives sooner than
:data:`MIN_ANSWER_SECONDS` after the prompt is refused with the recorded
"too quickly" screen and asked again, as the real `claude` does. The real
threshold has not been measured exactly; an instant answer is refused and one
a fraction of a second later is accepted.

A server shows the recorded *connected* screen (no session yet) for
:data:`READY_SECONDS` — overridable with ``FAKE_CLAUDE_READY_SECONDS`` — then
the *ready* screen. Interrupting a server before it is ready has not been
recorded, so the fake finishes becoming ready before it handles the interrupt.

Signing in succeeds for ACCEPTED_CODE and fails for any other, each with the
recorded screens. Tests that need a logged-in fake call :func:`write_state`.

``fake-exec COMMAND [ARGUMENT...]`` is the one verb the real `claude` has no
counterpart of: it runs the command with the environment the fake was started with
(but for ``FAKE_CLAUDE_VERSION``, the fake's own), and with the standard streams it
has, and ends with the command's exit status. A workspace starts Claude through
``env -i`` with exactly ``runtime.child_env``, so a command run this way sees
what a command Claude itself runs, as git does for it, would see. It needs no
sign-in and writes nothing. No command, or one that cannot be run, fails loudly.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

from devtools import record_claude as rec

STATE_FILE = "fake-state.json"
CREDENTIALS = ".credentials.json"
CONFIG_FILE = ".claude.json"
# The one code a sign-in accepts.
ACCEPTED_CODE = "fake-accepted-code#fake-state"
# How long the refresh token of a sign-in lasts.
REFRESH_DAYS = 365
FAKE_ACCOUNT = {
    "email": "fake-user@example.com",
    "org_id": "00000000-0000-0000-0000-000000000000",
    "org_name": "Fake Org",
    "subscription_type": "max",
}
# The one observed screen that takes an answer.
REMOTE_CONTROL_PROMPT = "remote_control_prompt"
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
    """The fake's folder as a sign-in, or none, leaves it; and its trusted folders and
    their sessions."""
    config_dir.mkdir(parents=True, exist_ok=True)
    state = {"trusted": trusted or [], "servers": servers or {}}
    (config_dir / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    if logged_in:
        sign_in(config_dir, FAKE_ACCOUNT["email"])
    elif (config_dir / CREDENTIALS).exists():
        (config_dir / CREDENTIALS).write_text("", encoding="utf-8")


def credentials() -> dict:
    """A sign-in's credentials, with the keys a real sign-in writes."""
    now = int(time.time() * 1000)
    return {
        "claudeAiOauth": {
            "accessToken": "fake-access-token",
            "refreshToken": "fake-refresh-token",
            "expiresAt": now + 8 * 3600 * 1000,
            "refreshTokenExpiresAt": now + REFRESH_DAYS * 86400 * 1000,
            "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
            "subscriptionType": FAKE_ACCOUNT["subscription_type"],
            "rateLimitTier": "default_claude_max_20x",
        }
    }


def oauth_account(email: str) -> dict:
    """The account a sign-in names in ``.claude.json``, with the keys a real one writes."""
    return {
        "accountUuid": "00000000-0000-0000-0000-000000000001",
        "emailAddress": email,
        "organizationUuid": FAKE_ACCOUNT["org_id"],
        "organizationName": FAKE_ACCOUNT["org_name"],
        "accountCreatedAt": "2026-01-01T00:00:00.000Z",
        "billingType": "stripe_subscription",
        "ccOnboardingFlags": {},
        "claudeCodeTrialDurationDays": None,
        "claudeCodeTrialEndsAt": None,
        "displayName": "Fake",
        "fullName": "Fake User",
        "hasExtraUsageEnabled": False,
        "organizationRateLimitTier": "default_claude_max_20x",
        "organizationRole": "admin",
        "organizationType": "claude_max",
        "profileFetchedAt": 0,
        "seatTier": None,
        "subscriptionCreatedAt": "2026-01-01T00:00:00.000Z",
        "userRateLimitTier": None,
        "workspaceRole": None,
    }


def sign_in(config_dir: Path, email: str) -> None:
    """Write what a sign-in as ``email`` writes: the credentials, and ``oauthAccount``."""
    write_credentials(config_dir / CREDENTIALS, credentials())
    path = config_dir / CONFIG_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    path.write_text(json.dumps({**config, "oauthAccount": oauth_account(email)}), "utf-8")


def signed_in(config_dir: Path) -> bool:
    try:
        found = json.loads((config_dir / CREDENTIALS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(found, dict) and isinstance(found.get("claudeAiOauth"), dict)


def write_credentials(path: Path, data: dict) -> None:
    """Replace the file, owner-only, by rename; in place when the rename is EBUSY."""
    target = Path(os.path.realpath(path))
    text = json.dumps(data)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f"{CREDENTIALS}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except OSError as error:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        if error.errno != errno.EBUSY:
            raise
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)


def remove_credentials(path: Path) -> None:
    """As ``auth logout`` does: remove the file; one that cannot be removed is left."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        if error.errno != errno.EBUSY:
            raise


def _load_state(directory: str) -> dict:
    path = Path(directory) / STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"trusted": [], "servers": {}}


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
            session_id="cse_fake" + hashlib.sha256(self.cwd.encode()).hexdigest()[:20],
            **FAKE_ACCOUNT,
        )

    def show(self, name: str, prompt: bool = False) -> int | None:
        return self.show_lines(self.fixtures[name].lines, self.fixtures[name].exit_code, prompt)

    def show_lines(self, lines: list[str], exit_code: int | None, prompt: bool = False):
        self.values["time"] = datetime.datetime.now().strftime("%H:%M:%S")
        text = rec.fill("\n".join(lines), self.values)
        clear = CLEAR if sys.stdout.isatty() else ""
        sys.stdout.write(clear + text + ("" if prompt else "\n"))
        sys.stdout.flush()
        return exit_code

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
            signed = signed_in(Path(self.state_dir))
            return self.show("auth_status_logged_in" if signed else "auth_status_logged_out")
        if a == ["auth", "login", "--help"]:
            return self.show("auth_login_help")
        if a[:2] == ["auth", "login"]:
            return self.login()
        if a[:2] == ["auth", "logout"]:
            remove_credentials(Path(self.state_dir) / CREDENTIALS)
            return self.show("auth_logout")
        if a[:1] == ["remote-control"]:
            return self.remote_control()
        if a[:1] == ["fake-exec"] and len(a) > 1:
            return self.fake_exec(a[1:])
        return self.unsupported()

    def fake_exec(self, command: list[str]) -> int:
        """``command`` run as a child of Claude: in the fake's own environment, on its
        own streams, without the version a release's launcher bakes in for the fake
        alone (the real Claude's children never see such a variable)."""
        child = {k: v for k, v in self.env.items() if k != "FAKE_CLAUDE_VERSION"}
        try:
            return subprocess.run(command, env=child, check=False).returncode
        except OSError as e:
            sys.stderr.write(f"fake claude: cannot run {command[0]}: {e}\n")
            return 127

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
        if code != ACCEPTED_CODE:
            return self.show("login_failed")
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        sign_in(Path(self.state_dir), email or FAKE_ACCOUNT["email"])
        self.show("login_succeeded", prompt=True)
        self.read_line()  # Claude Code waits for Enter
        return self.show("login_succeeded_done")

    def config(self) -> dict:
        try:
            found = json.loads((Path(self.state_dir) / CONFIG_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

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
        if "--help" in self.args:
            return self.unsupported()  # not recorded; the real one prints help, signed in or not
        if not signed_in(Path(self.state_dir)):
            return self.show("rc_logged_out")
        if not isinstance(self.config().get("oauthAccount"), dict):
            refusal = self.fixtures["rc_no_oauth_account"]
            sys.stderr.write(rec.fill("\n".join(refusal.lines), self.values) + "\n")
            return refusal.exit_code
        if self.env.get("FAKE_CLAUDE_SCENARIO") == "unrecognised":
            sys.stdout.write(CLEAR + UNRECOGNISED_SCREEN + "\n")
            sys.stdout.flush()
            self.wait_for_interrupt()
            return 1
        if self.env.get("FAKE_CLAUDE_SCENARIO") == REMOTE_CONTROL_PROMPT:
            return self.remote_control_prompt()
        # Every other observed screen is a scenario of its own name.
        if screen := rec.load_observed().get(self.env.get("FAKE_CLAUDE_SCENARIO", "")):
            self.show_lines(screen.lines, screen.exit_code)
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
        return self.serve(screens)

    def remote_control_prompt(self) -> int:
        """The observed one-time question of an account that has never used Remote Control,
        the one observed screen that takes an answer. Not observed: that `y` goes on as
        after the folder-trust question, that `n` and Ctrl+C end it with exit status 1,
        and how soon after the question an answer may come."""
        spawn = _option(self.args, "--spawn")
        if spawn not in (None, "same-dir"):
            return self.unsupported()
        screen = rec.load_observed()[REMOTE_CONTROL_PROMPT]
        self.show_lines(screen.lines, screen.exit_code, prompt=True)
        try:
            answer = self.read_line().strip().lower()
        except KeyboardInterrupt:
            return 1
        if answer != "y":
            return 1
        branch = _branch(self.cwd)
        if branch is not None:
            self.values["branch"] = branch
        server = "git_server" if branch is not None else "server"
        return self.serve(
            tuple(f"{server}_{s}_after_trust" for s in ("connected", "ready", "stopped"))
        )

    def serve(self, screens: tuple[str, str, str]) -> int:
        """A server that connects, becomes ready, and shows its last screen on Ctrl+C."""
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
        every answer is ignored. SIGTERM ends it, as it ends any process."""
        interrupted: list[bool] = []

        def interrupt(*_) -> None:
            interrupted.append(True)
            self.show("spawn_prompt_interrupted", prompt=True)

        signal.signal(signal.SIGINT, interrupt)
        self.show("spawn_prompt", prompt=True)
        answer = self.read_line()
        while interrupted:
            self.show("spawn_prompt_answered_after_interrupt", prompt=True)
            self.read_line()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
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
