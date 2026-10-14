#!/usr/bin/env python3
"""Record the real `claude` into the fixtures the fake replays.

Each :data:`SEQUENCES` entry drives `claude` through tmux exactly as rcman will
and captures named screens. Captures are sanitised — paths, URLs, times,
session names, session IDs, account details and the version become
``{placeholders}`` — and written to ``tests/fakes/fixtures/`` as ``<capture>.txt``
plus ``manifest.json``. The fake fills the placeholders back in at run time.
Screens seen on a live server that no sequence can bring about are kept, sanitised
the same way, in ``tests/fakes/observed/`` (:func:`load_observed`).

Sequences marked ``needs_account`` run under the real default Claude
configuration and need a logged-in subscription account; each sequence that
brings a server to ready (``server``, ``git_server`` and ``spawn_answered``)
creates one Remote Control session in that account's claude.ai/code list each
time it runs. Sequences marked ``git_repository`` run in a throwaway
repository on :data:`RECORD_BRANCH`, made in the recorder's own scratch folder.
Sequences marked ``submits_code`` send an invalid one-time code to Anthropic's
sign-in service. Everything else runs with a throwaway configuration directory
and needs no account.

Usage::

    uv run python devtools/record_claude.py                # no-account sequences
    uv run python devtools/record_claude.py --with-account # everything
    uv run python devtools/record_claude.py --with-account --sequence server
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devtools import tmuxctl  # noqa: E402
from devtools.held_folders import held, remove_tree, tmux_sockets  # noqa: E402
from helpers.claude_checks import EMAIL_ADDRESS, SESSION_ID  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fakes" / "fixtures"
# Screens seen on a live server that no sequence can bring about; see load_observed.
OBSERVED = REPO / "tests" / "fakes" / "observed"
MANIFEST = "manifest.json"
FAKE_CLAUDE = REPO / "tests" / "fakes" / "claude"

VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
LOGIN_URL_PREFIX = "https://claude.com/cai/oauth/authorize?"
ENVIRONMENT_URL_PREFIX = "https://claude.ai/code?environment="
PROBE_EMAIL = "user@example.com"
PROBE_PREFIX = "rcprobe"
INVALID_CODE = "invalidcode#invalidstate"
TIMEOUT = 60.0
# What the folders the recorder makes in /tmp are named with.
PANES = "rcrs-"  # each sequence's tmux socket
VERSIONS = "rcv-"  # reading the installed version
HELP = "rchelp-"  # running --help
WORK = "rcrec-"  # a recording's working folder

PLACEHOLDERS = (
    "version",
    "cwd",
    "config_dir",
    "folder",
    "login_url",
    "environment_url",
    "time",
    "session_name",
    "email",
    "org_id",
    "org_name",
    "subscription_type",
    "branch",
    "session_id",
)
ACCOUNT_FIELDS = {
    "email": "email",
    "orgId": "org_id",
    "orgName": "org_name",
    "subscriptionType": "subscription_type",
}


@dataclass(frozen=True)
class Context:
    cwd: str
    config_dir: str
    version: str
    branch: str | None = None  # the repository's branch; None outside a repository


@dataclass(frozen=True)
class Recorded:
    lines: list[str]
    exit_code: int | None  # None: still running when captured


# --- sanitising -------------------------------------------------------------------


def sanitise(text: str, ctx: Context) -> str:
    text = re.sub(re.escape(LOGIN_URL_PREFIX) + r"\S+", "{login_url}", text)
    text = re.sub(re.escape(ENVIRONMENT_URL_PREFIX) + r"\S+", "{environment_url}", text)
    for key, placeholder in ACCOUNT_FIELDS.items():
        text = re.sub(rf'("{key}": )"[^"]*"', rf'\1"{{{placeholder}}}"', text)
    text = EMAIL_ADDRESS.sub("{email}", text)
    paths = sorted([(ctx.cwd, "{cwd}"), (ctx.config_dir, "{config_dir}")], key=lambda p: -len(p[0]))
    for path, placeholder in paths:
        text = text.replace(path, placeholder)
    text = re.sub(r"( Connected · )(.+?)( · )", r"\1{folder}\3", text)
    if ctx.branch:
        text = re.sub(
            r"( Connected · \{folder\} · )" + re.escape(ctx.branch) + "$",
            r"\1{branch}",
            text,
            flags=re.MULTILINE,
        )
    text = re.sub(r"\[\d\d:\d\d:\d\d\]", "[{time}]", text)
    text = SESSION_ID.sub("{session_id}", text)
    text = re.sub(r"(^ *Capacity:.*\n {4})(\S+)$", r"\1{session_name}", text, flags=re.MULTILINE)
    return text.replace(ctx.version, "{version}")


def placeholder_values(ctx: Context, **values: str) -> dict[str, str]:
    return {
        "version": ctx.version,
        "cwd": ctx.cwd,
        "config_dir": ctx.config_dir,
        "folder": os.path.basename(ctx.cwd),
        **({"branch": ctx.branch} if ctx.branch else {}),
        **values,
    }


def fill(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        return values[match.group(1)]

    return re.sub(r"\{(" + "|".join(PLACEHOLDERS) + r"|[a-z_]+)\}", replace, template)


ANY_VALUES = {name: f"<{name}>" for name in PLACEHOLDERS}


# --- environment ----------------------------------------------------------------


def claude_env(config_dir: Path | None) -> dict[str, str]:
    """The allowlisted environment every `claude` run gets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/bin/false",
    }
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


