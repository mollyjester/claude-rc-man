"""A git origin made from the files at /src, and a clone of it owned by ``dev``.

/src holds a snapshot of the checkout's files as they are on disk, uncommitted
changes included, with no ``.git`` (tests/snapshot.py), so what is tested is
what is on disk. Every file there is committed. Used inside the test container
only.
"""

import os
import shutil
import subprocess
from pathlib import Path

SRC = Path("/src")
ORIGIN = Path("/srv/origin.git")
CLONE = Path("/home/dev/rcman")


def sh(*argv, user=None, check=True, **kw):
    if user:
        argv = ("runuser", "-u", user, "--", *argv)
    proc = subprocess.run(argv, capture_output=True, text=True, **kw)
    if check and proc.returncode != 0:
        raise AssertionError(f"{argv} failed:\n{proc.stdout}{proc.stderr}")
    return proc


def git(*args, cwd, user="dev"):
    return sh("git", "-C", str(cwd), *args, user=user).stdout.strip()


def make_clone() -> Path:
    host_uid = int(os.environ.get("HOST_UID", "1500"))
    sh("useradd", "--create-home", "--uid", str(host_uid), "--shell", "/bin/bash", "dev")
    sh("chown", "dev:dev", "/home/dev")  # docker may have created it for the npm cache mount
    for key, value in (
        ("user.name", "dev"),
        ("user.email", "dev@example.com"),
        ("init.defaultBranch", "main"),
    ):
        sh("git", "config", "--global", key, value, user="dev")
    sh("git", "config", "--system", "--add", "safe.directory", "*")
    work = Path("/tmp/src-snapshot")
    shutil.copytree(SRC, work)
    sh("chown", "-R", "dev:dev", str(work))
    git("init", "-q", cwd=work)
    git("add", "-A", "-f", cwd=work)  # a tracked file its .gitignore names, too
    git("commit", "-qm", "snapshot", cwd=work)
    ORIGIN.parent.mkdir(parents=True, exist_ok=True)
    sh("chown", "dev:dev", str(ORIGIN.parent))
    sh("git", "clone", "-q", "--bare", str(work), str(ORIGIN), user="dev")
    sh("git", "clone", "-q", str(ORIGIN), str(CLONE), user="dev")
    return CLONE
