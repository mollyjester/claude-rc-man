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
import contextlib
import dataclasses
import datetime
import errno
import fcntl
import hashlib
import json
import os
import posixpath
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from helpers import git_run
from helpers.rules import Rule
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
    "mkdir",
    "delete",
    "git-status",
    "git-branches",
    "git-stage",
    "git-unstage",
    "git-commit",
    "git-switch",
    "git-branch-new",
    "git-fetch",
)

# Words both a message and the path rules' sentences say.
_INSIDE_HOME = "Choose a folder inside your home folder, not the home folder itself"

MESSAGES = {
    "usage": "The command line was not understood.",
    "internal_error": "rc-helper failed unexpectedly.",
    "refused_as_root": "rc-helper does not run as root.",
    "path_is_home": f"{_INSIDE_HOME}.",
    "path_ends_in_dot": (
        "Name the folder itself: its path may not end in . or .., which name only where they lead."
    ),
    "path_outside_home": "The folder is outside your home folder.",
    "path_not_found": "The folder does not exist.",
    "path_not_directory": "That is a file, not a folder.",
    "depth_invalid": "The folder depth is out of range.",
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
    "folder_exists": "Something with that name is there already.",
    "path_hidden": (
        "rcman does not delete a folder whose name begins with a dot, or anything in one."
    ),
    "folder_busy": "Your folders are being changed by another request. Try again in a moment.",
    "delete_stop_failed": "A server in the folder did not stop, so nothing was deleted.",
    "delete_forget_failed": (
        "A server in the folder could not be forgotten, so nothing was deleted."
    ),
    "delete_incomplete": (
        "Some of the folder was deleted, but not all of it; what is left is listed. "
        "Delete it again once that can be removed."
    ),
    "delete_failed": "The folder could not be deleted, and nothing in it was removed.",
    "path_name_differs": "Write the folder's path exactly as its names are, letter for letter.",
    "path_through_link": (
        "rcman does not delete through a symbolic link. Select the folder where it really is."
    ),
    "delete_mount_inside": (
        "Another file system is mounted inside the folder, so nothing was deleted."
    ),
    "delete_unsafe": (
        "Folders cannot be deleted safely on this system, so rcman does not delete them."
    ),
    # git
    "git_not_installed": "git is not installed on this machine.",
    "git_failed": "git refused. What it said is below.",
    "git_timeout": "git did not finish in time, so it was stopped.",
    "git_unrecognised_output": "git answered with output rc-helper does not recognise.",
    "not_a_repository": "This folder is not the top folder of a git repository.",
    "repository_outside_home": (
        "This repository keeps its git data outside your home folder, so rcman leaves it alone."
    ),
    "file_list_invalid": "The list of files was not understood.",
    "file_invalid": "A file name is not one rcman can stage or unstage.",
    "file_outside_repository": "A file is outside the repository.",
    "nothing_staged": "Nothing is staged, so there is nothing to commit.",
    "branch_invalid": "git does not take that as a branch name.",
    "branch_not_found": "There is no such branch.",
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
# A path as rc-helper takes one: relative, with no null character, which no
# path can hold, and of a length a path can have; empty for the home folder.
# FOLDER_PATH is the same without the empty path, where a folder is needed.
_PATH_PATTERN = rf"[^/\x00][^\x00]{{0,{MAX_PATH_LENGTH - 1}}}"
_PATH_WORDS = (
    "a path relative to your home folder, without a null character, and at most "
    f"{MAX_PATH_LENGTH} characters long"
)
PATH = Rule(re.compile(f"(?:{_PATH_PATTERN})?"), f"The folder must be {_PATH_WORDS}.")
FOLDER_PATH = Rule(re.compile(_PATH_PATTERN), f"{_INSIDE_HOME}, written as {_PATH_WORDS}.")
PREFIX = Rule(
    re.compile(rf"[A-Za-z0-9][A-Za-z0-9-]{{0,{MAX_PREFIX_LENGTH - 1}}}"),
    "The session name prefix may use letters a to z in either case, digits and hyphens, "
    f"start with a letter or digit, and be at most {MAX_PREFIX_LENGTH} characters long.",
)
MAX_AUTHOR_NAME_LENGTH = 128
# Code points no name may hold anywhere, because they show as nothing or as
# something they are not, and make two names look alike; each as a tuple of
# (first, last).
CONTROLS = ((0x00, 0x1F), (0x7F, 0x9F))
SEPARATORS = ((0x2028, 0x2029),)  # line and paragraph separators
# Invisible format characters (Unicode category Cf); tests/test_rc_helper.py
# derives them from the Unicode database. Not ZWNJ and ZWJ (JOINERS): Persian
# and Indic names are written with them.
INVISIBLE_FORMAT = (
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
)
# The Hangul fillers: letters that show as nothing.
HANGUL_FILLERS = ((0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0))
# ZWNJ and ZWJ, which join or part the characters beside them: a name may
# hold them, but not alone and not at either end.
JOINERS = (0x200C, 0x200D)


def _escape(code: int) -> str:
    """A code point as an escape both Python's re and the backend's validator read."""
    return f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}"


