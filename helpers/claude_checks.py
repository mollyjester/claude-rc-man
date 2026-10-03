"""Checks a candidate Claude Code version must pass before rcman rolls it out.

They run the candidate with a throwaway configuration, so no account is
needed, on a pseudo-terminal as rcman runs it, and look for exactly what
rcman relies on: the version, ``auth status`` JSON, the logged-out refusal of
``remote-control``, the sign-in screen, the options rcman passes to
``auth login``, and ``auth logout``. What needs a signed-in account — the
trust question and the server screens — is covered by the recorded tests
only.

Python standard library and the helpers' own modules only: rc-admin runs these
as root.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from pathlib import Path

from helpers import rc_helper

# One control token at a time: CSI sequences, OSC strings, other escapes, or a
# single control character.
TOKEN = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"  # CSI: parameters, final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, e.g. window titles
    r"|\x1b[ -/]*[0-~]"  # other escapes, e.g. save/restore cursor, character sets
    r"|[\x00-\x1f]"
)


def strip_terminal(text: str) -> str:
    """The text a terminal would show, line by line, without its control sequences.

    Renders what the checks depend on — carriage return, newline, cursor moves
    along a line (``C``, ``D``, ``G``) and erase to end of line (``K``) — and
    ignores every other control.
    """
    lines: list[list[str]] = [[]]
    col = 0

    def put(ch: str) -> None:
        nonlocal col
        line = lines[-1]
        line.extend(" " * (col - len(line)))
        if col < len(line):
            line[col] = ch
        else:
            line.append(ch)
        col += 1

    pos = 0
    for match in TOKEN.finditer(text):
        for ch in text[pos : match.start()]:
            put(ch)
        pos = match.end()
        token = match.group(0)
        params, final = match.group(1), match.group(2)
        if token == "\n":
            lines.append([])
            col = 0
        elif token == "\r":
            col = 0
        elif token == "\t":
            col = (col // 8 + 1) * 8
        elif final in ("C", "D", "G") and (params == "" or params.isdigit()):
            n = int(params) if params else 1
            col = col + n if final == "C" else max(0, col - n) if final == "D" else n - 1
        elif final == "K" and params in ("", "0"):
            del lines[-1][col:]
    for ch in text[pos:]:
        put(ch)
    return "\n".join("".join(line).rstrip() for line in lines)


TIMEOUT = 60.0
CHECK_EMAIL = "check@example.com"

# run(args, until=None, timeout=...) -> (exit code, or None if stopped at ``until``; output)
Runner = Callable[..., tuple[int | None, str]]


# The pseudo-terminal's size. Claude wraps its output, auth status's JSON included, at
# the terminal's width; a line holding any path must fit in one.
ROWS = 50
COLUMNS = 2 * os.pathconf("/", "PC_PATH_MAX")


def run_in_pty(argv: list[str], env: dict[str, str], cwd: Path, until: str | None, timeout: float):
    """Run ``argv`` on a pseudo-terminal of ROWS by COLUMNS; stop at ``until`` or exit.
    Output without escapes."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    proc = subprocess.Popen(
        argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, start_new_session=True
    )
    os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    pattern = re.compile(until) if until else None
    exit_code = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:  # every writer closed the terminal: the program is done
                    data = b""
                if not data:
                    exit_code = proc.wait(timeout=10)
                    break
                chunks.append(data)
                if pattern and pattern.search(
                    strip_terminal(b"".join(chunks).decode(errors="replace"))
                ):
                    break
            elif proc.poll() is not None:
                exit_code = proc.returncode
                break
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        os.close(master)
    return exit_code, strip_terminal(b"".join(chunks).decode(errors="replace"))


def local_runner(binary: str, env: dict[str, str], cwd: Path, config_dir: Path) -> Runner:
    """Run ``binary`` with ``CLAUDE_CONFIG_DIR`` set to a fresh ``config_dir``."""

    def run(args: list[str], until: str | None = None, timeout: float = TIMEOUT):
        config_dir.mkdir(parents=True, exist_ok=True)
        full_env = {**env, "CLAUDE_CONFIG_DIR": str(config_dir), "DISABLE_AUTOUPDATER": "1"}
        return run_in_pty([binary, *args], full_env, cwd, until, timeout)

    return run


def _version(run: Runner, version: str) -> bool:
    code, out = run(["--version"])
    return code == 0 and version in out


def _auth_status(run: Runner, version: str) -> bool:
    _code, out = run(["auth", "status"])
    try:
        return json.loads(out.strip()).get("loggedIn") is False
    except (json.JSONDecodeError, AttributeError):
        return False


def _logged_out_refusal(run: Runner, version: str) -> bool:
    args = [
        "remote-control",
        "--remote-control-session-name-prefix",
        "check",
        "--permission-mode",
        "default",
    ]
    code, out = run(args)
    return (
        code not in (None, 0)
        and rc_helper.classify(out.splitlines(), code)["state"] == "logged_out"
    )


def _sign_in_screen(run: Runner, version: str) -> bool:
    args = ["auth", "login", "--claudeai", "--email", CHECK_EMAIL]
    _code, out = run(args, until=re.escape(rc_helper.LOGIN_PROMPT))
    return rc_helper.classify_login(out.splitlines(), None)["state"] == "awaiting_code"


def _login_options(run: Runner, version: str) -> bool:
    _code, out = run(["auth", "login", "--help"])
    return all(option in out for option in ("--claudeai", "--email"))


LOGGED_OUT_MESSAGE = "logged out"  # "Successfully logged out from your Anthropic account."


def _logout(run: Runner, version: str) -> bool:
    code, out = run(["auth", "logout"])
    return code == 0 and LOGGED_OUT_MESSAGE in out.lower()


CHECKS: list[tuple[str, Callable[[Runner, str], bool]]] = [
    ("version", _version),
    ("auth_status", _auth_status),
    ("logged_out_refusal", _logged_out_refusal),
    ("sign_in_screen", _sign_in_screen),
    ("login_options", _login_options),
    ("logout", _logout),
]


def candidate_checks(
    run: Runner, version: str, on_check: Callable[[str], None] | None = None
) -> list[str]:
    """The names of the checks ``version`` fails; empty when it passes them all."""
    failed = []
    for name, check in CHECKS:
        if on_check:
            on_check(name)
        try:
            passed = check(run, version)
        except Exception:  # noqa: BLE001 - a check that cannot run has failed
            passed = False
        if not passed:
            failed.append(name)
    return failed
