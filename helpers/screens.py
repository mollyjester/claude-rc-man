"""What Claude Code shows on a server's screen, and the state rcman reads from it.

Pure functions of a screen's lines and the exit status, held to every recorded
fixture and observed screen by tests/test_screens.py. Shared by the backend,
the upgrade checks (claude_checks.py) and the recorder. Python standard library
only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

# The size of the window every server, and the sign-in, runs in.
WIDTH, HEIGHT = 200, 50
# States a server can be resumed from: stopped when asked, or stopped by itself.
RESUMABLE = ("stopped", "exited")

# What Claude Code shows; held to the recorded fixtures by tests/test_screens.py.
TRUST_PROMPT = re.compile(r"^Trust .+\? \[y/N\]$")
TOO_FAST = "answer arrived too quickly"
CAPACITY_CONNECTED = re.compile(r"^\s*Capacity: 0/\d+")
CAPACITY_READY = re.compile(r"^\s*Capacity: [1-9]\d*/\d+")
ENVIRONMENT_URL = re.compile(r"https://claude\.ai/code\?environment=\S+")
# A line Claude Code logs above its status block, and the one it logs once Remote
# Control has reconnected (tests/fakes/observed/).
LOG_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] \S.*$")
RECONNECTED = re.compile(r"^\[\d\d:\d\d:\d\d\] Reconnected after \d+s$")
LOGGED_OUT = "You must be logged in to use Remote Control"
TRUST_DECLINED = "Remote Control did not start:"
STOPPED = "Environment preserved."
LOGIN_URL = re.compile(r"https://claude\.com/cai/oauth/authorize\?\S+")
LOGIN_PROMPT = "Paste code here if prompted"
LOGIN_FAILED = "Login failed"
LOGIN_SUCCEEDED = "Login successful"


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
    elif RECONNECTED.match(nonempty[-1]) and all(LOG_LINE.match(line) for line in nonempty):
        # Claude has stopped drawing its status block, so nothing names the session or
        # the environment, and redraws it, if at all, on its own tick.
        state = "reconnected"
        info["session_name"] = info["environment_url"] = None
    else:
        state = "unrecognised"
    info["state"] = state
    if state in ("unrecognised", "reconnected", "exited"):
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


def zombie_status_of(stat: str) -> int | None:
    """The wait status of an exited, not yet reaped process, from its ``/proc/<pid>/stat``;
    None for one that is not a zombie, or a line that cannot be read."""
    try:
        fields = stat.rsplit(")", 1)[1].split()
        return int(fields[-1]) if fields[0] == "Z" else None
    except (IndexError, ValueError):
        return None


def _zombie_status(pid: int) -> int | None:
    """The wait status of an exited, not yet reaped process, from this machine's ``/proc``."""
    try:
        return zombie_status_of(Path(f"/proc/{pid}/stat").read_text())
    except OSError:
        return None


def pane_exit_status(
    dead: str,
    status: str,
    signal: str,
    pid: str,
    zombie_status: Callable[[int], int | None] | None = None,
) -> int | None:
    """Exit status from tmux's pane fields; ``None`` while running or not yet known.

    tmux can mark a pane dead before it has reaped the process, leaving both
    status fields empty; the zombie's own exit status is then read instead, by
    ``zombie_status``: by default from this machine's ``/proc``, and for a
    container from its own (servers.py).
    """
    if dead != "1":
        return None
    if status:
        return int(status)
    if signal:
        return 128 + int(signal)
    read = zombie_status or _zombie_status
    wait_status = read(int(pid)) if pid else None
    if wait_status is None:
        return None
    code = os.waitstatus_to_exitcode(wait_status)
    return code if code >= 0 else 128 - code
