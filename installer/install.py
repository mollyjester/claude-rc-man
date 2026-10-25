"""Install or upgrade rcman on this machine. Run through ``install.sh``, as root.

``install.sh`` updates the clone first; this module does everything else:
settings, the packages rootless Podman needs, system users and groups, rcman's
subordinate ids, linger and Podman socket, the toolchain, a release built from
the clone's commit, the static tools and the default workspace image, the
Claude Code store, configuration, systemd services, the first administrator,
and a health check; and, when an administrator turns it on, the units and the wrapper that
let rcman's administrators upgrade it from its web interface (installer/upgrade.py). Every
step leaves things as they are when they are already right, so running it again changes
nothing.

Root never follows a link inside a path rcman can write: the static tools are in a folder
of root's, and whatever the installer makes inside rcman's home or the socket's folder is
made by rcman (``as_rcman``), or, for the folder itself, changed through a descriptor opened
without following a link (``own_directory``). tests/test_install.py reads this file, one
function at a time, for the mistakes of acting on such a path by its name; that is a net for
what can be seen in the function, not a proof about the program.

install.sh holds the installer's lock (INSTALL_LOCK) on fd 9 for the whole run; ``check_lock``
holds it to that. Python standard library and the clone's helpers only: it runs under the
system interpreter.
"""

from __future__ import annotations

import argparse
import ast
import errno
import fcntl
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import shlex
import shutil
import stat
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
    from helpers import claude_release, instructions, seccomp
    from helpers.rules import EMAIL, Rule
    from installer import upgrade
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
# The static tools mounted into every workspace: root's, so out of rcman's reach. The Claude
# Code store is rcman's, which writes it at run time; the database is rcman's too.
TOOLS = ROOT / "tools"
# Podman in a workspace: the wrappers and their configuration (sources in backend/rcman/podman),
# in this folder of the tools; and the seccomp profile of every workspace container, beside it,
# derived from Podman's own default (helpers/seccomp.py).
PODMAN_TOOLS = "podman"
PODMAN_CONFIGURATION = ("containers.conf", "storage.conf", "registries.conf")
# What the wrapper sources from its folder: what it says of a port published in a workspace.
PODMAN_PUBLISH = "publish.sh"
# What its bin folder holds, which comes before the image's own programs on every workspace's
# PATH: the wrapper, and docker, a link to it.
PODMAN_COMMANDS = ("podman", "docker")
WORKSPACE_SECCOMP = seccomp.NAME  # rcman/runtime.py SECCOMP
PODMAN_SECCOMP = seccomp.PODMAN_DEFAULT
CLAUDE_STORE = STATE / "claude"
DATABASE = STATE / "rcman.db"
CADDY_SNIPPET = ETC / "Caddyfile.snippet"
RUN = Path("/run/rcman")
SOCKET = RUN / "rcman.sock"  # rcman's listener; Caddy's group alone may enter RUN
# rcman's forwarding server (rcman/forward.py): workspaces' forwarded ports, and Caddy's ask.
FORWARD_SOCKET = RUN / "forward.sock"
ASK_PATH = "/.rcman/ask"  # rcman/forward.py ASK_PATH
TMPFILES = Path("/etc/tmpfiles.d/rcman.conf")
# Where the commands an administrator runs as root are put: on root's path. Each is a
# script of the project (pyproject.toml), in the release's virtualenv.
COMMAND_DIR = Path("/usr/local/sbin")
COMMANDS = ("rcman-admin",)
# The installer's lock: install.sh takes it on fd LOCK_FD, before anything else, a rollback too,
# and every run holds it to its end.
INSTALL_LOCK = ROOT / ".install.lock"
LOCK_FD = 9

# Upgrading from the web interface, off unless WEB_UPGRADE is yes. rcman writes the request file
# in its home; the path unit watches it (root never opens it) and starts the service, a oneshot
# that runs the wrapper as root: a copy of UPGRADE_SOURCE in a folder of root's. The wrapper
# writes the status in UPGRADE_DIR, root's and rcman's group's. The units are not among UNITS:
# the installer never starts or restarts the service, which may be the one running it.
UPGRADE_UNITS = ("rcman-upgrade.path", "rcman-upgrade.service")
UPGRADE_REQUEST = STATE / "upgrade.request"
UPGRADE_SOURCE = Path(__file__).resolve().parent / "upgrade.py"
UPGRADE_WRAPPER = ROOT / "bin" / "rcman-upgrade"
UPGRADE_DIR = upgrade.STATUS_DIR
UPGRADE_DIR_MODE = 0o750
# How long the clone's origin may take to answer git ls-remote when the option is turned on.
LS_REMOTE_TIMEOUT = 120
# A clone's path as a unit's ExecStart= holds it as it is: nothing systemd would split, quote,
# expand (% and $) or escape.
UNIT_PATH = re.compile(r"/[A-Za-z0-9._/+-]*")

# rcman's home: its own and closed to others. Podman adds the right to enter to every folder above
# a container that runs in a user namespace of its own, rcman's home among them; that lets no
# one read or list anything in it, so it is as right as without, and is not taken away: the
# next container would add it again.
STATE_MODE = 0o750
STATE_MODES = (STATE_MODE, STATE_MODE | 0o001)
RUN_MODE = 0o750
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
    "rcman-upgrade.service",
    "rcman-upgrade.path",
)