# Claude Code's credentials file, in its configuration folder.
CREDENTIALS = ".credentials.json"


# --- sequences --------------------------------------------------------------------


@dataclass
class Run:
    """What a sequence step needs: the pane, and how to capture."""

    pane: tmuxctl.Pane
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    ctx: Context
    out: dict[str, Recorded] = field(default_factory=dict)

    def capture(self, name: str, exit_code: int | None) -> None:
        lines = sanitise("\n".join(self.pane.screen()), self.ctx).split("\n")
        self.out[name] = Recorded(lines=lines, exit_code=exit_code)

    def capture_exit(self, name: str) -> None:
        code = self.pane.wait_exit(TIMEOUT)
        self.pane.wait_stable(quiet=0.5)
        self.capture(name, code)

    def capture_when(self, pattern: str, name: str, quiet: float = 1.0) -> None:
        """Capture once ``pattern`` is on screen and the screen has settled.

        Refuses if the settled screen no longer matches, so a screen that moves
        on while settling is never stored under the wrong name.
        """
        self.pane.wait_for(pattern, TIMEOUT)
        settled = "\n".join(self.pane.wait_stable(quiet=quiet))
        if not re.search(pattern, settled, re.MULTILINE):
            raise RuntimeError(f"{pattern!r} no longer on screen after settling:\n{settled}")
        self.capture(name, None)


@dataclass(frozen=True)
class Sequence:
    name: str
    args: list[str]
    captures: tuple[str, ...]
    steps: Callable[[Run], None]
    needs_account: bool = False
    submits_code: bool = False
    needs_human: bool = False  # recorded only when named, with a person signing in
    git_repository: bool = False  # run in a throwaway repository (make_repository)
    # run with the account's credentials file, linked, and a configuration of its own
    # with no `.claude.json`, so no `oauthAccount`, as a workspace before rcman seeds it
    without_oauth_account: bool = False


def _exit_only(capture: str) -> Callable[[Run], None]:
    return lambda r: r.capture_exit(capture)


def _login_prompt(r: Run) -> None:
    r.capture_when(r"Paste code here if prompted", "login_prompt")


def _login_failed(r: Run) -> None:
    r.pane.wait_for(r"Paste code here if prompted", TIMEOUT)
    r.pane.type_line(INVALID_CODE)
    r.capture_exit("login_failed")


def _trust_declined(r: Run) -> None:
    r.capture_when(r"\[y/N\]", "trust_prompt")  # waits until the screen is stable
    r.pane.type_line("n")
    r.capture_exit("trust_declined")


def _trust_too_fast(r: Run) -> None:
    """Answer the moment the prompt appears; `claude` refuses and asks again."""
    r.pane.wait_for(r"\[y/N\]", TIMEOUT)
    r.pane.type_line("n")
    r.capture_when(r"too quickly", "trust_answer_too_fast")
    r.pane.interrupt()
    r.pane.wait_exit(TIMEOUT)


CONNECTED = r"Capacity: 0/"  # the server is up; no session has attached yet
# The connected stage can be brief, so it is captured after a short settle.
CONNECTED_QUIET = 0.3
READY = r"Capacity: [1-9]"  # a session has attached and is named


