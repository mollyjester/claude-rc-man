"""Install or upgrade rcman on this machine. Run through ``install.sh``, as root.

``install.sh`` updates the clone first; this module does everything else:
settings, the packages rootless Podman needs, system users and groups, rcman's
subordinate ids, linger and Podman socket, the toolchain, a release built from
the clone's commit, the static tools and the default workspace image, the
Claude Code store, configuration, systemd services, the first administrator,
and a health check. Every step leaves things as they are when they are already
right, so running it again changes nothing.

Python standard library and the clone's helpers only: it runs under the system
interpreter.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# install.sh runs this in isolated mode, which puts nothing of the clone on the
# path; the rule for addresses is the clone's helpers/rules.py. It runs as root, so
# it writes no bytecode into the clone, which is someone else's, and takes the
# clone off the path again once it has it.
_on_path = str(REPO) in sys.path
if not _on_path:
    sys.path.insert(0, str(REPO))
_write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
try:
    from helpers import claude_release
    from helpers.rules import EMAIL, Rule
finally:
    sys.dont_write_bytecode = _write_bytecode
    if not _on_path:
        sys.path.remove(str(REPO))

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# The default workspace image's build file: its content names its tag (workspace_image).
CONTAINERFILE = Path(__file__).resolve().parent / "workspace" / "Containerfile"
# The devcontainer builder image's build context: its Containerfile, the Podman
# configuration it carries, and the lock that pins the Dev Containers CLI.
BUILDER_DIR = Path(__file__).resolve().parent / "builder"

ROOT = Path("/opt/rcman")
CURRENT = ROOT / "current"
TOOLCHAIN = ROOT / "toolchain"
ETC = Path("/etc/rcman")
STATE = Path("/var/lib/rcman")
SETTINGS_FILE = ETC / "install.env"
SYSTEMD = Path("/etc/systemd/system")
# The static tools mounted into every workspace, and the Claude Code store.
TOOLS = STATE / "tools"
CLAUDE_STORE = STATE / "claude"
CADDY_SNIPPET = ETC / "Caddyfile.snippet"
RUN = Path("/run/rcman")
SOCKET = RUN / "rcman.sock"  # rcman's only listener; Caddy's group alone may enter RUN
TMPFILES = Path("/etc/tmpfiles.d/rcman.conf")
# Where the commands an administrator runs as root are put: on root's path. Each is a
# script of the project (pyproject.toml), in the release's virtualenv.
COMMAND_DIR = Path("/usr/local/sbin")
COMMANDS = ("rcman-admin",)

# rcman's home: its own and closed to others. Podman adds the right to enter to every folder above
# a container that runs in a user namespace of its own, rcman's home among them; that lets no
# one read or list anything in it, so it is as right as without, and is not taken away: the
# next container would add it again.
STATE_MODE = 0o750
STATE_MODES = (STATE_MODE, STATE_MODE | 0o001)
APP_USER = "rcman"
PROXY_USER = "rcman-proxy"
WEB_GROUP = "rcman-web"  # its only member is Caddy's account
COMPLETE = ".complete"  # marks a release that was fully built
UNITS = ("rcman.service", "rcman-oauth2-proxy.service")
TEMPLATES = (
    "rcman.env",
    "oauth2-proxy.cfg",
    "rcman.service",
    "rcman-oauth2-proxy.service",
    "Caddyfile",
)

UV_VERSION = "0.12.21"
OAUTH2_PROXY_VERSION = "v7.15.5"
# tmux, built statically for the workspaces from its release's source, which is
# checked against this sha256: tmux publishes no checksum of its own.
TMUX_VERSION = "3.5a"
TMUX_SHA256 = "16216bd0877170dfcc64157085ba9013610b12b082548c7c9542cc0103198951"
TMUX_URL = (
    f"https://github.com/tmux/tmux/releases/download/{TMUX_VERSION}/tmux-{TMUX_VERSION}.tar.gz"
)
# What tmux is built in: musl, so that the static binary needs nothing of any image.
TMUX_BUILD_IMAGE = "docker.io/library/alpine:3.20"
TMUX_BUILD = (
    "apk add -q build-base libevent-dev libevent-static ncurses-dev ncurses-static bison "
    ">/dev/null && cd /tmp && tar xzf /tmux.tar.gz && cd tmux-* && "
    "./configure --enable-static >/dev/null && make -j4 >/dev/null && cp tmux /out/tmux"
)
# The terminal descriptions a workspace's tmux and Claude need, from the host's ncurses-base.
TERMINFO = ("x/xterm-256color", "t/tmux", "t/tmux-256color", "s/screen", "s/screen-256color")
TERMINFO_DIRS = (Path("/usr/share/terminfo"), Path("/lib/terminfo"))
# What rootless Podman, its network and the workspaces' tools need.
PACKAGES = (
    "podman",
    "uidmap",
    "catatonit",
    "slirp4netns",
    "dbus-user-session",
    "busybox-static",
    "ncurses-base",
)
BUSYBOX = Path("/bin/busybox")
# rcman's subordinate ids: room for rcman's own range (slot 0), every person's and the
# devcontainer builder's, which sits above them all (rcman/people.py MAX_PEOPLE and
# BUILDER_SLOT, rcman/runtime.py RANGE), from the first id useradd leaves to such ranges.
RANGE = 65536
SUBORDINATE_COUNT = (1 + 64 + 1) * RANGE
SUBORDINATE_FIRST = 100000
SUBORDINATE_FILES = (Path("/etc/subuid"), Path("/etc/subgid"))
LINGER = Path("/var/lib/systemd/linger")
CGROUP_ROOT = Path("/sys/fs/cgroup")
# The cgroup controllers limits on memory, CPU and processes need the user manager to have.
LIMIT_CONTROLLERS = ("cpu", "memory", "pids")
# How long rcman's user manager may take to bring up its Podman socket.
SOCKET_WITHIN = 60.0
# Where Claude Code's releases are: helpers/claude_release.py, which rcman fetches them with too.
CLAUDE_DOWNLOAD = claude_release.DOWNLOAD
HEALTH_TIMEOUT = 60.0
CADDY_ADMIN_PORT = 2019  # Caddy's default admin API address is localhost:2019
CADDY_ADMIN_HOST = "127.0.0.1"  # what localhost is
CADDY_ADMIN_WARNING = (
    f"Something answers on Caddy's admin API address, localhost:{CADDY_ADMIN_PORT} "
    f"({CADDY_ADMIN_HOST}:{CADDY_ADMIN_PORT}). Any local user can "
    "rewrite Caddy's configuration through it, and so reach rcman as anyone. Put `admin off` "
    "(or an admin address on a unix socket) in the global options of your Caddyfile."
)

LABEL = r"(?!-)[A-Za-z0-9-]+(?<!-)"  # a host name label: no leading or trailing hyphen
HOSTNAME = re.compile(LABEL + r"(?:\." + LABEL + r")+")
PORT = re.compile(r"[1-9][0-9]{1,4}")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # a Linux account's name
# An address releases are downloaded from: http or https, with no slash at its end.
DOWNLOAD_ADDRESS = claude_release.DOWNLOAD_ADDRESS


class InstallError(Exception):
    pass


# What only an rcman from before workspaces left: there is no upgrade from one.
OLD_INSTALL_FILES = (Path("/etc/sudoers.d/rcman"), Path("/etc/rcman/users.toml"))
# What a release of rcman with workspaces has.
WORKSPACES_MARK = Path("backend/rcman/workspaces.py")
FRESH = (
    "install rcman afresh: remove the old installation first, as docs/installation.md "
    "says, since there is no upgrade from it"
)


def check_not_an_old_install() -> None:
    """Refuse a machine an rcman from before workspaces is installed on."""
    found = [str(path) for path in OLD_INSTALL_FILES if path.exists()]
    database = STATE / "rcman.db"
    if database.exists():
        import sqlite3

        con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in con.execute("select name from sqlite_master")}
        finally:
            con.close()
        if "people" not in tables:
            found.append(str(database))
    if found:
        raise InstallError(
            f"this machine has an rcman from before workspaces ({', '.join(found)}); {FRESH}"
        )


# --- settings -----------------------------------------------------------------------

# key: (question, secret, default, pattern). A default of None means it must be given.
# A pattern that is a Rule (helpers/rules.py) explains a refusal with its sentence.
FIELDS = {
    "DOMAIN": ("Public host name of rcman (such as rcman.example.com)", False, None, HOSTNAME),
    "GOOGLE_CLIENT_ID": ("Google OAuth client ID", False, None, None),
    "GOOGLE_CLIENT_SECRET": ("Google OAuth client secret", True, None, None),
    "ADMIN_EMAIL": ("Google email address of the first administrator", False, None, EMAIL),
    "PROXY_PORT": ("Local port for oauth2-proxy", False, "4180", PORT),
    "CADDY_USER": ("Linux account Caddy runs as", False, "caddy", ACCOUNT),
    "CLAUDE_DOWNLOAD": (
        "Address Claude Code's releases are downloaded from",
        False,
        CLAUDE_DOWNLOAD,
        DOWNLOAD_ADDRESS,
    ),
}


def load_settings(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def save_settings(path: Path, values: dict[str, str]) -> bool:
    text = "# Answers install.sh saved; it reads them on every run.\n"
    text += "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_if_changed(path, text, 0o600)


def gather(
    saved: Path, environ: dict[str, str], flags: dict[str, str | None], ask
) -> dict[str, str]:
    """Settings from flags, then RCMAN_INSTALL_* variables, then the saved file, then questions.

    Only the fields and the cookie secret are kept, so a saved setting the
    installer no longer has is dropped when the settings are saved again.
    """
    keep = (*FIELDS, "COOKIE_SECRET")
    values = {k: v for k, v in load_settings(saved).items() if k in keep}
    for key in keep:
        if environ.get(f"RCMAN_INSTALL_{key}"):
            values[key] = environ[f"RCMAN_INSTALL_{key}"]
        if flags.get(key.lower()):
            values[key] = flags[key.lower()]
    missing = []
    for key, (question, secret, default, _pattern) in FIELDS.items():
        if values.get(key):
            continue
        if default is not None:
            values[key] = default
        elif ask is not None:
            values[key] = ask(key, question, secret)
        else:
            missing.append(key)
    if missing:
        raise InstallError(
            "missing settings (give them as --options or RCMAN_INSTALL_* variables, "
            "or run from a terminal to be asked): " + ", ".join(missing)
        )
    values.setdefault("COOKIE_SECRET", secrets.token_urlsafe(24))
    for key, (_question, _secret, _default, pattern) in FIELDS.items():
        if not values[key] or (pattern and not pattern.fullmatch(values[key])):
            why = f" {pattern.sentence}" if isinstance(pattern, Rule) else ""
            raise InstallError(f"invalid {key}: {values[key]!r}.{why}")
    return values


def _ask_terminal(key: str, question: str, secret: bool) -> str:
    import getpass

    return (getpass.getpass if secret else input)(f"{question}: ").strip()


# --- rendering ------------------------------------------------------------------------


def _paths() -> dict[str, str]:
    return {
        "CURRENT": str(CURRENT),
        "ETC": str(ETC),
        "STATE": str(STATE),
        "TOOLS": str(TOOLS),
        "CLAUDE_STORE": str(CLAUDE_STORE),
        "SOCKET": str(SOCKET),
        "OAUTH2_PROXY": str(oauth2_proxy_binary()),
    }


def render(name: str, settings: dict[str, str]) -> str:
    """A template with the settings, and what the installer found out — rcman's uid as
    APP_UID, the default image as WORKSPACE_IMAGE and the devcontainer builder image as
    BUILDER_IMAGE — filled in."""
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    values = {**settings, **_paths()}

    def replace(match: re.Match) -> str:
        return values[match.group(1)]

    return re.sub(r"@([A-Z0-9_]+)@", replace, text)


def tmpfiles() -> str:
    """The tmpfiles.d line that makes the socket's directory at every boot."""
    return f"d {RUN} 0750 {APP_USER} {WEB_GROUP} -\n"