def _class(ranges=(), chars: str = "") -> str:
    """The inside of a character class holding ``ranges`` and ``chars``, as escapes."""
    spans = (_escape(a) if a == b else f"{_escape(a)}-{_escape(b)}" for a, b in ranges)
    return "".join(spans) + "".join(_escape(ord(c)) for c in chars)


# What no name may hold anywhere, and what it may besides not begin or end with,
# each with the words the rules' sentences say it in.
_UNSEEN = _class((*CONTROLS, *SEPARATORS, *INVISIBLE_FORMAT, *HANGUL_FILLERS))
_UNSEEN_WORDS = (
    "a control character or an invisible character other than the joiners some "
    "scripts need between letters"
)
_NOT_AT_AN_END = _class(chars="".join(map(chr, JOINERS))) + r"\s"
_NOT_AT_AN_END_WORDS = "white space"


def _either(words: list[str]) -> str:
    """Words as alternatives: "a, b or c"."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"


# A commit author's name may not hold < and >, which git puts around the
# address, nor begin or end with what git strips from either end besides white
# space: crud() in git's ident.c. tests/test_rc_helper.py asks real git.
_GIT_TRIMS = ",:;<>\"'\\"
_IN_ADDRESS_MARKS = "<>"
_CHARACTER_WORDS = {
    ",": "a comma",
    ":": "a colon",
    ";": "a semicolon",
    "<": "<",
    ">": ">",
    '"': "a quotation mark",
    "'": "an apostrophe",
    "\\": "a backslash",
}
_NOT_IN_AUTHOR_NAME = _UNSEEN + _class(chars=_IN_ADDRESS_MARKS)
_NOT_AT_AUTHOR_ENDS = _NOT_IN_AUTHOR_NAME + _NOT_AT_AN_END + _class(chars=_GIT_TRIMS)
# No lookaround, so the backend's validator reads it as this module does.
AUTHOR_NAME = Rule(
    re.compile(
        rf"[^{_NOT_AT_AUTHOR_ENDS}]"
        rf"(?:[^{_NOT_IN_AUTHOR_NAME}]{{0,{MAX_AUTHOR_NAME_LENGTH - 2}}}[^{_NOT_AT_AUTHOR_ENDS}])?"
    ),
    f"A commit author's name may not be empty or longer than {MAX_AUTHOR_NAME_LENGTH} "
    f"characters, hold {_either([_CHARACTER_WORDS[c] for c in _IN_ADDRESS_MARKS])}, "
    f"{_UNSEEN_WORDS}, or begin or end with "
    + _either(
        [_NOT_AT_AN_END_WORDS]
        + [_CHARACTER_WORDS[c] for c in _GIT_TRIMS if c not in _IN_ADDRESS_MARKS]
    )
    + ".",
)
# A folder's name may not hold a slash, nor begin with a dot: such a folder is
# hidden, and . and .. are not names. How long it may be is the file system's
# to say (check_folder_name).
_NOT_IN_FOLDER_NAME = _UNSEEN + _class(chars="/")
_NOT_AT_FOLDER_ENDS = _NOT_IN_FOLDER_NAME + _NOT_AT_AN_END
FOLDER_NAME = Rule(
    re.compile(
        rf"[^{_NOT_AT_FOLDER_ENDS}{_class(chars='.')}]"
        rf"(?:[^{_NOT_IN_FOLDER_NAME}]*[^{_NOT_AT_FOLDER_ENDS}])?"
    ),
    f"A folder's name may not be empty, begin with a dot, hold a slash, {_UNSEEN_WORDS}, "
    f"begin or end with {_NOT_AT_AN_END_WORDS}, or be longer than the file system allows.",
)
MAX_COMMIT_MESSAGE_LENGTH = 10_000
# A commit message must hold something besides what git counts as white space,
# which git would strip to nothing, and no NUL, at which git would cut it.
_GIT_SPACE = _class(chars=" \t\n\v\f\r")
_NUL = _class(chars="\0")
COMMIT_MESSAGE = Rule(
    re.compile(rf"[{_GIT_SPACE}]*[^{_GIT_SPACE}{_NUL}][^{_NUL}]*"),
    "A commit message must hold something other than white space, no NUL character, "
    f"and be at most {MAX_COMMIT_MESSAGE_LENGTH} characters long.",
)
# The rules rc-helper refuses a value by, each with the code it refuses it with;
# MESSAGES gives each code its rule's own sentence.
RULE_CODES = {
    "path_invalid": PATH,
    "prefix_invalid": PREFIX,
    "name_invalid": FOLDER_NAME,
    "email_invalid": EMAIL,
    "author_name_invalid": AUTHOR_NAME,
    "message_invalid": COMMIT_MESSAGE,
}
MESSAGES.update({code: rule.sentence for code, rule in RULE_CODES.items()})
# A message that states a length, so written once the length is.
MESSAGES["folder_path_too_long"] = (
    f"The new folder's path would be longer than {MAX_PATH_LENGTH} characters."
)
# The trust prompt refuses an answer that arrives too quickly after the question.
ANSWER_PACE_SECONDS = 0.5
START_TIMEOUT = 30.0
STOP_TIMEOUT = 30.0
# States a server can be resumed from: stopped when asked, or stopped by itself.
RESUMABLE = ("stopped", "exited")
LOGIN_TIMEOUT = 60.0
POLL_SECONDS = 0.1
# How many of the paths a delete could not remove its answer lists.
DELETE_REPORT_LIMIT = 50
UNRECOGNISED_GRACE_SECONDS = 3.0
# git: how long a verb's git calls may take, all together; fetch, over the network.
GIT_TIMEOUT = 30.0
GIT_NETWORK_TIMEOUT = 120.0
# How many files each of a status's two lists, and branches each list, holds;
# the rest are counted.
MAX_LISTED_FILES = 500
MAX_LISTED_BRANCHES = 200
# How many files one stage or unstage takes: every file a list can show.
MAX_FILES_PER_CALL = MAX_LISTED_FILES
MAX_BRANCH_NAME_LENGTH = 255
# Every change a file is listed with; docs/git.md has a row for each.
CHANGES = git_run.CHANGES

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


def git_env(cfg: Config) -> dict[str, str]:
    """The only environment git is started with: the server's, cut down, with git
    unable to ask anything on a terminal and taking no lock it does not need.

    Hooks run with it too, so they find the person's own programs on PATH;
    git itself is looked for on the system path only (git_run).
    """
    server = child_env(cfg)
    return {
        **{key: server[key] for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG")},
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
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


def check_rel(rel: str) -> None:
    """A path as rc-helper takes one: relative, and of a length a path can have."""
    if not PATH.fullmatch(rel):
        raise HelperError("path_invalid")


def confine(home: Path, rel: str, allow_home: bool = False) -> Path:
    """Where ``rel`` leads, following links as far as it exists, refused outside the home.

    Whether anything is there is not checked.
    """
    check_rel(rel)
    root = home.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HelperError("path_outside_home")
    if target == root and not allow_home:
        raise HelperError("path_is_home")
    return target


def resolve_folder(home: Path, rel: str, allow_home: bool = False) -> Path:
    target = confine(home, rel, allow_home)
    if not target.exists():
        raise HelperError("path_not_found")
    if not target.is_dir():
        raise HelperError("path_not_directory")
    check_names(home, relative(home, target))
    return target


def check_names(home: Path, rel: str) -> None:
    """Refuse a path whose parts are not the names on disk, letter for letter.

    A file system that ignores case reaches beta as Beta. Every folder a
    server is started in or a folder is made in is resolved through
    resolve_folder, which checks this, so servers are recorded, and folders
    found, under their names on disk only.
    """
    root = home.resolve()
    parts = rel.split("/") if rel else []
    for i, part in enumerate(parts):
        if part not in os.listdir(root.joinpath(*parts[:i])):
            raise HelperError("path_name_differs")


def _joined(parent_rel: str, name: str) -> str:
    return f"{parent_rel}/{name}" if parent_rel else name


def open_dir(root: Path, folder: Path) -> int:
    """An fd on ``folder``, a resolved folder in ``root``, reached without following a link.

    A part of the path that has become a link since it was resolved is refused.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in folder.relative_to(root).parts:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                raise HelperError("path_not_found") from None
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise HelperError("path_outside_home") from None
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_folder_name(name: str, parent_fd: int, parent_rel: str) -> str:
    """The path of a new folder ``name`` in the folder open as ``parent_fd``."""
    try:
        encoded = name.encode()
    except UnicodeEncodeError:  # bytes that are not UTF-8, from a command line
        raise HelperError("name_invalid") from None
    if not FOLDER_NAME.fullmatch(name) or len(encoded) > os.fpathconf(parent_fd, "PC_NAME_MAX"):
        raise HelperError("name_invalid")
    rel = _joined(parent_rel, name)
    if len(rel) > MAX_PATH_LENGTH:
        raise HelperError("folder_path_too_long")
    return rel


