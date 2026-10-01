#!/usr/bin/env python3
"""Record the real `claude` into the fixtures the fake replays.

Each :data:`SEQUENCES` entry drives `claude` through tmux exactly as rcman will
and captures named screens. Captures are sanitised — paths, URLs, times,
session names, account details and the version become ``{placeholders}`` — and
written to ``tests/fakes/fixtures/`` as ``<capture>.txt`` plus ``manifest.json``.
The fake fills the placeholders back in at run time.

Sequences marked ``needs_account`` run under the real default Claude
configuration and need a logged-in subscription account; the ``server``
sequence creates one Remote Control session in that account's claude.ai/code
list each time it runs. Sequences marked ``submits_code`` send an invalid
one-time code to Anthropic's sign-in service. Everything else runs with a
throwaway configuration directory and needs no account.

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
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devtools import tmuxctl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fakes" / "fixtures"
MANIFEST = "manifest.json"
FAKE_CLAUDE = REPO / "tests" / "fakes" / "claude"

VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
LOGIN_URL_PREFIX = "https://claude.com/cai/oauth/authorize?"
ENVIRONMENT_URL_PREFIX = "https://claude.ai/code?environment="
PROBE_EMAIL = "user@example.com"
PROBE_PREFIX = "rcprobe"
INVALID_CODE = "invalidcode#invalidstate"
TIMEOUT = 60.0

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
    paths = sorted([(ctx.cwd, "{cwd}"), (ctx.config_dir, "{config_dir}")], key=lambda p: -len(p[0]))
    for path, placeholder in paths:
        text = text.replace(path, placeholder)
    text = re.sub(r"( Connected · )(.+?)( · )", r"\1{folder}\3", text)
    text = re.sub(r"\[\d\d:\d\d:\d\d\]", "[{time}]", text)
    text = re.sub(r"(^ *Capacity:.*\n {4})(\S+)$", r"\1{session_name}", text, flags=re.MULTILINE)
    return text.replace(ctx.version, "{version}")


def placeholder_values(ctx: Context, **values: str) -> dict[str, str]:
    return {
        "version": ctx.version,
        "cwd": ctx.cwd,
        "config_dir": ctx.config_dir,
        "folder": os.path.basename(ctx.cwd),
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


def _server(r: Run) -> None:
    r.pane.wait_for(r"\[y/N\]", TIMEOUT)
    r.pane.wait_stable()  # an answer that arrives too quickly is refused
    r.pane.type_line("y")
    r.capture_when(CONNECTED, "server_connected_after_trust", quiet=CONNECTED_QUIET)
    r.capture_when(READY, "server_ready_after_trust")
    r.pane.interrupt()
    r.capture_exit("server_stopped_after_trust")
    r.pane.rerun(r.argv, r.cwd, r.env)
    r.capture_when(CONNECTED, "server_connected_resumed", quiet=CONNECTED_QUIET)
    r.capture_when(READY, "server_ready_resumed")
    r.pane.interrupt()
    r.capture_exit("server_stopped_resumed")


RC_ARGS = [
    "remote-control",
    "--remote-control-session-name-prefix",
    PROBE_PREFIX,
    "--permission-mode",
    "default",
]
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
    Sequence("auth_logout", ["auth", "logout"], ("auth_logout",), _exit_only("auth_logout")),
    Sequence("login_prompt", LOGIN_ARGS, ("login_prompt",), _login_prompt),
    Sequence("login_failed", LOGIN_ARGS, ("login_failed",), _login_failed, submits_code=True),
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
        (
            "server_connected_after_trust",
            "server_ready_after_trust",
            "server_stopped_after_trust",
            "server_connected_resumed",
            "server_ready_resumed",
            "server_stopped_resumed",
        ),
        _server,
        needs_account=True,
    ),
]


def select_sequences(names: list[str] | None, with_account: bool) -> list[Sequence]:
    by_name = {s.name: s for s in SEQUENCES}
    if not names:
        return [s for s in SEQUENCES if with_account or not s.needs_account]
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
    with tempfile.TemporaryDirectory(prefix="rchelp-", dir="/tmp") as cwd:
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
    with tempfile.TemporaryDirectory(prefix="rcv-", dir="/tmp") as tmp:
        with tmuxctl.Pane(socket_dir=Path(tmp)) as pane:
            pane.run([str(claude_bin), "--version"], cwd=Path(tmp), env=claude_env(Path(tmp)))
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
        sock = Path(tempfile.mkdtemp(prefix="s-", dir="/tmp"))
        if seq.needs_account:
            config = account_config_dir
            config_text = str(config) if config else str(Path.home() / ".claude")
        else:
            config = Path(tempfile.mkdtemp(prefix="cfg-", dir=workdir))
            config_text = str(config)
        env = claude_env(config)
        argv = [str(claude_bin), *seq.args]
        try:
            with tmuxctl.Pane(socket_dir=sock) as pane:
                pane.run(argv, cwd=folder, env=env)
                run = Run(pane, argv, folder, env, Context(str(folder), config_text, version))
                seq.steps(run)
                out.update(run.out)
        finally:
            shutil.rmtree(sock, ignore_errors=True)
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
    with tempfile.TemporaryDirectory(prefix="rcrec-", dir="/tmp") as work:
        recorded = record(args.claude, sequences, None, Path(work))
    write_fixtures(recorded, version, args.out)
    for name in sorted(recorded):
        print(f"recorded {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