def _answer_trust(r: Run, capture: str | None = None) -> None:
    if capture:
        r.capture_when(r"\[y/N\]", capture)  # waits until the screen is stable
    else:
        r.pane.wait_for(r"\[y/N\]", TIMEOUT)
        r.pane.wait_stable()  # an answer that arrives too quickly is refused
    r.pane.type_line("y")


def _server_steps(name: str, trust_capture: str | None = None) -> Callable[[Run], None]:
    """Trust, connected, ready, stopped; then resumed: connected, ready, stopped."""

    def steps(r: Run) -> None:
        _answer_trust(r, trust_capture)
        r.capture_when(CONNECTED, f"{name}_connected_after_trust", quiet=CONNECTED_QUIET)
        r.capture_when(READY, f"{name}_ready_after_trust")
        r.pane.interrupt()
        r.capture_exit(f"{name}_stopped_after_trust")
        r.pane.rerun(r.argv, r.cwd, r.env)
        r.capture_when(CONNECTED, f"{name}_connected_resumed", quiet=CONNECTED_QUIET)
        r.capture_when(READY, f"{name}_ready_resumed")
        r.pane.interrupt()
        r.capture_exit(f"{name}_stopped_resumed")

    return steps


def _server_captures(name: str) -> tuple[str, ...]:
    return tuple(
        f"{name}_{stage}_{when}"
        for when in ("after_trust", "resumed")
        for stage in ("connected", "ready", "stopped")
    )


SPAWN_QUESTION = r"^Choose \[1/2\]"  # Claude asks for a spawn mode
# How long Claude is watched after Ctrl+C, or an answer, at the spawn-mode question.
IGNORED_SECONDS = 3.0


def _still_asking(r: Run, after: str) -> None:
    time.sleep(IGNORED_SECONDS)
    if r.pane.exit_status() is not None or re.search(CONNECTED, r.pane.text()):
        raise RuntimeError(f"{after} moved the spawn-mode question on:\n{r.pane.text()}")
    r.pane.wait_stable()


def _spawn_prompt(r: Run) -> None:
    """The spawn-mode question a repository gets without --spawn: Ctrl+C, then an
    answer, then SIGTERM, are all ignored; SIGKILL ends it."""
    _answer_trust(r)
    r.capture_when(SPAWN_QUESTION, "spawn_prompt")
    r.pane.interrupt()
    _still_asking(r, "Ctrl+C")
    r.capture("spawn_prompt_interrupted", None)
    r.pane.type_line("1")
    _still_asking(r, "An answer after Ctrl+C")
    r.capture("spawn_prompt_answered_after_interrupt", None)
    r.pane.signal(signal.SIGTERM)
    _still_asking(r, "SIGTERM")
    r.pane.signal(signal.SIGKILL)
    r.capture_exit("spawn_prompt_killed")


def _spawn_answered(r: Run) -> None:
    """The spawn-mode question answered 1, same-dir; then as a server goes on."""
    _answer_trust(r)
    r.pane.wait_for(SPAWN_QUESTION, TIMEOUT)
    r.pane.wait_stable()
    r.pane.type_line("1")
    r.capture_when(CONNECTED, "spawn_connected_same_dir", quiet=CONNECTED_QUIET)
    r.capture_when(READY, "spawn_ready_same_dir")
    r.pane.interrupt()
    r.capture_exit("spawn_stopped_same_dir")


RECORD_BRANCH = "main"  # the branch of the repository a git_repository sequence runs in


def make_repository(folder: Path) -> None:
    """Make ``folder`` a repository on RECORD_BRANCH with one empty commit,
    with no configuration of the person's, the system's or a global one."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(folder),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "rcman recorder",
        "GIT_AUTHOR_EMAIL": "recorder@example.invalid",
        "GIT_COMMITTER_NAME": "rcman recorder",
        "GIT_COMMITTER_EMAIL": "recorder@example.invalid",
    }
    for args in (
        ["init", "-q", "-b", RECORD_BRANCH],
        ["commit", "-q", "--allow-empty", "-m", "First commit"],
    ):
        subprocess.run(["git", *args], cwd=folder, env=env, check=True, capture_output=True)


HUMAN_TIMEOUT = 15 * 60.0
HANDOFF_POLL = 0.5  # seconds between looks at the handoff folder


def _ended_line(path: Path) -> str | None:
    """What ``path`` holds once it is a non-empty line ended with a newline; else None."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return text if text.endswith("\n") and text.strip() else None


