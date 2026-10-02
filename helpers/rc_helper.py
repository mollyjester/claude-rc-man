"""rc-helper: control one user's Claude Remote Control servers.

rcman runs this as the target user (``sudo -u <user> rc-helper <verb> ...``).
It is the only code that touches that user's tmux and Claude processes. Every
verb prints one JSON object on stdout; failures print ``{"error": {"code",
"message"}}`` and exit 1 (2 for a malformed command line). Secrets are read
from stdin, never from the command line.

The user's home comes from the password database, not from ``$HOME``. Three
environment variables are test hooks — ``RC_HELPER_HOME``, ``RC_HELPER_CLAUDE``
and ``RC_HELPER_TMUX_SOCKET`` — and the sudoers rule resets the environment,
so they never reach a helper started through sudo.

Python standard library and the helpers' own modules only: it runs under the
system interpreter.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from helpers.users_file import EMAIL

VERBS = (
    "tree",
    "list",
    "start",
    "resume",
    "stop",
    "forget",
    "output",
    "confirm",
    "auth-status",
    "auth-login",
    "auth-code",
)

MESSAGES = {
    "usage": "The command line was not understood.",
    "internal_error": "rc-helper failed unexpectedly.",
    "refused_as_root": "rc-helper does not run as root.",
    "path_invalid": "The folder must be a path relative to your home folder.",
    "path_is_home": "Choose a folder inside your home folder, not the home folder itself.",
    "path_outside_home": "The folder is outside your home folder.",
    "path_not_found": "The folder does not exist.",
    "path_not_directory": "That is a file, not a folder.",
    "depth_invalid": "The folder depth is out of range.",
    "prefix_invalid": (
        "The session name prefix may use letters, digits and hyphens, "
        "starting with a letter or digit."
    ),
    "email_invalid": "That is not a valid email address.",
    "claude_not_installed": "Claude Code is not installed for this account.",
    "claude_not_logged_in": "Connect your Claude account first.",
    "claude_unrecognised_output": "Claude Code answered with output rc-helper does not recognise.",
    "already_running": "A server is already running in this folder.",
    "not_running": "No server is running in this folder.",
    "not_stopped": "There is no server in this folder that stopped and can be resumed.",
    "still_running": "Stop the server before forgetting it.",
    "unknown_server": "There is no server for this folder.",
    "nothing_to_confirm": "The server is not asking anything.",
    "confirm_not_accepted": "The server did not accept the answer.",
    "timeout": "Claude Code did not respond in time.",
    "no_login_in_progress": "Start connecting your Claude account first.",
    "login_failed": "Signing in to Claude failed. Start again and use a fresh code.",
}

TMUX_SOCKET_NAME = "rcman"
TMUX_CONF = Path(__file__).resolve().parent / "tmux.conf"
SESSION_PREFIX = "rc-"
LOGIN_SESSION = "rc-login"
PATH_OPTION = "@rcman_path"
WIDTH, HEIGHT = 200, 50
MAX_TREE_DEPTH = 3
MAX_PREFIX_LENGTH = 32
MAX_PATH_LENGTH = 1024
PREFIX = re.compile(rf"[A-Za-z0-9][A-Za-z0-9-]{{0,{MAX_PREFIX_LENGTH - 1}}}")
MAX_AUTHOR_NAME_LENGTH = 128
# The Hangul fillers, as (first, last): letters that show as nothing.
HANGUL_FILLERS = ((0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0))
# Code points a commit author's name may not hold anywhere, as (first, last).
_NOT_IN_AUTHOR_NAME = (
    (0x00, 0x1F),  # control characters
    (0x7F, 0x9F),
    (0x3C, 0x3C),  # < and >, which git puts around the address
    (0x3E, 0x3E),
    (0x2028, 0x2029),  # line and paragraph separators
    # Invisible format characters (Unicode category Cf), which make two names
    # look alike; tests/test_rc_helper.py derives them from the Unicode
    # database. Not ZWNJ and ZWJ (_NOT_ALONE_OR_AT_AN_END): Persian and Indic
    # names are written with them.
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200B),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
    *HANGUL_FILLERS,
)
# What git strips from either end of a name besides white space: crud() in
# git's ident.c. tests/test_rc_helper.py asks real git.
_GIT_TRIMS = ",:;<>\"'\\"
# ZWNJ and ZWJ, which join or part the characters beside them: a name may
# hold them, but not alone and not at either end.
_NOT_ALONE_OR_AT_AN_END = (0x200C, 0x200D)


def _escape(code: int) -> str:
    """A code point as an escape both Python's re and the backend's validator read."""
    return f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}"


_NOWHERE = "".join(
    _escape(first) if first == last else f"{_escape(first)}-{_escape(last)}"
    for first, last in _NOT_IN_AUTHOR_NAME
)
_NOT_AT_ENDS = (
    _NOWHERE
    + "".join(_escape(ord(c)) for c in _GIT_TRIMS)
    + "".join(_escape(code) for code in _NOT_ALONE_OR_AT_AN_END)
    + r"\s"
)
# No lookaround, so the backend's validator reads it as this module does.
AUTHOR_NAME = re.compile(
    rf"[^{_NOT_AT_ENDS}]"
    rf"(?:[^{_NOWHERE}]{{0,{MAX_AUTHOR_NAME_LENGTH - 2}}}[^{_NOT_AT_ENDS}])?"
)
# The trust prompt refuses an answer that arrives too quickly after the question.
ANSWER_PACE_SECONDS = 0.5
START_TIMEOUT = 30.0
STOP_TIMEOUT = 30.0
# States a server can be resumed from: stopped when asked, or stopped by itself.
RESUMABLE = ("stopped", "exited")
LOGIN_TIMEOUT = 60.0
POLL_SECONDS = 0.1
UNRECOGNISED_GRACE_SECONDS = 3.0

# What Claude Code shows; held to the recorded fixtures by tests/test_rc_helper.py.
TRUST_PROMPT = re.compile(r"^Trust .+\? \[y/N\]$")
TOO_FAST = "answer arrived too quickly"
CAPACITY_CONNECTED = re.compile(r"^\s*Capacity: 0/\d+")
CAPACITY_READY = re.compile(r"^\s*Capacity: [1-9]\d*/\d+")
ENVIRONMENT_URL = re.compile(r"https://claude\.ai/code\?environment=\S+")
LOGGED_OUT = "You must be logged in to use Remote Control"
TRUST_DECLINED = "Remote Control did not start:"
STOPPED = "Environment preserved."
LOGIN_URL = re.compile(r"https://claude\.com/cai/oauth/authorize\?\S+")
LOGIN_PROMPT = "Paste code here if prompted"
LOGIN_FAILED = "Login failed"
LOGIN_SUCCEEDED = "Login successful"
CREDENTIALS = Path(".claude") / ".credentials.json"  # relative to the home folder


class HelperError(Exception):
    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class Config:
    home: Path
    claude: Path
    tmux_socket: Path | None  # None: the named socket TMUX_SOCKET_NAME


def config_from_env(env: dict[str, str]) -> Config:
    home = Path(env.get("RC_HELPER_HOME") or pwd.getpwuid(os.getuid()).pw_dir)
    claude = Path(env.get("RC_HELPER_CLAUDE") or home / ".local" / "bin" / "claude")
    socket = env.get("RC_HELPER_TMUX_SOCKET")
    return Config(home=home, claude=claude, tmux_socket=Path(socket) if socket else None)


def child_env(cfg: Config) -> dict[str, str]:
    """The only environment Claude Code is started with."""
    user = pwd.getpwuid(os.getuid()).pw_name
    return {
        "HOME": str(cfg.home),
        "USER": user,
        "LOGNAME": user,
        "PATH": f"{cfg.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/bin/false",
    }


def _command(cfg: Config, *args: str) -> list[str]:
    env = [f"{k}={v}" for k, v in child_env(cfg).items()]
    return ["/usr/bin/env", "-i", *env, str(cfg.claude), *args]


def server_command(cfg: Config, prefix: str) -> list[str]:
    return _command(
        cfg,
        "remote-control",
        "--remote-control-session-name-prefix",
        prefix,
        "--permission-mode",
        "default",
    )


# --- paths ------------------------------------------------------------------------


def resolve_folder(home: Path, rel: str, allow_home: bool = False) -> Path:
    if "\0" in rel or rel.startswith("/") or len(rel) > MAX_PATH_LENGTH:
        raise HelperError("path_invalid")
    root = home.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HelperError("path_outside_home")
    if target == root and not allow_home:
        raise HelperError("path_is_home")
    if not target.exists():
        raise HelperError("path_not_found")
    if not target.is_dir():
        raise HelperError("path_not_directory")
    return target


def relative(cfg: Config, folder: Path) -> str:
    return folder.relative_to(cfg.home.resolve()).as_posix()


def session_for(rel: str) -> str:
    return SESSION_PREFIX + hashlib.sha256(rel.encode()).hexdigest()[:16]


# --- screens ------------------------------------------------------------------------


def _session_name(lines: list[str]) -> str | None:
    for i, line in enumerate(lines[:-1]):
        if CAPACITY_READY.match(line):
            name = lines[i + 1].strip()
            return name or None
    return None


def classify(lines: list[str], exit_code: int | None) -> dict:
    """The state of a Remote Control server from its screen and exit status."""
    text = "\n".join(lines)
    url = ENVIRONMENT_URL.search(text)
    info = {
        "environment_url": url.group(0) if url else None,
        "session_name": _session_name(lines),
        "exit_code": exit_code,
    }
    nonempty = [line for line in lines if line.strip()]
    if exit_code is not None:
        if LOGGED_OUT in text:
            state = "logged_out"
        elif TRUST_DECLINED in text:
            state = "trust_declined"
        elif exit_code == 0 and STOPPED in text:
            state = "stopped"
        else:
            state = "exited"
    elif not nonempty:
        state = "starting"
    elif TRUST_PROMPT.match(nonempty[-1].strip()):
        state = "trust_prompt"
    elif any(CAPACITY_READY.match(line) for line in lines):
        state = "ready"
    elif any(CAPACITY_CONNECTED.match(line) for line in lines):
        state = "connected"
    else:
        state = "unrecognised"
    info["state"] = state
    if state in ("unrecognised", "exited"):
        info["screen"] = lines
    return info


def classify_login(lines: list[str], exit_code: int | None) -> dict:
    text = "\n".join(lines)
    url = LOGIN_URL.search(text)
    if LOGIN_FAILED in text:
        state = "failed"
    elif LOGIN_SUCCEEDED in text:
        state = "succeeded"
    elif exit_code is not None:
        state = "exited"
    elif LOGIN_PROMPT in text and url:
        state = "awaiting_code"
    elif not text.strip():
        state = "starting"
    else:
        state = "unrecognised"
    return {"state": state, "url": url.group(0) if url else None, "exit_code": exit_code}


# --- tmux -----------------------------------------------------------------------------


def _zombie_status(pid: int) -> int | None:
    """The wait status of an exited, not yet reaped process, from ``/proc``."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    if fields[0] != "Z":
        return None
    return int(fields[-1])


