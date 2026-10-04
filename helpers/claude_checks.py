"""Checks a candidate Claude Code version must pass before rcman rolls it out.

They run the candidate with a throwaway configuration, so no account is
needed, as rc-helper runs it: ``auth status`` through pipes, with no terminal,
and the rest on a pseudo-terminal of the size rc-helper gives Claude's screens
(``WIDTH`` and ``HEIGHT`` in rc_helper). They look for exactly what
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

# run(args, until=None, timeout=..., terminal=True)
#   -> (exit code, or None if stopped at ``until`` or at its timeout; output)
Runner = Callable[..., tuple[int | None, str]]


class Ran(tuple):
    """What a run gives a check: (exit code, output), as a ``Runner`` returns it.

    Through pipes it keeps, for the report only, what the check does not read:
    ``stdout`` as written even where the program did not exit, and ``stderr``."""

    def __new__(cls, code: int | None, output: str, stdout: str = "", stderr: str = ""):
        ran = super().__new__(cls, (code, output))
        ran.stdout, ran.stderr = stdout, stderr
        return ran


def run_in_pty(argv: list[str], env: dict[str, str], cwd: Path, until: str | None, timeout: float):
    """Run ``argv`` on a pseudo-terminal of the size rc-helper gives Claude's screens; stop
    at ``until`` or exit. Output without escapes."""
    master, slave = os.openpty()
    size = struct.pack("HHHH", rc_helper.HEIGHT, rc_helper.WIDTH, 0, 0)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
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


def _text(data: str | bytes | None) -> str:
    return data.decode(errors="replace") if isinstance(data, bytes) else data or ""


def run_piped(argv: list[str], env: dict[str, str], cwd: Path, timeout: float) -> Ran:
    """Run ``argv`` with no terminal, as rc-helper runs ``auth status``: stdin empty,
    stdout read. Output as written; none where it does not exit before ``timeout``."""
    try:
        proc = subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as stopped:
        return Ran(None, "", _text(stopped.stdout), _text(stopped.stderr))
    return Ran(proc.returncode, proc.stdout, proc.stdout, proc.stderr)


def run_command(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    until: str | None,
    timeout: float,
    terminal: bool = True,
):
    """Run ``argv`` on a terminal, or through pipes where ``terminal`` is false."""
    if terminal:
        return run_in_pty(argv, env, cwd, until, timeout)
    return run_piped(argv, env, cwd, timeout)


def local_runner(binary: str, env: dict[str, str], cwd: Path, config_dir: Path) -> Runner:
    """Run ``binary`` with ``CLAUDE_CONFIG_DIR`` set to a fresh ``config_dir``."""

    def run(
        args: list[str], until: str | None = None, timeout: float = TIMEOUT, terminal: bool = True
    ):
        config_dir.mkdir(parents=True, exist_ok=True)
        full_env = {**env, "CLAUDE_CONFIG_DIR": str(config_dir), "DISABLE_AUTOUPDATER": "1"}
        return run_command([binary, *args], full_env, cwd, until, timeout, terminal)

    return run


def _version(run: Runner, version: str) -> bool:
    code, out = run(["--version"])
    return code == 0 and version in out


def _auth_status(run: Runner, version: str) -> bool:
    _code, out = run(["auth", "status"], terminal=False)
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


SECRETS = [
    (rc_helper.LOGIN_URL, "{login_url}"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "{token}"),
]


def redact(text: str) -> str:
    """``text`` with every sign-in URL and every token replaced by a placeholder."""
    for pattern, placeholder in SECRETS:
        text = pattern.sub(placeholder, text)
    return text


def _describe(args: list[str], terminal: bool, ran: tuple) -> str:
    code, output = ran
    how = "on a terminal" if terminal else "through pipes"
    if code is not None:
        ended = f"exit code {code}"
    elif terminal:
        ended = "stopped before it exited"
    else:
        ended = "did not exit before its timeout"
    lines = [f"ran: claude {' '.join(args)} ({how}); {ended}"]
    if terminal:
        lines += ["output:", output]
    else:
        lines += ["stdout:", getattr(ran, "stdout", output)]
    stderr = getattr(ran, "stderr", "")
    if stderr:
        lines += ["stderr:", stderr]
    return "\n".join(lines)


def _failure(run: Runner, check: Callable[[Runner, str], bool], version: str) -> str | None:
    """What was run and what came back, where ``check`` fails; None where it passes."""
    runs: list[str] = []

    def recording(args, until=None, timeout=TIMEOUT, terminal=True):
        ran = run(args, until=until, timeout=timeout, terminal=terminal)
        runs.append(_describe(args, terminal, ran))
        return ran

    try:
        if check(recording, version):
            return None
    except Exception as error:  # noqa: BLE001 - a check that cannot run has failed
        runs.append(f"raised: {error!r}")
    return redact("\n".join(runs))


def candidate_failures(
    run: Runner, version: str, on_check: Callable[[str], None] | None = None
) -> dict[str, str]:
    """The checks ``version`` fails, each with what was run and what came back, with no
    sign-in URL or token in it; empty when it passes them all."""
    failed = {}
    for name, check in CHECKS:
        if on_check:
            on_check(name)
        detail = _failure(run, check, version)
        if detail is not None:
            failed[name] = detail
    return failed


def candidate_checks(
    run: Runner, version: str, on_check: Callable[[str], None] | None = None
) -> list[str]:
    """The names of the checks ``version`` fails; empty when it passes them all."""
    return list(candidate_failures(run, version, on_check))
