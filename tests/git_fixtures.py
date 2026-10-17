"""Real git repositories for the tests, made with the system's own git.

Not a test module: the tests that need a repository import it. Git runs here
with a fixed author and an empty configuration of its own, so the person's
configuration, which git under test reads, is only what a test plants.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

FIXTURE_AUTHOR = ("Fixture Author", "fixture@example.com")


def fixture_env(home: Path) -> dict[str, str]:
    """The environment fixture git runs in: system PATH, ``home`` as HOME, which it does
    not make, and every transport refused but a local repository's (protocol.allow
    never, protocol.file.allow always), so that git refuses an http, https, ssh, git or
    scp-like address before connecting to anything, and no address a test gives it can
    reach the real GitHub."""
    name, email = FIXTURE_AUTHOR
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "protocol.allow",
        "GIT_CONFIG_VALUE_0": "never",
        "GIT_CONFIG_KEY_1": "protocol.file.allow",
        "GIT_CONFIG_VALUE_1": "always",
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


# Fixture git's HOME: a folder that does not exist, so git reads no configuration of a
# person's, and nothing is made that a run killed by a signal could leave behind.
FIXTURE_HOME = Path("/nonexistent")


def fixture_git(cwd: Path, *args: str, input: bytes | None = None) -> str:
    """Run git in ``cwd`` as the fixtures do; its output, or a failed assertion."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=fixture_env(FIXTURE_HOME),
        input=input,
        capture_output=True,
    )
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout.decode("utf-8", "replace")


def write(path: Path, text: str = "content\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# Long enough that git still sees a renamed file as renamed after a small change.
BODY = "".join(f"line {i} of a file long enough to be recognised when renamed\n" for i in range(40))


def make_repo(path: Path, origin: Path | None = None, url: str | None = None) -> Path:
    """A repository at ``path`` on main with one commit.

    With ``origin``: a bare repository there, holding main and feature, which
    the repository has as its remote ``origin``; main tracks origin/main, and
    there is no local feature. With ``url`` besides, origin's address is
    ``url``, which serves that bare repository, as tests/github_server.py does;
    the branches are pushed to it by its path first.
    """
    path.mkdir(parents=True, exist_ok=True)
    fixture_git(path, "init", "-q", "-b", "main")
    write(path / "README.md", BODY)
    fixture_git(path, "add", "README.md")
    fixture_git(path, "commit", "-q", "-m", "First commit")
    if origin is not None:
        origin.parent.mkdir(parents=True, exist_ok=True)
        if not origin.exists():
            fixture_git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
        fixture_git(path, "remote", "add", "origin", str(origin))
        fixture_git(path, "branch", "feature")
        fixture_git(path, "push", "-q", "origin", "main", "feature")
        fixture_git(path, "branch", "-D", "feature")
        fixture_git(path, "branch", "-q", "--set-upstream-to=origin/main", "main")
        if url is not None:
            fixture_git(path, "remote", "set-url", "origin", url)
    return path


def push_branch_to_origin(repo: Path, origin: Path, name: str) -> None:
    """A branch that exists on origin only: made in a scratch clone and pushed there."""
    scratch = origin.parent / f"scratch-{name.replace('/', '-')}"
    fixture_git(origin.parent, "clone", "-q", str(origin), str(scratch))
    fixture_git(scratch, "switch", "-q", "-c", name)
    write(scratch / f"{name.replace('/', '-')}.txt")
    fixture_git(scratch, "add", ".")
    fixture_git(scratch, "commit", "-q", "-m", f"On {name}")
    fixture_git(scratch, "push", "-q", "origin", name)


def commit_on_origin(origin: Path, branch: str, name: str, text: str = "content\n") -> str:
    """A commit on ``branch`` of the bare repository ``origin`` writing the file ``name``, as
    someone else's push makes one: in a scratch clone beside ``origin``, removed once it is
    pushed. Its id."""
    scratch = Path(tempfile.mkdtemp(prefix="pusher-", dir=origin.parent))
    try:
        fixture_git(origin.parent, "clone", "-q", "-b", branch, str(origin), str(scratch / "c"))
        clone = scratch / "c"
        write(clone / name, text)
        fixture_git(clone, "add", name)
        fixture_git(clone, "commit", "-q", "-m", f"Theirs: {name}")
        fixture_git(clone, "push", "-q", "origin", branch)
        return fixture_git(clone, "rev-parse", "HEAD").strip()
    finally:
        shutil.rmtree(scratch)


def add_submodule(repo: Path, sub_source: Path, name: str) -> None:
    """``sub_source``, a repository, added to ``repo`` as the submodule ``name``."""
    fixture_git(
        repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_source), name
    )


# --- the stand-in for github.com ---------------------------------------------------------

# Every address under this is a folder of the stand-in's instead, for a git command that may
# reach a remote run through the host runtime; and the transports it could otherwise reach
# GitHub by.
GITHUB = "https://github.com/"
STAND_IN_CONFIG = ("protocol.ssh.allow=never", "protocol.git.allow=never")
# The subcommands that may reach a remote. Nothing else is rewritten: `remote get-url --push`
# rewritten would name the stand-in's folder, and a push to GitHub would not be known as one.
NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
# A variable an argv sets before its program (env's NAME=value), whose value is no program.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _network_at(argv: list[str]) -> int | None:
    """Where the subcommand of the git in ``argv`` is, when it is one of NETWORK_SUBCOMMANDS;
    None for any other argv, and for one with no git in it. The git is the first token
    named git that is no NAME=value; git's options before the subcommand (-C and -c, which
    take a value, and the rest, which do not) are skipped."""
    gits = [
        i
        for i, token in enumerate(argv)
        if os.path.basename(token) == "git" and not _ASSIGNMENT.match(token)
    ]
    if not gits:
        return None
    at = gits[0] + 1
    while at < len(argv) and argv[at].startswith("-"):
        at += 2 if argv[at] in ("-C", "-c") else 1
    return at if at < len(argv) and argv[at] in NETWORK_SUBCOMMANDS else None


def _rule(base: Path) -> str:
    return f"url.{base}/.insteadOf={GITHUB}"


def through_stand_in(argv: list[str], base: Path) -> list[str]:
    """``argv``, if it is a git command that may reach a remote, with git told that GITHUB is
    the folder ``base`` and that it may not connect by ssh or git; any other argv as it is."""
    at = _network_at(argv)
    if at is None:
        return argv
    configs = [_rule(base), *STAND_IN_CONFIG]
    return [*argv[:at], *(t for c in configs for t in ("-c", c)), *argv[at:]]


def assert_through_stand_in(argv: list[str], base: Path) -> None:
    """The guard: a git command in ``argv`` that may reach a remote and is not told to take
    GITHUB from ``base`` would connect to the real github.com, and is an AssertionError."""
    at = _network_at(argv)
    if at is None:
        return
    before = argv[:at]
    told = set(zip(before, before[1:], strict=False))
    assert all(("-c", c) in told for c in (_rule(base), *STAND_IN_CONFIG)), (
        f"a git {argv[at]} that is not taken from the stand-in would reach github.com: {argv}"
    )