def _login_succeeded(r: Run) -> None:
    """A real sign-in: a person opens the URL and hands the code back through a handoff
    folder.

    The folder, ``handoff-*`` in the recording's working folder, is named on stderr.
    The URL is written to ``url`` in it; the sequence waits for ``code`` there to hold
    a line ended with a newline, types it, captures the success screens, and then logs
    out, which revokes the sign-in it just made.
    """
    match = r.pane.wait_for(re.escape(LOGIN_URL_PREFIX) + r"\S+", TIMEOUT)
    handoff = Path(tempfile.mkdtemp(prefix="handoff-", dir=r.cwd.parent))
    code_file = handoff / "code"
    # Written under another name and renamed, so whoever reads it never reads part of it.
    (handoff / ".url").write_text(match.group(0) + "\n", encoding="utf-8")
    (handoff / ".url").rename(handoff / "url")
    print(f"sign in at the URL in {handoff / 'url'}; put the code in {code_file}", file=sys.stderr)
    deadline = time.monotonic() + HUMAN_TIMEOUT
    # Read only once the line is ended, so a code still being written is never typed.
    while not (written := _ended_line(code_file)):
        if time.monotonic() > deadline:
            raise TimeoutError("no sign-in code was handed over")
        time.sleep(HANDOFF_POLL)
    code = written.strip()
    r.pane.type_line(code)
    r.capture_when(r"Login successful", "login_succeeded")
    r.pane.type_line("")
    r.capture_exit("login_succeeded_done")
    r.pane.rerun([r.argv[0], "auth", "logout"], r.cwd, r.env)
    r.pane.wait_exit(TIMEOUT)


# As rcman starts a server (tests/test_claude_server.py holds the two together).
RC_ARGS = [
    "remote-control",
    "--remote-control-session-name-prefix",
    PROBE_PREFIX,
    "--permission-mode",
    "default",
    "--spawn",
    "same-dir",
]
# Without --spawn, Claude asks for a spawn mode in a repository.
RC_ARGS_ASKING_SPAWN = [a for a in RC_ARGS if a not in ("--spawn", "same-dir")]
LOGIN_ARGS = ["auth", "login", "--claudeai", "--email", PROBE_EMAIL]

SEQUENCES: list[Sequence] = [
    Sequence("version", ["--version"], ("version",), _exit_only("version")),
    Sequence(
        "auth_status_logged_out",
        ["auth", "status"],
        ("auth_status_logged_out",),
        _exit_only("auth_status_logged_out"),
    ),
    Sequence("rc_logged_out", RC_ARGS, ("rc_logged_out",), _exit_only("rc_logged_out")),
    Sequence(
        "rc_no_oauth_account",
        RC_ARGS,
        ("rc_no_oauth_account",),
        _exit_only("rc_no_oauth_account"),
        needs_account=True,
        without_oauth_account=True,
    ),
    Sequence("auth_logout", ["auth", "logout"], ("auth_logout",), _exit_only("auth_logout")),
    Sequence(
        "auth_login_help",
        ["auth", "login", "--help"],
        ("auth_login_help",),
        _exit_only("auth_login_help"),
    ),
    Sequence("login_prompt", LOGIN_ARGS, ("login_prompt",), _login_prompt),
    Sequence("login_failed", LOGIN_ARGS, ("login_failed",), _login_failed, submits_code=True),
    Sequence(
        "login_succeeded",
        LOGIN_ARGS,
        ("login_succeeded", "login_succeeded_done"),
        _login_succeeded,
        needs_human=True,
    ),
    Sequence(
        "auth_status_logged_in",
        ["auth", "status"],
        ("auth_status_logged_in",),
        _exit_only("auth_status_logged_in"),
        needs_account=True,
    ),
    Sequence(
        "trust_declined",
        RC_ARGS,
        ("trust_prompt", "trust_declined"),
        _trust_declined,
        needs_account=True,
    ),
    Sequence(
        "trust_too_fast",
        RC_ARGS,
        ("trust_answer_too_fast",),
        _trust_too_fast,
        needs_account=True,
    ),
    Sequence(
        "server",
        RC_ARGS,
        _server_captures("server"),
        _server_steps("server"),
        needs_account=True,
    ),
    Sequence(
        "git_server",
        RC_ARGS,
        ("git_trust_prompt", *_server_captures("git_server")),
        _server_steps("git_server", trust_capture="git_trust_prompt"),
        needs_account=True,
        git_repository=True,
    ),
    Sequence(
        "spawn_prompt",
        RC_ARGS_ASKING_SPAWN,
        (
            "spawn_prompt",
            "spawn_prompt_interrupted",
            "spawn_prompt_answered_after_interrupt",
            "spawn_prompt_killed",
        ),
        _spawn_prompt,
        needs_account=True,
        git_repository=True,
    ),
    Sequence(
        "spawn_answered",
        RC_ARGS_ASKING_SPAWN,
        ("spawn_connected_same_dir", "spawn_ready_same_dir", "spawn_stopped_same_dir"),
        _spawn_answered,
        needs_account=True,
        git_repository=True,
    ),
]