@dataclass(frozen=True)
class Entry:
    """Something in a folder, found by its resolved parent and its own name, unresolved."""

    parent: Path
    parent_fd: int  # the caller closes it
    name: str
    rel: str
    is_link: bool


def resolve_entry(home: Path, rel: str) -> Entry:
    """The entry ``rel`` names: a folder, or a link, which is never followed."""
    check_rel(rel)
    parts = [part for part in rel.split("/") if part]
    if all(part == "." for part in parts):
        raise HelperError("path_is_home")
    root = home.resolve()
    if parts[-1] in (".", ".."):  # names no entry; only where it leads
        target = confine(home, rel, allow_home=True)
        if target == root:
            raise HelperError("path_is_home")
        raise HelperError("path_ends_in_dot")
    parent = resolve_folder(home, "/".join(parts[:-1]), allow_home=True)
    name = parts[-1]
    fd = open_dir(root, parent)
    try:
        try:
            mode = os.lstat(name, dir_fd=fd).st_mode
        except FileNotFoundError:
            raise HelperError("path_not_found") from None
        if not stat.S_ISLNK(mode) and not stat.S_ISDIR(mode):
            raise HelperError("path_not_directory")
    except BaseException:
        os.close(fd)
        raise
    return Entry(parent, fd, name, _joined(relative(home, parent), name), stat.S_ISLNK(mode))