def write_if_changed(path: Path, text: str, mode: int, gid: int | None = None) -> bool:
    """Write ``text`` atomically unless it is already there; True when something changed."""
    changed = not path.exists() or path.read_text(encoding="utf-8") != text
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    st = path.stat()
    if (st.st_mode & 0o7777) != mode:
        os.chmod(path, mode)
        changed = True
    if gid is not None and st.st_gid != gid:
        os.chown(path, 0, gid)
        changed = True
    return changed


# --- releases ---------------------------------------------------------------------------


def _link(root: Path, name: str) -> str | None:
    path = root / name
    return os.readlink(path) if path.is_symlink() else None


def _set_link(root: Path, name: str, target: str) -> None:
    tmp = root / f".{name}.new"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, root / name)


def switch(root: Path, release: str) -> bool:
    """Make ``release`` current and the old current previous; remove the old previous."""
    target = f"releases/{release}"
    if not (root / target / COMPLETE).exists():
        raise InstallError(f"release {release} is incomplete")
    current, old_previous = _link(root, "current"), _link(root, "previous")
    if current == target:
        return False
    if current:
        _set_link(root, "previous", current)
    _set_link(root, "current", target)
    if old_previous and old_previous not in (current, target):
        shutil.rmtree(root / old_previous, ignore_errors=True)  # it fell off the end
    return True