def select_sequences(names: list[str] | None, with_account: bool) -> list[Sequence]:
    by_name = {s.name: s for s in SEQUENCES}
    if not names:
        return [s for s in SEQUENCES if (with_account or not s.needs_account) and not s.needs_human]
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise ValueError(f"unknown sequence(s): {', '.join(unknown)}; known: {', '.join(by_name)}")
    chosen = [by_name[n] for n in names]
    if not with_account and any(s.needs_account for s in chosen):
        raise ValueError("a chosen sequence needs an account: pass --with-account")
    return chosen


def options_used() -> dict[tuple[str, ...], set[str]]:
    """The command-line options rcman's sequences pass, per subcommand."""
    used: dict[tuple[str, ...], set[str]] = {}
    for seq in SEQUENCES:
        command: list[str] = []
        for arg in seq.args:
            if arg.startswith("-"):
                break
            command.append(arg)
        used.setdefault(tuple(command), set()).update(a for a in seq.args if a.startswith("--"))
    return used


def help_text(claude_bin: Path | str, command: tuple[str, ...], env: dict[str, str]) -> str:
    """``--help`` output, run with empty stdin from a scratch folder.

    Logged in, ``remote-control --help`` waits on an inherited stdin.
    """
    with held(HELP) as cwd:
        proc = subprocess.run(
            [str(claude_bin), *command, "--help"],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return proc.stdout + proc.stderr


def help_refused_logged_out(text: str) -> bool:
    """Whether ``text`` is the recorded logged-out refusal rather than help."""
    refusal = next(line for line in load_fixtures()["rc_logged_out"].lines if line.strip())
    return refusal in text


def installed_version(claude_bin: Path | str) -> str:
    with held(VERSIONS) as tmp:
        with tmuxctl.Pane(socket_dir=tmp) as pane:
            pane.run([str(claude_bin), "--version"], cwd=tmp, env=claude_env(tmp))
            pane.wait_exit(TIMEOUT)
            match = VERSION_PATTERN.search(pane.text())
    if not match:
        raise RuntimeError(f"cannot read the version of {claude_bin}")
    return match.group(0)


def record(
    claude_bin: Path | str,
    sequences: list[Sequence],
    account_config_dir: Path | None,
    workdir: Path,
) -> dict[str, Recorded]:
    """Run each sequence in a fresh folder and pane; return sanitised captures.

    ``account_config_dir`` is the configuration for ``needs_account`` sequences;
    ``None`` means the real default configuration.
    """
    version = installed_version(claude_bin)
    workdir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Recorded] = {}
    for seq in sequences:
        folder = Path(tempfile.mkdtemp(prefix=f"{seq.name}-", dir=workdir))
        branch = None
        if seq.git_repository:
            make_repository(folder)
            branch = RECORD_BRANCH
        if seq.without_oauth_account:
            config = Path(tempfile.mkdtemp(prefix="cfg-", dir=workdir))
            account = account_config_dir or Path.home() / ".claude"
            (config / CREDENTIALS).symlink_to(account / CREDENTIALS)
            config_text = str(config)
        elif seq.needs_account:
            config = account_config_dir
            config_text = str(config) if config else str(Path.home() / ".claude")
        else:
            config = Path(tempfile.mkdtemp(prefix="cfg-", dir=workdir))
            config_text = str(config)
        env = claude_env(config)
        argv = [str(claude_bin), *seq.args]
        with tmux_sockets(PANES) as sock, tmuxctl.Pane(socket_dir=sock) as pane:
            pane.run(argv, cwd=folder, env=env)
            ctx = Context(str(folder), config_text, version, branch)
            run = Run(pane, argv, folder, env, ctx)
            seq.steps(run)
            out.update(run.out)
    return out


# --- fixtures ---------------------------------------------------------------------