def pane_exit_status(dead: str, status: str, signal: str, pid: str) -> int | None:
    """Exit status from tmux's pane fields; ``None`` while running or not yet known.

    tmux can mark a pane dead before it has reaped the process, leaving both
    status fields empty; the zombie's own exit status is then read instead.
    """
    if dead != "1":
        return None
    if status:
        return int(status)
    if signal:
        return 128 + int(signal)
    wait_status = _zombie_status(int(pid)) if pid else None
    if wait_status is None:
        return None
    code = os.waitstatus_to_exitcode(wait_status)
    return code if code >= 0 else 128 - code


Runner = Callable[..., subprocess.CompletedProcess]


class Tmux:
    def __init__(self, cfg: Config, runner: Runner = subprocess.run):
        socket = ["-S", str(cfg.tmux_socket)] if cfg.tmux_socket else ["-L", TMUX_SOCKET_NAME]
        self.base = ["tmux", *socket, "-f", str(TMUX_CONF)]
        self.runner = runner

    def run(self, *args: str, input: str | None = None, check: bool = True) -> str:
        proc = self.runner([*self.base, *args], input=input, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"tmux {args[0]}: {proc.stderr.strip()}")
        return proc.stdout

    def exists(self, session: str) -> bool:
        return (
            self.runner(
                [*self.base, "has-session", "-t", f"={session}"], capture_output=True
            ).returncode
            == 0
        )

    def new(self, session: str, cwd: Path, argv: list[str], options: dict[str, str]) -> bool:
        """Create the session running ``argv``; False if it already exists."""
        args = [
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            str(WIDTH),
            "-y",
            str(HEIGHT),
            "-c",
            str(cwd),
            *argv,
        ]
        proc = self.runner([*self.base, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            if self.exists(session):
                return False
            raise RuntimeError(f"tmux new-session: {proc.stderr.strip()}")
        for key, value in options.items():
            self.run("set-option", "-t", f"={session}:", key, value)
        return True

    def respawn(self, session: str, cwd: Path, argv: list[str]) -> None:
        self.run("respawn-pane", "-k", "-t", f"={session}:", "-c", str(cwd), *argv)
        self.run("clear-history", "-t", f"={session}:")

    def kill(self, session: str) -> None:
        self.run("kill-session", "-t", f"={session}", check=False)

    def screen(self, session: str) -> list[str]:
        raw = self.run("capture-pane", "-p", "-J", "-t", f"={session}:")
        lines = [line.rstrip() for line in raw.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def exit_status(self, session: str) -> int | None:
        fmt = "#{pane_dead}:#{pane_dead_status}:#{pane_dead_signal}:#{pane_pid}"
        fields = self.run("display-message", "-p", "-t", f"={session}:", fmt).strip().split(":")
        return pane_exit_status(*fields)

    def sessions(self) -> list[tuple[str, str]]:
        """(session name, folder) for every rcman server session."""
        out = self.run(
            "list-sessions", "-F", "#{session_name}\t#{" + PATH_OPTION + "}", check=False
        )
        pairs = []
        for line in out.splitlines():
            name, _, path = line.partition("\t")
            if name.startswith(SESSION_PREFIX) and name != LOGIN_SESSION and path:
                pairs.append((name, path))
        return pairs

    def type(self, session: str, text: str) -> None:
        self.run("send-keys", "-t", f"={session}:", "-l", text)
        self.run("send-keys", "-t", f"={session}:", "Enter")

    def paste_line(self, session: str, secret: str) -> None:
        """Type ``secret`` and Enter without it ever appearing in an argv."""
        buffer = "rcman-secret"
        self.run("load-buffer", "-b", buffer, "-", input=secret)
        self.run("paste-buffer", "-d", "-b", buffer, "-t", f"={session}:")
        self.run("send-keys", "-t", f"={session}:", "Enter")

    def interrupt(self, session: str) -> None:
        self.run("send-keys", "-t", f"={session}:", "C-c")


# --- verbs ----------------------------------------------------------------------------


class Helper:
    def __init__(self, cfg: Config, tmux: Tmux | None = None):
        self.cfg = cfg
        self.tmux = tmux or Tmux(cfg)

    def _server(self, session: str, rel: str) -> dict:
        info = classify(self.tmux.screen(session), self.tmux.exit_status(session))
        return {"path": rel, **info}

    def _wait(self, session: str, rel: str, until: Callable[[dict], bool], timeout: float) -> dict:
        """Poll until ``until`` holds.

        Screens in transition match no known state, so an unrecognised screen
        counts as an answer only once it has lasted UNRECOGNISED_GRACE_SECONDS.
        """
        deadline = time.monotonic() + timeout
        unrecognised_since = None
        while True:
            server = self._server(session, rel)
            if server["state"] == "unrecognised":
                unrecognised_since = unrecognised_since or time.monotonic()
                if time.monotonic() - unrecognised_since >= UNRECOGNISED_GRACE_SECONDS:
                    return server
            else:
                unrecognised_since = None
                if until(server):
                    return server
            if time.monotonic() > deadline:
                raise HelperError("timeout", {"server": server})
            time.sleep(POLL_SECONDS)

    def _settled(self, session: str, rel: str) -> dict:
        return self._wait(session, rel, lambda s: s["state"] != "starting", START_TIMEOUT)

    def _require_claude(self) -> None:
        if not self.cfg.claude.exists():
            raise HelperError("claude_not_installed")

    def _require_login(self) -> None:
        if not self.auth_status()["logged_in"]:
            raise HelperError("claude_not_logged_in")

    def tree(self, rel: str, depth: int, show_all: bool) -> dict:
        if not 1 <= depth <= MAX_TREE_DEPTH:
            raise HelperError("depth_invalid")
        folder = resolve_folder(self.cfg.home, rel, allow_home=True)
        return {
            "path": relative(self.cfg, folder) if rel else "",
            "entries": self._entries(folder, depth, show_all),
        }

    def _entries(self, folder: Path, depth: int, show_all: bool) -> list[dict]:
        root = self.cfg.home.resolve()
        entries = []
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for child in children:
            if not show_all and child.name.startswith("."):
                continue
            try:
                target = child.resolve()
                if not target.is_dir() or (target != root and root not in target.parents):
                    continue
            except OSError:
                continue
            rel = (folder / child.name).relative_to(root).as_posix()
            sub = self._entries(target, depth - 1, show_all) if depth > 1 else None
            has_children = (
                bool(sub) if sub is not None else bool(self._entries(target, 1, show_all))
            )
            entry = {"name": child.name, "path": rel, "has_children": has_children}
            if sub is not None:
                entry["children"] = sub
            entries.append(entry)
        return entries

    def list(self) -> dict:
        return {
            "servers": sorted(
                (self._server(s, rel) for s, rel in self.tmux.sessions()), key=lambda s: s["path"]
            )
        }

    def start(self, rel: str, prefix: str) -> dict:
        folder, rel = self._folder(rel)
        self._check_prefix(prefix)
        self._require_claude()
        self._require_login()
        session = session_for(rel)
        if self.tmux.exists(session):
            if self.tmux.exit_status(session) is None:
                raise HelperError("already_running")
            self.tmux.respawn(session, folder, server_command(self.cfg, prefix))
        elif not self.tmux.new(
            session, folder, server_command(self.cfg, prefix), {PATH_OPTION: rel}
        ):
            raise HelperError("already_running")
        return {"server": self._settled(session, rel)}

    def resume(self, rel: str, prefix: str) -> dict:
        folder, rel = self._folder(rel)
        session = session_for(rel)
        if not self.tmux.exists(session) or self._server(session, rel)["state"] not in RESUMABLE:
            raise HelperError("not_stopped")
        return self.start(rel, prefix)

    def stop(self, rel: str) -> dict:
        _, rel = self._folder(rel)
        session = self._existing(rel)
        if self.tmux.exit_status(session) is not None:
            raise HelperError("not_running")
        self.tmux.interrupt(session)
        server = self._wait(session, rel, lambda s: s["exit_code"] is not None, STOP_TIMEOUT)
        return {"server": server}

    def forget(self, rel: str) -> dict:
        _, rel = self._folder(rel)
        session = self._existing(rel)
        if self.tmux.exit_status(session) is None:
            raise HelperError("still_running")
        self.tmux.kill(session)
        return {"forgotten": rel}

    def output(self, rel: str) -> dict:
        _, rel = self._folder(rel)
        session = self._existing(rel)
        return {
            "path": rel,
            "screen": self.tmux.screen(session),
            "server": self._server(session, rel),
        }

    def confirm(self, rel: str, answer: str) -> dict:
        _, rel = self._folder(rel)
        session = self._existing(rel)
        if self._server(session, rel)["state"] != "trust_prompt":
            raise HelperError("nothing_to_confirm")
        for _attempt in range(2):
            time.sleep(ANSWER_PACE_SECONDS)
            self.tmux.type(session, answer)
            server = self._wait(session, rel, lambda s: self._answered(session, s), START_TIMEOUT)
            if server["state"] != "trust_prompt":
                return {"server": server}
        raise HelperError("confirm_not_accepted", {"server": server})

    def _answered(self, session: str, server: dict) -> bool:
        """The prompt went away, or Claude asked again because the answer was too fast."""
        if server["state"] != "trust_prompt":
            return server["state"] != "starting"
        lines = [line for line in self.tmux.screen(session) if line.strip()]
        return len(lines) > 1 and TOO_FAST in lines[-2]

    def auth_status(self) -> dict:
        self._require_claude()
        proc = subprocess.run(
            _command(self.cfg, "auth", "status"),
            cwd=self.cfg.home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise HelperError(
                "claude_unrecognised_output", {"screen": proc.stdout.splitlines()}
            ) from None
        if not data.get("loggedIn"):
            return {"logged_in": False}
        return {
            "logged_in": True,
            "email": data.get("email"),
            "org_name": data.get("orgName"),
            "subscription_type": data.get("subscriptionType"),
            "auth_method": data.get("authMethod"),
            "login_expires_at": self._login_expires_at(),
        }

    def _login_expires_at(self) -> str | None:
        """When the sign-in itself expires, from Claude Code's credentials file.

        Only ``claudeAiOauth.refreshTokenExpiresAt`` is read; it is an internal
        file, so anything unexpected means the expiry is unknown.
        """
        try:
            data = json.loads((self.cfg.home / CREDENTIALS).read_text(encoding="utf-8"))
            millis = data["claudeAiOauth"]["refreshTokenExpiresAt"]
            return datetime.datetime.fromtimestamp(int(millis) / 1000, datetime.UTC).isoformat()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def auth_login(self, email: str) -> dict:
        if not EMAIL.fullmatch(email):
            raise HelperError("email_invalid")
        self._require_claude()
        self.tmux.kill(LOGIN_SESSION)
        argv = _command(self.cfg, "auth", "login", "--claudeai", "--email", email)
        self.tmux.new(LOGIN_SESSION, self.cfg.home, argv, {})
        login = self._wait_login(lambda s: s["state"] != "starting")
        if login["state"] != "awaiting_code":
            raise HelperError(
                "claude_unrecognised_output", {"screen": self.tmux.screen(LOGIN_SESSION)}
            )
        return {"url": login["url"]}

    def auth_code(self, code: str) -> dict:
        if not self.tmux.exists(LOGIN_SESSION) or self._login()["state"] != "awaiting_code":
            raise HelperError("no_login_in_progress")
        self.tmux.paste_line(LOGIN_SESSION, code.strip())
        login = self._wait_login(
            lambda s: s["exit_code"] is not None or s["state"] in ("failed", "succeeded")
        )
        if login["state"] == "succeeded" and login["exit_code"] is None:
            self.tmux.type(LOGIN_SESSION, "")  # Claude Code waits for Enter after signing in
            self._wait_login(lambda s: s["exit_code"] is not None)
        self.tmux.kill(LOGIN_SESSION)
        status = self.auth_status()
        if not status["logged_in"]:
            raise HelperError("login_failed")
        return status

    def _login(self) -> dict:
        return classify_login(self.tmux.screen(LOGIN_SESSION), self.tmux.exit_status(LOGIN_SESSION))

    def _wait_login(self, until: Callable[[dict], bool]) -> dict:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while not until(login := self._login()):
            if time.monotonic() > deadline:
                raise HelperError("timeout")
            time.sleep(POLL_SECONDS)
        return login

    def _folder(self, rel: str) -> tuple[Path, str]:
        folder = resolve_folder(self.cfg.home, rel)
        return folder, relative(self.cfg, folder)

    def _existing(self, rel: str) -> str:
        session = session_for(rel)
        if not self.tmux.exists(session):
            raise HelperError("unknown_server")
        return session

    @staticmethod
    def _check_prefix(prefix: str) -> None:
        if not PREFIX.fullmatch(prefix):
            raise HelperError("prefix_invalid")


# --- command line ----------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # usage errors are JSON like every other
        raise HelperError("usage", {"detail": message})


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="rc-helper", description="Control your Claude Remote Control servers.")
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    tree = verbs.add_parser("tree", help="list subfolders of a folder in your home")
    tree.add_argument("path", nargs="?", default="")
    tree.add_argument("--depth", type=int, default=1)
    tree.add_argument("--all", action="store_true", help="include dot-folders")
    verbs.add_parser("list", help="every server and its state")
    for name, text in [
        ("start", "start a server in a folder"),
        ("resume", "resume a stopped server"),
    ]:
        p = verbs.add_parser(name, help=text)
        p.add_argument("path")
        p.add_argument("--prefix", required=True, help="session name prefix")
    for name, text in [
        ("stop", "stop a running server"),
        ("forget", "remove a stopped server from the list"),
        ("output", "the server's current screen"),
    ]:
        verbs.add_parser(name, help=text).add_argument("path")
    confirm = verbs.add_parser("confirm", help="answer the server's question")
    confirm.add_argument("path")
    confirm.add_argument("--answer", choices=["y", "n"], required=True)
    verbs.add_parser("auth-status", help="whether a Claude account is connected")
    login = verbs.add_parser("auth-login", help="start connecting a Claude account")
    login.add_argument("--email", required=True)
    verbs.add_parser("auth-code", help="finish connecting; the code is read from stdin")
    return parser


def _dispatch(helper: Helper, args: argparse.Namespace, stdin) -> dict:
    v = args.verb
    if v == "tree":
        return helper.tree(args.path, args.depth, args.all)
    if v == "list":
        return helper.list()
    if v in ("start", "resume"):
        return getattr(helper, v)(args.path, args.prefix)
    if v in ("stop", "forget", "output"):
        return getattr(helper, v)(args.path)
    if v == "confirm":
        return helper.confirm(args.path, args.answer)
    if v == "auth-status":
        return helper.auth_status()
    if v == "auth-login":
        return helper.auth_login(args.email)
    return helper.auth_code(stdin.readline())


def main(argv: list[str], env: dict[str, str], stdin=sys.stdin) -> int:
    try:
        if os.getuid() == 0:
            raise HelperError("refused_as_root")
        args = _parser().parse_args(argv)
        result = _dispatch(Helper(config_from_env(env)), args, stdin)
        print(json.dumps(result))
        return 0
    except HelperError as error:
        body = {"code": error.code, "message": MESSAGES[error.code], **error.detail}
        print(json.dumps({"error": body}))
        return 2 if error.code == "usage" else 1
    except Exception:  # noqa: BLE001 - the contract is JSON on stdout, always
        print(
            json.dumps({"error": {"code": "internal_error", "message": MESSAGES["internal_error"]}})
        )
        return 1