UV_VERSION = "0.12.21"
OAUTH2_PROXY_VERSION = "v7.15.5"
# The sha256 of each archive downloaded, by (version, architecture), pinned here and not read
# from the release page that serves the archive: a page that is not to be trusted could swap
# both. A version changed is changed with its checksums (tests/test_install.py holds that);
# Node.js's version is frontend/.nvmrc's, and its pin is the one that names the same.
NODE_SHA256 = {
    ("24.21.0", "x64"): "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6",
    ("24.21.0", "arm64"): "6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2",
}
UV_SHA256 = {
    ("0.12.21", "x64"): "23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0",
    ("0.12.21", "arm64"): "030b69227b40af8c1981b7301793dc66e71ed3c796ea8688209dd268bd91ec51",
}
OAUTH2_PROXY_SHA256 = {
    ("v7.15.5", "x64"): "f63f94bf72c5f46ab002a0a275aa8b3cf19b4d828aed08a13978cb9a62c3a1fd",
    ("v7.15.5", "arm64"): "ebef73d06a05f76a03b0b2f95aa8424c10201228bd64621a2a7d375ce44dd637",
}
# tmux, built statically for the workspaces from its release's source, which is
# checked against this sha256: tmux publishes no checksum of its own.
TMUX_VERSION = "3.5a"
TMUX_SHA256 = "16216bd0877170dfcc64157085ba9013610b12b082548c7c9542cc0103198951"
TMUX_URL = (
    f"https://github.com/tmux/tmux/releases/download/{TMUX_VERSION}/tmux-{TMUX_VERSION}.tar.gz"
)
# What tmux is built in: musl, so that the static binary needs nothing of any image. Named by
# tag, which says what it is, and by the registry's digest of that tag, which cannot move.
TMUX_BUILD_IMAGE = "docker.io/library/alpine:3.20"
TMUX_BUILD_DIGEST = "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
TMUX_BUILD_REFERENCE = f"{TMUX_BUILD_IMAGE}@{TMUX_BUILD_DIGEST}"
# The build says what it has to say on standard error, with the program's sha256 on a line of
# its own at the end, and the program comes out on standard output: no folder of rcman's is
# mounted for it to be written in. The checksum is what shows the program arrived whole.
TMUX_SHA256_LINE = re.compile(rb"^sha256 ([0-9a-f]{64})$", re.MULTILINE)
TMUX_BUILD = (
    "(apk add -q build-base libevent-dev libevent-static ncurses-dev ncurses-static bison "
    "&& cd /tmp && tar xzf /tmux.tar.gz && cd tmux-* && "
    "./configure --enable-static && make -j4) >&2 && "
    "printf 'sha256 %s\\n' \"$(sha256sum /tmp/tmux-*/tmux | cut -d' ' -f1)\" >&2 && "
    "cat /tmp/tmux-*/tmux"
)
# The terminal descriptions a workspace's tmux and Claude need, from the host's ncurses-base.
TERMINFO = ("x/xterm-256color", "t/tmux", "t/tmux-256color", "s/screen", "s/screen-256color")
TERMINFO_DIRS = (Path("/usr/share/terminfo"), Path("/lib/terminfo"))
# What rootless Podman, its network and the workspaces' tools need.
PACKAGES = (
    "podman",
    "crun",
    "uidmap",
    "catatonit",
    "slirp4netns",
    "dbus-user-session",
    "busybox-static",
    "ncurses-base",
)
BUSYBOX = Path("/bin/busybox")
# The OCI runtime workspaces and the builder are tested with, which rcman's Podman must report.
OCI_RUNTIME = "crun"
# The first kernel that mounts overlay inside a user namespace, which the devcontainer builder's
# Podman stores its images with (native overlay, no FUSE device).
USERNS_OVERLAY_KERNEL = (5, 11)
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
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # an account's name
YES_NO = re.compile(r"yes|no")
# An address releases are downloaded from: http or https, with no slash at its end.
DOWNLOAD_ADDRESS = claude_release.DOWNLOAD_ADDRESS


class InstallError(Exception):
    pass


# --- settings -----------------------------------------------------------------------

