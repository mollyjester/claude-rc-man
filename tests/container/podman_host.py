"""The podman host: rootless Podman for a service account, under systemd.

Not a test module. ``tests/test_container_suite.py`` runs it as root inside a container
built from ``tests/container/podman/Dockerfile`` once systemd has booted there:

- ``setup`` makes the service account, with a subordinate id range, linger, and so a
  user manager of its own, and ``podman.socket`` enabled in it;
- ``load`` loads the image archive on stdin into the account's Podman, through the
  socket, and prints what ``podman load`` prints;
- ``run`` runs the rest of its command line as the account, with its user manager's
  runtime folder.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys

ACCOUNT = "rcman"
# The account's home, and Podman's storage in it: a volume, so that Podman's overlay is
# never on the image's own overlay.
HOME = "/var/lib/rcman"
# The account's subordinate user and group ids: the first, and how many, which are the
# installer's own count: room for every person's slot and the devcontainer builder's.
SUBORDINATE_FIRST = 1000000


def as_account(*argv: str) -> list[str]:
    """``argv`` run as the account, with its user manager's runtime folder."""
    uid = pwd.getpwnam(ACCOUNT).pw_uid
    return ["runuser", "-u", ACCOUNT, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}", *argv]


def run(*argv: str) -> None:
    subprocess.run(argv, check=True, timeout=300, cwd="/")


def setup() -> None:
    from installer import install as ins

    subordinate = f"{SUBORDINATE_FIRST}:{ins.SUBORDINATE_COUNT}"
    # Docker makes / a private mount; on a host it is shared, as rootless Podman expects.
    run("mount", "--make-rshared", "/")
    run("useradd", "--system", "--user-group", "--home-dir", HOME,
        "--shell", "/usr/sbin/nologin", ACCOUNT)  # fmt: skip
    run("chown", f"{ACCOUNT}:{ACCOUNT}", HOME)  # the volume, made root's
    for ids in ("/etc/subuid", "/etc/subgid"):
        with open(ids, "a") as file:
            file.write(f"{ACCOUNT}:{subordinate}\n")
    run("loginctl", "enable-linger", ACCOUNT)
    run("systemctl", "start", f"user@{pwd.getpwnam(ACCOUNT).pw_uid}.service")
    run("systemctl", "--user", "--machine", f"{ACCOUNT}@", "enable", "--now", "podman.socket")


def load() -> None:
    os.chdir("/")
    os.execvp("runuser", as_account("podman", "--remote", "load"))


def run_as_account() -> None:
    os.execvp("runuser", as_account(*sys.argv[2:]))


if __name__ == "__main__":
    {"setup": setup, "load": load, "run": run_as_account}[sys.argv[1]]()