def rollback(root: Path) -> None:
    current, previous = _link(root, "current"), _link(root, "previous")
    if not previous:
        raise InstallError("there is no previous release to roll back to")
    if not (root / previous / WORKSPACES_MARK).exists():
        raise InstallError(f"the previous release is from before workspaces; {FRESH}")
    _set_link(root, "current", previous)
    _set_link(root, "previous", current)


def link_commands(bin_dir: Path = COMMAND_DIR, current: Path = CURRENT) -> list[str]:
    """Each of COMMANDS in ``bin_dir``, a link to the current release's script of that
    name; the links it made or repaired. Through ``current``, so a switch or a rollback
    needs no change to them. Something there that is not a link is refused, not replaced."""
    changed = []
    for name in COMMANDS:
        path, target = bin_dir / name, str(current / ".venv" / "bin" / name)
        if path.is_symlink():
            if os.readlink(path) == target:
                continue
        elif path.exists():
            raise InstallError(f"{path} is there already and is not rcman's; remove it first")
        tmp = bin_dir / f".{name}.new"
        tmp.unlink(missing_ok=True)
        os.symlink(target, tmp)
        os.replace(tmp, path)
        changed.append(str(path))
    return changed


# --- system ---------------------------------------------------------------------------


def run(argv: list[str], user: str | None = None, **kw) -> subprocess.CompletedProcess:
    if user:
        argv = ["runuser", "-u", user, "--", *argv]
    kw.setdefault("text", True)
    proc = subprocess.run(argv, capture_output=True, **kw)
    if proc.returncode != 0:
        output = [
            o.decode(errors="replace") if isinstance(o, bytes) else o
            for o in (proc.stdout, proc.stderr)
        ]
        raise InstallError(f"{' '.join(argv)} failed:\n{''.join(output)}")
    return proc


def _exists(kind: str, name: str) -> bool:
    return subprocess.run(["getent", kind, name], capture_output=True).returncode == 0


def check_caddy_user(name: str) -> None:
    if not _exists("passwd", name):
        raise InstallError(
            f"there is no Linux account {name!r} for Caddy; give the account Caddy runs as "
            "with --caddy-user"
        )


def ensure_accounts(caddy_user: str) -> list[str]:
    check_caddy_user(caddy_user)
    changes = []
    if not _exists("passwd", APP_USER):
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--home-dir",
                str(STATE),
                "--shell",
                "/usr/sbin/nologin",
                APP_USER,
            ]
        )
        changes.append(f"user {APP_USER}")
    if not _exists("passwd", PROXY_USER):
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                PROXY_USER,
            ]
        )
        changes.append(f"user {PROXY_USER}")
    if not _exists("group", WEB_GROUP):
        run(["groupadd", "--system", WEB_GROUP])
        changes.append(f"group {WEB_GROUP}")
    if WEB_GROUP not in run(["id", "-nG", caddy_user]).stdout.split():  # to reach rcman's socket
        run(["usermod", "--append", "--groups", WEB_GROUP, caddy_user])
        changes.append(f"{caddy_user} in group {WEB_GROUP}")
    for member in run(["getent", "group", WEB_GROUP]).stdout.strip().split(":")[3].split(","):
        if member and member != caddy_user:  # Caddy's account is the only one let in
            run(["gpasswd", "--delete", member, WEB_GROUP])
            changes.append(f"{member} removed from group {WEB_GROUP}")
    proxy_groups = run(["id", "-nG", PROXY_USER]).stdout.split()
    if APP_USER not in proxy_groups:  # to read the allowed list, which belongs to group rcman
        run(["usermod", "--append", "--groups", APP_USER, PROXY_USER])
        changes.append(f"{PROXY_USER} in group {APP_USER}")
    app = pwd.getpwnam(APP_USER)
    STATE.mkdir(parents=True, exist_ok=True)
    if STATE.stat().st_uid != app.pw_uid or (STATE.stat().st_mode & 0o777) not in STATE_MODES:
        os.chown(STATE, app.pw_uid, app.pw_gid)
        os.chmod(STATE, STATE_MODE)
        changes.append(str(STATE))
    ETC.mkdir(parents=True, exist_ok=True)
    return changes


def ensure_runtime_dir() -> list[str]:
    """The socket's directory: made at boot by tmpfiles.d, and made or repaired here."""
    changes = []
    if write_if_changed(TMPFILES, tmpfiles(), 0o644):
        changes.append(str(TMPFILES))
    uid, gid = pwd.getpwnam(APP_USER).pw_uid, _gid(WEB_GROUP)
    RUN.mkdir(parents=True, exist_ok=True)
    st = RUN.stat()
    if (st.st_uid, st.st_gid, st.st_mode & 0o7777) != (uid, gid, 0o750):
        os.chown(RUN, uid, gid)
        os.chmod(RUN, 0o750)
        changes.append(str(RUN))
    return changes


# --- rootless Podman for rcman -----------------------------------------------------------


def ensure_packages() -> list[str]:
    """PACKAGES, those missing installed with apt."""
    missing = []
    for package in PACKAGES:
        found = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Status}", package], capture_output=True, text=True
        )
        if found.stdout.strip() != "installed":
            missing.append(package)
    if not missing:
        return []
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    run(["apt-get", "update", "-qq"], env=env)
    run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing], env=env)
    return [f"package {name}" for name in missing]