def load_manifest(directory: Path = FIXTURES) -> dict:
    return json.loads((directory / MANIFEST).read_text(encoding="utf-8"))


def load_fixtures(directory: Path = FIXTURES) -> dict[str, Recorded]:
    manifest = load_manifest(directory)
    return {
        name: Recorded(
            lines=(directory / f"{name}.txt").read_text(encoding="utf-8").split("\n"),
            exit_code=entry["exit_code"],
        )
        for name, entry in manifest["captures"].items()
    }


@dataclass(frozen=True)
class Observed:
    """A screen seen on a live server, kept because no sequence can bring it about."""

    lines: list[str]
    exit_code: int | None  # None: still running when seen
    seen_on: str  # the Claude Code version it was seen on
    kept_for: list[str]  # the versions it is kept for, starting with seen_on


def load_observed(directory: Path = OBSERVED) -> dict[str, Observed]:
    """The observed screens, ``<name>.txt`` each, as ``manifest.json`` beside them lists
    them, sanitised as the fixtures are. The manifest also records how each was seen."""
    manifest = load_manifest(directory)
    return {
        name: Observed(
            lines=(directory / f"{name}.txt").read_text(encoding="utf-8").split("\n"),
            exit_code=entry["exit_code"],
            seen_on=entry["seen_on"],
            kept_for=list(entry["kept_for"]),
        )
        for name, entry in manifest["screens"].items()
    }


def write_fixtures(recorded: dict[str, Recorded], version: str, out: Path = FIXTURES) -> None:
    """Write captures and the manifest, keeping existing captures not re-recorded."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / MANIFEST
    manifest = load_manifest(out) if path.exists() else {"captures": {}}
    by_capture = {c: s for s in SEQUENCES for c in s.captures}
    for name, rec in recorded.items():
        (out / f"{name}.txt").write_text("\n".join(rec.lines), encoding="utf-8")
        seq = by_capture.get(name)
        manifest["captures"][name] = {
            "sequence": seq.name if seq else name,
            "exit_code": rec.exit_code,
            "needs_account": bool(seq and seq.needs_account),
        }
    for stale in set(manifest["captures"]) - set(by_capture):
        del manifest["captures"][stale]
        (out / f"{stale}.txt").unlink(missing_ok=True)
    manifest["claude_version"] = version
    manifest["captures"] = dict(sorted(manifest["captures"].items()))
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def compare(recorded: dict[str, Recorded], expected: dict[str, Recorded]) -> list[str]:
    problems = []
    for name in sorted(set(recorded) | set(expected)):
        got, want = recorded.get(name), expected.get(name)
        if got is None or want is None:
            problems.append(f"{name}: {'not recorded' if got is None else 'no fixture'}")
            continue
        if got.exit_code != want.exit_code:
            problems.append(f"{name}: exit code {got.exit_code}, fixture {want.exit_code}")
        if got.lines != want.lines:
            diff = "\n".join(f"  - {w!r}\n  + {g!r}" for w, g in _pairs(want.lines, got.lines))
            problems.append(f"{name}: screen differs\n{diff}")
    return problems


def _pairs(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    n = max(len(a), len(b))
    a, b = a + [""] * (n - len(a)), b + [""] * (n - len(b))
    return [(x, y) for x, y in zip(a, b, strict=True) if x != y]


# --- command line -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--with-account",
        action="store_true",
        help="also run sequences that need a logged-in account",
    )
    parser.add_argument(
        "--claude", default=shutil.which("claude") or "claude", help="the claude binary to record"
    )
    parser.add_argument("--out", type=Path, default=FIXTURES, help="fixture directory")
    parser.add_argument(
        "--sequence",
        action="append",
        dest="sequences",
        help="record only this sequence (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        sequences = select_sequences(args.sequences, args.with_account)
    except ValueError as error:
        parser.error(str(error))

    version = installed_version(args.claude)
    current = (
        load_manifest(args.out).get("claude_version") if (args.out / MANIFEST).exists() else None
    )
    complete = args.with_account and not args.sequences
    if current not in (None, version) and not complete:
        parser.error(
            f"fixtures are for {current}, installed is {version}: "
            "re-record everything with --with-account and no --sequence"
        )
    with held(WORK, remover=remove_tree) as work:
        recorded = record(args.claude, sequences, None, work)
    write_fixtures(recorded, version, args.out)
    for name in sorted(recorded):
        print(f"recorded {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
