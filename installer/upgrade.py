"""Upgrade rcman from its web interface: what ``rcman-upgrade.service`` runs as root.

rcman asks for an upgrade by writing its request file, which ``rcman-upgrade.path`` watches;
the service then runs this program, a copy the installer put in a folder of root's
(``/opt/rcman/bin/rcman-upgrade``), with ``/usr/bin/python3 -I``. It checks the clone and its
owner before anything of the clone runs, runs the clone's ``install.sh`` with a fixed
environment and no terminal, and writes what happened to ``STATUS``, which rcman reads.
``--finish``, the service's ``ExecStopPost=``, writes the end of a run the service ended
itself, from the result systemd gives it.

The installer turns this on only when an administrator asks (``--web-upgrade yes``), and
runs the same checks of the clone then. The standard library only: it runs alone.
"""

from __future__ import annotations

import argparse
import datetime
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path("/opt/rcman")  # the installation, installer/install.py ROOT
# What rcman reads: the state of the last upgrade asked for. Root's, in rcman's group.
STATUS_DIR = ROOT / "upgrade"
STATUS = STATUS_DIR / "status.json"
STATUS_MODE = 0o640
STATES = ("running", "succeeded", "failed")
# The status's fields, every one always there: the state; the boot it ran in; the invocation of
# the unit that ran it ($INVOCATION_ID); when it started and finished; the commit of the
# current release before and after; and the last line of what went wrong.
FIELDS = ("state", "boot_id", "invocation", "started", "finished", "from", "to", "error")
# A key no longer in FIELDS that a status read may still hold, dropped before it is checked: the
# first upgrade from the web interface onto this release is finished by the old wrapper, still in
# memory, which writes it; read with it, the status is not one, and --finish would mark the
# upgrade that succeeded failed.
LEFTOVER = "notices"
ERROR_LENGTH = 500  # the most of the error line kept
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

# The first uid of the accounts people sign in with, as useradd gives them.
LOGIN_DEFS = Path("/etc/login.defs")
UID_MIN_FALLBACK = 1000  # login.defs's own default, where it names none
# What the clone's owner must be the only one besides root able to write: every file and folder
# of the clone, as root runs, reads or builds from what is there (install.sh, installer/, the
# helpers, git's objects, configuration and hooks, and the project uv sync builds), and every
# folder above it, which could be put in its place. Only the frontend's packages and its build,
# which the owner's npm makes and uses, are passed by; the build is copied only when it holds
# no link (installer/install.py copy_built_tree). These must be there.
OWNER_ONLY = ("frontend/node_modules", "frontend/dist")
REQUIRED = (".git", "install.sh", "installer", "helpers/rules.py")
# Where a link is refused: git's own files and the installer and helpers, which root reads by
# their path from the clone, tracked or not; and every path git tracks, which a release is built
# from. Elsewhere a link is what the owner's tools make (the virtualenv's interpreter, a
# worktree's packages' programs), which root never reads: it is not followed, and passes.
LINKS_REFUSED_UNDER = (".git", "installer", "helpers")
# How long git may take to list what it tracks.
GIT_TIMEOUT = 120
# The extended attributes that hold an ACL, which could let others write what the mode does not.
ACL_ATTRIBUTES = ("system.posix_acl_access", "system.posix_acl_default")

# Git asks nothing, and gives up on a remote that does not answer, as a service has no one to
# answer it. install.sh gives git the same.
GIT_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=60",
    "GCM_INTERACTIVE": "never",
    "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
    "GIT_HTTP_LOW_SPEED_TIME": "60",
}
# Ubuntu's secure_path: the PATH install.sh has when an administrator runs it.
SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
# The whole environment install.sh runs with, and the unit's (installer/install.py renders it).
# What tells the installer it runs inside the upgrade service, where it must not remove it.
SERVICE_MARK = "RCMAN_UPGRADE_SERVICE"
ENVIRONMENT = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "PATH": SECURE_PATH,
    SERVICE_MARK: "1",
    **GIT_ENVIRONMENT,
}


# rcman's own accounts, never the clone's owner (installer/install.py APP_USER and PROXY_USER).
RCMAN_ACCOUNTS = ("rcman", "rcman-proxy")