def subordinate_start(lines: list[str], name: str, count: int) -> int | None:
    """Where a new subordinate range of ``count`` ids for ``name`` starts, after every
    range in ``lines``, as /etc/subuid has them; None when ``name`` has one that big."""
    ends = [SUBORDINATE_FIRST]
    for line in lines:
        fields = line.strip().split(":")
        if len(fields) != 3 or line.lstrip().startswith("#"):
            continue
        try:
            start, size = int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if fields[0] == name and size >= count:
            return None
        ends.append(start + size)
    return max(ends)


def ensure_subordinate_ids() -> list[str]:
    """rcman's subordinate uids and gids: SUBORDINATE_COUNT of each, a range of its own."""
    changes = []
    for path, option in zip(SUBORDINATE_FILES, ("--add-subuids", "--add-subgids"), strict=True):
        lines = path.read_text().splitlines() if path.exists() else []
        start = subordinate_start(lines, APP_USER, SUBORDINATE_COUNT)
        if start is None:
            continue
        run(["usermod", option, f"{start}-{start + SUBORDINATE_COUNT - 1}", APP_USER])
        changes.append(f"{path} {APP_USER}")
    return changes


def app_uid() -> int:
    return pwd.getpwnam(APP_USER).pw_uid


def podman_socket(uid: int) -> Path:
    return Path(f"/run/user/{uid}/podman/podman.sock")


def as_rcman_podman(uid: int, *args: str) -> list[str]:
    """``podman --remote`` as rcman, through its own user manager's socket."""
    return [
        "runuser", "-u", APP_USER, "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
        f"HOME={STATE}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "podman", "--remote", "--url", f"unix://{podman_socket(uid)}", *args,
    ]  # fmt: skip


def ensure_podman(subordinate_changed: bool) -> list[str]:
    """Linger for rcman, so its user manager runs from boot, and Podman's socket enabled in
    it; Podman told of rcman's subordinate ids when they changed."""
    changes = []
    uid = app_uid()
    if not (LINGER / APP_USER).exists():
        run(["loginctl", "enable-linger", APP_USER])
        changes.append(f"linger {APP_USER}")
    run(["systemctl", "start", f"user@{uid}.service"])
    manager = ["systemctl", "--user", "--machine", f"{APP_USER}@"]
    if subordinate_changed:  # Podman takes new subordinate ids only once told to, locally
        run(
            ["runuser", "-u", APP_USER, "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
             f"HOME={STATE}", f"XDG_RUNTIME_DIR=/run/user/{uid}", "podman", "system", "migrate"]
        )  # fmt: skip
        subprocess.run([*manager, "stop", "podman.service"], capture_output=True)
    enabled = subprocess.run([*manager, "is-enabled", "podman.socket"], capture_output=True)
    if enabled.returncode != 0:
        run([*manager, "enable", "--now", "podman.socket"])
        changes.append("podman.socket")
    else:
        run([*manager, "start", "podman.socket"])
    deadline = time.monotonic() + SOCKET_WITHIN
    while not podman_socket(uid).is_socket():
        if time.monotonic() > deadline:
            raise InstallError(f"rcman's Podman socket {podman_socket(uid)} did not appear")
        time.sleep(0.5)
    return changes


def as_rcman_systemctl(uid: int, *args: str) -> list[str]:
    """``systemctl --user`` as rcman, in its own user manager."""
    return [
        "runuser", "-u", APP_USER, "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
        f"HOME={STATE}", f"XDG_RUNTIME_DIR=/run/user/{uid}", "systemctl", "--user", *args,
    ]  # fmt: skip


def warn(text: str) -> None:
    print(f"Warning: {text}", file=sys.stderr)


def limits_problem(uid: int) -> str | None:
    """Why limits on memory, CPU and processes cannot be applied on this machine, or None when
    they can: cgroup v2 is what the unit manager's slices are limited through, and rcman's user
    manager must have been given the controllers for them, which its control group lists. Where
    that cannot be told - the group is not known, or its list cannot be read - it says so: a
    path that came out empty would be the root cgroup, which lists every controller there is."""
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        return (
            "limits cannot be applied: they need cgroup v2, and this machine does not use "
            f"only it ({CGROUP_ROOT} has no cgroup.controllers). Setting a limit will say so."
        )
    manager = f"user@{uid}.service"
    try:
        group = run(["systemctl", "show", "-p", "ControlGroup", "--value", manager])
        path = group.stdout.strip().strip("/")
        if not path:
            raise InstallError("systemd names no control group for it")
        listed = set((CGROUP_ROOT / path / "cgroup.controllers").read_text("utf-8").split())
    except (InstallError, OSError) as error:
        return (
            f"limits may not be applicable: cannot tell which controllers {manager}, rcman's "
            f"user manager, has ({error}). Setting a limit will say if it cannot be applied."
        )
    missing = [c for c in LIMIT_CONTROLLERS if c not in listed]
    if not missing:
        return None
    return (
        f"limits cannot be applied: {manager}, rcman's user manager, has not been given: "
        f"{', '.join(missing)}. Delegate {' '.join(LIMIT_CONTROLLERS)} to the user managers "
        "(Delegate= in a drop-in of user@.service) and run install.sh again; until then "
        "setting a limit will say it could not be applied."
    )


USER_SLICE_DROP_IN = """\
# Written by rcman's installer: the limits rcman sets on a person's workspaces are the only
# limits on processes there are, so the user slice that holds rcman's user manager has none.
[Slice]
TasksMax=infinity
"""
USER_MANAGER_DROP_IN = """\
# Written by rcman's installer: a container's scope has no limit on processes of its own
# unless an administrator sets one (rcman's limits are on slices).
[Manager]
DefaultTasksMax=infinity
"""