# key: (question, secret, default, pattern). A default of None means it must be given.
# A pattern that is a Rule (helpers/rules.py) explains a refusal with its sentence.
FIELDS = {
    "DOMAIN": ("Public host name of rcman (such as rcman.example.com)", False, None, HOSTNAME),
    "GOOGLE_CLIENT_ID": ("Google OAuth client ID", False, None, None),
    "GOOGLE_CLIENT_SECRET": ("Google OAuth client secret", True, None, None),
    "ADMIN_EMAIL": ("Google email address of the first administrator", False, None, EMAIL),
    "PROXY_PORT": ("Local port for oauth2-proxy", False, "4180", PORT),
    "ASK_PORT": (
        "Local port Caddy asks rcman on before it gets a certificate for a forwarded port",
        False,
        "4181",
        PORT,
    ),
    "CADDY_USER": ("Account Caddy runs as", False, "caddy", ACCOUNT),
    "CLAUDE_DOWNLOAD": (
        "Address Claude Code's releases are downloaded from",
        False,
        CLAUDE_DOWNLOAD,
        DOWNLOAD_ADDRESS,
    ),
    "WEB_UPGRADE": (
        "Whether administrators may upgrade rcman from its web interface (yes or no)",
        False,
        "no",
        YES_NO,
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
    if values["ASK_PORT"] == values["PROXY_PORT"]:
        raise InstallError(f"invalid ASK_PORT: {values['ASK_PORT']!r} is oauth2-proxy's port too.")
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
        "FORWARD_SOCKET": str(FORWARD_SOCKET),
        "OAUTH2_PROXY": str(oauth2_proxy_binary()),
        "CLONE": str(REPO),
        "UPGRADE_REQUEST": str(UPGRADE_REQUEST),
    }


def unit_environment(environment: dict[str, str]) -> str:
    """``environment`` as the value of a unit's Environment=: each assignment in double quotes.
    What systemd would expand, unescape or split otherwise is refused, not escaped."""
    items = []
    for key, value in environment.items():
        if re.search(r"""[%$"'\\]|[^\S ]""", key + value):
            raise InstallError(f"{key}={value!r} cannot be written into a unit as it is")
        items.append(f'"{key}={value}"')
    return " ".join(items)


def render(name: str, settings: dict[str, str]) -> str:
    """A template with the settings, and what the installer found out — rcman's uid as
    APP_UID, the default image as WORKSPACE_IMAGE and the devcontainer builder image as
    BUILDER_IMAGE — filled in; and the upgrade wrapper's environment, UPGRADE_ENVIRONMENT."""
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    values = {
        **settings,
        **_paths(),
        "UPGRADE_ENVIRONMENT": unit_environment(upgrade.ENVIRONMENT),
    }

    def replace(match: re.Match) -> str:
        return values[match.group(1)]

    return re.sub(r"@([A-Z0-9_]+)@", replace, text)


def tmpfiles() -> str:
    """The tmpfiles.d line that makes the socket's directory at every boot."""
    return f"d {RUN} {RUN_MODE:04o} {APP_USER} {WEB_GROUP} -\n"


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
    _set_link(root, "current", previous)
    _set_link(root, "previous", current)


def release_schema_version(source: str) -> int:
    """The schema version a release's database module (``backend/rcman/db.py``) leaves a
    database at: the number of its ``MIGRATIONS``, read from the source without running it."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise InstallError(
            "the schema version of that release cannot be told: db.py is not Python"
        ) from None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "MIGRATIONS" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            return len(node.value.elts)
    raise InstallError("the schema version of that release cannot be told: no list MIGRATIONS")


# Run as rcman: the database's schema version, which SQLite keeps in the log until a
# checkpoint, so that only SQLite can say. A missing database is at version zero, and asking
# makes none.
SCHEMA_VERSION_SCRIPT = (
    "import os, sqlite3, sys\n"
    "path = sys.argv[1]\n"
    "print(sqlite3.connect(path).execute('pragma user_version').fetchone()[0]"
    " if os.path.exists(path) else 0)\n"
)


def database_schema_version(database: Path = DATABASE) -> int:
    """The schema version of rcman's database, asked of SQLite as rcman: root opens nothing in
    rcman's home, and a database root opened would be left files of root's."""
    done = rcman_do("/usr/bin/python3", "-I", "-c", SCHEMA_VERSION_SCRIPT, str(database))
    return int(done.stdout.strip())


def check_rollback(root: Path) -> None:
    """Refuse a rollback to a release that cannot read the database: it would not start.
    A newer release is installed instead."""
    previous = _link(root, "previous")
    if not previous:
        raise InstallError("there is no previous release to roll back to")
    try:
        source = (root / previous / "backend" / "rcman" / "db.py").read_text(encoding="utf-8")
    except OSError as error:
        raise InstallError(f"the schema version of {previous} cannot be told: {error}") from None
    reads, have = release_schema_version(source), database_schema_version()
    if have > reads:
        raise InstallError(
            f"the database is at schema version {have}, newer than the {reads} that the "
            f"previous release ({previous}) reads, and that release would not start. Nothing "
            "was changed. Run install.sh to install a newer release instead."
        )


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


def as_account(user: str, argv: list[str]) -> list[str]:
    """``argv`` run as the account ``user``: the one place a command is made to switch."""
    return ["runuser", "-u", user, "--", *argv]


def run(
    argv: list[str], user: str | None = None, check: bool = True, **kw
) -> subprocess.CompletedProcess:
    if user:
        argv = as_account(user, argv)
    kw.setdefault("text", True)
    proc = subprocess.run(argv, capture_output=True, **kw)
    if check and proc.returncode != 0:
        output = [
            o.decode(errors="replace") if isinstance(o, bytes) else o
            for o in (proc.stdout, proc.stderr)
        ]
        raise InstallError(f"{' '.join(argv)} failed:\n{''.join(output)}")
    return proc


def _exists(kind: str, name: str) -> bool:
    return subprocess.run(["getent", kind, name], capture_output=True).returncode == 0


def own_directory(path: Path, uid: int, gid: int, mode: int, accept: tuple[int, ...] = ()) -> bool:
    """Make ``path`` a directory of ``uid`` and ``gid`` with ``mode`` (``accept`` lists other
    modes that are as right); True when it was made or changed.

    ``path`` is an entry of a folder only root can write (/var/lib, /run) and is owned by
    rcman, which cannot replace it but can put a link where something inside it goes. It is
    opened without following a link and changed through its descriptor, so that root is never
    led elsewhere; a link in its place is refused. Nothing inside it is touched."""
    created = False
    try:
        os.mkdir(path, mode)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise InstallError(
                f"{path} is a link, or is not a directory, and root will not change it through "
                "a link. Remove it and run install.sh again."
            ) from None
        raise
    try:
        found = os.fstat(fd)
        right = (found.st_uid, found.st_gid) == (uid, gid) and (
            stat.S_IMODE(found.st_mode) in (mode, *accept)
        )
        if not right:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        return created or not right
    finally:
        os.close(fd)


def check_lock(fd: int = LOCK_FD, path: Path = INSTALL_LOCK, owner_uid: int = 0) -> None:
    """Refuse to run unless ``fd`` is install.sh's hold on the installer's lock: the file at
    ``path`` (the same device and inode, and not a link), in a folder only ``owner_uid``, root,
    can write, and locked by this run. Its children are not given it."""
    try:
        held = os.fstat(fd)
    except OSError:
        raise InstallError(
            "run install.sh, which takes the installer's lock; install.py is not run alone"
        ) from None
    try:
        there = os.lstat(path)
        folder = os.lstat(path.parent)
    except OSError as error:
        raise InstallError(f"the installer's lock {path} is not there: {error}") from None
    if stat.S_ISLNK(there.st_mode) or not stat.S_ISREG(there.st_mode):
        raise InstallError(f"{path} is a link or not a file; remove it and run install.sh again")
    if folder.st_uid != owner_uid or folder.st_mode & 0o022 or stat.S_ISLNK(folder.st_mode):
        raise InstallError(f"{path.parent} must be a folder of root's that only root can write")
    if (held.st_dev, held.st_ino) != (there.st_dev, there.st_ino):
        raise InstallError(f"descriptor {fd} is not the installer's lock {path}; run install.sh")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise InstallError(
            "another install.sh is running; wait for it to end, then run it again"
        ) from None
    os.set_inheritable(fd, False)


def check_kernel(release: str) -> None:
    """Refuse a kernel older than USERNS_OVERLAY_KERNEL, or one whose release cannot be read."""
    needed = ".".join(map(str, USERNS_OVERLAY_KERNEL))
    found = re.match(r"(\d+)\.(\d+)", release)
    if found is None or (int(found[1]), int(found[2])) < USERNS_OVERLAY_KERNEL:
        raise InstallError(
            f"the kernel is {release}, and the builder of devcontainer images mounts overlay "
            f"inside a user namespace, which needs Linux {needed} or later; upgrade the kernel "
            "and run install.sh again"
        )


def check_caddy_user(name: str) -> None:
    if not _exists("passwd", name):
        raise InstallError(
            f"there is no account {name!r} for Caddy; give the account Caddy runs as "
            "with --caddy-user"
        )


def _add_account(name: str, *options: str) -> None:
    """A system account with its own group and no login."""
    run(["useradd", "--system", "--user-group", "--shell", "/usr/sbin/nologin", *options, name])


def ensure_accounts(caddy_user: str) -> list[str]:
    check_caddy_user(caddy_user)
    changes = []
    if not _exists("passwd", APP_USER):
        _add_account(APP_USER, "--home-dir", str(STATE))
        changes.append(f"user {APP_USER}")
    if not _exists("passwd", PROXY_USER):
        _add_account(PROXY_USER, "--no-create-home")
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
    if own_directory(STATE, app.pw_uid, app.pw_gid, STATE_MODE, accept=STATE_MODES):
        changes.append(str(STATE))
    ETC.mkdir(parents=True, exist_ok=True)
    return changes


def ensure_runtime_dir() -> list[str]:
    """The socket's directory: made at boot by tmpfiles.d, and made or repaired here."""
    changes = []
    if write_if_changed(TMPFILES, tmpfiles(), 0o644):
        changes.append(str(TMPFILES))
    uid, gid = pwd.getpwnam(APP_USER).pw_uid, _gid(WEB_GROUP)
    if own_directory(RUN, uid, gid, RUN_MODE):
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


def _subordinate_start(path: Path) -> int | None:
    lines = path.read_text().splitlines() if path.exists() else []
    return subordinate_start(lines, APP_USER, SUBORDINATE_COUNT)


def new_ranges_needed() -> bool:
    """Whether rcman lacks the range of subordinate ids in either file."""
    return any(_subordinate_start(path) is not None for path in SUBORDINATE_FILES)


def running_containers(uid: int) -> list[str]:
    """The names of the containers rcman's Podman has running or paused. Where its socket is
    not there, nothing can be: its user manager runs every container."""
    if not podman_socket_exists(uid):
        return []
    listed = run(
        as_rcman_podman(
            uid,
            "ps",
            "--filter",
            "status=running",
            "--filter",
            "status=paused",
            "--format",
            "{{.Names}}",
        )  # fmt: skip
    )
    return listed.stdout.split()


def check_containers_stopped_before_new_ranges() -> None:
    """Refuse, before the range is added, to add a range of subordinate ids while a container
    runs: Podman takes the range only through ``podman system migrate``, which stops every
    running container, and so every workspace's server. What came before in this run (the
    settings, the packages, the accounts) is done and stays; the next run asks again, since no
    range was added. rcman's interface cannot stop a container, and rcman starts a stopped one
    again when it starts, so the way on is to stop rcman first, then the containers."""
    if not new_ranges_needed():
        return
    uid = app_uid()
    names = running_containers(uid)
    if names:
        stop_all = shlex.join(as_rcman(uid, "podman", "stop", "--all"))
        raise InstallError(
            "rcman's Podman has containers running (" + ", ".join(names) + "), and rcman's "
            "account is to be given a range of subordinate ids, which Podman takes only by "
            "`podman system migrate`, and that stops every running container. So far the "
            "settings, the packages and the accounts are done, and it is safe to run "
            "install.sh again. To go on, stop rcman, so that it does not start the "
            "containers again, and then the containers, as root:\n"
            "  systemctl stop rcman\n"
            f"  {stop_all}\n"
            "then run install.sh again; it starts rcman and the workspaces again when it "
            "ends. Or remove rcman and install it again."
        )


def ensure_subordinate_ids() -> list[str]:
    """rcman's subordinate uids and gids: SUBORDINATE_COUNT of each, a range of its own."""
    changes = []
    for path, option in zip(SUBORDINATE_FILES, ("--add-subuids", "--add-subgids"), strict=True):
        start = _subordinate_start(path)
        if start is None:
            continue
        run(["usermod", option, f"{start}-{start + SUBORDINATE_COUNT - 1}", APP_USER])
        changes.append(f"{path} {APP_USER}")
    return changes


def app_uid() -> int:
    return pwd.getpwnam(APP_USER).pw_uid


def podman_socket(uid: int) -> Path:
    return Path(f"/run/user/{uid}/podman/podman.sock")


def podman_socket_exists(uid: int) -> bool:
    """Whether rcman's Podman socket is there, as rcman sees it: its runtime folder is its
    own, and root looks at nothing in it."""
    return rcman_has("-S", str(podman_socket(uid)))


def as_rcman(uid: int, *argv: str) -> list[str]:
    """``argv`` run as rcman, in its home and its user manager's runtime folder, with nothing
    of root's environment. Whatever the installer does inside rcman's home it does like this."""
    return as_account(
        APP_USER,
        ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", f"HOME={STATE}",
         f"XDG_RUNTIME_DIR=/run/user/{uid}", *argv],
    )  # fmt: skip