def relative(home: Path, folder: Path) -> str:
    """A resolved folder's path relative to the home folder; empty for the home itself."""
    root = home.resolve()
    return "" if folder == root else folder.relative_to(root).as_posix()


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


# --- git: what it is given ------------------------------------------------------------

STATUS_ARGS = [
    "status",
    "--porcelain=v2",
    "-z",
    "--branch",
    "--untracked-files=normal",
    "--ignored=no",
]
BRANCHES_ARGS = [
    "for-each-ref",
    f"--format={git_run.BRANCH_FORMAT}",
    "refs/heads",
    "refs/remotes",
]
# A file list as git reads one: NUL-separated on stdin, each name taken literally.
PATHSPEC_FROM_STDIN = ["--pathspec-from-file=-", "--pathspec-file-nul"]
# The most a list of files can take on stdin: each name at its longest in UTF-8, and
# the JSON around it.
MAX_FILE_LIST_BYTES = MAX_FILES_PER_CALL * (MAX_PATH_LENGTH * 6 + 4) + 2
MAX_COMMIT_MESSAGE_BYTES = MAX_COMMIT_MESSAGE_LENGTH * 4


def read_files(data: bytes) -> list[str]:
    """The JSON list of file names a stage or unstage reads on stdin."""
    try:
        files = json.loads(data)
    except ValueError:
        raise HelperError("file_list_invalid") from None
    if (
        not isinstance(files, list)
        or not files
        or len(files) > MAX_FILES_PER_CALL
        or not all(isinstance(f, str) for f in files)
    ):
        raise HelperError("file_list_invalid")
    return files


def check_file(repo: Path, name: str) -> bytes:
    """A file name in a repository, as git gets it; refused when it could reach outside.

    The name is read as written, then its folder resolved, so that a link
    to elsewhere is refused while a link in the repository is the link. A
    folder, which an untracked folder is listed as, may end in one slash.
    """
    if not name or "\0" in name or len(name) > MAX_PATH_LENGTH:
        raise HelperError("file_invalid")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:  # a name that is not UTF-8 is listed, but not selectable
        raise HelperError("file_invalid") from None
    if name.startswith("/"):
        raise HelperError("file_outside_repository")
    parts = name.removesuffix("/").split("/")
    if any(part in ("", ".") for part in parts):
        raise HelperError("file_invalid")
    if any(part == ".." or part.casefold() == ".git" for part in parts):
        raise HelperError("file_outside_repository")
    folder = (repo / name.removesuffix("/")).parent.resolve()
    if folder != repo and repo not in folder.parents:
        raise HelperError("file_outside_repository")
    if any(part.casefold() == ".git" for part in folder.relative_to(repo).parts):
        raise HelperError("file_outside_repository")
    return encoded