def ensure_limits() -> list[str]:
    """What limits on memory, CPU and processes need (see limits.py of the backend): no
    default limit on processes holds rcman's user slice or the scopes of its containers, and
    rcman's user manager reads that default. The manager is never restarted, so nothing it
    runs is ended; asked to read its configuration again, which is asked on every install so
    that one that failed is not left undone, it keeps everything running. Limits are off until
    an administrator sets one, so a machine that cannot apply them is a warning and not a
    failure of the install; the drop-ins are written all the same.

    root writes into folders rcman owns, so none of them, and not the file, may be a link:
    root would follow it, and what it gives away it gives without following one."""
    uid = app_uid()
    app = pwd.getpwnam(APP_USER)
    slice_file = SYSTEMD / f"user-{uid}.slice.d" / "rcman.conf"
    user_file = Path(app.pw_dir) / ".config" / "systemd" / "user.conf.d" / "rcman.conf"
    owned = [*reversed(user_file.parents[:3]), user_file]  # .config, systemd, user.conf.d, file
    for path in owned:
        if path.is_symlink():
            raise InstallError(
                f"{path} is a link, and root will not write through a link in rcman's own "
                "folders. Remove it and run install.sh again."
            )
    if (problem := limits_problem(uid)) is not None:
        warn(problem)
    changes = []
    if write_if_changed(slice_file, USER_SLICE_DROP_IN, 0o644):
        changes.append(str(slice_file))  # read by the daemon-reload that starts the services
    if write_if_changed(user_file, USER_MANAGER_DROP_IN, 0o644):
        changes.append(str(user_file))
    for path in owned:
        os.lchown(path, app.pw_uid, app.pw_gid)
    run(as_rcman_systemctl(uid, "daemon-reexec"))
    return changes


# --- toolchain ---------------------------------------------------------------------------


def _arch() -> str:
    try:
        return claude_release.architecture()
    except claude_release.ReleaseError:
        raise InstallError(f"unsupported machine: {platform.machine()}") from None


def _download(url: str) -> bytes:
    try:
        return claude_release.read(url, claude_release.MAX_BINARY_BYTES)
    except claude_release.ReleaseError as error:
        raise InstallError(str(error)) from None


def _verified(url: str, expected: str) -> bytes:
    data = _download(url)
    try:
        claude_release.check(hashlib.sha256(data).hexdigest(), expected, url)
    except claude_release.ReleaseError as error:
        raise InstallError(str(error)) from None
    return data


def node_version() -> str:
    return (REPO / "frontend" / ".nvmrc").read_text(encoding="utf-8").strip()