def as_rcman_podman(uid: int, *args: str) -> list[str]:
    """``podman --remote`` as rcman, through its own user manager's socket."""
    return as_rcman(uid, "podman", "--remote", "--url", f"unix://{podman_socket(uid)}", *args)


def as_rcman_systemctl(uid: int, *args: str) -> list[str]:
    """``systemctl --user`` as rcman, in its own user manager."""
    return as_rcman(uid, "systemctl", "--user", *args)


def rcman_do(*argv: str, **kw) -> subprocess.CompletedProcess:
    """``argv`` run as rcman, in a folder that is no one's; ``kw`` as for ``run``."""
    kw.setdefault("cwd", "/")
    return run(as_rcman(app_uid(), *argv), **kw)


def rcman_has(*test: str) -> bool:
    """Whether ``test`` (the arguments of test(1)) holds, as rcman sees it."""
    return rcman_do("test", *test, check=False).returncode == 0


def rcman_link(path: Path) -> str | None:
    """Where the link ``path`` leads, as rcman reads it; None when it is not one."""
    done = rcman_do("readlink", "--", str(path), check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def rcman_set_link(directory: Path, name: str, target: str) -> None:
    """The link ``name`` in ``directory`` made to lead to ``target`` in one step, by rcman."""
    new = directory / f".{name}.new"
    rcman_do("ln", "-sfT", "--", target, str(new))
    rcman_do("mv", "-T", "--", str(new), str(directory / name))


# Run as rcman: the text on standard input becomes the file named, with the mode given (in
# octal), by a rename of a file beside it, its folders made. Standard input is read by its
# descriptor: a pipe of root's cannot be opened again by its path (/dev/stdin) once the uid has
# changed.
WRITE_SCRIPT = (
    "import os, sys\n"
    "path, mode = sys.argv[1], int(sys.argv[2], 8)\n"
    "folder, name = os.path.split(path)\n"
    "os.makedirs(folder, exist_ok=True)\n"
    "data = sys.stdin.buffer.read()\n"
    "new = os.path.join(folder, '.' + name + '.new')\n"
    "try:\n"
    "    try:\n"
    "        os.unlink(new)\n"
    "    except FileNotFoundError:\n"
    "        pass\n"
    "    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)\n"
    "    with os.fdopen(fd, 'wb') as sink:\n"
    "        os.fchmod(fd, mode)\n"
    "        sink.write(data)\n"
    "    os.replace(new, path)\n"
    "finally:\n"
    "    try:\n"
    "        os.unlink(new)\n"
    "    except FileNotFoundError:\n"
    "        pass\n"
)


def rcman_write(path: Path, text: str, mode: int) -> bool:
    """The file ``path`` holding ``text`` with ``mode``, written by rcman, which also makes the
    folders it is in; True when anything changed. Root reads and writes none of it."""
    wanted = f"{mode:o}"
    held = rcman_do("cat", "--", str(path), check=False)
    found = rcman_do("stat", "-c", "%a", "--", str(path), check=False)
    if held.returncode == 0 and held.stdout == text and found.stdout.strip() == wanted:
        return False
    rcman_do("/usr/bin/python3", "-I", "-c", WRITE_SCRIPT, str(path), wanted, input=text)
    return True


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
    if subordinate_changed:
        # Podman takes new subordinate ids only once told to, locally. That stops every running
        # container: main has refused to get here with one running
        # (check_containers_stopped_before_new_ranges).
        run(as_rcman(uid, "podman", "system", "migrate"))
        subprocess.run([*manager, "stop", "podman.service"], capture_output=True)
    enabled = subprocess.run([*manager, "is-enabled", "podman.socket"], capture_output=True)
    if enabled.returncode != 0:
        run([*manager, "enable", "--now", "podman.socket"])
        changes.append("podman.socket")
    else:
        run([*manager, "start", "podman.socket"])
    deadline = time.monotonic() + SOCKET_WITHIN
    while not podman_socket_exists(uid):
        if time.monotonic() > deadline:
            raise InstallError(f"rcman's Podman socket {podman_socket(uid)} did not appear")
        time.sleep(0.5)
    return changes


def ensure_oci_runtime() -> list[str]:
    """Fail unless rcman's Podman reports OCI_RUNTIME as its runtime: workspaces and builds are
    tested with it, and a host that has another one would run them differently."""
    reported = run(
        as_rcman_podman(app_uid(), "info", "--format", "{{.Host.OCIRuntime.Name}}")
    ).stdout.strip()
    if reported != OCI_RUNTIME:
        raise InstallError(
            f"rcman's Podman reports {reported!r} as its OCI runtime, and rcman is tested with "
            f"{OCI_RUNTIME}. Install the package {OCI_RUNTIME} and make it Podman's runtime "
            f'(runtime = "{OCI_RUNTIME}" in the [engine] table of containers.conf), then run '
            "install.sh again."
        )
    return []


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


# The drop-ins' files: the first in systemd's folder, named after the account's id; the second
# in the account's home.
SLICE_DROP_IN_FILE = Path("user-{uid}.slice.d") / "rcman.conf"
MANAGER_DROP_IN_FILE = Path(".config/systemd/user.conf.d/rcman.conf")
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

    The user manager's drop-in is in rcman's home, where rcman can put a link, so rcman reads
    and writes it, and makes its folders: root follows no link there."""
    uid = app_uid()
    app = pwd.getpwnam(APP_USER)
    slice_file = SYSTEMD / str(SLICE_DROP_IN_FILE).format(uid=uid)
    user_file = Path(app.pw_dir) / MANAGER_DROP_IN_FILE
    if (problem := limits_problem(uid)) is not None:
        warn(problem)
    changes = []
    if write_if_changed(slice_file, USER_SLICE_DROP_IN, 0o644):
        changes.append(str(slice_file))  # read by the daemon-reload that starts the services
    if rcman_write(user_file, USER_MANAGER_DROP_IN, 0o644):
        changes.append(str(user_file))
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


def _pinned(table: dict, version: str, arch: str, tool: str) -> str:
    """The checksum the installer pins for ``tool`` ``version`` on ``arch``."""
    try:
        return table[(version, arch)]
    except KeyError:
        raise InstallError(
            f"{tool} {version} has no checksum pinned in the installer for {arch}; "
            "pin one with the version"
        ) from None


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
        expected = _pinned(NODE_SHA256, version, arch, "Node.js")
        _unpack(_verified(f"{base}/{name}", expected), node_bin(toolchain).parent, "r:xz", strip=1)
        changes.append(f"node {version}")
    if not uv_binary(toolchain).exists():
        triple = {"x64": "x86_64", "arm64": "aarch64"}[arch] + "-unknown-linux-gnu"
        url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{triple}.tar.gz"
        expected = _pinned(UV_SHA256, UV_VERSION, arch, "uv")
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
        expected = _pinned(OAUTH2_PROXY_SHA256, OAUTH2_PROXY_VERSION, arch, "oauth2-proxy")
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


def copy_built_tree(source: Path, dest: Path) -> None:
    """Copy the built web interface into the release. It was built by code the clone's owner
    runs (npm), and the copy is root's, so a link in it would be followed out of the tree:
    one is refused, before anything is copied."""
    links = [str(p) for p in source.rglob("*") if p.is_symlink()]
    if links:
        raise InstallError(f"the built web interface holds links, which are not copied: {links}")
    shutil.copytree(source, dest, symlinks=True)


def mark_complete(release: Path, commit: str) -> None:
    """Mark ``release`` fully built, with its commit: readable by everyone whatever root's
    umask, as rcman reads which release it runs from it."""
    mark = release / COMPLETE
    mark.write_text(commit + "\n", encoding="utf-8")
    os.chmod(mark, 0o644)


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
        copy_built_tree(frontend / "dist", work / "frontend" / "dist")
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
        mark_complete(work, commit)
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


def tmux_build_argv(tarball: Path) -> list[str]:
    """podman's arguments that build tmux statically from ``tarball`` and write the program on
    standard output. The container's output is not logged (``--log-driver=none``): with a log
    driver of the journal, the program's bytes, which are not text, would be written to the
    system journal, and the build's output with them."""
    return [
        "run", "--rm", "--log-driver=none", f"--volume={tarball}:/tmux.tar.gz:ro",
        TMUX_BUILD_REFERENCE, "sh", "-c", TMUX_BUILD,
    ]  # fmt: skip


def _install_file(source: Path, dest: Path, mode: int) -> None:
    tmp = dest.with_name(f".{dest.name}.new")
    shutil.copyfile(source, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def ensure_tools(
    tools: Path = TOOLS,
    conf: Path | None = None,
    podman: Path | None = None,
    fragments: Path | None = None,
) -> list[str]:
    """The static tools every workspace mounts read-only: tmux, built here from its pinned
    source, busybox and a shell that is busybox, the terminal descriptions, and tmux's
    configuration ``conf``, by default the release's, the Podman tools of ``podman``, by
    default the release's (ensure_podman_tools), and the instructions for Claude made of the
    fragments in ``fragments``, by default the release's (ensure_instructions). Root's, and
    readable by everyone: they
    are in a folder only root can write, and the program is built by rcman's Podman and
    comes out on standard output, so nothing here is made in a folder rcman can write."""
    changes = []
    (tools / "bin").mkdir(parents=True, exist_ok=True)
    os.chmod(tools, 0o755)
    os.chmod(tools / "bin", 0o755)
    built = f"{TMUX_VERSION} {TMUX_SHA256}\n"
    stamp = tools / ".tmux-version"
    if not (tools / "bin" / "tmux").exists() or not stamp.exists() or stamp.read_text() != built:
        program = run(as_rcman_podman(app_uid(), *tmux_build_argv(tmux_tarball())), text=False)
        staged = tools / "bin" / ".tmux.new"
        try:
            reported = TMUX_SHA256_LINE.findall(program.stderr or b"")
            if not reported or reported[-1].decode() != hashlib.sha256(program.stdout).hexdigest():
                raise InstallError(
                    "tmux did not arrive whole: the sha256 the container reports for it is "
                    "not that of the bytes received"
                )
            staged.write_bytes(program.stdout)
            if not is_static(staged):
                raise InstallError("tmux was not built static")
            os.chmod(staged, 0o755)
            os.replace(staged, tools / "bin" / "tmux")
        finally:
            staged.unlink(missing_ok=True)
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
    changes += ensure_podman_tools(tools, podman or CURRENT / "backend" / "rcman" / PODMAN_TOOLS)
    changes += ensure_instructions(
        tools, fragments or CURRENT / "backend" / "rcman" / instructions.FOLDER
    )
    return changes


def ensure_instructions(tools: Path, fragments: Path) -> list[str]:
    """The instructions for Claude (helpers/instructions.py): each of its files made of the
    fragments in ``fragments``, in the tools' folder of them, root's and readable by everyone,
    and no other file there. A file whose text changed is renamed over the old one, so that a
    container bound to it keeps the old one, whole, until its next start."""
    folder = tools / instructions.FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o755)
    changes = []
    for name in instructions.FILES:
        if write_if_changed(folder / name, instructions.compose(fragments, name), 0o644):
            changes.append(f"instructions {name}")
    for found in sorted(folder.iterdir()):
        if found.name not in instructions.FILES and not found.is_dir():
            found.unlink()  # a file of another release, or a copy a killed run left staged
            changes.append(f"instructions {found.name}")
    return changes


def ensure_podman_tools(tools: Path, source: Path) -> list[str]:
    """The podman and docker wrappers, their configuration and the script they source from
    ``source``, and the workspaces' seccomp profile, in the tools, root's and readable by
    everyone."""
    if not PODMAN_SECCOMP.is_file():
        raise InstallError(f"{PODMAN_SECCOMP}, Podman's seccomp profile, is not there")
    changes = []
    folder = tools / PODMAN_TOOLS
    (folder / "bin").mkdir(parents=True, exist_ok=True)
    for path in (tools, folder, folder / "bin"):
        os.chmod(path, 0o755)
    # What the wrapper sources is there before a wrapper that sources it.
    sourced = (source / PODMAN_PUBLISH).read_text(encoding="utf-8")
    if write_if_changed(folder / PODMAN_PUBLISH, sourced, 0o644):
        changes.append(f"podman {PODMAN_PUBLISH}")
    wrapper = (source / "podman").read_text(encoding="utf-8")
    if write_if_changed(folder / "bin" / "podman", wrapper, 0o755):
        changes.append("podman")
    docker = folder / "bin" / "docker"
    if not docker.is_symlink() or os.readlink(docker) != "podman":
        docker.unlink(missing_ok=True)
        os.symlink("podman", docker)
        changes.append("docker")
    for name in PODMAN_CONFIGURATION:
        if write_if_changed(folder / name, (source / name).read_text(encoding="utf-8"), 0o644):
            changes.append(f"podman {name}")
    profile = seccomp.workspace_profile(PODMAN_SECCOMP.read_text(encoding="utf-8"))
    if write_if_changed(tools / WORKSPACE_SECCOMP, profile, 0o644):
        changes.append(WORKSPACE_SECCOMP)
    return changes


def workspace_image() -> str:
    """The default workspace image's name, tagged after its build file's content, so a
    changed build file makes a new image and the old one stays the workspaces' it is."""
    digest = hashlib.sha256(CONTAINERFILE.read_bytes()).hexdigest()[:12]
    return f"localhost/rcman-workspace:{digest}"


def image_context(context: Path) -> None:
    """The default image's build context in the folder ``context``: its build file, readable
    by everyone, as rcman's Podman, which builds it, reads it."""
    shutil.copy(CONTAINERFILE, context / "Containerfile")
    os.chmod(context, 0o755)
    os.chmod(context / "Containerfile", 0o644)


def ensure_image() -> list[str]:
    """The default workspace image, built with rcman's Podman unless it is there."""
    uid, image = app_uid(), workspace_image()
    if subprocess.run(as_rcman_podman(uid, "image", "exists", image)).returncode == 0:
        return []
    context = Path(tempfile.mkdtemp(prefix="rcman-image."))
    try:
        image_context(context)
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
    the default image; it needs the network for the same reason. Older ones stay: the running
    service is configured with one of them until it restarts, and
    ``prune_devcontainer_builder_images`` removes them after that."""
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
    return changes


def found_out(settings: dict[str, str]) -> dict[str, str]:
    """What the installer finds out and renders along with the settings: rcman's uid, the
    names of the default image and of the devcontainer builder image, and whether upgrading
    from the web interface is on, as rcman's setting RCMAN_WEB_UPGRADE has it."""
    return {
        "APP_UID": str(app_uid()),
        "WORKSPACE_IMAGE": workspace_image(),
        "BUILDER_IMAGE": devcontainer_builder_image(),
        "WEB_UPGRADE_ENABLED": "true" if web_upgrade_on(settings) else "false",
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
) -> list[str]:
    """The Claude Code store: ``versions/<version>`` for the tested version, downloaded from
    ``download`` and checked against its release's manifest (helpers/claude_release.py);
    ``current`` naming a version, which is set only where there is none, since rcman switches
    it to the version an administrator approves; and ``bin/claude`` leading to ``current``.
    Readable by everyone.

    The store is rcman's, which writes it at run time and can put a link anywhere in it, so
    rcman makes all of it: root downloads and checks the program in a folder of its own, and
    rcman copies the checked file in and sets the links."""
    version = version or claude_version()
    versions, bin_dir = store / "versions", store / "bin"
    target = versions / version
    changes = []
    rcman_do("mkdir", "-p", "-m", "755", "--", str(versions), str(bin_dir))
    if not rcman_has("-e", str(target)):
        with tempfile.TemporaryDirectory(prefix="rcman-claude.") as staging:
            os.chmod(staging, 0o755)  # rcman reads the file from here
            staged = Path(staging) / version
            try:
                claude_release.fetch(download, version, staged)
            except claude_release.ReleaseError as error:
                raise _release_failed(error, version) from None
            partial = versions / f".{version}.new"
            try:
                rcman_do("install", "-m", "755", "--", str(staged), str(partial))
                rcman_do("mv", "-T", "--", str(partial), str(target))
            finally:
                rcman_do("rm", "-f", "--", str(partial), check=False)
        changes.append(f"claude {version}")
    if not rcman_has("-e", str(store / "current")):  # none, or one naming a version not there
        rcman_set_link(store, "current", f"versions/{version}")
    if rcman_link(bin_dir / "claude") != "../current":
        rcman_set_link(bin_dir, "claude", "../current")
    rcman_do("chmod", "755", "--", str(store), str(versions), str(bin_dir))
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
    if not rcman_has("-e", str(allowed)):
        # rcman writes it as it starts; oauth2-proxy needs it to exist. It is made by rcman,
        # in its own group, which oauth2-proxy's account is in to read it.
        rcman_do("install", "-m", "640", "/dev/null", str(allowed))
        changes.append(str(allowed))
    return changes


def start_services(restart: bool) -> None:
    run(["systemctl", "daemon-reload"])
    for unit in UNITS:
        run(["systemctl", "enable", unit])
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
        if restart or not active:
            run(["systemctl", "restart", unit])


# --- upgrading from the web interface ---------------------------------------------------


def web_upgrade_on(settings: dict[str, str]) -> bool:
    return settings.get("WEB_UPGRADE") == "yes"


def ls_remote(owner: str) -> None:
    """Refuse unless the clone's origin answers ``git ls-remote origin main`` as ``owner``, as
    the upgrade service will pull it: with nothing of the environment but git's settings that
    ask nothing (upgrade.GIT_ENVIRONMENT), no terminal and no input."""
    home = pwd.getpwnam(owner).pw_dir
    asked = ["git", "-C", str(REPO), "ls-remote", "origin", "main"]
    argv = [
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", f"HOME={home}",
        *(f"{key}={value}" for key, value in upgrade.GIT_ENVIRONMENT.items()), *asked,
    ]  # fmt: skip
    why = None
    try:
        done = run(argv, user=owner, check=False, cwd="/", stdin=subprocess.DEVNULL,
                   start_new_session=True, timeout=LS_REMOTE_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        why = f"no answer within {LS_REMOTE_TIMEOUT} seconds"
    else:
        if done.returncode != 0:
            why = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
        elif not re.search(r"\trefs/heads/main$", done.stdout, re.MULTILINE):
            why = "origin has no main"
    if why:
        raise InstallError(
            f"upgrading from the web interface is not turned on: `{' '.join(asked)}` as {owner}, "
            f"with no terminal and git asking nothing, failed: {why}. Give {owner} credentials "
            "for the clone's origin that need no answer (a credential helper or an SSH key "
            "without a passphrase), or run install.sh with --web-upgrade no"
        )


def check_web_upgrade(settings: dict[str, str], owner: str) -> None:
    """When upgrading from the web interface is on, refuse what it must not run with: a clone
    or an owner the wrapper's checks refuse (upgrade.check_clone), a clone whose path a unit
    cannot hold as it is, or an origin the owner cannot reach without being asked."""
    if not web_upgrade_on(settings):
        return
    try:
        upgrade.check_clone(
            REPO, os.environ.get("SUDO_USER"), (APP_USER, PROXY_USER, settings["CADDY_USER"])
        )
    except upgrade.Refused as refused:
        raise InstallError(
            f"upgrading from the web interface is not turned on: {refused}"
        ) from None
    if not UNIT_PATH.fullmatch(str(REPO)):
        raise InstallError(
            f"upgrading from the web interface is not turned on: the clone's path {REPO} holds "
            "characters a systemd unit would change; clone it to a path of letters, digits, "
            "and . _ / + - only"
        )
    ls_remote(owner)


def install_program(source: Path, dest: Path, mode: int) -> bool:
    """``source`` installed at ``dest`` with ``mode``, in one rename of a whole file beside it,
    so a run of the old one keeps it and a link where it goes is replaced, not followed; True
    when anything changed. ``dest`` is in a folder of root's."""
    data = source.read_bytes()
    if (
        not dest.is_symlink() and dest.is_file() and dest.read_bytes() == data
        and stat.S_IMODE(dest.stat().st_mode) == mode
    ):  # fmt: skip
        return False
    dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, new = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(new, dest)
    except BaseException:
        Path(new).unlink(missing_ok=True)
        raise
    return True


def ensure_upgrade_dir(path: Path, uid: int, gid: int) -> bool:
    """The status folder: ``uid``'s (root's) and ``gid``'s (rcman's group, which reads it),
    UPGRADE_DIR_MODE; True when it was made or changed. It is in a folder of root's; a link in
    its place is refused."""
    made = False
    try:
        path.mkdir(mode=UPGRADE_DIR_MODE)
        made = True
    except FileExistsError:
        pass
    found = os.lstat(path)
    if not stat.S_ISDIR(found.st_mode):
        raise InstallError(f"{path} is a link or not a folder; remove it and run install.sh again")
    if (found.st_uid, found.st_gid) != (uid, gid):
        os.chown(path, uid, gid)
        made = True
    if stat.S_IMODE(found.st_mode) != UPGRADE_DIR_MODE:
        os.chmod(path, UPGRADE_DIR_MODE)
        made = True
    return made


def ensure_web_upgrade(settings: dict[str, str]) -> list[str]:
    """On: the wrapper, the status folder and both units, the path unit enabled and started;
    the service is only ever started by it. Off: the units disabled and removed with the
    wrapper, where they are there. ``settings`` with what ``found_out`` returns."""
    changes = []
    units = [SYSTEMD / unit for unit in UPGRADE_UNITS]
    if not web_upgrade_on(settings):
        there = [path for path in units if path.exists()]
        if (there or UPGRADE_WRAPPER.exists()) and os.environ.get(upgrade.SERVICE_MARK):
            raise InstallError(
                "upgrading from the web interface cannot be turned off by an upgrade from the "
                "web interface, which runs in the service it would remove; run install.sh with "
                "--web-upgrade no from a shell"
            )
        if there:
            run(["systemctl", "disable", "--now", *(path.name for path in there)])
        for path in there:
            path.unlink()
            changes.append(str(path))
        if UPGRADE_WRAPPER.exists() or UPGRADE_WRAPPER.is_symlink():
            UPGRADE_WRAPPER.unlink()
            changes.append(str(UPGRADE_WRAPPER))
        if there:
            run(["systemctl", "daemon-reload"])
        return changes
    if install_program(UPGRADE_SOURCE, UPGRADE_WRAPPER, 0o755):
        changes.append(str(UPGRADE_WRAPPER))
    if ensure_upgrade_dir(UPGRADE_DIR, 0, _gid(APP_USER)):
        changes.append(str(UPGRADE_DIR))
    written = [path for path in units if write_if_changed(path, render(path.name, settings), 0o644)]
    changes += [str(path) for path in written]
    if written:
        run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", UPGRADE_UNITS[0]])
    if units[0] in written:  # the path unit watches as its file now says; the service is left
        run(["systemctl", "restart", UPGRADE_UNITS[0]])
    return changes


def rcman_admin(*args: str) -> list[str]:
    """rcman-admin with ``args``, run as rcman with its settings, in rcman's clean environment
    (run it from a folder that is no one's: ``cwd="/"``)."""
    return as_rcman(
        app_uid(), str(CURRENT / ".venv" / "bin" / "rcman-admin"),
        "--env-file", str(ETC / "rcman.env"), *args,
    )  # fmt: skip


def admin_argv(email: str) -> list[str]:
    """rcman-admin adding ``email`` as an administrator."""
    return rcman_admin("add", "--admin", "--", email)


def ensure_admin(settings: dict[str, str]) -> list[str]:
    """The first administrator, unless they are there already."""
    listing = rcman_admin("list")
    email = settings["ADMIN_EMAIL"].lower()
    listed = run(listing, cwd="/").stdout
    if email in [line.split()[0] for line in listed.splitlines() if line.strip()]:
        return []
    run(admin_argv(settings["ADMIN_EMAIL"]), cwd="/")
    return [f"administrator {email}"]


# Run as rcman: GET the path on rcman's socket and print the status. The socket is in a folder
# rcman owns, where a link could lead a connection of root's elsewhere.
HEALTH_SCRIPT = (
    "import http.client, socket, sys\n"
    "class Connection(http.client.HTTPConnection):\n"
    "    def connect(self):\n"
    "        self.sock = socket.socket(socket.AF_UNIX)\n"
    "        self.sock.settimeout(5)\n"
    "        self.sock.connect(sys.argv[1])\n"
    "connection = Connection('rcman')\n"
    "connection.request('GET', sys.argv[2])\n"
    "print(connection.getresponse().status)\n"
)


def _status_over_socket(path: Path, url: str) -> int:
    done = rcman_do("/usr/bin/python3", "-I", "-c", HEALTH_SCRIPT, str(path), url, check=False)
    if done.returncode != 0:
        raise OSError(f"no answer on {path}")
    return int(done.stdout.strip() or 0)


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
        "forwarding server": lambda: _status_over_socket(FORWARD_SOCKET, "/.rcman/health"),
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


def summary(release: str, changes: list[str], notices: list[str]) -> str:
    """The line the installer prints for programs to read, the upgrade wrapper among them."""
    return json.dumps({"release": release, "changed": changes, "notices": notices})


def global_block(ask_port: str) -> str:
    """What Caddy's global options must hold for certificates of forwarded ports: they are got
    on demand, and only once rcman's forwarding server, asked on the ask site, allows one."""
    return f"on_demand_tls {{\n\task {ask_url(ask_port)}\n}}"


# Where Ubuntu's Caddy reads its configuration: the global options rcman's block goes in.
CADDYFILE = Path("/etc/caddy/Caddyfile")
ADAPT_WITHIN = 60.0


def ask_url(ask_port: str) -> str:
    """Where Caddy asks rcman whether it may get a certificate for a host on demand."""
    return f"http://127.0.0.1:{ask_port}{ASK_PATH}"


def on_demand_asks(config: object) -> list[str]:
    """The addresses Caddy's JSON configuration, as ``caddy adapt`` gives it, asks before it gets
    a certificate on demand: ``ask`` (Caddy 2.6, Ubuntu's) or an ``http`` permission's
    ``endpoint`` (later Caddy)."""
    found: object = config
    for key in ("apps", "tls", "automation", "on_demand"):
        found = found.get(key) if isinstance(found, dict) else None
    if not isinstance(found, dict):
        return []
    asks = [found["ask"]] if isinstance(found.get("ask"), str) else []
    permission = found.get("permission")
    if isinstance(permission, dict) and permission.get("module") == "http":
        if isinstance(permission.get("endpoint"), str):
            asks.append(permission["endpoint"])
    return asks


def caddy_asks_rcman(caddy_user: str, ask_port: str, caddyfile: Path = CADDYFILE) -> bool | None:
    """Whether the Caddyfile's global options ask rcman before an on-demand certificate, as
    ``caddy adapt`` reads the file, as Caddy's account, which works with the admin API off and
    starts nothing; None where it could not be read so."""
    argv = ["caddy", "adapt", "--adapter", "caddyfile", "--config", str(caddyfile)]
    # As everywhere ``run`` is given a timeout: one that passes kills runuser, which may leave
    # caddy itself running a moment longer.
    try:
        done = run(argv, user=caddy_user, check=False, timeout=ADAPT_WITHIN)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        config = json.loads(done.stdout)
    except ValueError:
        return None
    return ask_url(ask_port) in on_demand_asks(config)


def caddy_ask_warning(found: bool | None, ask_port: str) -> str | None:
    """What to warn of after ``caddy_asks_rcman`` found ``found``: nothing where the global
    options ask rcman."""
    if found:
        return None
    if found is None:
        why = (
            f"rcman could not read {CADDYFILE} with `caddy adapt` to check that its global "
            "options ask rcman before Caddy gets a certificate on demand."
        )
    else:
        why = (
            f"The global options of {CADDYFILE} do not ask rcman before Caddy gets a "
            "certificate on demand."
        )
    return (
        f"{why} Without that, Caddy gets a certificate for any name under *.ws.<DOMAIN> that "
        "anyone asks it for, each one published, and each one counted against your certificate "
        "authority's limits. Put this in the global options, once:\n" + global_block(ask_port)
    )


def caddy_notices(changes: list[str], caddy_user: str, ask_port: str) -> list[str]:
    """What to tell the administrator to do to Caddy after this run."""
    notices = []
    if str(CADDY_SNIPPET) in changes:
        notices.append(
            f"The Caddy site block changed ({CADDY_SNIPPET}). If your Caddyfile imports "
            "it, nothing more is needed there; if it holds a copy, copy it again. "
            "Then restart Caddy."
        )
        notices.append(
            "Caddy's global options, the block at the top of your Caddyfile, must hold this "
            "once, for the certificates of forwarded ports; put it there if it is not, or "
            "change its port if it is another:\n" + global_block(ask_port)
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
    # Every command run as another account (rcman's Podman, its user manager) inherits this
    # folder, and rcman cannot enter the installing user's home (0750): ``runuser`` then fails
    # with "cannot chdir". Every path the installer uses is absolute, so stand in a folder that
    # is no one's.
    os.chdir("/")
    try:
        if os.geteuid() != 0:
            raise InstallError("run install.sh with sudo")
        check_kernel(platform.release())
        check_lock()
        if args.rollback:
            check_rollback(ROOT)
            rollback(ROOT)
            start_services(restart=True)
            print(f"Rolled back to {os.readlink(CURRENT)}.")
            return 0
        try:  # the clone's, which must be the account that ran install.sh where one did
            owner = upgrade.clone_owner(REPO, os.environ.get("SUDO_USER"))
        except upgrade.Refused as refused:
            raise InstallError(str(refused)) from None
        ask = _ask_terminal if sys.stdin.isatty() else None
        settings = gather(SETTINGS_FILE, dict(os.environ), vars(args), ask)
        check_web_upgrade(settings, owner)  # before it is saved: a refused yes is not kept
        changes = []
        if save_settings(SETTINGS_FILE, settings):
            changes.append(str(SETTINGS_FILE))
        check_caddy_user(settings["CADDY_USER"])
        changes += ensure_packages()
        changes += ensure_accounts(settings["CADDY_USER"])
        check_containers_stopped_before_new_ranges()
        subordinate = ensure_subordinate_ids()
        changes += subordinate
        changes += ensure_podman(subordinate_changed=bool(subordinate))
        changes += ensure_oci_runtime()
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
        changes += ensure_claude_store(CLAUDE_STORE, settings["CLAUDE_DOWNLOAD"])
        settings = {**settings, **found_out(settings)}
        changes += configure(settings)
        changes += ensure_web_upgrade(settings)
        changes += link_commands()
        added = ensure_admin(settings)
        start_services(restart=bool(changes))
        changes += added
        health(settings)
        # only now: the service runs with the new builder image's name, and an older one
        # is no one's but a build still running from it
        prune_devcontainer_builder_images(app_uid(), devcontainer_builder_image())
        notices = caddy_notices(changes, settings["CADDY_USER"], settings["ASK_PORT"])
        warnings = [CADDY_ADMIN_WARNING] if caddy_admin_exposed() else []
        asks = caddy_asks_rcman(settings["CADDY_USER"], settings["ASK_PORT"])
        unasked = caddy_ask_warning(asks, settings["ASK_PORT"])
        warnings += [unasked] if unasked else []
        print(summary(release, changes, notices + warnings))
        print(f"\nAdd this to your Caddyfile ({CADDY_SNIPPET}):\n")
        print(CADDY_SNIPPET.read_text(encoding="utf-8"))
        for notice in notices:
            print(notice)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0
    except InstallError as error:
        print(f"install.sh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