class Refused(Exception):
    pass


# --- the clone and its owner --------------------------------------------------------------


def uid_min() -> int:
    """UID_MIN of LOGIN_DEFS, or UID_MIN_FALLBACK where it is missing or names none."""
    try:
        text = LOGIN_DEFS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UID_MIN_FALLBACK
    found = re.search(r"^\s*UID_MIN\s+(\d+)\s*$", text, re.MULTILINE)
    return int(found[1]) if found else UID_MIN_FALLBACK


def clone_owner(clone: Path, sudo_user: str | None) -> str:
    """The account that owns the clone's folder, which must be the one that ran install.sh
    through sudo (``sudo_user``) where there is one, and is never root."""
    uid = os.lstat(clone).st_uid
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        raise Refused(f"the clone {clone} belongs to uid {uid}, which has no account") from None
    if uid == 0:
        raise Refused(f"the clone {clone} is root's; clone it as the account that installs")
    if sudo_user and sudo_user != name:
        raise Refused(
            f"the clone {clone} is {name}'s, not {sudo_user}'s; run install.sh with sudo "
            f"from {name}'s account"
        )
    return name


def check_owner(name: str, refused) -> None:
    """Refuse an owner that is root, below UID_MIN, or one of ``refused`` (rcman's accounts and
    Caddy's): an upgrade runs code the owner can change as root."""
    if name in refused:
        raise Refused(f"the clone belongs to {name}, an account of rcman's or Caddy's")
    uid = pwd.getpwnam(name).pw_uid
    if uid == 0:
        raise Refused("the clone belongs to root")
    if uid < (least := uid_min()):
        raise Refused(
            f"the clone belongs to {name} (uid {uid}), below UID_MIN {least} of {LOGIN_DEFS}: "
            "a system account"
        )


def ancestors(path: Path) -> list[Path]:
    """Every folder above ``path``, the nearest first."""
    return list(Path(path).parents)


def group_members(gid: int) -> set[str]:
    """The accounts in the group ``gid``: its members, and those whose own group it is."""
    try:
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        members = {f"gid {gid}"}
    return members | {a.pw_name for a in pwd.getpwall() if a.pw_gid == gid}


def writers(found: os.stat_result, owner_uid: int) -> set[str]:
    """Who besides root and the owner can write what ``found`` describes."""
    who = set()
    if found.st_uid not in (0, owner_uid):
        who.add(f"uid {found.st_uid}")
    if found.st_mode & stat.S_IWOTH:
        who.add("anyone")
    if found.st_mode & stat.S_IWGRP:
        try:
            allowed = {"root", pwd.getpwuid(owner_uid).pw_name}
        except KeyError:
            allowed = {"root"}
        who |= group_members(found.st_gid) - allowed
    return who


def tracked(clone: Path, owner: str) -> set[str]:
    """The paths git tracks in the clone, relative to it, asked of git as ``owner`` with an
    empty environment but git's settings that ask nothing, no terminal, and nothing of the
    owner's configuration run to answer (core.fsmonitor)."""
    home = pwd.getpwnam(owner).pw_dir
    argv = [
        "runuser", "-u", owner, "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
        f"HOME={home}", *(f"{key}={value}" for key, value in GIT_ENVIRONMENT.items()),
        "git", "-c", "core.fsmonitor=false", "-C", str(clone), "ls-files", "-z",
    ]  # fmt: skip
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, cwd="/",
                              start_new_session=True, timeout=GIT_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        raise Refused(f"git ls-files gave no answer in {clone} within {GIT_TIMEOUT}s") from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no reason"]
        raise Refused(f"git cannot list what it tracks in {clone}: {said[-1]}")
    return {name for name in done.stdout.decode("utf-8", "surrogateescape").split("\0") if name}


def checked_paths(clone: Path, within: tuple[str, ...] | None = None) -> list[Path]:
    """The clone, every file and folder in it but OWNER_ONLY, followed into no link, and every
    folder above it; with ``within``, of the clone only those paths and what is in them."""
    passed = {clone / name for name in OWNER_ONLY}
    found = [clone]
    tops = [clone / name for name in within] if within is not None else [clone]
    for top in tops:
        if top != clone:
            found.append(top)
            if top.is_symlink() or not top.is_dir():
                continue
        for folder, folders, files in os.walk(top, followlinks=False):
            here = Path(folder)
            folders[:] = [name for name in folders if here / name not in passed]
            found += [here / name for name in (*folders, *files)]
    return [*found, *ancestors(clone)]