def node_bin(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"node-v{node_version()}" / "bin"


def uv_binary(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"uv-{UV_VERSION}" / "uv"


def oauth2_proxy_binary(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"oauth2-proxy-{OAUTH2_PROXY_VERSION}" / "oauth2-proxy"


def tmux_tarball(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"tmux-{TMUX_VERSION}.tar.gz"


def _unpack(
    data: bytes, dest: Path, mode: str, strip: int, members: list[str] | None = None
) -> None:
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}."))
    try:
        with tempfile.TemporaryFile() as archive:
            archive.write(data)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode=mode) as tar:
                for member in tar.getmembers():
                    parts = Path(member.name).parts[strip:]
                    if not parts or (members and parts[-1] not in members):
                        continue
                    member.name = str(Path(*parts))
                    tar.extract(member, tmp, filter="data")
        os.chmod(tmp, 0o755)  # mkdtemp makes it private; the services' users must reach it
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def open_up(root: Path) -> None:
    """Make every directory under ``root`` reachable by the services' users."""
    for path in [root, *root.rglob("*")]:
        if path.is_dir() and not path.is_symlink() and (path.stat().st_mode & 0o755) != 0o755:
            os.chmod(path, path.stat().st_mode | 0o755)


TOOLCHAIN_LOCK = ".lock"


def ensure_toolchain(toolchain: Path = TOOLCHAIN) -> list[str]:
    """Node.js (to build the frontend), uv, oauth2-proxy and tmux's source, each
    checksum-verified.
    Callers filling the same toolchain take turns, holding a lock in it."""
    toolchain.mkdir(parents=True, exist_ok=True)
    with open(toolchain / TOOLCHAIN_LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _fill_toolchain(toolchain)


def _fill_toolchain(toolchain: Path) -> list[str]:
    changes = []
    arch = _arch()
    if not (node_bin(toolchain) / "node").exists():
        version = node_version()
        name = f"node-v{version}-linux-{arch}.tar.xz"
        base = f"https://nodejs.org/dist/v{version}"
        sums = _download(f"{base}/SHASUMS256.txt").decode()
        expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(f"  {name}"))
        _unpack(_verified(f"{base}/{name}", expected), node_bin(toolchain).parent, "r:xz", strip=1)
        changes.append(f"node {version}")
    if not uv_binary(toolchain).exists():
        triple = {"x64": "x86_64", "arm64": "aarch64"}[arch] + "-unknown-linux-gnu"
        url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{triple}.tar.gz"
        expected = _download(url + ".sha256").decode().split()[0]
        _unpack(
            _verified(url, expected), uv_binary(toolchain).parent, "r:gz", strip=1, members=["uv"]
        )
        changes.append(f"uv {UV_VERSION}")
    if not oauth2_proxy_binary(toolchain).exists():
        goarch = {"x64": "amd64", "arm64": "arm64"}[arch]
        name = f"oauth2-proxy-{OAUTH2_PROXY_VERSION}.linux-{goarch}.tar.gz"
        base = (
            f"https://github.com/oauth2-proxy/oauth2-proxy/releases/download/{OAUTH2_PROXY_VERSION}"
        )
        expected = _download(f"{base}/{name}-sha256sum.txt").decode().split()[0]
        _unpack(
            _verified(f"{base}/{name}", expected),
            oauth2_proxy_binary(toolchain).parent,
            "r:gz",
            strip=1,
            members=["oauth2-proxy"],
        )
        changes.append(f"oauth2-proxy {OAUTH2_PROXY_VERSION}")
    if not tmux_tarball(toolchain).exists():
        data = _verified(TMUX_URL, TMUX_SHA256)
        partial = tmux_tarball(toolchain).with_suffix(".partial")
        partial.write_bytes(data)
        partial.rename(tmux_tarball(toolchain))
        changes.append(f"tmux {TMUX_VERSION} source")
    open_up(toolchain)
    return changes


# --- building a release ---------------------------------------------------------------


def build_release(repo: Path, owner: str) -> tuple[str, bool]:
    """The release for the clone's commit, built when it does not exist yet."""
    commit = run(["git", "-C", str(repo), "rev-parse", "HEAD"], user=owner).stdout.strip()
    name = commit[:12]
    release = ROOT / "releases" / name
    if (release / COMPLETE).exists():
        return name, False
    (ROOT / "releases").mkdir(parents=True, exist_ok=True)
    # Built where it will stay: the virtualenv's scripts name their own path.
    # Until .complete is written, it is never switched to and is rebuilt.
    work = release
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    try:
        archive = run(
            ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"], user=owner, text=False
        )
        with tempfile.TemporaryFile() as handle:
            handle.write(archive.stdout)
            handle.seek(0)
            with tarfile.open(fileobj=handle) as tar:
                tar.extractall(work, filter="data")
        path = f"{node_bin()}:/usr/local/bin:/usr/bin:/bin"
        frontend = repo / "frontend"
        env = ["/usr/bin/env", f"PATH={path}", f"HOME={pwd.getpwnam(owner).pw_dir}"]
        run([*env, "npm", "ci", "--no-audit", "--no-fund"], user=owner, cwd=frontend)
        run([*env, "npm", "run", "build"], user=owner, cwd=frontend)
        shutil.copytree(frontend / "dist", work / "frontend" / "dist")
        uv_env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(work / ".venv"),
            "UV_PYTHON": "/usr/bin/python3",
        }
        run(
            [
                str(uv_binary()),
                "sync",
                "--frozen",
                "--no-dev",
                "--no-editable",
                "--python",
                "/usr/bin/python3",
            ],
            cwd=work,
            env=uv_env,
        )
        for path_ in [work, *work.rglob("*")]:
            if not path_.is_symlink():
                os.chown(path_, 0, 0)
                os.chmod(
                    path_, (path_.stat().st_mode & 0o755) | (0o755 if path_.is_dir() else 0o644)
                )
        (work / COMPLETE).write_text(commit + "\n")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return name, True


# --- what workspaces are made of -------------------------------------------------------

PT_INTERP = 3  # the program header naming a dynamic loader


def is_static(path: Path) -> bool:
    """Whether ``path`` is a 64-bit little-endian ELF program that needs no dynamic loader,
    so that it runs in any image."""
    import struct

    with open(path, "rb") as program:
        head = program.read(64)
        if len(head) < 64 or head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
            return False
        offset = struct.unpack_from("<Q", head, 0x20)[0]
        size, count = struct.unpack_from("<HH", head, 0x36)
        program.seek(offset)
        table = program.read(size * count)
    if len(table) < size * count:
        return False
    return all(struct.unpack_from("<I", table, i * size)[0] != PT_INTERP for i in range(count))


def tmux_build_argv(tarball: Path, out: Path) -> list[str]:
    """podman's arguments that build tmux statically from ``tarball`` into ``out``."""
    return [
        "run", "--rm", f"--volume={tarball}:/tmux.tar.gz:ro", f"--volume={out}:/out",
        TMUX_BUILD_IMAGE, "sh", "-c", TMUX_BUILD,
    ]  # fmt: skip


def _install_file(source: Path, dest: Path, mode: int) -> None:
    tmp = dest.with_name(f".{dest.name}.new")
    shutil.copyfile(source, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def ensure_tools(tools: Path = TOOLS, conf: Path | None = None) -> list[str]:
    """The static tools every workspace mounts read-only: tmux, built here from its pinned
    source, busybox and a shell that is busybox, the terminal descriptions, and tmux's
    configuration ``conf``, by default the release's. Root's, and readable by everyone."""
    changes = []
    (tools / "bin").mkdir(parents=True, exist_ok=True)
    os.chmod(tools, 0o755)
    os.chmod(tools / "bin", 0o755)
    built = f"{TMUX_VERSION} {TMUX_SHA256}\n"
    stamp = tools / ".tmux-version"
    if not (tools / "bin" / "tmux").exists() or not stamp.exists() or stamp.read_text() != built:
        out = Path(tempfile.mkdtemp(dir=STATE, prefix=".tmux-build."))
        try:
            app = pwd.getpwnam(APP_USER)
            os.chown(out, app.pw_uid, app.pw_gid)
            run(as_rcman_podman(app.pw_uid, *tmux_build_argv(tmux_tarball(), out)))
            if not is_static(out / "tmux"):
                raise InstallError("tmux was not built static")
            _install_file(out / "tmux", tools / "bin" / "tmux", 0o755)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        stamp.write_text(built)
        changes.append(f"tmux {TMUX_VERSION}")
    if not is_static(BUSYBOX):
        raise InstallError(f"{BUSYBOX} is not static; install busybox-static")
    busybox = tools / "bin" / "busybox"
    if not busybox.exists() or busybox.read_bytes() != BUSYBOX.read_bytes():
        _install_file(BUSYBOX, busybox, 0o755)
        changes.append("busybox")
    if not (tools / "bin" / "sh").is_symlink():
        (tools / "bin" / "sh").unlink(missing_ok=True)
        os.symlink("busybox", tools / "bin" / "sh")
        changes.append("sh")
    for entry in TERMINFO:
        source = next((d / entry for d in TERMINFO_DIRS if (d / entry).exists()), None)
        if source is None:
            raise InstallError(f"there is no terminal description {entry}; install ncurses-base")
        dest = tools / "terminfo" / entry
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(dest.parent, 0o755)
        os.chmod(dest.parent.parent, 0o755)
        if not dest.exists() or dest.read_bytes() != source.read_bytes():
            _install_file(source, dest, 0o644)
            changes.append(f"terminfo {entry}")
    conf = conf or CURRENT / "backend" / "rcman" / "tmux.conf"
    if write_if_changed(tools / "tmux.conf", conf.read_text(encoding="utf-8"), 0o644):
        changes.append("tmux.conf")
    return changes


def workspace_image() -> str:
    """The default workspace image's name, tagged after its build file's content, so a
    changed build file makes a new image and the old one stays the workspaces' it is."""
    digest = hashlib.sha256(CONTAINERFILE.read_bytes()).hexdigest()[:12]
    return f"localhost/rcman-workspace:{digest}"


def ensure_image() -> list[str]:
    """The default workspace image, built with rcman's Podman unless it is there."""
    uid, image = app_uid(), workspace_image()
    if subprocess.run(as_rcman_podman(uid, "image", "exists", image)).returncode == 0:
        return []
    context = Path(tempfile.mkdtemp(prefix="rcman-image."))
    try:
        shutil.copy(CONTAINERFILE, context / "Containerfile")
        os.chmod(context, 0o755)
        os.chmod(context / "Containerfile", 0o644)
        run(as_rcman_podman(uid, "build", "--tag", image, "--file",
                            str(context / "Containerfile"), str(context)))  # fmt: skip
    finally:
        shutil.rmtree(context, ignore_errors=True)
    return [f"image {image}"]


BUILDER_REPOSITORY = "localhost/rcman-builder"


def devcontainer_builder_image() -> str:
    """The devcontainer builder image's name, tagged after every file of BUILDER_DIR, names
    and content, so that a change to any of them makes a new image and the old one stays
    the builds' it is."""
    digest = hashlib.sha256()
    for path in sorted(p for p in BUILDER_DIR.rglob("*") if p.is_file()):
        name = path.relative_to(BUILDER_DIR).as_posix().encode()
        digest.update(len(name).to_bytes(4, "big") + name)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big") + content)
    return f"{BUILDER_REPOSITORY}:{digest.hexdigest()[:12]}"


def prune_devcontainer_builder_images(uid: int, keep: str) -> None:
    """Remove the builder images but ``keep``, as far as Podman lets: an image a running
    builder uses refuses, and stays until a later run."""
    listed = subprocess.run(
        as_rcman_podman(uid, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                        "--filter", f"reference={BUILDER_REPOSITORY}"),
        capture_output=True, text=True,
    )  # fmt: skip
    for name in listed.stdout.split():
        if name.startswith(f"{BUILDER_REPOSITORY}:") and name != keep:
            subprocess.run(as_rcman_podman(uid, "rmi", name), capture_output=True)


def ensure_devcontainer_builder() -> list[str]:
    """The devcontainer builder image, built with rcman's Podman unless it is there, like
    the default image; it needs the network for the same reason. Older ones are pruned."""
    uid, image = app_uid(), devcontainer_builder_image()
    changes = []
    if subprocess.run(as_rcman_podman(uid, "image", "exists", image)).returncode != 0:
        context = Path(tempfile.mkdtemp(prefix="rcman-builder."))
        try:
            shutil.copytree(BUILDER_DIR, context, dirs_exist_ok=True)
            for path in (context, *context.rglob("*")):  # rcman reads it, not the clone's owner
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
            run(as_rcman_podman(uid, "build", "--tag", image, "--file",
                                str(context / "Containerfile"), str(context)))  # fmt: skip
        finally:
            shutil.rmtree(context, ignore_errors=True)
        changes.append(f"image {image}")
    prune_devcontainer_builder_images(uid, image)
    return changes


def found_out() -> dict[str, str]:
    """What the installer finds out and renders along with the settings: rcman's uid and
    the names of the default image and of the devcontainer builder image."""
    return {
        "APP_UID": str(app_uid()),
        "WORKSPACE_IMAGE": workspace_image(),
        "BUILDER_IMAGE": devcontainer_builder_image(),
    }


def claude_platform() -> str:
    return f"linux-{_arch()}"


def claude_version() -> str:
    """The Claude Code version the store holds: the tested one, which the recorded
    fixtures of the fake claude come from."""
    return (REPO / "helpers" / "claude-version").read_text(encoding="utf-8").strip()


def _release_failed(error: claude_release.ReleaseError, version: str) -> InstallError:
    """What went wrong fetching Claude Code ``version``, as the installer says it."""
    if error.kind == "no_such_version":
        return InstallError(f"Claude Code {version} has no release at the download address")
    if error.kind == "no_build":
        return InstallError(f"Claude Code {version} has no build for {claude_platform()}")
    return InstallError(str(error))


def ensure_claude_store(
    store: Path = CLAUDE_STORE,
    download: str = CLAUDE_DOWNLOAD,
    version: str | None = None,
    owner: tuple[int, int] | None = None,
) -> list[str]:
    """The Claude Code store: ``versions/<version>`` for the tested version, downloaded from
    ``download`` and checked against its release's manifest (helpers/claude_release.py);
    ``current`` naming a version, which is set only where there is none, since rcman switches
    it to the version an administrator approves; and ``bin/claude`` leading to ``current``.
    Readable by everyone, and with ``owner`` (a uid and gid) rcman's, which writes the store
    from then on."""
    version = version or claude_version()
    target = store / "versions" / version
    changes = []
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        try:
            claude_release.fetch(download, version, target)
        except claude_release.ReleaseError as error:
            raise _release_failed(error, version) from None
        changes.append(f"claude {version}")
    if not (store / "current").exists():  # none, or one that names a version that is not there
        _set_link(store, "current", f"versions/{version}")
    (store / "bin").mkdir(exist_ok=True)
    if _link(store / "bin", "claude") != "../current":
        _set_link(store / "bin", "claude", "../current")
    for folder in (store, store / "versions", store / "bin"):
        os.chmod(folder, 0o755)
    if owner is not None:
        for path in (store, *store.rglob("*")):
            os.lchown(path, *owner)
    return changes


# --- configuration, services, first administrator ------------------------------------


def _gid(group: str) -> int:
    return int(run(["getent", "group", group]).stdout.split(":")[2])


def configure(settings: dict[str, str]) -> list[str]:
    """Every file the installer renders; ``settings`` with what ``found_out`` returns."""
    changes = []
    app_gid, proxy_gid = _gid(APP_USER), _gid(PROXY_USER)
    files = [
        (ETC / "rcman.env", render("rcman.env", settings), 0o640, app_gid),
        (ETC / "oauth2-proxy.cfg", render("oauth2-proxy.cfg", settings), 0o640, proxy_gid),
        (CADDY_SNIPPET, render("Caddyfile", settings), 0o644, None),
    ]
    for unit in UNITS:
        files.append((SYSTEMD / unit, render(unit, settings), 0o644, None))
    for path, text, mode, gid in files:
        if write_if_changed(path, text, mode, gid):
            changes.append(str(path))
    allowed = STATE / "allowed-emails"
    if not allowed.exists():  # rcman writes it as it starts; oauth2-proxy needs it to exist
        app = pwd.getpwnam(APP_USER)
        write_if_changed(allowed, "", 0o640, app_gid)
        os.chown(allowed, app.pw_uid, app_gid)
        changes.append(str(allowed))
    return changes


def start_services(restart: bool) -> None:
    run(["systemctl", "daemon-reload"])
    for unit in UNITS:
        run(["systemctl", "enable", unit])
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
        if restart or not active:
            run(["systemctl", "restart", unit])


def admin_argv(email: str) -> list[str]:
    """rcman-admin adding ``email`` as an administrator, run as rcman with its settings."""
    return [
        "runuser", "-u", APP_USER, "--", str(CURRENT / ".venv" / "bin" / "rcman-admin"),
        "--env-file", str(ETC / "rcman.env"), "add", "--admin", "--", email,
    ]  # fmt: skip


def ensure_admin(settings: dict[str, str]) -> list[str]:
    """The first administrator, unless they are there already."""
    listing = [
        "runuser", "-u", APP_USER, "--", str(CURRENT / ".venv" / "bin" / "rcman-admin"),
        "--env-file", str(ETC / "rcman.env"), "list",
    ]  # fmt: skip
    email = settings["ADMIN_EMAIL"].lower()
    if email in [line.split()[0] for line in run(listing).stdout.splitlines() if line.strip()]:
        return []
    run(admin_argv(settings["ADMIN_EMAIL"]))
    return [f"administrator {email}"]


def _status_over_socket(path: Path, url: str) -> int:
    import http.client
    import socket

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(5)
            self.sock.connect(str(path))

    connection = Connection("rcman")
    try:
        connection.request("GET", url)
        return connection.getresponse().status
    finally:
        connection.close()


def _status_over_tcp(host: str, port: str, path: str) -> int:
    import http.client

    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        connection.request("GET", path)
        return connection.getresponse().status
    finally:
        connection.close()


def health(settings: dict[str, str]) -> None:
    checks = {
        "rcman": lambda: _status_over_socket(SOCKET, "/api/health"),
        "oauth2-proxy": lambda: _status_over_tcp("127.0.0.1", settings["PROXY_PORT"], "/ping"),
    }
    deadline = time.monotonic() + HEALTH_TIMEOUT
    pending = dict(checks)
    while pending:
        for name, check in list(pending.items()):
            try:
                if check() == 200:
                    del pending[name]
            except OSError:
                pass
        if pending and time.monotonic() > deadline:
            raise InstallError("not healthy: " + ", ".join(pending))
        if pending:
            time.sleep(1)


def caddy_admin_exposed(port: int = CADDY_ADMIN_PORT) -> bool:
    """Whether something accepts connections on Caddy's default admin API address."""
    import socket

    try:
        socket.create_connection((CADDY_ADMIN_HOST, port), timeout=2).close()
    except OSError:
        return False
    return True


def caddy_notices(changes: list[str], caddy_user: str) -> list[str]:
    """What to tell the administrator to do to Caddy after this run."""
    notices = []
    if str(CADDY_SNIPPET) in changes:
        notices.append(
            f"The Caddy site block changed ({CADDY_SNIPPET}). If your Caddyfile imports "
            "it, nothing more is needed there; if it holds a copy, copy it again. "
            "Then restart Caddy."
        )
    elif f"{caddy_user} in group {WEB_GROUP}" in changes:
        notices.append(
            f"{caddy_user} joined the group {WEB_GROUP}: restart Caddy so it can reach rcman."
        )
    return notices


# --- command line -----------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="install.sh", description="Install or upgrade rcman.")
    for key, (question, _secret, _default, _pattern) in FIELDS.items():
        parser.add_argument("--" + key.lower().replace("_", "-"), dest=key.lower(), help=question)
    parser.add_argument(
        "--rollback", action="store_true", help="switch back to the previous release"
    )
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InstallError("run install.sh with sudo")
        if args.rollback:
            rollback(ROOT)
            start_services(restart=True)
            print(f"Rolled back to {os.readlink(CURRENT)}.")
            return 0
        owner = os.environ.get("SUDO_USER") or ""
        if not owner or owner == "root":
            raise InstallError("run install.sh with sudo from the account that owns the clone")
        ask = _ask_terminal if sys.stdin.isatty() else None
        check_not_an_old_install()
        settings = gather(SETTINGS_FILE, dict(os.environ), vars(args), ask)
        changes = []
        if save_settings(SETTINGS_FILE, settings):
            changes.append(str(SETTINGS_FILE))
        check_caddy_user(settings["CADDY_USER"])
        changes += ensure_packages()
        changes += ensure_accounts(settings["CADDY_USER"])
        subordinate = ensure_subordinate_ids()
        changes += subordinate
        changes += ensure_podman(subordinate_changed=bool(subordinate))
        changes += ensure_limits()
        changes += ensure_runtime_dir()
        changes += ensure_toolchain()
        release, built = build_release(REPO, owner)
        switched = switch(ROOT, release)
        if built or switched:
            changes.append(f"release {release}")
        changes += ensure_tools()
        changes += ensure_image()
        changes += ensure_devcontainer_builder()
        app = pwd.getpwnam(APP_USER)
        changes += ensure_claude_store(
            CLAUDE_STORE, settings["CLAUDE_DOWNLOAD"], owner=(app.pw_uid, app.pw_gid)
        )
        changes += configure({**settings, **found_out()})
        changes += link_commands()
        added = ensure_admin(settings)
        start_services(restart=bool(changes))
        changes += added
        health(settings)
        print(json.dumps({"release": release, "changed": changes}))
        print(f"\nAdd this to your Caddyfile ({CADDY_SNIPPET}):\n")
        print(CADDY_SNIPPET.read_text(encoding="utf-8"))
        for notice in caddy_notices(changes, settings["CADDY_USER"]):
            print(notice)
        if caddy_admin_exposed():
            print(f"Warning: {CADDY_ADMIN_WARNING}", file=sys.stderr)
        return 0
    except InstallError as error:
        print(f"install.sh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
