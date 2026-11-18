"""Contract tests: the installed real `claude` still does what rcman relies on.

These need no Claude account. The real `claude` passes rcman's own checks
(helpers/claude_checks.py), the ones a version is held to before it is rolled out;
every option rcman passes is still in its command's own `--help`; and it treats the
credentials file rcman shares and empties as rcman expects.

When `claude` is not installed these tests skip, unless
``RCMAN_REQUIRE_REAL_CLAUDE=1`` (set by ``make check``), in which case they fail.
"""

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from devtools import fake_claude as fake
from helpers import claude_checks as cc
from helpers import screens
from tests.hang_guard import HANG_GUARD

REAL = shutil.which("claude")
REQUIRED = os.environ.get("RCMAN_REQUIRE_REAL_CLAUDE") == "1"
BACKEND = Path(__file__).resolve().parent.parent / "backend" / "rcman"
VERSION = re.compile(r"\d+\.\d+\.\d+")

pytestmark = pytest.mark.skipif(
    REAL is None and not REQUIRED,
    reason="claude is not installed (make check sets RCMAN_REQUIRE_REAL_CLAUDE=1 to fail instead)",
)


def claude_env(config_dir: Path | None, home: Path) -> dict[str, str]:
    """The allowlisted environment every `claude` run here gets: ``home`` is its HOME, a
    temporary folder of the caller's, made if it is not there, and never the home the run was
    started from, which Claude would write its own files into; ``config_dir`` its
    configuration folder where one is given."""
    if Path(home).resolve() == Path.home().resolve():
        raise ValueError(f"{home} is the home this run was started from, not a folder of its own")
    Path(home).mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/bin/false",
    }
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def options_used() -> dict[tuple[str, ...], set[str]]:
    """The options rcman passes `claude`, by command: the literal arguments of every
    ``claude_command`` call in the backend, the one that starts a server among them
    (``claude_server.server_command``), read from the source. A command is the arguments
    before the first option."""
    used: dict[tuple[str, ...], set[str]] = {}
    for path in sorted(BACKEND.rglob("*.py")):
        for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(call, ast.Call):
                continue
            if (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) != (
                "claude_command"
            ):
                continue
            args = [a.value for a in call.args[1:] if isinstance(a, ast.Constant)]
            command: list[str] = []
            for arg in args:
                if arg.startswith("-"):
                    break
                command.append(arg)
            used.setdefault(tuple(command), set()).update(a for a in args if a.startswith("--"))
    return used


def help_text(
    claude_bin: str, command: tuple[str, ...], env: dict[str, str], cwd: Path
) -> tuple[int, str]:
    """``--help`` of ``command``, run with empty stdin in ``cwd``: its exit status and what it
    printed. Logged in, ``remote-control --help`` waits on an inherited stdin."""
    proc = subprocess.run(
        [claude_bin, *command, "--help"],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    return proc.returncode, proc.stdout + proc.stderr


def help_gaps(
    claude_bin: str, env: dict[str, str], used: dict[tuple[str, ...], set[str]], cwd: Path
) -> dict[str, list[str]]:
    """What each command's ``--help`` lacks, by command: the command's own usage, such as
    ``claude auth login [options]``, which the top-level help an unknown command prints does
    not name, then the options of ``used`` it does not name; or that it was refused for want
    of a login, as rcman reads a server's screen (screens.classify). Commands whose help has it
    all are left out."""
    gaps: dict[str, list[str]] = {}
    for command, options in used.items():
        code, text = help_text(claude_bin, command, env, cwd)
        if screens.classify(text.splitlines(), code)["state"] == "logged_out":
            absent = ["refused: not logged in"]
        else:
            wanted = [" ".join(("claude", *command, "[options]")), *sorted(options)]
            absent = [w for w in wanted if w not in text]
        if absent:
            gaps[" ".join(command) or "(top level)"] = absent
    return gaps


def test_real_claude_is_installed():
    assert REAL is not None, "claude is not on PATH"


def test_the_installed_real_claude_passes_rcmans_checks(tmp_path):
    env = claude_env(None, tmp_path / "home")
    said = subprocess.run(
        [REAL, "--version"],
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    version = VERSION.search(said.stdout)
    assert version, said.stdout
    run = cc.local_runner(REAL, env=env, cwd=tmp_path, config_dir=tmp_path / "cfg")
    assert cc.candidate_failures(run, version.group(0)) == {}


def test_every_option_rcman_passes_is_in_help(tmp_path):
    used = options_used()
    assert "--claudeai" in used.get(("auth", "login"), set())  # non-vacuity
    env = claude_env(tmp_path / "cfg", tmp_path / "home")
    # Each command's help names the command and every option rcman passes it; no
    # command's help is refused without a login, so every option is checked here.
    assert help_gaps(REAL, env, used, tmp_path) == {}
    # Known negative: an unknown command prints the top-level help, which is not its own.
    unknown = ("rcman-no-such-command",)
    assert help_gaps(REAL, env, {unknown: set()}, tmp_path) == {
        "rcman-no-such-command": ["claude rcman-no-such-command [options]"]
    }


# --- the credentials file rcman shares and empties (backend/rcman/accounts.py) ---------


def _claude(args: list[str], config_dir) -> subprocess.CompletedProcess:
    return subprocess.run(
        [REAL, *args],
        env=claude_env(config_dir, Path(config_dir).parent / f"{Path(config_dir).name}-home"),
        cwd=config_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )


@pytest.mark.parametrize("content", ["", "{}"])
def test_an_empty_credentials_file_is_signed_out_and_left_as_it_is(tmp_path, content):
    """rcman makes the file empty, and empties it to sign out: the real binary reads
    both an empty file and ``{}`` as no sign-in, and does not rewrite either."""
    (tmp_path / fake.CREDENTIALS).write_text(content)
    (tmp_path / fake.CREDENTIALS).chmod(0o600)
    found = _claude(["auth", "status"], tmp_path)
    assert json.loads(found.stdout)["loggedIn"] is False
    assert (tmp_path / fake.CREDENTIALS).read_text() == content


def test_logout_removes_the_credentials_file(tmp_path):
    """What the fake's logout copies; over a bound file the removal is refused, and
    rcman empties the file in place afterwards."""
    (tmp_path / fake.CREDENTIALS).write_text("{}")
    assert (tmp_path / fake.CREDENTIALS).exists()  # positive control
    _claude(["auth", "logout"], tmp_path)
    assert not (tmp_path / fake.CREDENTIALS).exists()