def acl(path: Path) -> bool:
    """Whether ``path`` itself has an ACL; a filesystem without extended attributes has none."""
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError:
        return False
    return any(name in ACL_ATTRIBUTES for name in names)


def check_paths(
    clone: Path, owner_uid: int, tracked_paths: set[str], within: tuple[str, ...] | None = None
) -> None:
    """Refuse a clone where anyone but root and its owner can change what root runs, reads or
    builds from it, or where a link stands where root would follow it (LINKS_REFUSED_UNDER and
    ``tracked_paths``, the paths git tracks, and the folders they are in)."""
    held = set(tracked_paths)
    for name in tracked_paths:
        held |= {str(parent) for parent in Path(name).parents if str(parent) != "."}
    for name in REQUIRED:
        if not os.path.lexists(clone / name):
            raise Refused(f"{clone / name} is not there: the clone is not a clone of rcman")
    git = os.lstat(clone / ".git")
    if not stat.S_ISDIR(git.st_mode):
        raise Refused(f"{clone / '.git'} is not a folder: a worktree's .git leads elsewhere")
    for path in checked_paths(clone, within):
        found = os.lstat(path)
        if stat.S_ISLNK(found.st_mode):
            inside = path.relative_to(clone).as_posix() if clone in path.parents else None
            if (
                inside is None or inside in held or inside in REQUIRED
                or inside.split("/")[0] in LINKS_REFUSED_UNDER
            ):  # fmt: skip
                raise Refused(f"{path} is a link, which could lead anywhere")
            if found.st_uid not in (0, owner_uid):  # a link's mode means nothing; its owner does
                raise Refused(f"uid {found.st_uid} owns the link {path}, not the clone's owner")
            continue
        if who := writers(found, owner_uid):
            raise Refused(f"{', '.join(sorted(who))} can write {path}, besides its owner and root")
        if acl(path):
            raise Refused(f"{path} has an ACL, which may let others write it; remove it")


def check_clone(clone: Path, sudo_user: str | None, refused) -> str:
    """The clone's owner, once the clone, its owner and what install.sh runs pass the checks."""
    name = clone_owner(clone, sudo_user)
    check_owner(name, refused)
    uid = pwd.getpwnam(name).pw_uid
    # git, run as the owner to say what it tracks, reads .git: that, the required paths and the
    # folders above are checked before it is asked; then the whole clone.
    check_paths(clone, uid, set(), within=REQUIRED)
    check_paths(clone, uid, tracked(clone, name))
    return name


# --- the status ---------------------------------------------------------------------------


def write_status(status: dict, path: Path | None = None, owner: tuple[int, int] | None = None):
    """``status`` written whole to ``path`` (STATUS), by a rename, mode STATUS_MODE, owned by
    ``owner`` (uid, gid) where given."""
    path = path or STATUS
    if set(status) != set(FIELDS) or status["state"] not in STATES:
        raise ValueError(f"not a status: {sorted(status)}")
    text = json.dumps({key: status[key] for key in FIELDS}, indent=2) + "\n"
    if not stat.S_ISDIR(os.lstat(path.parent).st_mode):  # the installer's, never made here
        raise NotADirectoryError(f"{path.parent} is not a folder")
    fd, new = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, STATUS_MODE)
        if owner is not None:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(new, path)
    except BaseException:
        Path(new).unlink(missing_ok=True)
        raise