def _name(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _file(change: git_run.Change) -> dict:
    """A changed file as rc-helper lists it."""
    listed = {"path": _name(change.path), "change": change.change}
    if change.origin is not None:
        listed["from"] = _name(change.origin)
    if change.submodule:
        listed["submodule"] = True
    if not _is_utf8(change.path):
        listed["selectable"] = False
    return listed


def _short(ref: str | None) -> str | None:
    """A branch's full name without refs/heads/ or refs/remotes/."""
    if ref is None:
        return None
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


class _Capped:
    """A list holding at most ``limit`` items, counting those beyond."""

    def __init__(self, limit: int, key: str):
        self.limit, self.key = limit, key
        self.items: list = []
        self.more = 0

    def add(self, item) -> None:
        if len(self.items) < self.limit:
            self.items.append(item)
        else:
            self.more += 1

    def answer(self) -> dict:
        return {self.key: self.items, "more": self.more}


def git_verb(timeout: float = GIT_TIMEOUT):
    """A git verb: it holds the folder lock shared, so git never runs in a folder being
    deleted, has one deadline for all its git calls, and has git_run's exceptions
    as rc-helper's codes."""

    def wrap(method):
        def verb(self, *args, **kwargs):
            with self._lock(exclusive=False):
                try:
                    return method(self, time.monotonic() + timeout, *args, **kwargs)
                except git_run.GitMissing:
                    raise HelperError("git_not_installed") from None
                except git_run.GitFailed as error:
                    detail = {"git": error.lines, "git_more": error.more}
                    raise HelperError("git_failed", detail) from None
                except git_run.GitTimedOut:
                    raise HelperError("git_timeout") from None
                except git_run.GitUnrecognised as error:
                    raise HelperError("git_unrecognised_output", {"git": error.lines}) from None

        verb.__name__ = method.__name__
        verb.__doc__ = method.__doc__
        return verb

    return wrap


def check_branch_name(name: str) -> None:
    """What any branch name must be before git is asked about it."""
    if not name or name.startswith("-") or len(name) > MAX_BRANCH_NAME_LENGTH:
        raise HelperError("branch_invalid")


def check_author(name: str, email: str) -> None:
    if not AUTHOR_NAME.fullmatch(name):
        raise HelperError("author_name_invalid")
    if not EMAIL.fullmatch(email):
        raise HelperError("email_invalid")


def read_message(data: bytes) -> bytes:
    """A commit message as read on stdin: UTF-8, and one COMMIT_MESSAGE accepts."""
    if len(data) > MAX_COMMIT_MESSAGE_BYTES:
        raise HelperError("message_invalid")
    try:
        message = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HelperError("message_invalid") from None
    if len(message) > MAX_COMMIT_MESSAGE_LENGTH or not COMMIT_MESSAGE.fullmatch(message):
        raise HelperError("message_invalid")
    return data


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
            "path": relative(self.cfg.home, folder),
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
            entry = {
                "name": child.name,
                "path": rel,
                "has_children": has_children,
                "repository": (target / ".git").exists(),
            }
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

    @contextlib.contextmanager
    def _lock(self, exclusive: bool):
        """Held while servers start, stop, are answered or forgotten and while folders
        are made (shared), or while a folder is deleted (exclusive).

        A flock on the home folder itself. One that is held already the other
        way is refused at once with ``folder_busy``, never waited for.
        """
        fd = os.open(self.cfg.home.resolve(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HelperError("folder_busy") from None
            yield
        finally:
            os.close(fd)

    def start(self, rel: str, prefix: str) -> dict:
        with self._lock(exclusive=False):
            return self._start(rel, prefix)

    def _start(self, rel: str, prefix: str) -> dict:
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
        with self._lock(exclusive=False):
            folder, rel = self._folder(rel)
            session = session_for(rel)
            if (
                not self.tmux.exists(session)
                or self._server(session, rel)["state"] not in RESUMABLE
            ):
                raise HelperError("not_stopped")
            return self._start(rel, prefix)

    def stop(self, rel: str) -> dict:
        with self._lock(exclusive=False):
            return self._stop(rel)

    def _stop(self, rel: str) -> dict:
        rel = self._known(rel)
        session = self._existing(rel)
        if self.tmux.exit_status(session) is not None:
            raise HelperError("not_running")
        self.tmux.interrupt(session)
        server = self._wait(session, rel, lambda s: s["exit_code"] is not None, STOP_TIMEOUT)
        return {"server": server}

    def forget(self, rel: str) -> dict:
        with self._lock(exclusive=False):
            return self._forget(rel)

    def _forget(self, rel: str) -> dict:
        rel = self._known(rel)
        session = self._existing(rel)
        if self.tmux.exit_status(session) is None:
            raise HelperError("still_running")
        self.tmux.kill(session)
        return {"forgotten": rel}

    def output(self, rel: str) -> dict:
        rel = self._known(rel)
        session = self._existing(rel)
        return {
            "path": rel,
            "screen": self.tmux.screen(session),
            "server": self._server(session, rel),
        }

    def confirm(self, rel: str, answer: str) -> dict:
        with self._lock(exclusive=False):
            return self._confirm(rel, answer)

    def _confirm(self, rel: str, answer: str) -> dict:
        rel = self._known(rel)
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

    def mkdir(self, rel: str, name: str) -> dict:
        with self._lock(exclusive=False):
            root = self.cfg.home.resolve()
            folder = resolve_folder(self.cfg.home, rel, allow_home=True)
            fd = open_dir(root, folder)
            try:
                created = check_folder_name(name, fd, relative(self.cfg.home, folder))
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    raise HelperError("folder_exists") from None
            finally:
                os.close(fd)
        return {"created": created}

    def delete(self, rel: str) -> dict:
        """Stop and forget every server in a folder or below it, then delete the folder.

        A link is deleted as the link. Nothing is forgotten unless every server
        stopped, and nothing is deleted unless every one was forgotten.
        """
        if not shutil.rmtree.avoids_symlink_attacks:
            raise HelperError("delete_unsafe")
        entry = resolve_entry(self.cfg.home, rel)
        try:
            given = [part for part in rel.split("/") if part not in ("", ".", "..")]
            if any(part.startswith(".") for part in [*given, *entry.rel.split("/")]):
                raise HelperError("path_hidden")
            # The servers are found by their folders' paths as recorded, so a
            # path through a link, or one written otherwise than the names on
            # disk, would name servers other than those it reaches.
            root = str(self.cfg.home.resolve())
            written = posixpath.relpath(posixpath.normpath(posixpath.join(root, rel)), root)
            if entry.rel != written:
                raise HelperError("path_through_link")
            check_names(self.cfg.home, entry.rel)
            with self._lock(exclusive=True):
                before = self._survey(entry)
                servers = self._servers_in(entry.rel)
                stopped = self._stop_all(servers)
                forgotten = self._forget_all(servers)
                self._remove(entry, forgotten, before)
        finally:
            os.close(entry.parent_fd)
        return {
            "deleted": entry.rel,
            "link": entry.is_link,
            "stopped": stopped,
            "forgotten": forgotten,
        }

    @staticmethod
    def _survey(entry: Entry) -> set[tuple[str, int, int]]:
        """What a delete would remove, the entry itself among it: each as its
        path from the parent, its device and its inode, so that something
        removed is told from something else that appeared in its place.

        A folder is walked without following links, and refused with
        ``delete_mount_inside`` when anything in it is on another device.
        """
        try:
            top = os.lstat(entry.name, dir_fd=entry.parent_fd)
        except FileNotFoundError:
            return set()
        found = {(entry.name, top.st_dev, top.st_ino)}
        if entry.is_link:
            return found
        device = os.fstat(entry.parent_fd).st_dev
        walk = os.fwalk(entry.name, dir_fd=entry.parent_fd, follow_symlinks=False)
        for path, folders, files, fd in walk:
            if os.fstat(fd).st_dev != device:
                walk.close()
                raise HelperError("delete_mount_inside")
            for name in (*folders, *files):
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                found.add((posixpath.join(path, name), st.st_dev, st.st_ino))
        return found

    def _servers_in(self, rel: str) -> list[tuple[str, str]]:
        """(folder, session) of every server in the folder ``rel`` or below it, by folder."""
        return sorted(
            (path, session)
            for session, path in self.tmux.sessions()
            if path == rel or path.startswith(f"{rel}/")
        )

    def _stop_all(self, servers: list[tuple[str, str]]) -> list[str]:
        """Stop every running server, all within one STOP_TIMEOUT; the folders of those stopped.

        Waits for the processes to exit rather than for a screen, which may be
        one rcman does not recognise.
        """
        running = [(path, s) for path, s in servers if self.tmux.exit_status(s) is None]
        for _path, session in running:
            self.tmux.interrupt(session)
        deadline = time.monotonic() + STOP_TIMEOUT
        waiting = running
        while waiting := [(path, s) for path, s in waiting if self.tmux.exit_status(s) is None]:
            if time.monotonic() > deadline:
                path, session = waiting[0]
                server = {**self._server(session, path), "screen": self.tmux.screen(session)}
                stopped = [p for p, s in running if (p, s) not in waiting]
                raise HelperError("delete_stop_failed", {"server": server, "stopped": stopped})
            time.sleep(POLL_SECONDS)
        return [path for path, _session in running]

    def _forget_all(self, servers: list[tuple[str, str]]) -> list[str]:
        forgotten: list[str] = []
        for path, session in servers:
            self.tmux.kill(session)
            if self.tmux.exists(session):
                detail = {"server": self._server(session, path), "forgotten": forgotten}
                raise HelperError("delete_forget_failed", detail)
            forgotten.append(path)
        return forgotten

    @classmethod
    def _remove(cls, entry: Entry, forgotten: list[str], before: set) -> None:
        """Delete the entry; whatever could not be removed is reported, and nothing else done.

        ``delete_failed`` when nothing was removed — everything ``before``
        surveyed is still there, whatever else may have appeared — and
        ``delete_incomplete`` when anything was.
        """
        left: set[str] = set()
        if entry.is_link:
            try:
                os.unlink(entry.name, dir_fd=entry.parent_fd)
            except OSError:
                left.add(entry.name)
        else:
            shutil.rmtree(
                entry.name, dir_fd=entry.parent_fd, onexc=lambda _f, path, _e: left.add(path)
            )
        if left:
            parent = entry.rel.rpartition("/")[0]
            report = sorted(_joined(parent, path) for path in left)[:DELETE_REPORT_LIMIT]
            detail = {"forgotten": forgotten, "left": report}
            if before <= cls._survey(entry):
                raise HelperError("delete_failed", detail)
            raise HelperError("delete_incomplete", detail)

    # --- git ---------------------------------------------------------------------------

    def _git(self, repo: Path, args: list[str], deadline: float, **kwargs) -> str:
        return git_run.Git(git_env(self.cfg)).run(repo, args, deadline=deadline, **kwargs)

    def _git_records(self, repo: Path, args: list[str], sep: bytes, deadline: float):
        return git_run.Git(git_env(self.cfg)).stream(repo, args, sep=sep, deadline=deadline)

    def _repository(self, rel: str, deadline: float) -> tuple[Path, str]:
        """A repository's top folder, in the home folder, with its git data there too.

        Without a .git of its own a folder is no repository, and git is not
        asked: git would find one above it, the home folder's among them.
        """
        folder, rel = self._folder(rel)
        if not (folder / ".git").exists():
            raise HelperError("not_a_repository")
        where = self._git(
            folder,
            [
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
            ],
            deadline,
        ).split("\n")
        if len(where) < 3:
            raise git_run.GitUnrecognised("\n".join(where))
        top, git_dir, common_dir = where[:3]
        if Path(top) != folder:
            raise HelperError("not_a_repository")
        root = self.cfg.home.resolve()
        for data in (git_dir, common_dir):
            if root not in Path(data).resolve().parents:
                raise HelperError("repository_outside_home")
        return folder, rel

    def _changes(self, repo: Path, deadline: float, branch: git_run.Branch):
        records = self._git_records(repo, STATUS_ARGS, b"\0", deadline)
        return git_run.parse_status(records, branch)

    def _branches(self, repo: Path, deadline: float):
        return git_run.parse_branches(self._git_records(repo, BRANCHES_ARGS, b"\n", deadline))

    def _current(self, repo: Path, deadline: float) -> str | None:
        return self._git(repo, ["branch", "--show-current"], deadline).strip() or None

    @git_verb()
    def git_status(self, deadline: float, rel: str) -> dict:
        repo, rel = self._repository(rel, deadline)
        branch = git_run.Branch()
        lists = {
            True: _Capped(MAX_LISTED_FILES, "files"),
            False: _Capped(MAX_LISTED_FILES, "files"),
        }
        for change in self._changes(repo, deadline, branch):
            lists[change.staged].add(_file(change))
        return {
            "path": rel,
            "branch": dataclasses.asdict(branch),
            "staged": lists[True].answer(),
            "not_staged": lists[False].answer(),
        }

    @git_verb()
    def git_branches(self, deadline: float, rel: str) -> dict:
        repo, rel = self._repository(rel, deadline)
        local = _Capped(MAX_LISTED_BRANCHES, "branches")
        remote = _Capped(MAX_LISTED_BRANCHES, "branches")
        tracking: dict[str, str] = {}  # a remote branch's full name -> a local branch on it
        remotes = []
        for ref in self._branches(repo, deadline):
            if ref.remote:
                remotes.append(ref.name)
                continue
            local.add({"name": ref.name, "current": ref.current, "upstream": _short(ref.upstream)})
            if ref.upstream and ref.upstream.startswith("refs/remotes/"):
                tracking.setdefault(ref.upstream, ref.name)
        for name in remotes:
            remote.add({"name": name, "local": tracking.get(f"refs/remotes/{name}")})
        return {"path": rel, "local": local.answer(), "remote": remote.answer()}

    @git_verb()
    def git_stage(self, deadline: float, rel: str, data: bytes) -> dict:
        files = read_files(data)
        repo, rel = self._repository(rel, deadline)
        names = [check_file(repo, name) for name in files]
        args = ["--literal-pathspecs", "add", *PATHSPEC_FROM_STDIN]
        self._git(repo, args, deadline, input=b"".join(n + b"\0" for n in names))
        return {"path": rel}

    @git_verb()
    def git_unstage(self, deadline: float, rel: str, data: bytes) -> dict:
        """Unstage files; a staged rename's target takes its origin with it, which
        would otherwise stay staged as deleted."""
        files = read_files(data)
        repo, rel = self._repository(rel, deadline)
        names = [check_file(repo, name) for name in files]
        renamed = {
            change.path: change.origin
            for change in self._changes(repo, deadline, git_run.Branch())
            if change.staged and change.change == "renamed"
        }
        names += [renamed[n] for n in names if n in renamed]
        args = ["--literal-pathspecs", "reset", "-q", *PATHSPEC_FROM_STDIN]
        self._git(repo, args, deadline, input=b"".join(n + b"\0" for n in names))
        return {"path": rel}

    @git_verb()
    def git_commit(self, deadline: float, rel: str, name: str, email: str, data: bytes) -> dict:
        """Commit what is staged, with the message read on stdin, as the author given.

        The message reaches git on stdin too, and is never part of any answer.
        """
        check_author(name, email)
        message = read_message(data)
        repo, rel = self._repository(rel, deadline)
        if not any(c.staged for c in self._changes(repo, deadline, git_run.Branch())):
            raise HelperError("nothing_staged")
        author = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        self._git(repo, ["commit", "-q", "-F", "-"], deadline, input=message, extra_env=author)
        commit = self._git(repo, ["rev-parse", "HEAD"], deadline).strip()
        return {"path": rel, "commit": commit, "branch": self._current(repo, deadline)}

    @git_verb()
    def git_switch(self, deadline: float, rel: str, branch: str, remote: bool) -> dict:
        """Switch to a local branch, or make a local branch tracking a remote one."""
        check_branch_name(branch)
        repo, rel = self._repository(rel, deadline)
        if not any(r.remote is remote and r.name == branch for r in self._branches(repo, deadline)):
            raise HelperError("branch_not_found")
        args = ["switch", "--track", branch] if remote else ["switch", "--no-guess", branch]
        self._git(repo, args, deadline)
        return {"path": rel, "branch": self._current(repo, deadline)}

    @git_verb()
    def git_branch_new(self, deadline: float, rel: str, name: str) -> dict:
        """Make a branch at the current commit and switch to it.

        The name must be one git takes as it is: check-ref-format would expand
        @{-1} and the like into another branch's name.
        """
        check_branch_name(name)
        repo, rel = self._repository(rel, deadline)
        try:
            checked = self._git(repo, ["check-ref-format", "--branch", name], deadline)
        except git_run.GitFailed:
            raise HelperError("branch_invalid") from None
        if checked.rstrip("\n") != name:
            raise HelperError("branch_invalid")
        self._git(repo, ["switch", "-c", name], deadline)
        return {"path": rel, "branch": self._current(repo, deadline)}

    @git_verb(timeout=GIT_NETWORK_TIMEOUT)
    def git_fetch(self, deadline: float, rel: str) -> dict:
        """git fetch --prune from the default remote: no files and no local branch change."""
        repo, rel = self._repository(rel, deadline)
        self._git(repo, ["fetch", "--prune"], deadline)
        return {"path": rel}

    def _folder(self, rel: str) -> tuple[Path, str]:
        folder = resolve_folder(self.cfg.home, rel)
        return folder, relative(self.cfg.home, folder)

    def _known(self, rel: str) -> str:
        """The path a folder's server is known by, also once the folder is gone or renamed."""
        try:
            return self._folder(rel)[1]
        except HelperError as error:
            if error.code not in ("path_not_found", "path_name_differs"):
                raise
        return relative(self.cfg.home, confine(self.cfg.home, rel))

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
    mkdir = verbs.add_parser("mkdir", help="make a folder in a folder, or in your home")
    mkdir.add_argument("parent", nargs="?", default="")
    mkdir.add_argument("--name", required=True, help="the new folder's name")
    delete = verbs.add_parser(
        "delete", help="stop and forget every server in a folder or below it, then delete it"
    )
    delete.add_argument("path")
    for name, text in [
        ("git-status", "a repository's branch, and its staged and not staged files"),
        ("git-branches", "a repository's local and remote-tracking branches"),
        ("git-fetch", "git fetch --prune"),
    ]:
        verbs.add_parser(name, help=text).add_argument("path")
    for name, text in [
        ("git-stage", "stage files; their names are read from stdin as a JSON list"),
        ("git-unstage", "unstage files; their names are read from stdin as a JSON list"),
    ]:
        verbs.add_parser(name, help=text).add_argument("path")
    commit = verbs.add_parser("git-commit", help="commit; the message is read from stdin")
    commit.add_argument("path")
    commit.add_argument("--author-name", required=True)
    commit.add_argument("--author-email", required=True)
    switch = verbs.add_parser("git-switch", help="switch to a branch")
    switch.add_argument("path")
    switch.add_argument("--branch", required=True)
    switch.add_argument(
        "--remote", action="store_true", help="a remote-tracking branch, tracked from a new one"
    )
    new = verbs.add_parser("git-branch-new", help="make a branch here and switch to it")
    new.add_argument("path")
    new.add_argument("--name", required=True)
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
    if v == "mkdir":
        return helper.mkdir(args.parent, args.name)
    if v == "delete":
        return helper.delete(args.path)
    if v == "auth-login":
        return helper.auth_login(args.email)
    if v == "auth-code":
        return helper.auth_code(stdin.readline())
    raw = getattr(stdin, "buffer", stdin)  # what git is given is passed on as bytes
    if v in ("git-status", "git-branches", "git-fetch"):
        return getattr(helper, v.replace("-", "_"))(args.path)
    if v in ("git-stage", "git-unstage"):
        return getattr(helper, v.replace("-", "_"))(args.path, raw.read(MAX_FILE_LIST_BYTES + 1))
    if v == "git-commit":
        data = raw.read(MAX_COMMIT_MESSAGE_BYTES + 1)
        return helper.git_commit(args.path, args.author_name, args.author_email, data)
    if v == "git-switch":
        return helper.git_switch(args.path, args.branch, args.remote)
    return helper.git_branch_new(args.path, args.name)


def _terminate(signum, _frame):
    """SIGTERM ends rc-helper as an exception would, so what it started is ended too."""
    raise SystemExit(128 + signum)


def main(argv: list[str], env: dict[str, str], stdin=sys.stdin) -> int:
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return _main(argv, env, stdin)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _main(argv: list[str], env: dict[str, str], stdin) -> int:
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
