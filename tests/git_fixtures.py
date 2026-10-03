"""Real git repositories for the tests, made with the system's own git.

Not a test module: the tests that need a repository import it. Git runs here
with a fixed author and an empty configuration of its own, so the person's
configuration, which rc-helper's git does read, is only what a test plants.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

FIXTURE_AUTHOR = ("Fixture Author", "fixture@example.com")


def fixture_env(home: Path) -> dict[str, str]:
    """The environment fixture git runs in: system PATH, its own empty HOME."""
    home.mkdir(parents=True, exist_ok=True)
    name, email = FIXTURE_AUTHOR
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


# An empty HOME for fixture git, outside every home the tests give rc-helper.
FIXTURE_HOME = Path(tempfile.mkdtemp(prefix="rcman-fixture-git-home-"))
atexit.register(shutil.rmtree, FIXTURE_HOME, ignore_errors=True)


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


def make_repo(path: Path, origin: Path | None = None) -> Path:
    """A repository at ``path`` on main with one commit.

    With ``origin``: a bare repository there, holding main and feature, which
    the repository has as its remote ``origin``; main tracks origin/main, and
    there is no local feature.
    """
    path.mkdir(parents=True, exist_ok=True)
    fixture_git(path, "init", "-q", "-b", "main")
    write(path / "README.md", BODY)
    fixture_git(path, "add", "README.md")
    fixture_git(path, "commit", "-q", "-m", "First commit")
    if origin is not None:
        origin.parent.mkdir(parents=True, exist_ok=True)
        fixture_git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
        fixture_git(path, "remote", "add", "origin", str(origin))
        fixture_git(path, "branch", "feature")
        fixture_git(path, "push", "-q", "origin", "main", "feature")
        fixture_git(path, "branch", "-D", "feature")
        fixture_git(path, "branch", "-q", "--set-upstream-to=origin/main", "main")
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


def add_submodule(repo: Path, sub_source: Path, name: str) -> None:
    """``sub_source``, a repository, added to ``repo`` as the submodule ``name``."""
    fixture_git(
        repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_source), name
    )