def read_status(path: Path | None = None) -> dict | None:
    """The status at ``path`` (STATUS), or None where there is none or it is not one."""
    try:
        found = json.loads((path or STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    found.pop(LEFTOVER, None)
    if set(found) != set(FIELDS):
        return None
    return found if found["state"] in STATES else None


def status_owner() -> tuple[int, int]:
    """Root, and rcman's group, which reads the status."""
    return 0, grp.getgrnam(RCMAN_ACCOUNTS[0]).gr_gid


def release() -> str | None:
    """The commit of the current release, which its mark names."""
    try:
        return (ROOT / "current" / ".complete").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def boot_id() -> str:
    return BOOT_ID.read_text(encoding="utf-8").strip()


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


# --- a run --------------------------------------------------------------------------------


def _copy(source, sink, lines: list[str] | None = None) -> None:
    """What ``source`` says, to ``sink`` as it comes and into ``lines`` where given."""
    for line in iter(source.readline, ""):
        sink.write(line)
        sink.flush()
        if lines is not None:
            lines.append(line.rstrip("\n"))


def run_install(clone: Path) -> tuple[int, str | None]:
    """The clone's install.sh, run with ENVIRONMENT alone and no terminal, its output passed
    on to the journal as it comes: its exit code and its last error line."""
    child = subprocess.Popen(
        [str(clone / "install.sh")],
        env=ENVIRONMENT,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    err: list[str] = []
    copiers = [
        threading.Thread(target=_copy, args=(child.stdout, sys.stdout)),
        threading.Thread(target=_copy, args=(child.stderr, sys.stderr, err)),
    ]
    for copier in copiers:
        copier.start()
    code = child.wait()
    for copier in copiers:
        copier.join()
    lines = [line.strip() for line in err if line.strip()]
    # what install.sh and the installer say when they stop begins with its name
    said = [line for line in lines if line.startswith("install.sh:")]
    error = (said or lines or [None])[-1]
    return code, error[:ERROR_LENGTH] if error else None


def upgrade(
    clone: Path, caddy_user: str, owner: tuple[int, int] | None = None, invocation=None
) -> int:
    """Check the clone, run its install.sh, and write the status as it goes, with the unit's
    ``invocation``: 0 when it succeeded."""
    status = dict.fromkeys(FIELDS)
    status.update(state="running", boot_id=boot_id(), invocation=invocation, started=now())
    status["from"] = release()
    write_status(status, owner=owner)
    try:
        check_clone(clone, None, (*RCMAN_ACCOUNTS, caddy_user))
        code, error = run_install(clone)
    except (Refused, OSError) as refused:
        code, error = 1, str(refused)[:ERROR_LENGTH]
    status.update(
        state="succeeded" if code == 0 else "failed", finished=now(), to=release(),
        error=None if code == 0 else error or f"install.sh exited {code}",
    )  # fmt: skip
    write_status(status, owner=owner)
    return 0 if code == 0 else 1


def finish(environ: dict[str, str], owner: tuple[int, int] | None = None) -> int:
    """The end of a run, from the result systemd gives ``ExecStopPost=`` (``SERVICE_RESULT``,
    ``EXIT_STATUS``) and the status of the same invocation (``INVOCATION_ID``): a run the
    service ended before it wrote its own end, or that systemd saw fail, is failed; one that
    ended by itself is left as it is, and so is another invocation's."""
    result, invocation = environ.get("SERVICE_RESULT", ""), environ.get("INVOCATION_ID")
    ended = f"the upgrade ended with {result or 'no result'} ({environ.get('EXIT_STATUS', '')})"
    status = read_status()
    if status is None:
        status = dict.fromkeys(FIELDS)
        status.update(state="running", boot_id=boot_id(), invocation=invocation)
    if status["invocation"] != invocation:
        return 0
    if status["state"] == "running" or (status["state"] == "succeeded" and result != "success"):
        status.update(state="failed", finished=status["finished"] or now(), error=ended)
        write_status(status, owner=owner)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="rcman-upgrade", description="Upgrade rcman, as rcman-upgrade.service runs it."
    )
    parser.add_argument("--clone", type=Path, help="the clone whose install.sh is run")
    parser.add_argument("--caddy-user", help="the account Caddy runs as, refused as the owner")
    parser.add_argument(
        "--finish", action="store_true", help="write the end of the run the service ended"
    )
    args = parser.parse_args(argv)
    if args.finish:
        return finish(dict(os.environ), status_owner())
    if args.clone is None or not args.caddy_user:
        parser.error("--clone and --caddy-user are needed, or --finish")
    return upgrade(args.clone, args.caddy_user, status_owner(), os.environ.get("INVOCATION_ID"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
