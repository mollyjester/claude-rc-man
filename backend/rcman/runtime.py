"""Where workspaces run: rootless Podman, or folders on the host for the tests.

A workspace is one container, named after its row (``container_name``), with a
volume of its own and the person's uid range. Each person has one Claude sign-in,
the file CREDENTIALS in a volume of theirs (``account_volume``), which every
workspace of theirs binds as its own, and a short-lived sign-in container
(``start_sign_in``) writes. rcman reaches Podman only through
``podman --remote`` against its own user manager's socket, with a fixed argv
and an allowlisted environment, never through a shell. ``HostRuntime`` keeps
the same interface with a folder and a tmux socket per workspace, for the tests
and ``make e2e``; tests/runtime_contract.py holds both to the same cases.

A repository's devcontainer.json may ask for an image to be built (``build_image``): by the
devcontainer CLI, in a *builder* container of its own that rcman starts for the one build, in a
uid range no person has, and removes after it (builder.py); the image is copied out to rcman's
own storage and named for the workspace (``built_name``).

Limits on what a person's workspaces, and each workspace, may use (limits.py) are properties of
systemd slices in rcman's user manager, set with ``systemctl --user`` (``apply_limits``): a
workspace's container is made in its workspace's slice, the builder in the slice of the person
who asked for the build, and nothing else in any slice, so that signing out and the housekeeping
are never held up by a cap that is full.

A candidate Claude Code version is tried by the *canary* (``run_canary``): a throwaway container
in the builder's uid range with no network, which runs the checks of helpers/claude_canary.py
against the candidate and reports the ones it fails.

Python standard library, the helpers and the backend's own channel and builder only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import io
import json
import logging
import os
import pwd
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path

import helpers
from helpers import claude_checks, rules
from rcman import builder, limits
from rcman.channel import Channel, Result
from rcman.limits import Caps, Limits, Usage

log = logging.getLogger("rcman.runtime")

# Where things are inside a workspace container.
TOOLS_MOUNT = "/opt/rcman"  # the static tools, read-only: bin/tmux, bin/busybox, terminfo
CLAUDE_MOUNT = "/opt/claude"  # the Claude Code store, read-only
# The tools' podman and docker (backend/rcman/podman), on every workspace's PATH right after
# Claude's folder; the folder holds nothing else (installer/install.py PODMAN_COMMANDS).
PODMAN_BIN = f"{TOOLS_MOUNT}/podman/bin"
RUN_MOUNT = "/run/rcman"  # a tmpfs: the tmux socket and the channel's files
# A tmpfs of the container's root alone, empty at every start: the record of root's Podman nest
# (backend/rcman/podman/podman STATE), never the user's even where the ids are swapped.
ROOT_PODMAN_STATE = "/run/rcman-podman"
VOLUME_MOUNT = "/rcman"  # the workspace's volume
# The workspace's temporary folder: an anonymous volume of its container, on rcman's disk and not
# on the container's overlay root, so that overlay storage a Podman in the workspace puts there is
# native; the container's root's, and everyone's to write in, whoever Podman made it. An image
# whose /tmp is a link or not a folder is refused (TMP_REFUSED): what is changed there would
# reach where a link leads.
TMP_MOUNT = "/tmp"
TMP_REFUSED = "the image's {path} is a link or not a folder."
# What the check of TMP_MOUNT answers when it is a link or not a folder; Podman's own exec fails
# with codes of its own (125 to 127).
TMP_CHECK_REFUSED = 1
# The folders of a workspace's volume: the one its server works in, Claude's
# configuration (CLAUDE_CONFIG_DIR) and the home of the programs rcman runs there.
WORK, CONFIG, HOME = "work", "claude", "home"
# Claude Code's credentials file, in its configuration folder: a person's sign-in.
CREDENTIALS = ".credentials.json"
SIGNED_IN, NOT_SIGNED_IN_EMPTY, NOT_SIGNED_IN_UNREADABLE = "signed_in", "empty", "unreadable"

# The Terminal tab's shells (terminals.py): each writes its pidfile, named by a token of its own,
# in this folder of the runtime folder, which goes with the container.
TERMINALS = "terminals"
TERMINAL_PIDFILE = re.compile(r"[0-9a-f]{16}\.pid\Z")
# What every shell of the Terminal tab is started by, a fixed program, as the workspace's shell
# runs it with ``-c`` and its arguments: the work folder, the pidfile and the shell to fall back
# on. It writes its own id and start time to the pidfile, which ending the terminal checks
# before it signals anything (terminals.py), and execs the user's own shell, from the image's
# passwd, or the fallback where that is none it can run, interactive, with SHELL naming it. It
# reads nothing a person sent.
TERMINAL_LAUNCHER = r"""
cd -- "$1" || exit 1
pidfile=$2 fallback=$3
read -r stat < /proc/$$/stat
set -- ${stat##*) }
printf '%s %s\n' "$$" "${20}" > "$pidfile" || exit 1
uid=
while read -r key value rest; do
  if [ "$key" = Uid: ]; then uid=$value; break; fi
done < /proc/self/status
shell=
if [ -r /etc/passwd ]; then
  while IFS=: read -r _ _ id _ _ _ login; do
    if [ "$id" = "$uid" ]; then shell=$login; break; fi
  done < /etc/passwd
fi
case ${shell##*/} in nologin|false) shell= ;; esac
if [ -z "$shell" ] || [ -d "$shell" ] || [ ! -x "$shell" ]; then shell=$fallback; fi
SHELL=$shell
export SHELL
exec "$shell" -i
"""


def credentials_state(raw: bytes) -> str:
    """What a credentials file holds: ``signed_in`` for a sign-in (an object with the OAuth
    entry Claude Code writes), ``empty`` for nothing, and ``unreadable`` for anything else -
    half of a file two workspaces' Claude wrote at once, or what is not JSON."""
    if not raw.strip():
        return NOT_SIGNED_IN_EMPTY
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return NOT_SIGNED_IN_UNREADABLE
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return SIGNED_IN if isinstance(oauth, dict) else NOT_SIGNED_IN_UNREADABLE


# Labels every workspace container and volume carries: whose it is, and which row. A
# person's account volume and sign-in container carry only the person's.
LABEL_WORKSPACE = "rcman.workspace"
LABEL_PERSON = "rcman.person"
# The id of the database a workspace's volume and container were made for (db.install_id): a
# volume of another database is never adopted, whatever person and workspace ids it has.
LABEL_INSTALL = "rcman.install"
# The canary: a throwaway container that runs the checks against a candidate Claude Code
# version. It carries only this label, so no restore sees it as a workspace.
CANARY_NAME = "rcman-canary"
LABEL_CANARY = "rcman.canary"
# The candidate's file in the folder the canary is given, and where that folder and rcman's
# helpers are in the canary; the helpers' package is the folder ``helpers`` in CANARY_HELPERS_MOUNT,
# which is what Python is told to import from.
CANDIDATE = "claude"
CANARY_CANDIDATE_MOUNT = "/candidate"
CANARY_HELPERS_MOUNT = "/canary"
HELPERS_DIR = Path(helpers.__file__).resolve().parent
# The canary's home and temporary folder: a tmpfs the candidate may fill and nothing outlasts.
CANARY_TMP = "/tmp"
# How long the checks may take in all: every check's own limit, and one more for the container.
CANARY_SECONDS = (len(claude_checks.CHECKS) + 1) * claude_checks.TIMEOUT
# The name a folder starts with in which an image copied out of a builder is staged.
STAGING_PREFIX = "rcman-image-"
# What a builder container and its volume carry besides the person: which workspace's build it is.
LABEL_BUILDER = "rcman.builder"
# An image built for a workspace is named localhost/<BUILT_REPOSITORY>/<workspace>:<BUILT_TAG>,
# in rcman's own storage. A person's devcontainer.json may not name such an image, or any by its
# id (devcontainer.py), so no person's workspace is made from another's.
BUILT_REPOSITORY = "rcman-built"
BUILT_TAG = "latest"
# The name Podman gives a volume it makes without being asked to.
ANONYMOUS_VOLUME = re.compile(r"[0-9a-f]{64}\Z")
# An image's digest as a registry names it, which a pull may be given after an "@".
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
BUILT_NAME = re.compile(rf"localhost/{re.escape(BUILT_REPOSITORY)}/(\d+):{re.escape(BUILT_TAG)}\Z")
# Where a one-off container that makes or reads a person's credentials file mounts
# their account volume.
ACCOUNT_MOUNT = "/account"
# The folder of the account volume that holds the person's git configuration and
# credential, bound at HOME/GIT_HOME in every workspace of theirs; and the only files
# rcman puts in it: git's, and gh's, which gh reads there because GH_CONFIG_DIR names the
# folder (child_env).
GIT_DIR = "git"
GIT_HOME = ".config/git"
GIT_FILES = ("config", "credential")
# gh's configuration, which rcman owns whole: only its version, so that gh takes it as current
# and never migrates, and so rewrites, the folder; and its hosts, which hold the token.
GH_CONFIG_FILE = "config.yml"
GH_HOSTS_FILE = "hosts.yml"
GH_FILES = (GH_CONFIG_FILE, GH_HOSTS_FILE)
ACCOUNT_FILES = (*GIT_FILES, *GH_FILES)
GH_CONFIG = 'version: "1"\n'
# The variable that points gh at that folder.
GH_CONFIG_DIR = "GH_CONFIG_DIR"
# The scripts of an account container get the credentials file's path as $1, the git
# folder's as $2 and, for a write, the name of the file in it as $3.
# Makes the credentials file, owner-only, if it is not there, and never truncates it;
# makes the git folder and its credential file, which git's credential helper reads, so
# that there is always one to read; and gh's configuration if it is not there, never replacing
# one that is. That is written whole into a temporary file of its own and linked into place,
# which fails where another run linked it first: runs at once (a workspace is made outside the
# person's lock) all succeed, and none sees it empty or half written. Any name there counts as
# there, a link the container cannot follow among them (the volume is all it mounts): it is
# left for a put to replace, and the script goes on.
ENSURE_SCRIPT = (
    'umask 077 && mkdir -p "$2" && : >> "$1" && : >> "$2/credential" '
    f'&& {{ [ -e "$2/{GH_CONFIG_FILE}" ] || [ -L "$2/{GH_CONFIG_FILE}" ] '
    f'|| {{ t=$(mktemp "$2/{GH_CONFIG_FILE}.XXXXXX") '
    f'&& {{ printf %s {shlex.quote(GH_CONFIG)} > "$t" '
    f'&& ln "$t" "$2/{GH_CONFIG_FILE}" 2>/dev/null; rm -f "$t"; '
    f'[ -e "$2/{GH_CONFIG_FILE}" ] || [ -L "$2/{GH_CONFIG_FILE}" ]; }}; '
    "}; }"
)
READ_SCRIPT = f'{ENSURE_SCRIPT} && cat "$1"'
# Empties it in place, so every file bound to it, by inode, sees it empty.
CLEAR_SCRIPT = 'umask 077 && : > "$1"'
# Writes standard input as a file of the git folder, made if it is not there: whole and
# owner-only, by a rename inside the folder, so that a reader never sees half of it; a copy
# that could not be put in place is removed, so no token is left in a temporary file.
PUT_SCRIPT = (
    f"{ENSURE_SCRIPT} && trap 'rm -f \"$2/$3.new\"' EXIT "
    '&& cat > "$2/$3.new" && mv -f "$2/$3.new" "$2/$3"'
)
# What is said when a volume that is there is not labelled for the person a workspace is for.
VOLUME_OF_ANOTHER = "the volume {name} is there and is for another person, so it is not used"
# A workspace's container is made only where none of its name is: what ``create`` removes when it
# fails is then only ever what it made itself.
NAME_TAKEN = "a container named {name} is there already"
# Each person's uid range: slot k maps the container's ids 0 to RANGE - 1 onto rcman's
# subordinate ids from k * RANGE. Slot 0 would be rcman's own ids, so no person has it.
RANGE = 65536
# The network of every container a person's programs run in: their own, with no way to the
# host's loopback.
NETWORK = "slirp4netns:allow_host_loopback=false"
# What every workspace container is given so that a container nested in it can mount a /proc of
# its own: the kernel refuses a new /proc where parts of the one there are covered.
UNMASK_PROC = "--security-opt=unmask=/proc/*"
# The seccomp profile of every workspace container, in the tools (the installer writes it,
# installer/install.py workspace_seccomp): Podman's default, with a container nested in the
# workspace allowed to name its own UTS namespace.
SECCOMP = "seccomp.json"
# The fields of PodmanConfig a test engine gives values of its own (keyword-only, the product's
# values by default; tests/test_main.py holds the product to the defaults).
INJECTED = ("range", "network", "slices")
# podman's exit codes: 1 from `exists` for something missing; 125 when it could not
# reach Podman at all, or failed before running anything.
MISSING = 1
UNAVAILABLE = 125
PODMAN_TIMEOUT = 300.0
# What systemctl is given to answer: it talks to the user manager, which answers at once.
SYSTEMCTL_TIMEOUT = 30.0
# What systemctl says when it cannot reach the user manager's bus.
NO_BUS = "failed to connect to bus"
# A container made from an image of a repository's choosing: Podman copies the image into the
# person's uid range the first time the range uses it, which takes much longer than
# PODMAN_TIMEOUT for a big one.
IMAGE_CREATE_TIMEOUT = 1800.0
# How long pulling an image may take in all, how often a pull is looked at, and how many of
# the last lines it printed an error keeps.
PULL_TIMEOUT = 3600.0
PULL_POLL_SECONDS = 1.0
PULL_TAIL_LINES = 5
# What a pull says when the image it could not pull is one the runtime has already.
USING_LOCAL_IMAGE = "The pull failed; the image already here is used"
# How long an image with no name is left alone: it may be a layer of a build under way, which an
# upgrade runs beside the service.
IMAGE_GRACE_SECONDS = 3600.0
# What is said of an image named ``localhost/...``, which is never pulled.
IN_STORAGE = "The image is in rcman's storage; nothing is pulled"
# What podman says, with the exit code UNAVAILABLE, when it cannot reach its service at all.
# Other failures share the code and the word "connect": a registry's refusal, and a program
# run in a workspace that exits with that code after failing to connect somewhere.
SERVICE_UNREACHABLE = "cannot connect to podman"
# The most of an image's passwd or group file that is read, in bytes, and what a tar adds to a
# file it holds.
MAX_ID_FILE_BYTES = 1024 * 1024
TAR_OVERHEAD_BYTES = 64 * 1024
# How much of what podman said on standard error is kept when it copies a file, and the words
# of it that mean the file is not in the image.
MAX_SAID_BYTES = 4096
FILE_ABSENT = ("no such file", "could not be found", "does not exist")
# The label and the name prefix of the container made to read an image without running it.
LABEL_PROBE = "rcman.probe"
PROBE_PREFIX = "rcman-probe-"
STOP_TIMEOUT_SECONDS = 10
# How long a command's output readers are waited for once it has ended.
TAIL_JOIN_SECONDS = 5.0
# What a command run under the ``timeout`` tool exits with when it was ended for taking too
# long: GNU timeout's 124 and busybox's 128 + the signal, which a process that exec reports
# as the negative signal, and GNU timeout, which kills itself with KILL after it has killed
# the command, as -9 (the host runtime).
TIMED_OUT = (124, 137, 143, -9, -15)
# What conmon says when it could not start an exec at all: nothing ran, so it is tried
# once more (channel.CONMON_FAILED).
CONMON_FAILED = "no logs from conmon"
# What a Podman exec writes on stderr before the program starts (``marked_exec``). Podman's
# remote exec sometimes, under load, ends with the program's exit code and nothing of what it
# wrote on either stream: an answer without the mark is one whose output was lost, not one of
# a program that wrote nothing.
EXEC_MARK = "<rcman-exec>"
# The shell that writes the mark, then becomes the program ``$0`` with its arguments. The
# variables busybox's shell sets, changes or exports of its own (SHLVL, PWD, IFS, OPTIND, PPID,
# and PATH when there is none) are put back as the environment it was started with has them,
# read from /proc, or taken away, so the program's environment is Podman's; a ``.`` after each
# value keeps a newline at its end. A bare name is looked up on PATH, in a subshell so that IFS
# stays as it was, and run by its path: the shell would otherwise run its own applet of that
# name instead of the image's program. The lookup is Podman's for an executable file on a PATH
# that is set; with no PATH a bare name is not found, and a file that is neither a binary nor
# starts with ``#!`` is run by the shell as a script, where Podman refuses it.
EXEC_SCRIPT = (
    f"printf '%s' '{EXEC_MARK}' >&2\n"
    "for v in SHLVL PWD IFS OPTIND PPID PATH; do\n"
    '  if o=$(grep -z "^$v=" /proc/$$/environ && echo .); then\n'
    '    o=${o%.}; export "$v=${o#*=}"\n'
    "  else\n"
    '    unset "$v"\n'
    "  fi\n"
    "done\n"
    'case $0 in */*) exec "$0" "$@" ;; esac\n'
    "p=$(set -f; IFS=:; for d in $PATH; do\n"
    '  if [ -n "$d" ] && [ -f "$d/$0" ] && [ -x "$d/$0" ]; then printf \'%s.\' "$d/$0"; exit; fi\n'
    "done)\n"
    'if [ -n "$p" ]; then exec "${p%.}" "$@"; fi\n'
    "printf '%s: executable file not found in $PATH\\n' \"$0\" >&2\n"
    "exit 127\n"
)
# How long one wait for Podman's socket polls, between tries.
READY_POLL_SECONDS = 1.0


# The variables that move git's configuration from where rcman writes it (the git folder, which
# HOME/GIT_HOME makes the one git reads when XDG_CONFIG_HOME is not set) or add to it: the plan
# of a workspace may not set them, and git is not run with them.
GIT_CONFIG_NAMES = frozenset(
    {
        "XDG_CONFIG_HOME",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CURL_VERBOSE",
    }
)
# The numbered pairs of the configuration, and the traces, which print what git sends (a token
# among it) unless told not to.
GIT_CONFIG_PAIR = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+\Z|GIT_TRACE(?:_[A-Z0-9_]+)?\Z")


def moves_git_config(name: str) -> bool:
    """Whether the variable ``name`` moves git's configuration (``GIT_CONFIG_NAMES``, or one
    of the numbered pairs ``GIT_CONFIG_KEY_n`` and ``GIT_CONFIG_VALUE_n``) or makes git print what
    it sends (``GIT_TRACE`` and its kin, ``GIT_CURL_VERBOSE``)."""
    return name in GIT_CONFIG_NAMES or GIT_CONFIG_PAIR.match(name) is not None


def registry_host(image: str) -> str | None:
    """The host of the registry an image name gives, lower case and without its port, or None
    for a name of the default registry: the first part is a host when it is ``localhost`` or
    has a dot or a port in it, and a user of the default registry otherwise."""
    first, slash, _ = image.partition("/")
    if not slash or not (first.lower() == "localhost" or "." in first or ":" in first):
        return None
    return first.rsplit(":", 1)[0].lower().rstrip(".")


def qualified_name(image: str) -> str:
    """``image`` as Podman lists it: the registry written out (``docker.io/library/`` for a
    name of the default registry) and a tag (``latest``) unless a digest names it."""
    host = registry_host(image)
    name = image
    if host is None:
        name = f"docker.io/{image if '/' in image else 'library/' + image}"
    elif host == "index.docker.io":
        name = "docker.io" + image[len("index.docker.io") :]
    last = name.rsplit("/", 1)[-1]
    if "@" not in last and ":" not in last:
        name += ":latest"
    return name


def repository_of(image: str) -> str:
    """The name of ``image`` without its tag or digest."""
    name = image.partition("@")[0]
    head, slash, last = name.rpartition("/")
    return f"{head}{slash}{last.partition(':')[0]}"


def container_name(workspace_id: int) -> str:
    return f"rcman-ws-{workspace_id}"


def built_name(workspace_id: int) -> str:
    """The name of the image built for the workspace."""
    return f"localhost/{BUILT_REPOSITORY}/{workspace_id}:{BUILT_TAG}"


def workspace_of_built_name(name: str) -> int | None:
    """The workspace an image built for it is named for, or None for any other name."""
    found = BUILT_NAME.match(name)
    return int(found.group(1)) if found else None


def account_volume(person: int) -> str:
    """The volume that holds the person's credentials file and git folder, and nothing else."""
    return f"rcman-person-{person}"


def _checked(name: str) -> str:
    """``name``, a file of the git folder rcman writes; nothing else is."""
    if name not in ACCOUNT_FILES:
        raise ValueError(f"{name!r} is not a file rcman writes in the git folder")
    return name


def sign_in_name(person: int) -> str:
    """The person's short-lived sign-in container; one at a time."""
    return f"rcman-sign-in-{person}"


class RuntimeUnavailable(Exception):
    """Podman could not be reached."""


class RuntimeFailed(Exception):
    """Podman, or the host runtime, refused or failed; the message is what it said."""


class BuildFailed(RuntimeFailed):
    """The build failed. ``tail`` is the end of what the builder said, as it said it; ``kind`` is
    the key of ``problems.DEVCONTAINER_MESSAGES`` where the runtime knows the cause (a time or
    size limit) and None where the cause is for the tail to show (``builder.explain``);
    ``fields`` fill the message in."""

    def __init__(self, tail: str = "", kind: str | None = None, **fields):
        self.tail, self.kind, self.fields = tail, kind, fields
        super().__init__(tail or kind or "the build failed")


@dataclass(frozen=True)
class BuildRequest:
    """One build: for the workspace ``id`` of the person ``person``, in the builder's uid range
    ``slot``, of the repository in the work folder of the container ``source``. The CLI is given
    the file ``config_text``, which is written to ``config_path`` in the copy of the repository
    (devcontainer.build_config)."""

    id: int
    person: int
    slot: int
    source: str
    config_path: str
    config_text: str


@dataclass(frozen=True)
class Spec:
    """A workspace container to make: by default from the workspace image, as root of the
    person's uid range. With ``image`` it is made from that image, running as ``user`` (a name
    or number, or ``uid:gid``, as ``--user`` takes it; ``groups`` are then its supplementary
    groups) whose ids in the image are ``uid`` and ``gid``: the person's range is mapped so
    that they are the host ids root has in the person's other workspaces. ``env`` is the
    container's environment."""

    name: str
    slot: int
    workspace: int
    person: int
    image: str | None = None
    user: str | None = None
    uid: int = 0
    gid: int = 0
    groups: tuple[int, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    # The id of the image the container is made from, where that is known and it is to be
    # made from exactly that image whatever its name names now (a container made again): with
    # no ``image`` it is the default image's. It is used in place of the name.
    image_id: str = ""
    # The id of the database the workspace is for (``LABEL_INSTALL``); empty where there is none.
    install: str = ""


@dataclass(frozen=True)
class ImageFacts:
    """What an image says about itself, read without running it: its passwd and group files
    (empty where it has none), the user it is set to run as, its environment, its labels, its
    id, and the digest it was pulled by where it came from a registry (empty otherwise)."""

    passwd: str = ""
    group: str = ""
    user: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    id: str = ""
    digest: str = ""


@dataclass(frozen=True)
class Layout:
    """Where a workspace's things are, as the programs run in it see them."""

    work: str
    config: str
    home: str
    run: str
    tmux_socket: str
    tmux: str
    tmux_conf: str
    claude: str  # the program that runs whatever the store's `current` names
    shell: str
    terminfo: str | None
    path: str
    user: str  # who the programs run as there
    # name -> the argv that runs it there: env, kill, cat, wc, rm, mkdir, chmod, mv, timeout, tar
    tools: dict
    # The argv, but for the group's id, that sends SIGKILL to a whole process group:
    # busybox's kill refuses a "--" before it, and procps' kill needs one.
    kill_group: tuple = ()
    # What the workspace's own plan adds (``for_plan``): variables every program rcman runs
    # there gets besides its own, and the folder of rcman's own programs, which stays first
    # on a PATH the plan sets.
    env: Mapping[str, str] = field(default_factory=dict)
    path_prefix: str = ""
    # The Claude Code store as the programs in the workspace see it: a version is the file
    # ``versions/<version>`` in it.
    claude_root: str = ""

    def tool(self, name: str) -> list[str]:
        return list(self.tools[name])

    def pinned(self, version: str) -> Layout:
        """This layout with Claude Code the exact file of ``version`` in the store, whatever the
        store's `current` names: what a server is started with, so that approving another
        version changes nothing of a server that runs."""
        if not rules.VERSION.fullmatch(version):
            raise ValueError(f"{version!r} is not a Claude Code version")
        return dataclasses.replace(self, claude=f"{self.claude_root}/versions/{version}")

    def for_plan(self, user: str, env: Mapping[str, str], path: str | None) -> Layout:
        """This layout for a workspace made from a plan: its programs run as ``user`` with
        ``env`` besides their own variables, and with ``path``, if there is one, after the
        folder of rcman's own programs."""
        tail = self.path if path is None else ":".join(p for p in (self.path_prefix, path) if p)
        # A stored plan may hold GH_CONFIG_DIR, which devcontainer.RESERVED_ENV refuses only when
        # a plan is made.
        kept = {k: v for k, v in env.items() if not moves_git_config(k) and k != GH_CONFIG_DIR}
        return dataclasses.replace(self, user=user, env=kept, path=tail)

    def kill_group_of(self, pgid: int) -> list[str]:
        return [*self.kill_group, f"-{pgid}"]

    def with_env(self, env: dict[str, str], argv: list[str]) -> list[str]:
        """``argv`` run with exactly ``env``, and nothing of the environment it is run in."""
        return [*self.tool("env"), "-i", *(f"{k}={v}" for k, v in env.items()), *argv]


def gh_config_dir(layout: Layout) -> str:
    """The folder gh reads its configuration from in the workspace: the person's git folder,
    where rcman writes GH_FILES."""
    return f"{layout.home}/{GIT_HOME}"


def child_env(layout: Layout) -> dict[str, str]:
    """The only environment Claude Code is started with (claude_server.py), and the Terminal
    tab's shells. Its configuration is the workspace's own, and it never updates itself: the
    store it runs from is read-only, and an update would go into the workspace unapproved. gh
    reads the person's GitHub token from the files rcman writes in their git folder, which
    every command reads again, so a token saved or removed reaches a server that runs. The
    variables the workspace's plan sets come first, so that none of them replaces one of
    these."""
    return {
        **layout.env,
        "HOME": layout.home,
        "USER": layout.user,
        "LOGNAME": layout.user,
        "PATH": layout.path,
        "SHELL": layout.shell,
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": layout.config,
        GH_CONFIG_DIR: gh_config_dir(layout),
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/bin/false",
    }


@dataclass(frozen=True)
class Launch:
    """A program rcman starts on the host: its argv and its whole environment."""

    argv: list[str]
    env: dict[str, str]


def terminal_command(layout: Layout, pidfile: str) -> list[str]:
    """The Terminal tab's launcher, as the workspace runs it, writing ``pidfile``: with exactly
    the environment Claude has there and the terminal descriptions, and nothing of rcman's."""
    if not (
        pidfile.startswith("/")
        and "\n" not in pidfile
        and ".." not in pidfile.split("/")
        and TERMINAL_PIDFILE.fullmatch(pidfile.rpartition("/")[2])
    ):
        raise ValueError(f"{pidfile!r} is not a terminal's pidfile")
    env = child_env(layout)
    if layout.terminfo:
        env["TERMINFO"] = layout.terminfo
    launcher = [layout.shell, "-c", TERMINAL_LAUNCHER, "rcman-terminal", layout.work, pidfile]
    return layout.with_env(env, [*launcher, layout.shell])


class Runtime(ABC):
    # How many ids a slot's range holds: the product's RANGE unless a test engine gives another.
    id_range: int = RANGE

    @abstractmethod
    def layout(self, name: str) -> Layout: ...

    @abstractmethod
    def wait_ready(self, timeout: float) -> None:
        """Wait until the runtime answers; RuntimeUnavailable if it does not in time."""

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def running(self, name: str) -> bool: ...

    @abstractmethod
    def create(self, spec: Spec) -> None:
        """Make the workspace and its volume, start it, and make its folders; the person's
        credentials file is made sure of first, and bound at the workspace's
        ``config``/CREDENTIALS. The container is in the workspace's slice (``limits``), so
        the limits set on it hold it; ``apply_limits`` before this is what puts them there."""

    @abstractmethod
    def start(self, name: str) -> None: ...

    @abstractmethod
    def stop(self, name: str) -> None: ...

    @abstractmethod
    def remove(self, name: str) -> None:
        """Remove the workspace and its volume; nothing for one that is not there."""

    @abstractmethod
    def discard(self, name: str) -> None:
        """Remove the workspace's container but keep its volume, so that ``create`` makes it
        again on the same files; nothing for one that is not there."""

    @abstractmethod
    def pull(
        self,
        image: str,
        progress: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Pull ``image`` into the runtime's storage, calling ``progress`` with each line the
        pull prints; RuntimeFailed with what it said if it fails, or at once, with the pull
        stopped, if ``cancel`` is set."""

    @abstractmethod
    def image_facts(self, image: str, cancel: threading.Event | None = None) -> ImageFacts:
        """What the pulled ``image`` says about itself, without running it; RuntimeFailed if it
        cannot be read, or at once, with the reading stopped, if ``cancel`` is set."""

    @abstractmethod
    def has_image(self, image: str) -> bool:
        """Whether the runtime's storage has the image ``image``, a name or an id."""

    @abstractmethod
    def collect_images(self, keep_ids: set[str], keep_names: set[str]) -> list[str]:
        """Remove the images in the runtime's storage that nothing keeps: those whose id is not in
        ``keep_ids`` and none of whose names is in ``keep_names``, and that the runtime does not
        itself keep (the default image, the builder's). Never by force: an image a container is
        made from stays, and so does one that cannot be removed, which is not an error. The
        names (or ids, of those with none) that were removed."""

    @abstractmethod
    def default_image_id(self) -> str:
        """The id of the image new workspaces are made from now; empty where the runtime has
        no such thing."""

    @abstractmethod
    def build_image(
        self,
        request: BuildRequest,
        progress: Callable[[str], None],
        cancel: threading.Event | None,
        vet: Callable[[Mapping[str, str]], None],
    ) -> str:
        """Build the image ``request`` asks for in a builder of its own, and keep it in the
        runtime's storage as ``built_name(request.id)``, which is returned. It begins by removing
        what an earlier build of the same id left. ``progress`` is told the builder's steps and
        the copy's progress; ``vet`` is called with the image's labels before the image is
        copied out, and may raise to refuse it. Whatever happens the builder and its volume are
        removed, and the image too unless it is kept: a build that fails, is refused or is
        stopped leaves nothing. A failing build is a :class:`BuildFailed`; a stop (``cancel``
        set, or ``remove_build`` called meanwhile) is a RuntimeFailed that is not."""

    @abstractmethod
    def remove_build(self, workspace_id: int, image: bool = False) -> None:
        """Remove the builder of the workspace's build and its volume, which ends a build in
        progress, and with ``image`` the image built for it; nothing for what is not there."""

    @abstractmethod
    def builders(self) -> set[int]:
        """The workspaces whose builder container or volume there is."""

    @abstractmethod
    def built_images(self) -> dict[int, str]:
        """The images built for workspaces there are: workspace -> name."""

    @abstractmethod
    def clean_probes(self) -> None:
        """Remove what ``image_facts`` left if rcman ended while it read an image, and the
        staging folders of images copied out of a builder that no build can still be using."""

    @abstractmethod
    def labelled(self) -> dict[str, dict[str, str]]:
        """Every workspace there is, by name, with its labels."""

    @abstractmethod
    def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float = PODMAN_TIMEOUT,
        cwd: str | None = None,
        keep: int | None = None,
        again: bool = False,
    ) -> Result:
        """``argv`` run once in the workspace, in its folder ``cwd`` (the runtime's own default
        if there is none: callers that need a folder name it) as the user its container runs as,
        with ``stdin`` on its standard input; RuntimeFailed if it has not ended within
        ``timeout`` seconds. With ``keep``, only the last ``keep`` bytes of each of its output
        streams are held, however much it prints.

        A runtime that can lose what the program wrote while its exit code still arrives (the
        Podman runtime's remote exec, EXEC_MARK) runs it once more when that happens and
        ``again`` says the program changes nothing, and fails with RuntimeFailed if it is lost
        again. Without ``again`` the program is not run twice: the answer is its exit code with
        nothing of what it wrote."""

    @abstractmethod
    def channel(self, name: str) -> Channel:
        """A new channel to the workspace (channel.py), not yet opened."""

    @abstractmethod
    def terminal_argv(self, name: str, layout: Layout, pidfile: str) -> Launch:
        """What starts a Terminal tab's shell in the workspace, given the pseudo-terminal as its
        standard streams: ``setsid --ctty`` first, so that the pty becomes the controlling
        terminal of what it runs (^C, job control and a resize reach the shell), then the
        launcher (``terminal_command``), as the container's own user."""

    @abstractmethod
    def ensure_account(self, person: int, slot: int) -> None:
        """Make the person's credentials file, empty and theirs alone, if it is not there; and
        their git folder, with an empty credential and gh's configuration (GH_CONFIG) in it
        where they are not there. Nothing that is there is changed."""

    @abstractmethod
    def read_account(self, person: int, slot: int) -> bytes:
        """The person's credentials file, made sure of first."""

    @abstractmethod
    def clear_account(self, person: int, slot: int) -> None:
        """Empty the person's credentials file in place: the same file, not a new one."""

    @abstractmethod
    def put_account(self, person: int, slot: int, name: str, data: bytes) -> None:
        """Write ``data`` as the file ``name`` (one of ACCOUNT_FILES) of the person's git
        folder, which their workspaces bind at HOME/GIT_HOME: whole, owner-only, and by a
        rename inside the folder. It needs no workspace, and ``data`` is never in an argv."""

    @abstractmethod
    def remove_account(self, person: int) -> None:
        """Remove the person's credentials file and git folder; nothing for one that is not
        there."""

    @abstractmethod
    def start_sign_in(self, person: int, slot: int, seconds: float) -> str:
        """Start the person's sign-in container, which ends itself after ``seconds``, with
        the layout of a workspace and their credentials file bound as a workspace's; one
        left from before is removed first. Its name, for ``exec``, ``channel`` and
        ``end_sign_in``. It is not labelled a workspace."""

    @abstractmethod
    def end_sign_in(self, name: str) -> None:
        """Remove a sign-in container; nothing for one that is gone."""

    @abstractmethod
    def apply_limits(self, person: int, workspace: int | None, held: Limits) -> None:
        """Set ``held`` on the person's slice, or on the workspace's where ``workspace`` is
        given: all of them, a limit left out reset. They hold until the machine restarts, and
        apply at once to what runs in the slice. RuntimeFailed where they could not be set."""

    @abstractmethod
    def usage(self, person: int, workspace: int | None) -> Usage:
        """What the person's slice, everything of theirs beneath it, or the workspace's uses
        now. A slice nothing has run in uses nothing; RuntimeFailed where it cannot be read."""

    @abstractmethod
    def caps(self, person: int, workspace: int | None) -> Caps:
        """The caps on memory and tasks the person's slice or the workspace's has now, as the
        unit manager holds them, which may not be the ones rcman's table holds (a restart
        clears them, and a set can be applied and not recorded). No cap is None;
        RuntimeFailed where they cannot be read."""

    @abstractmethod
    def drop_limits(self, person: int, workspace: int | None) -> None:
        """Take every limit off the slice, at once, without stopping anything that runs in it:
        a slice is never stopped, as that would stop the containers in it."""

    @abstractmethod
    def run_canary(
        self, directory: Path, name: str, version: str, *, slot: int | None = None
    ) -> dict[str, dict]:
        """Run the checks of helpers/claude_canary.py against the candidate Claude Code
        ``version``, which is the file CANDIDATE in ``directory`` (readable and runnable by
        anyone), in a throwaway container called ``name``, in the uid range of ``slot`` (the
        builder's, people.BUILDER_SLOT, unless the service is given another), that has no
        network and nothing of rcman's but the helpers; one left from before is removed first.
        The checks the
        candidate fails, each with its report: empty when it passes them all. A canary that
        could not report - it did not start, did not finish, or said something else - is a
        RuntimeFailed: that says nothing about the candidate."""

    @abstractmethod
    def end_canary(self, name: str) -> None:
        """Remove a canary container; nothing for one that is gone."""


# --- Podman ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PodmanConfig:
    socket: Path
    image: str
    tools_dir: Path  # mounted read-only at TOOLS_MOUNT
    claude_store: Path  # mounted read-only at CLAUDE_MOUNT
    podman: str = "/usr/bin/podman"
    systemctl: str = "/usr/bin/systemctl"  # sets the limits of the slices (limits.py)
    builder_image: str = "localhost/rcman-builder"  # the image a build runs in
    # Where an image copied out of a builder is staged and checked before it is loaded: a folder
    # of rcman's own state, with room for the biggest image (builder.MAX_IMAGE_BYTES); the
    # system's temporary folder where none is given.
    scratch_dir: Path | None = None
    _: KW_ONLY
    # The size of a slot's range of ids (slot k maps from k * range).
    range: int = RANGE
    # The network of workspaces, sign-in containers and the builder.
    network: str = NETWORK
    # Whether workspaces and builds run in their systemd slices (limits.py), which Podman takes
    # only with cgroups enabled and the systemd manager.
    slices: bool = True


def podman_env() -> dict[str, str]:
    """The only environment podman is run with."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    for key in ("HOME", "XDG_RUNTIME_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _run_keeping_tail(
    argv: list[str],
    stdin: bytes | None,
    timeout: float,
    env: Mapping[str, str],
    cwd: str | None,
    keep: int,
) -> tuple[int, bytes, bytes]:
    """``argv`` run to its end, or to ``timeout`` (subprocess.TimeoutExpired, the process killed),
    holding of each of its output streams only the last ``keep`` bytes: its exit code and those
    two tails."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    tails = [bytearray(), bytearray()]

    def drain(stream, tail: bytearray) -> None:
        while chunk := stream.read1(65536):
            tail += chunk
            if len(tail) > keep:
                del tail[: len(tail) - keep]

    def feed() -> None:
        with contextlib.suppress(OSError):  # the process may end without reading it all
            proc.stdin.write(stdin)
            proc.stdin.close()

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, tails[0]), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, tails[1]), daemon=True),
    ]
    if stdin is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=TAIL_JOIN_SECONDS)
        proc.stdout.close()
        proc.stderr.close()
    return code, bytes(tails[0]), bytes(tails[1])


def _labels_of(inspected: dict) -> dict[str, str]:
    """The labels in the JSON ``podman image inspect`` printed."""
    return dict(inspected.get("Labels") or (inspected.get("Config") or {}).get("Labels") or {})


def _busybox(name: str) -> list[str]:
    return [f"{TOOLS_MOUNT}/bin/busybox", name]


def marked_exec(argv: list[str]) -> list[str]:
    """``argv`` as a Podman exec runs it: the same arguments, input, folder and environment,
    after the mounted busybox's shell has written EXEC_MARK on stderr; the same program for a
    path, and for a bare name of an executable file on a PATH that is set (EXEC_SCRIPT)."""
    return [*_busybox("sh"), "-c", EXEC_SCRIPT, *argv]


PODMAN_LAYOUT = Layout(
    work=f"{VOLUME_MOUNT}/{WORK}",
    config=f"{VOLUME_MOUNT}/{CONFIG}",
    home=f"{VOLUME_MOUNT}/{HOME}",
    run=RUN_MOUNT,
    tmux_socket=f"{RUN_MOUNT}/tmux.sock",
    tmux=f"{TOOLS_MOUNT}/bin/tmux",
    tmux_conf=f"{TOOLS_MOUNT}/tmux.conf",
    claude=f"{CLAUDE_MOUNT}/bin/claude",
    shell=f"{TOOLS_MOUNT}/bin/sh",
    terminfo=f"{TOOLS_MOUNT}/terminfo",
    path=f"{CLAUDE_MOUNT}/bin:{PODMAN_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    user="root",
    tools={
        name: _busybox(name)
        for name in (
            "env",
            "kill",
            "cat",
            "wc",
            "rm",
            "mkdir",
            "chmod",
            "mv",
            "wget",
            "timeout",
            "tar",
        )
    },
    kill_group=(*_busybox("kill"), "-KILL"),
    path_prefix=f"{CLAUDE_MOUNT}/bin:{PODMAN_BIN}",
    claude_root=CLAUDE_MOUNT,
)


class PodmanRuntime(Runtime):
    def __init__(self, config: PodmanConfig):
        self.config = config
        self.id_range = config.range
        # Held from the names of rcman's own images being read before a ``podman load`` to the
        # new image being named, so that no other build's naming of its image is taken for the
        # load's doing.
        self._naming = threading.Lock()
        # How many builds are under way, and the lock that makes taking part in one and
        # sweeping the staging folders exclusive of each other.
        self._staging_lock = threading.Lock()
        self._active_builds = 0

    def layout(self, name: str) -> Layout:
        return PODMAN_LAYOUT

    def _argv(self, *args: str) -> list[str]:
        return [self.config.podman, "--remote", "--url", f"unix://{self.config.socket}", *args]

    def _podman(
        self,
        *args: str,
        check: bool = True,
        timeout: float = PODMAN_TIMEOUT,
        stdin: bytes | None = None,
    ):
        try:
            proc = subprocess.run(
                self._argv(*args),
                capture_output=True,
                env=podman_env(),
                timeout=timeout,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        except subprocess.TimeoutExpired:
            raise RuntimeFailed(f"podman {args[0]} did not finish in time") from None
        said = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode == UNAVAILABLE and SERVICE_UNREACHABLE in said.lower():
            raise RuntimeUnavailable(said)
        if check and proc.returncode != 0:
            raise RuntimeFailed(said or f"podman {args[0]} failed")
        return proc

    def _remove_container(self, name: str, check: bool = True):
        """Remove the container at once, with its anonymous volumes: a workspace's TMP_MOUNT, and
        a volume the image declares, each made with the container and nobody's once it is gone."""
        return self._podman("rm", "--force", "--ignore", "--time=0", "--volumes", name, check=check)

    def _systemctl(self, *args: str) -> str:
        """``systemctl --user`` with ``args``, as rcman's own user, with an argv and the
        environment podman_env allows (which holds XDG_RUNTIME_DIR, without which it cannot
        find the user manager). What it printed; RuntimeFailed with what it said when it
        refuses, RuntimeUnavailable when the user manager cannot be reached."""
        try:
            proc = subprocess.run(
                [self.config.systemctl, "--user", *args],
                capture_output=True,
                env=podman_env(),
                timeout=SYSTEMCTL_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.systemctl} is not installed") from None
        except subprocess.TimeoutExpired:
            raise RuntimeFailed(f"systemctl {args[0]} did not finish in time") from None
        said = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            if NO_BUS in said.lower():
                raise RuntimeUnavailable(said)
            raise RuntimeFailed(said or f"systemctl {args[0]} failed")
        return proc.stdout.decode("utf-8", "replace")

    def apply_limits(self, person: int, workspace: int | None, held: Limits) -> None:
        self._systemctl(
            "set-property", "--runtime", limits.slice_of(person, workspace), *held.properties()
        )

    def usage(self, person: int, workspace: int | None) -> Usage:
        shown = self._systemctl(
            "show", "-p", "ActiveState", "-p", "MemoryCurrent", "-p", "MemorySwapCurrent",
            "-p", "TasksCurrent", limits.slice_of(person, workspace),
        )  # fmt: skip
        try:
            return limits.read_usage(shown)
        except ValueError as unreadable:
            raise RuntimeFailed(str(unreadable)) from None

    def caps(self, person: int, workspace: int | None) -> Caps:
        shown = self._systemctl(
            "show", "-p", "MemoryMax", "-p", "TasksMax", limits.slice_of(person, workspace)
        )
        try:
            return limits.read_caps(shown)
        except ValueError as unreadable:
            raise RuntimeFailed(str(unreadable)) from None

    def drop_limits(self, person: int, workspace: int | None) -> None:
        # Only revert: a slice stopped with a container running in it stops the container.
        self._systemctl("revert", limits.slice_of(person, workspace))

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._podman("version", timeout=30)
                return
            except (RuntimeUnavailable, RuntimeFailed) as error:
                if time.monotonic() > deadline:
                    raise RuntimeUnavailable(str(error)) from None
            time.sleep(READY_POLL_SECONDS)

    def exists(self, name: str) -> bool:
        proc = self._podman("container", "exists", name, check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == MISSING:
            return False
        raise RuntimeUnavailable(proc.stderr.decode("utf-8", "replace").strip())

    def running(self, name: str) -> bool:
        proc = self._podman("container", "inspect", "--format", "{{.State.Running}}", name)
        return proc.stdout.decode().strip() == "true"

    def create(self, spec: Spec) -> None:
        labels = [
            f"--label={LABEL_WORKSPACE}={spec.workspace}",
            f"--label={LABEL_PERSON}={spec.person}",
            *([f"--label={LABEL_INSTALL}={spec.install}"] if spec.install else []),
        ]
        ids = self._ids(spec.slot, spec.uid, spec.gid)  # refuses what is wrong before anything
        # is made
        if spec.image_id and not builder.is_image_id(spec.image_id):
            raise RuntimeFailed("the id of the image is not an id")
        if self.exists(spec.name):
            raise RuntimeFailed(NAME_TAKEN.format(name=spec.name))
        if self._podman("volume", "exists", spec.name, check=False).returncode != 0:
            self._podman("volume", "create", *labels, spec.name)
        else:
            self._own_volume(spec)
        self.ensure_account(spec.person, spec.slot)
        mapped = spec.uid != 0 or spec.gid != 0
        if mapped:
            self._prepare_folders(spec)
        try:
            self._podman(
                "run",
                "--detach",
                f"--name={spec.name}",
                f"--hostname={spec.name}",
                *labels,
                "--init",
                f"--network={self.config.network}",
                # In the workspace's slice, which holds the limits (limits.py). Podman gives a
                # container a limit on processes of its own unless it is told there is none; the
                # slice's TasksMax is the limit, and infinity where nobody set one.
                *(
                    [f"--cgroup-parent={limits.workspace_slice(spec.person, spec.workspace)}"]
                    if self.config.slices
                    else []
                ),
                "--pids-limit=-1",
                # A container nested in the workspace gets a /proc and a hostname of its own.
                UNMASK_PROC,
                f"--security-opt=seccomp={self.config.tools_dir}/{SECCOMP}",
                *ids,
                *([f"--user={spec.user}"] if spec.user else []),
                *(f"--group-add={g}" for g in spec.groups),
                *(f"--env={k}={v}" for k, v in spec.env.items()),
                f"--volume={spec.name}:{VOLUME_MOUNT}",
                # An anonymous volume, made and attached in this run and removed with the container.
                f"--volume={TMP_MOUNT}",
                *self._mounts(spec.person, owned=mapped),
                *self._root_podman_state(),
                # Where the process starts is the volume's mount, which exists and is the user's;
                # the image's own working folder may be one the user cannot enter.
                f"--workdir={VOLUME_MOUNT}",
                # The image was pulled, or is here already; nothing is pulled implicitly.
                *(["--pull=never"] if spec.image or spec.image_id else []),
                # The image's own entrypoint is not run: the process is rcman's.
                f"--entrypoint={TOOLS_MOUNT}/bin/busybox",
                spec.image_id or spec.image or self.config.image,
                "sleep",
                "2147483647",
                timeout=IMAGE_CREATE_TIMEOUT if spec.image or spec.image_id else PODMAN_TIMEOUT,
            )
            self._own_tmp(spec.name)
            self._folders(spec.name)
        except RuntimeFailed:
            # No container is left that was not made whole: Podman makes one before it starts it,
            # and one whose start failed, or whose /tmp or folders were not made so, would be
            # started, or taken for the workspace's, by a restore. The volume stays, with what is
            # in it. RuntimeUnavailable is not caught: no removal can reach Podman then either.
            self._discard_unmade(spec.name)
            raise

    def _discard_unmade(self, name: str) -> None:
        """Remove the container ``create`` made and could not finish; what Podman says when it
        cannot is logged, and the error that ``create`` raises is its own."""
        try:
            removed = self._remove_container(name, check=False)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning(
                "the container %s that was not made whole was not removed: %s", name, failed
            )
            return
        if removed.returncode != 0:
            said = removed.stderr.decode("utf-8", "replace").strip()
            log.warning("the container %s that was not made whole was not removed: %s", name, said)

    def _own_tmp(self, name: str) -> None:
        """Make the container's TMP_MOUNT its root's and everyone's to write in (``1777``),
        whoever Podman made the volume's, as the container's root, even where the ids are swapped
        and the workspace runs as its user, with the mounted busybox. First, after the run, as
        that root: TMP_MOUNT is a folder and not a link, or RuntimeFailed (TMP_REFUSED, on the
        check's own answer alone; Podman's exec failing is what it said) before
        anything is changed, which would reach where a link leads. RuntimeFailed
        with what was said when it cannot be made so."""
        found = self._podman(
            "exec", "--user=0", name,
            *_busybox("sh"), "-c", '[ -d "$1" ] && [ ! -L "$1" ]', "sh", TMP_MOUNT,
            check=False,
        )  # fmt: skip
        if found.returncode == TMP_CHECK_REFUSED:
            raise RuntimeFailed(TMP_REFUSED.format(path=TMP_MOUNT))
        if found.returncode != 0:
            said = found.stderr.decode("utf-8", "replace").strip()
            raise RuntimeFailed(said or "podman exec failed")
        for argv in (
            [*_busybox("chown"), "0:0", TMP_MOUNT],
            [*_busybox("chmod"), "1777", TMP_MOUNT],
        ):
            self._podman("exec", "--user=0", name, *argv)

    def _own_volume(self, spec: Spec) -> None:
        """A volume of the name that is there already is the workspace's own only if its labels
        say it is for the person and for this database (``LABEL_INSTALL``): it is the work of
        whoever it was made for, and ids come round again in a database restored from a
        backup. The labels are read from the volume and compared here: a filter of ``volume
        ls`` is Podman's to interpret, and a volume that does not match must never pass."""
        found = self._podman("volume", "inspect", "--format", "{{json .Labels}}", spec.name)
        try:
            labels = json.loads(found.stdout.decode("utf-8", "replace") or "null") or {}
        except ValueError:
            labels = {}
        wanted = {LABEL_PERSON: str(spec.person)}
        if spec.install:
            wanted[LABEL_INSTALL] = spec.install
        if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in wanted.items()):
            raise RuntimeFailed(VOLUME_OF_ANOTHER.format(name=spec.name))

    def _swap(self, base: int, own: int) -> list[str]:
        """The lines of a uid map (or gid map) of the person's range in which the container's
        id ``own`` is the host id root has in the person's ordinary map, and the container's
        root is the host id ``own`` has there: the two swap, every other id stays. ``own`` is
        an id of the range other than root's; a stretch that would be empty is left out."""
        stretches = [
            (0, base + own, 1),
            (1, base + 1, own - 1),
            (own, base, 1),
            (own + 1, base + own + 1, self.config.range - own - 1),
        ]
        return [f"{inside}:{outside}:{length}" for inside, outside, length in stretches if length]

    def _ids(self, slot: int, uid: int = 0, gid: int = 0) -> list[str]:
        """The uid and gid maps of the person's range. Ordinarily the container's root is
        the range's first host id; with ``uid`` or ``gid`` not 0 the ids swap with root's
        (``_swap``), so that the user of that id owns what root owns in the person's other
        workspaces: the volume, the shared credentials file and the git folder."""
        if slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        size = self.config.range
        if not (0 <= uid < size and 0 <= gid < size):
            raise RuntimeFailed("the user's ids are beyond the range of ids a workspace has")
        base = slot * size
        return [
            *(f"--uidmap={m}" for m in (self._swap(base, uid) if uid else [f"0:{base}:{size}"])),
            *(f"--gidmap={m}" for m in (self._swap(base, gid) if gid else [f"0:{base}:{size}"])),
        ]

    def _isolated(self, slot: int) -> list[str]:
        """What every container a person's programs run in has: an init, the network
        with no way to the host's loopback, and the person's uid range."""
        return ["--init", f"--network={self.config.network}", *self._ids(slot)]

    def _prepare_folders(self, spec: Spec) -> None:
        """Make, as root of the person's ordinary map, the folders of the volume that Podman
        would otherwise make for the mounts below them as the container's root, which in a
        swapped map is not the user: the user could not write beside a mount. It also makes
        the volume's root theirs alone."""
        folders = [
            f"{VOLUME_MOUNT}/{WORK}",
            f"{VOLUME_MOUNT}/{CONFIG}",
            f"{VOLUME_MOUNT}/{HOME}/{GIT_HOME}".rsplit("/", 1)[0],
        ]
        self._podman(
            "run",
            "--rm",
            "--network=none",
            *self._ids(spec.slot),
            f"--volume={spec.name}:{VOLUME_MOUNT}",
            f"--volume={self.config.tools_dir}:{TOOLS_MOUNT}:ro",
            self.config.image,
            *_busybox("sh"),
            "-c",
            f'mkdir -p "$@" && chmod 700 {VOLUME_MOUNT}',
            "sh",
            *folders,
        )

    def _mounts(self, person: int, owned: bool = False) -> list[str]:
        """The person's credentials file and git folder, bound by their paths, as the
        volume's ``subpath`` option is refused by the Podman this runs on; the tools; the
        Claude Code store; and the tmpfs for the runtime folder, the user's if ``owned``."""
        found = self._podman(
            "volume", "inspect", "--format", "{{.Mountpoint}}", account_volume(person)
        )
        mountpoint = found.stdout.decode().strip()
        if not mountpoint.startswith("/"):
            raise RuntimeFailed(f"the volume {account_volume(person)} has no mountpoint")
        credentials = f"{VOLUME_MOUNT}/{CONFIG}/{CREDENTIALS}"
        git = f"{VOLUME_MOUNT}/{HOME}/{GIT_HOME}"
        return [
            f"--volume={mountpoint}/{CREDENTIALS}:{credentials}",
            f"--volume={mountpoint}/{GIT_DIR}:{git}",
            f"--volume={self.config.tools_dir}:{TOOLS_MOUNT}:ro",
            f"--volume={self.config.claude_store}:{CLAUDE_MOUNT}:ro",
            # ``U`` makes the tmpfs the user's: uid= and gid= are refused where this runs
            f"--tmpfs={RUN_MOUNT}:mode=0700{',U' if owned else ''}",
        ]

    def _root_podman_state(self) -> list[str]:
        return [f"--tmpfs={ROOT_PODMAN_STATE}:mode=0700"]

    def _folders(self, name: str) -> None:
        layout = self.layout(name)
        made = self.exec(
            name, [*layout.tool("mkdir"), "-p", layout.work, layout.config, layout.home]
        )
        if made.code != 0:
            raise RuntimeFailed(made.err.decode("utf-8", "replace").strip())
        self.exec(name, [*layout.tool("chmod"), "700", VOLUME_MOUNT])

    def start(self, name: str) -> None:
        self._podman("start", name)

    def stop(self, name: str) -> None:
        self._podman("stop", f"--time={STOP_TIMEOUT_SECONDS}", name)

    def discard(self, name: str) -> None:
        self._remove_container(name)

    def remove(self, name: str) -> None:
        self.discard(name)
        if self._podman("volume", "exists", name, check=False).returncode == 0:
            self._podman("volume", "rm", "--force", name)

    def pull(
        self,
        image: str,
        progress: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        if registry_host(image) == "localhost":
            # Podman would read the name as the registry on this machine's own loopback: the
            # image is in rcman's storage, or it is nowhere.
            if self._podman("image", "exists", image, check=False).returncode != 0:
                raise RuntimeFailed(f"{image} is not in rcman's storage")
            if progress is not None:
                progress(IN_STORAGE)
            return
        failure = self._pull(image, progress, cancel)
        if failure is None:
            return
        # An image the runtime has needs no registry: one built here, or loaded, is used as it
        # is when its name cannot be pulled.
        if self._podman("image", "exists", image, check=False).returncode == 0:
            if progress is not None:
                progress(USING_LOCAL_IMAGE)
            return
        raise RuntimeFailed(failure)

    def _pull(
        self,
        image: str,
        progress: Callable[[str], None] | None,
        cancel: threading.Event | None,
    ) -> str | None:
        """Pull ``image``; None when it was pulled, else what podman said when it exited
        with a failure. A stop, a timeout and an unreachable podman raise."""
        try:
            proc = subprocess.Popen(
                self._argv("pull", image),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=podman_env(),
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        tail: deque[str] = deque(maxlen=PULL_TAIL_LINES)
        deadline = time.monotonic() + PULL_TIMEOUT
        waiting = selectors.DefaultSelector()
        waiting.register(proc.stdout, selectors.EVENT_READ)
        pending = b""
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeFailed("the pull was stopped")
                if time.monotonic() > deadline:
                    raise RuntimeFailed("podman pull did not finish in time")
                if waiting.select(PULL_POLL_SECONDS):
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    *lines, pending = re.split(rb"[\r\n]+", pending + chunk)
                    for line in lines:
                        self._pulled_line(line, tail, progress)
                elif proc.poll() is not None:
                    break
            self._pulled_line(pending, tail, progress)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            waiting.close()
            proc.stdout.close()
        said = "\n".join(tail)
        if code == UNAVAILABLE and SERVICE_UNREACHABLE in said.lower():
            raise RuntimeUnavailable(said)
        return None if code == 0 else said or "podman pull failed"

    @staticmethod
    def _pulled_line(raw: bytes, tail: deque, progress) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            tail.append(line)
            if progress is not None:
                progress(line)

    def image_facts(self, image: str, cancel: threading.Event | None = None) -> ImageFacts:
        inspected = json.loads(
            self._podman("image", "inspect", "--format", "{{json .}}", image).stdout
        )
        config = inspected.get("Config") or {}
        env = dict(item.partition("=")[::2] for item in (config.get("Env") or []))
        name = f"{PROBE_PREFIX}{uuid.uuid4().hex}"
        self._podman(
            "create",
            f"--name={name}",
            f"--label={LABEL_PROBE}=1",
            "--network=none",
            "--pull=never",
            "--entrypoint=/bin/true",  # never run; create wants some command
            image,
        )
        try:
            passwd, group = (self._copied(name, f"/etc/{f}", cancel) for f in ("passwd", "group"))
        finally:
            self._remove_container(name, check=False)
        found = str(inspected.get("Id") or "").removeprefix("sha256:")
        digest = str(inspected.get("Digest") or "")
        return ImageFacts(
            passwd=passwd,
            group=group,
            user=inspected.get("User") or config.get("User") or "",
            env=env,
            labels=_labels_of(inspected),
            id=found if builder.is_image_id(found) else "",
            # only an image that came from a registry has a digest to pull it by
            digest=digest if inspected.get("RepoDigests") and DIGEST.match(digest) else "",
        )

    def has_image(self, image: str) -> bool:
        return self._podman("image", "exists", image, check=False).returncode == 0

    def collect_images(self, keep_ids: set[str], keep_names: set[str]) -> list[str]:
        # Whole repositories of the default and builder images, every tag: an upgrade builds the
        # next ones under tags these settings do not name yet. Names are compared as Podman lists
        # them, fully qualified.
        repositories = {
            repository_of(qualified_name(n)) for n in (self.config.image, self.config.builder_image)
        }
        wanted = {qualified_name(n) for n in keep_names}
        listed = json.loads(self._podman("images", "--format=json").stdout or b"[]") or []
        removed: list[str] = []
        young = time.time() - IMAGE_GRACE_SECONDS
        for entry in listed:
            image = str(entry.get("Id") or "").removeprefix("sha256:")
            names = [*(entry.get("Names") or []), *(entry.get("RepoTags") or [])]
            names = list(dict.fromkeys(names))
            if image in keep_ids or any(
                qualified_name(n) in wanted or repository_of(qualified_name(n)) in repositories
                for n in names
            ):
                continue
            if not names and float(entry.get("Created") or 0) > young:
                continue  # no name and not old: a layer of a build that may be under way
            # By name where there are names: removing an image by its id when it has more than
            # one is refused, and a name that is the last one takes the image with it.
            for target in names or [image]:
                if not (target == image and not builder.is_image_id(image)):
                    gone = self._podman("image", "rm", target, check=False)
                    if gone.returncode == 0:
                        removed.append(target)
                    else:
                        log.info("the image %s stays: %s", target, self._said(gone.stderr, ""))
        return removed

    def default_image_id(self) -> str:
        found = self._podman(
            "image", "inspect", "--format", "{{.Id}}", self.config.image, check=False
        )
        image = found.stdout.decode("utf-8", "replace").strip().removeprefix("sha256:")
        return image if found.returncode == 0 and builder.is_image_id(image) else ""

    def _copied(self, container: str, path: str, cancel: threading.Event | None = None) -> str:
        """A file of an image's, read from a container that was made and not run; empty where
        the image has none, or has it not as a file, or one too big to be a user database.
        Reading stops, and podman with it, at PODMAN_TIMEOUT or when ``cancel`` is set, which
        are a RuntimeFailed; so is any failure of podman but the file not being there."""
        limit = MAX_ID_FILE_BYTES + TAR_OVERHEAD_BYTES
        try:
            proc = subprocess.Popen(
                self._argv("cp", f"{container}:{path}", "-"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=podman_env(),
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        data, said = b"", b""
        deadline = time.monotonic() + PODMAN_TIMEOUT
        waiting = selectors.DefaultSelector()
        waiting.register(proc.stdout, selectors.EVENT_READ, "out")
        waiting.register(proc.stderr, selectors.EVENT_READ, "err")
        try:
            while waiting.get_map():  # never more than the limit of the file is read
                if cancel is not None and cancel.is_set():
                    raise RuntimeFailed("reading the image was stopped")
                if time.monotonic() > deadline:
                    raise RuntimeFailed("podman cp did not finish in time")
                for key, _ in waiting.select(PULL_POLL_SECONDS):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        waiting.unregister(key.fileobj)
                    elif key.data == "out":
                        data += chunk
                        if len(data) > limit:
                            return ""
                    else:
                        said = (said + chunk)[:MAX_SAID_BYTES]
            code = proc.wait(timeout=max(deadline - time.monotonic(), PULL_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            raise RuntimeFailed("podman cp did not finish in time") from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            waiting.close()
            proc.stdout.close()
            proc.stderr.close()
        if code != 0:
            words = said.decode("utf-8", "replace").strip()
            if any(phrase in words.lower() for phrase in FILE_ABSENT):
                return ""
            raise RuntimeFailed(words or "podman cp failed")
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    reader = archive.extractfile(member) if member.isfile() else None
                    if reader is not None:
                        data = reader.read(MAX_ID_FILE_BYTES + 1)
                        return (
                            data.decode("utf-8", "replace")
                            if len(data) <= MAX_ID_FILE_BYTES
                            else ""
                        )
        except tarfile.TarError:
            pass
        return ""

    # --- building an image ------------------------------------------------------------------

    def remove_build(self, workspace_id: int, image: bool = False) -> None:
        name = builder.builder_name(workspace_id)
        self._remove_container(name)
        if self._podman("volume", "exists", name, check=False).returncode == 0:
            self._podman("volume", "rm", "--force", name)
        if image:
            # Without --force: an image a container is made from is not taken from under it.
            self._podman("image", "rm", "--ignore", built_name(workspace_id))

    def _discard_build(self, workspace_id: int, image: bool) -> None:
        """``remove_build`` for the end of a build, which never hides how the build ended."""
        try:
            self.remove_build(workspace_id, image)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning(
                "the build of workspace %s could not be cleaned up: %s", workspace_id, failed
            )

    def builders(self) -> set[int]:
        found: set[int] = set()
        for call in (("ps", "--all"), ("volume", "ls")):
            proc = self._podman(*call, f"--filter=label={LABEL_BUILDER}", "--format=json")
            for entry in json.loads(proc.stdout or b"[]") or []:
                with contextlib.suppress(TypeError, ValueError):
                    found.add(int((entry.get("Labels") or {}).get(LABEL_BUILDER)))
        return found

    def built_images(self) -> dict[int, str]:
        proc = self._podman(
            "images", f"--filter=reference=localhost/{BUILT_REPOSITORY}/*", "--format=json"
        )
        found: dict[int, str] = {}
        for entry in json.loads(proc.stdout or b"[]") or []:
            for name in [*(entry.get("Names") or []), *(entry.get("RepoTags") or [])]:
                workspace = workspace_of_built_name(name)
                if workspace is not None:
                    found[workspace] = name
        return found

    def build_image(
        self,
        request: BuildRequest,
        progress: Callable[[str], None],
        cancel: threading.Event | None,
        vet: Callable[[Mapping[str, str]], None],
    ) -> str:
        ids = self._ids(request.slot)  # refuses what is wrong before anything is made
        name = builder.builder_name(request.id)
        with self._staging_lock:
            self._active_builds += 1
        try:
            return self._build_image(request, name, ids, progress, cancel, vet)
        finally:
            with self._staging_lock:
                self._active_builds -= 1
            self._clean_staging()  # what is left, if no other build is under way

    def _build_image(self, request, name, ids, progress, cancel, vet) -> str:
        self.remove_build(request.id, image=True)  # what an earlier build of this id left
        kept = False
        staging: Path | None = None
        loaded: str | None = None  # the id of an image this build put in the storage
        try:
            labels = {LABEL_BUILDER: str(request.id), LABEL_PERSON: str(request.person)}
            self._podman("volume", "create", *(f"--label={k}={v}" for k, v in labels.items()), name)
            self._podman(
                *builder.run_argv(
                    request.id,
                    labels,
                    ids,
                    self.config.builder_image,
                    network=self.config.network,
                    cgroup_parent=(
                        limits.person_slice(request.person) if self.config.slices else None
                    ),
                ),
                timeout=IMAGE_CREATE_TIMEOUT,
            )
            progress("Copying the repository into the builder")
            self._copy_in(request, name, cancel)
            self._build(request, name, progress, cancel)
            found, image_id, size = self._inspected(name)
            vet(found)  # before the long copy
            staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=self._scratch()))
            archive = self._stage(request.id, image_id, size, staging, progress, cancel)
            with self._naming:
                before = self._own_images()
                if not self._image_exists(image_id):
                    loaded = image_id  # set before the load: a load that is stopped may have begun
                self._load(archive, image_id, cancel)
                self._hold_names(before, image_id, loaded is not None)
                self._podman("tag", image_id, built_name(request.id))
            kept = True
            return built_name(request.id)
        finally:
            self._discard_build(request.id, image=not kept)
            if loaded is not None and not kept:
                self._discard_loaded(loaded)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _scratch(self) -> Path:
        """The folder images are staged in, made owner-only if it is not there."""
        base = self.config.scratch_dir or Path(tempfile.gettempdir())
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        return base

    def _inspected(self, name: str) -> tuple[dict[str, str], str, int]:
        """What the builder says of the image it built: its labels, its id and its size. It is
        the builder's word, so it is read within a limit, and the id is not passed on to podman
        unless it is the whole of an id."""
        try:
            code, out, err = _run_keeping_tail(
                self._argv(
                    "exec", name, "podman", "image", "inspect", "--format", "{{json .}}",
                    builder.IN_BUILDER_IMAGE,
                ),
                None, PODMAN_TIMEOUT, podman_env(), None, builder.MAX_INSPECT_BYTES + 1,
            )  # fmt: skip
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        except subprocess.TimeoutExpired:
            raise RuntimeFailed("podman image inspect did not finish in time") from None
        if code != 0:
            raise RuntimeFailed(self._said(err, "the image could not be inspected"))
        if len(out) > builder.MAX_INSPECT_BYTES:
            raise BuildFailed(kind="build_image_refused", reason="its description is too big")
        try:
            inspected = json.loads(out)
            labels = _labels_of(inspected)
            image_id = str(inspected["Id"]).removeprefix("sha256:")
            size = int(inspected.get("Size") or 0)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise BuildFailed(
                kind="build_image_refused", reason="its description cannot be read"
            ) from None
        if not builder.is_image_id(image_id):
            raise BuildFailed(kind="build_image_refused", reason="it has no id an image has")
        return labels, image_id, size

    def _image_exists(self, image_id: str) -> bool:
        return self._podman("image", "exists", image_id, check=False).returncode == 0

    def _discard_loaded(self, image_id: str) -> None:
        """Remove an image this build loaded and did not keep, by its id, if no other name holds
        it (without force, so one that does, or that a container uses, stays)."""
        try:
            self._podman("image", "rm", "--ignore", image_id, check=False)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning("the image %s of a build could not be removed: %s", image_id, failed)

    def _pumped(
        self, source: list[str], sink: list[str], limit: int, too_big: str,
        cancel: threading.Event | None, seconds: float, progress=None,
    ) -> builder.Pumped:  # fmt: skip
        """``builder.pump`` of two podman commands, its stops as the build's failures."""
        try:
            return builder.pump(
                source, sink, env=podman_env(), limit=limit, cancel=cancel, seconds=seconds,
                progress=progress,
            )  # fmt: skip
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        except builder.PumpStopped as stopped:
            if stopped.reason == builder.CANCELLED:
                raise RuntimeFailed("the build was stopped") from None
            raise BuildFailed(
                kind="build_timeout" if stopped.reason == builder.TIMED_OUT else too_big
            ) from None

    @staticmethod
    def _said(raw: bytes, fallback: str) -> str:
        return raw.decode("utf-8", "replace").strip() or fallback

    @staticmethod
    def _copy_failed(pumped: builder.Pumped, source_default: str, sink_default: str) -> None:
        """Raise why a copy failed, if it did. When the sink failed and the source was ended by
        the pump (or went well), the sink's failure is the cause: a sink that has no space is a
        :class:`BuildFailed` whose tail says so, for ``builder.explain``. A source that failed
        by itself comes first."""
        if pumped.sink_code != 0 and pumped.source_code <= 0:
            said = PodmanRuntime._said(pumped.sink_err, sink_default)
            if "no space left on device" in said.lower():
                raise BuildFailed(said)
            raise RuntimeFailed(said)
        if pumped.source_code != 0:
            raise RuntimeFailed(PodmanRuntime._said(pumped.source_err, source_default))
        if pumped.sink_code != 0:
            raise RuntimeFailed(PodmanRuntime._said(pumped.sink_err, sink_default))

    def _copy_in(self, request: BuildRequest, name: str, cancel: threading.Event | None) -> None:
        """The repository, but for its git folder, as a tar archive from the workspace into the
        builder; the files are the builder's own, and symbolic links stay links."""
        layout = self.layout(request.source)
        self._podman("exec", name, "mkdir", "-p", builder.REPOSITORY_FOLDER)
        source = builder.source_argv(layout.shell, layout.work, [*layout.tool("tar"), "-c"])
        pumped = self._pumped(
            self._argv("exec", request.source, *source),
            self._argv(
                "exec",
                "--interactive",
                name,
                *builder.sink_argv(["tar"], builder.REPOSITORY_FOLDER),
            ),
            builder.MAX_CONTEXT_BYTES, "build_context_too_big", cancel, builder.COPY_SECONDS,
        )  # fmt: skip
        self._copy_failed(
            pumped, "the repository could not be read", "the repository could not be copied"
        )
        self._podman(
            "exec", "--interactive", name, "sh", "-c", 'rm -f "$1" && cat > "$1"', "sh",
            f"{builder.REPOSITORY_FOLDER}/{request.config_path}",
            stdin=request.config_text.encode(),
        )  # fmt: skip

    def _build(
        self, request: BuildRequest, name: str, progress, cancel: threading.Event | None
    ) -> None:
        """Run the devcontainer CLI in the builder, passing on the steps it takes, until it ends,
        ``builder.BUILD_SECONDS`` have passed, or ``cancel`` is set. Anything but its success is
        a :class:`BuildFailed` with the end of what it said."""
        lines = builder.ProgressLines(progress)
        try:
            proc = subprocess.Popen(
                self._argv("exec", name, *builder.cli_argv(request.config_path)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=podman_env(),
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        out = bytearray()
        deadline = time.monotonic() + builder.BUILD_SECONDS
        waiting = selectors.DefaultSelector()
        waiting.register(proc.stdout, selectors.EVENT_READ, "out")
        waiting.register(proc.stderr, selectors.EVENT_READ, "err")
        try:
            while waiting.get_map():
                if cancel is not None and cancel.is_set():
                    raise RuntimeFailed("the build was stopped")
                if time.monotonic() > deadline:
                    lines.close()
                    raise BuildFailed(lines.tail(), kind="build_timeout")
                for key, _ in waiting.select(builder.BUILD_POLL_SECONDS):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        waiting.unregister(key.fileobj)
                    elif key.data == "err":
                        lines.feed(chunk)
                    else:
                        out += chunk
                        del out[: max(len(out) - builder.MAX_OUTCOME_BYTES, 0)]
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            waiting.close()
            proc.stdout.close()
            proc.stderr.close()
        lines.close()
        if cancel is not None and cancel.is_set():
            raise RuntimeFailed("the build was stopped")
        if code != 0 or not self._succeeded(bytes(out)):
            raise BuildFailed(lines.tail())

    @staticmethod
    def _succeeded(out: bytes) -> bool:
        """Whether the CLI's last line of standard output, one JSON object, says it succeeded."""
        lines = [line for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
        try:
            return bool(lines) and json.loads(lines[-1]).get("outcome") == "success"
        except (ValueError, AttributeError, RecursionError):
            return False

    def _stage(
        self, workspace_id: int, image_id: str, size: int, staging: Path, progress, cancel
    ) -> Path:
        """The image, by its id, saved in the builder and written to a file of ``staging``, then
        checked and made again from the parts that were checked (``builder.repack_archive``)
        before anything of it is loaded: loading applies the names an archive carries, and the
        builder's word is not to be taken for them. The file that is returned is the one rcman
        wrote; the saved one is removed. What was copied and how long it took is logged."""
        name = builder.builder_name(workspace_id)
        archive = staging / "image.tar"
        began = time.monotonic()
        last = [began]
        megabyte = 1024 * 1024

        def counted(sent: int) -> None:
            if time.monotonic() - last[0] >= builder.PROGRESS_SECONDS:
                last[0] = time.monotonic()
                progress(f"Copying the image: {sent // megabyte} of {size // megabyte} MB")

        pumped = self._pumped(
            self._argv(
                "exec", name, "podman", "save", "--format", builder.ARCHIVE_FORMAT, image_id
            ),
            ["sh", "-c", 'exec cat > "$1"', "sh", str(archive)],
            builder.MAX_IMAGE_BYTES, "build_image_too_big", cancel, builder.COPY_SECONDS, counted,
        )  # fmt: skip
        self._copy_failed(pumped, "the image could not be saved", "the image could not be stored")
        log.info(
            "workspace %s: the image was copied out of its builder, %d bytes in %.1f s",
            workspace_id,
            pumped.sent,
            time.monotonic() - began,
        )
        fresh = staging / "fresh.tar"
        try:
            builder.repack_archive(archive, image_id, fresh, stop=cancel.is_set if cancel else None)
        except builder.ArchiveRefused as refused:
            raise BuildFailed(kind="build_image_refused", reason=str(refused)) from None
        except builder.PumpStopped:
            raise RuntimeFailed("the build was stopped") from None
        except OSError as failed:
            if failed.errno == errno.ENOSPC:
                raise BuildFailed("no space left on device") from None
            raise RuntimeFailed("the image could not be stored") from None
        finally:
            archive.unlink(missing_ok=True)  # only the archive rcman wrote is kept
        return fresh

    def _load(self, archive: Path, image_id: str, cancel: threading.Event | None) -> None:
        """``podman load`` of the checked archive, until it ends, ``builder.COPY_SECONDS`` have
        passed, or ``cancel`` is set; then the image of that id must be there."""
        try:
            proc = subprocess.Popen(
                self._argv("load", "-i", str(archive)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=podman_env(),
            )
        except FileNotFoundError:
            raise RuntimeUnavailable(f"{self.config.podman} is not installed") from None
        deadline = time.monotonic() + builder.COPY_SECONDS
        try:
            while True:
                try:
                    _, err = proc.communicate(timeout=builder.BUILD_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        raise RuntimeFailed("the build was stopped") from None
                    if time.monotonic() > deadline:
                        raise BuildFailed(kind="build_timeout") from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            raise RuntimeFailed(self._said(err, "the image could not be loaded"))
        if cancel is not None and cancel.is_set():
            raise RuntimeFailed("the build was stopped")
        found = self._podman("image", "inspect", "--format", "{{.Id}}", image_id, check=False)
        if found.stdout.decode("utf-8", "replace").strip().removeprefix("sha256:") != image_id:
            raise RuntimeFailed("the image was loaded under another id than it was saved with")

    def _own_images(self) -> dict[str, str]:
        """Every name of rcman's own that an image has - the default workspace image, the
        builder's, and each image built for a workspace - with the id of the image that has it."""
        own = {self.config.image, self.config.builder_image}
        own |= {f"{n}:latest" for n in own if ":" not in n.rsplit("/", 1)[-1]}
        proc = self._podman("images", "--format=json")
        found: dict[str, str] = {}
        for entry in json.loads(proc.stdout or b"[]") or []:
            image = str(entry.get("Id") or "").removeprefix("sha256:")
            for name in [*(entry.get("Names") or []), *(entry.get("RepoTags") or [])]:
                if name in own or workspace_of_built_name(name) is not None:
                    found[name] = image
        return found

    def _hold_names(self, before: dict[str, str], image_id: str, new: bool) -> None:
        """After ``podman load``: every name of rcman's own is on the image it was on before, and
        an image the load put in the storage has no name at all (the archive was written with
        none, so any it has was applied by something else). What was moved is put back, by id,
        and what was named is unnamed; then the build is refused. Names that the load had no part
        in - a workspace's image another build named meanwhile, which the lock keeps out - are
        not read as moved."""
        after = self._own_images()
        wrong = {n: before.get(n) for n, i in after.items() if before.get(n) != i}
        if new:
            carried = [
                name
                for entry in json.loads(self._podman("images", "--format=json").stdout or b"[]")
                if str(entry.get("Id") or "").removeprefix("sha256:") == image_id
                for name in [*(entry.get("Names") or []), *(entry.get("RepoTags") or [])]
            ]
        else:
            carried = []
        if not wrong and not carried:
            return
        for name in dict.fromkeys([*carried, *(n for n, o in wrong.items() if o is None)]):
            self._podman("untag", image_id, name, check=False)
        for name, old in wrong.items():
            if old is not None:
                self._podman("tag", old, name, check=False)
        raise BuildFailed(
            kind="build_image_refused", reason="loading it changed the name of an image of rcman's"
        )

    def _clean_staging(self) -> None:
        """Remove the staging folders of images that an earlier rcman left, or a build left that
        was killed: all of them where no build is under way, and where one is, those no build can
        still be using, which have outlasted the longest a build's copy and load may take."""
        base = self.config.scratch_dir or Path(tempfile.gettempdir())
        oldest = time.time() - 3 * builder.COPY_SECONDS
        with self._staging_lock:
            idle = self._active_builds == 0
            for folder in base.glob(f"{STAGING_PREFIX}*"):
                with contextlib.suppress(OSError):
                    if not folder.is_dir() or folder.is_symlink():
                        continue
                    if idle or folder.stat().st_mtime < oldest:
                        shutil.rmtree(folder, ignore_errors=True)

    def clean_probes(self) -> None:
        self._clean_staging()
        found = self._podman("ps", "--all", f"--filter=label={LABEL_PROBE}", "--format=json")
        for entry in json.loads(found.stdout or b"[]") or []:
            for name in entry.get("Names") or []:
                self._remove_container(name, check=False)
        self._clean_volumes()

    def _clean_volumes(self) -> None:
        """Remove the anonymous volumes nothing uses: the ones an image declares, or a
        workspace's TMP_MOUNT, made with a container that was removed without them. Every other
        volume rcman makes is named and labelled, so a volume with no label and the random name
        Podman gives its anonymous ones, that no container has, is one of those. A workspace's
        TMP_MOUNT is attached in the same ``run`` that makes it, so it is never one that no
        container has while its container exists, running or stopped."""
        found = self._podman("volume", "ls", "--filter=dangling=true", "--format=json")
        for entry in json.loads(found.stdout or b"[]") or []:
            name = str(entry.get("Name") or "")
            if ANONYMOUS_VOLUME.match(name) and not entry.get("Labels"):
                self._podman("volume", "rm", name, check=False)

    def labelled(self) -> dict[str, dict[str, str]]:
        proc = self._podman("ps", "--all", f"--filter=label={LABEL_WORKSPACE}", "--format=json")
        found = {}
        for entry in json.loads(proc.stdout or b"[]") or []:
            names = entry.get("Names") or []
            if names:
                found[names[0]] = dict(entry.get("Labels") or {})
        return found

    def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float = PODMAN_TIMEOUT,
        cwd: str | None = None,
        keep: int | None = None,
        again: bool = False,
    ) -> Result:
        args = [
            "exec",
            *(["--interactive"] if stdin is not None else []),
            *([f"--workdir={cwd}"] if cwd else []),
            name,
            *marked_exec(argv),
        ]
        mark = EXEC_MARK.encode()
        conmon_tries, lost_tries = 1, int(again)
        while True:
            try:
                if keep is None:
                    proc = subprocess.run(
                        self._argv(*args),
                        input=stdin,
                        stdin=None if stdin is not None else subprocess.DEVNULL,
                        capture_output=True,
                        env=podman_env(),
                        timeout=timeout,
                    )
                    code, out, err = proc.returncode, proc.stdout, proc.stderr
                else:
                    code, out, err = _run_keeping_tail(
                        self._argv(*args), stdin, timeout, podman_env(), None, keep
                    )
            except subprocess.TimeoutExpired:
                raise RuntimeFailed("podman exec did not finish in time") from None
            said = err.decode("utf-8", "replace")
            if code != 0 and CONMON_FAILED in said and conmon_tries:
                conmon_tries -= 1
                continue
            if code == UNAVAILABLE and SERVICE_UNREACHABLE in said.lower():
                raise RuntimeUnavailable(said.strip())
            if mark in err:
                # Only the exec's own, the first: what Podman's client wrote before it stays.
                at = err.index(mark)
                return Result(code, out, err[:at] + err[at + len(mark) :])
            # No mark: the program ran, and Podman lost what it wrote, unless Podman refused to
            # run it (UNAVAILABLE, its own words) or conmon could not start it; a kept tail may
            # have dropped the mark.
            lost = keep is None and code != UNAVAILABLE and CONMON_FAILED not in said
            if lost and again:
                if lost_tries:
                    lost_tries -= 1
                    continue
                raise RuntimeFailed("podman exec lost what the program wrote, twice")
            return Result(code, out, err)

    def channel(self, name: str) -> Channel:
        argv = self._argv("exec", "--interactive", name, f"{TOOLS_MOUNT}/bin/busybox", "sh", "-s")
        return Channel(argv, _busybox, RUN_MOUNT, env=podman_env())

    def terminal_argv(self, name: str, layout: Layout, pidfile: str) -> Launch:
        """An exec with a tty of the container's own, which the podman client, the pty's
        controlling process, passes everything to and resizes; no detach keys, so ^P^Q reach
        the shell."""
        exec_ = ["exec", "--interactive", "--tty", "--detach-keys=", f"--workdir={layout.work}"]
        argv = self._argv(*exec_, name, *terminal_command(layout, pidfile))
        return Launch([*_host_tool("setsid"), "--ctty", *argv], podman_env())

    # --- a person's Claude sign-in ----------------------------------------------------

    def _account_run(
        self, person: int, slot: int, script: str, *more: str, stdin: bytes | None = None
    ) -> bytes:
        """``script`` run by a container that removes itself, in the person's uid range
        with no network, their account volume at ACCOUNT_MOUNT, the credentials file's
        path as ``$1``, the git folder's as ``$2`` and ``more`` after them, with ``stdin``
        on its standard input; made labelled with the person if it is not there."""
        ids = self._ids(slot)
        volume = account_volume(person)
        if self._podman("volume", "exists", volume, check=False).returncode != 0:
            self._podman("volume", "create", f"--label={LABEL_PERSON}={person}", volume)
        proc = self._podman(
            "run",
            "--rm",
            *(["--interactive"] if stdin is not None else []),
            "--network=none",
            *ids,
            f"--volume={volume}:{ACCOUNT_MOUNT}",
            f"--volume={self.config.tools_dir}:{TOOLS_MOUNT}:ro",
            self.config.image,
            *_busybox("sh"),
            "-c",
            script,
            "sh",
            f"{ACCOUNT_MOUNT}/{CREDENTIALS}",
            f"{ACCOUNT_MOUNT}/{GIT_DIR}",
            *more,
            stdin=stdin,
        )
        return proc.stdout

    def ensure_account(self, person: int, slot: int) -> None:
        self._account_run(person, slot, ENSURE_SCRIPT)

    def read_account(self, person: int, slot: int) -> bytes:
        return self._account_run(person, slot, READ_SCRIPT)

    def clear_account(self, person: int, slot: int) -> None:
        self._account_run(person, slot, CLEAR_SCRIPT)

    def put_account(self, person: int, slot: int, name: str, data: bytes) -> None:
        _checked(name)
        self._account_run(person, slot, PUT_SCRIPT, name, stdin=data)

    def remove_account(self, person: int) -> None:
        volume = account_volume(person)
        if self._podman("volume", "exists", volume, check=False).returncode == 0:
            self._podman("volume", "rm", "--force", volume)

    def start_sign_in(self, person: int, slot: int, seconds: float) -> str:
        name = sign_in_name(person)
        self.end_sign_in(name)
        self.ensure_account(person, slot)
        self._podman(
            "run",
            "--rm",
            "--detach",
            f"--name={name}",
            f"--hostname={name}",
            f"--label={LABEL_PERSON}={person}",
            *self._isolated(slot),
            f"--tmpfs={VOLUME_MOUNT}:mode=0700",
            *self._mounts(person),
            self.config.image,
            f"{TOOLS_MOUNT}/bin/busybox",
            "sleep",
            str(int(seconds)),
        )
        self._folders(name)
        return name

    def end_sign_in(self, name: str) -> None:
        self._remove_container(name)

    def end_canary(self, name: str) -> None:
        self._remove_container(name)

    def run_canary(
        self, directory: Path, name: str, version: str, *, slot: int | None = None
    ) -> dict[str, dict]:
        from rcman import people  # not at the top: the people module needs the web framework

        if not directory.is_absolute() or any(c in str(directory) for c in ":,"):
            raise RuntimeFailed(f"{directory} is not a folder the canary can be given")
        ids = self._ids(people.BUILDER_SLOT if slot is None else slot)
        self.end_canary(name)
        proc = self._podman(
            "run",
            "--rm",
            f"--name={name}",
            f"--label={LABEL_CANARY}=1",
            f"--timeout={int(CANARY_SECONDS)}",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            *ids,
            f"--tmpfs={CANARY_TMP}:rw,mode=1777",
            f"--volume={directory}:{CANARY_CANDIDATE_MOUNT}:ro",
            f"--volume={HELPERS_DIR}:{CANARY_HELPERS_MOUNT}/{HELPERS_DIR.name}:ro",
            f"--workdir={CANARY_HELPERS_MOUNT}",
            f"--env=HOME={CANARY_TMP}",
            f"--env=TMPDIR={CANARY_TMP}",
            "--env=DISABLE_AUTOUPDATER=1",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            f"--env=PYTHONPATH={CANARY_HELPERS_MOUNT}",
            self.config.image,
            "python3",
            "-m",
            f"{HELPERS_DIR.name}.claude_canary",
            f"{CANARY_CANDIDATE_MOUNT}/{CANDIDATE}",
            version,
            check=False,
            timeout=CANARY_SECONDS + PODMAN_TIMEOUT,
        )
        return self._canary_report(proc)

    def _canary_report(self, proc) -> dict[str, dict]:
        """The checks the canary printed that the candidate fails; a RuntimeFailed where it
        printed anything else."""
        if proc.returncode != 0:
            raise RuntimeFailed(
                self._said(proc.stderr, "the canary did not finish")
                if proc.stderr.strip()
                else f"the canary ended with exit code {proc.returncode}"
            )
        return parsed_canary_report(proc.stdout)


def parsed_canary_report(raw: bytes) -> dict[str, dict]:
    """The JSON the canary program prints - the checks that failed, each with its report - or
    a RuntimeFailed where it is anything else."""
    try:
        report = json.loads(raw)
    except (ValueError, RecursionError):
        raise RuntimeFailed("the canary did not report") from None
    if not isinstance(report, dict) or not all(
        isinstance(name, str) and isinstance(entry, dict) for name, entry in report.items()
    ):
        raise RuntimeFailed("the canary reported something that is not a list of failed checks")
    return report


# --- the host -------------------------------------------------------------------------


def _host_tool(name: str) -> list[str]:
    found = shutil.which(name, path="/usr/bin:/bin")
    if found is None:
        raise RuntimeFailed(f"{name} is not installed")
    return [found]


class HostRuntime(Runtime):
    """A workspace is a folder under ``root``, holding what its volume would and a runtime
    folder, with its tmux socket in ``sockets``, whose path must stay short; a stopped
    one is marked so. Programs run on the host, as the user rcman runs as.

    A person's credentials file is in ACCOUNTS under ``root``, and a workspace's is a
    symbolic link to it, since nothing here can bind one file over another; a sign-in
    container is a folder like a workspace's with no labels, and ends only when
    ``end_sign_in`` removes it."""

    MARK_STOPPED = ".stopped"
    # A workspace whose container was discarded: its folder is its volume, which stays.
    MARK_DISCARDED = ".discarded"
    ACCOUNTS = ".accounts"

    def __init__(
        self, root: Path, sockets: Path, claude: Path, tmux_conf: Path, *, range: int = RANGE
    ):
        self.root, self.sockets, self.claude, self.tmux_conf = root, sockets, claude, tmux_conf
        self.id_range = range
        # There are no images here and no uids to map: the images a test plants, by name, what
        # was pulled, and the last spec each workspace was made from, so that a test can see
        # what reached ``create``.
        self.images: dict[str, ImageFacts] = {}
        # Image ids the storage has beside those of ``images`` (an image whose name moved on),
        # ids it has lost, and the id of the default image, which is empty until a test gives
        # one: no workspace is then pinned to an image.
        self.extra_ids: set[str] = set()
        # The images in ``images`` that were put there by something the runtime did for
        # someone (a pull, a build), which ``collect_images`` may remove.
        self.collectable: set[str] = set()
        self.lost_ids: set[str] = set()
        self.default_id = ""
        self.pulled: list[str] = []
        self.specs: dict[str, Spec] = {}
        # Builds: the workspaces that have a builder now, the image built for each, the builds
        # asked for, and what stands for the builder - a function of the request, the progress
        # function and the cancel event that returns the facts of the image it builds (labels
        # among them) or raises; none builds an image of no facts.
        self.builder_ids: set[int] = set()
        self.built: dict[int, str] = {}
        self.builds: list[BuildRequest] = []
        self.build_script: Callable[..., ImageFacts] | None = None
        # The canary: what stands for it where a test does not run the real one - a function
        # of the folder, the container's name and the version that returns the failed checks.
        self.canary_script: Callable[[Path, str, str], dict[str, dict]] | None = None
        # Limits: what is set on each slice now (by slice name), every set applied and every
        # slice dropped in order, the usage a test plants by slice, and an error to raise
        # instead of doing any of the three.
        self.limits: dict[str, Limits] = {}
        self.applied: list[tuple[str, Limits]] = []
        self.dropped: list[str] = []
        self.used: dict[str, Usage] = {}
        self.limits_error: Exception | None = None

    def layout(self, name: str) -> Layout:
        volume = self.root / name
        return Layout(
            work=str(volume / WORK),
            config=str(volume / CONFIG),
            home=str(volume / HOME),
            run=str(volume / "run"),
            tmux_socket=str(self.sockets / f"{name}.sock"),
            tmux=_host_tool("tmux")[0],
            tmux_conf=str(self.tmux_conf),
            claude=str(self.claude / "bin" / "claude"),
            claude_root=str(self.claude),
            shell=_host_tool("sh")[0],
            terminfo=None,
            path="/usr/local/bin:/usr/bin:/bin",
            user=pwd.getpwuid(os.getuid()).pw_name,
            tools={
                n: _host_tool(n)
                for n in (
                    "env",
                    "kill",
                    "cat",
                    "wc",
                    "rm",
                    "mkdir",
                    "chmod",
                    "mv",
                    "timeout",
                    "tar",
                )
            },
            kill_group=(*_host_tool("kill"), "-KILL", "--"),
        )

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self.root.is_dir():
            if time.monotonic() > deadline:
                raise RuntimeUnavailable(f"{self.root} is not there")
            time.sleep(READY_POLL_SECONDS)

    def exists(self, name: str) -> bool:
        if not self.root.is_dir():
            raise RuntimeUnavailable(f"{self.root} is not there")
        return (self.root / name).is_dir() and not (self.root / name / self.MARK_DISCARDED).exists()

    def running(self, name: str) -> bool:
        if not self.exists(name):
            raise RuntimeFailed(f"no such workspace: {name}")
        return not (self.root / name / self.MARK_STOPPED).exists()

    def create(self, spec: Spec) -> None:
        if spec.slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        if not (0 <= spec.uid < self.id_range and 0 <= spec.gid < self.id_range):
            raise RuntimeFailed("the user's ids are beyond the range of ids a workspace has")
        if spec.image and not spec.image_id and spec.image not in self.images:
            raise RuntimeFailed(f"{spec.image}: image not known")
        if spec.image_id and not self.has_image(spec.image_id):
            raise RuntimeFailed(f"{spec.image_id}: image not known")
        if self.exists(spec.name):
            raise RuntimeFailed(f"the workspace {spec.name} is there already")
        owner = self.root / spec.name / ".owner"  # kept when the container is discarded
        whose = f"{spec.person}:{spec.install}"
        if owner.exists() and owner.read_text() != whose:
            raise RuntimeFailed(VOLUME_OF_ANOTHER.format(name=spec.name))
        layout = self.layout(spec.name)
        for folder in (layout.work, layout.config, layout.home):
            Path(folder).mkdir(parents=True, exist_ok=True)
        self._link_account(spec.name, spec.person)
        labels = {LABEL_WORKSPACE: str(spec.workspace), LABEL_PERSON: str(spec.person)}
        (self.root / spec.name / ".labels").write_text(json.dumps(labels))
        owner.write_text(whose)
        (self.root / spec.name / self.MARK_DISCARDED).unlink(missing_ok=True)
        self.specs[spec.name] = spec
        self.start(spec.name)

    def _account_file(self, person: int) -> Path:
        return self.root / self.ACCOUNTS / str(person) / CREDENTIALS

    def _git_folder(self, person: int) -> Path:
        return self.root / self.ACCOUNTS / str(person) / GIT_DIR

    def _link_account(self, name: str, person: int) -> None:
        self.ensure_account(person, 1)
        layout = self.layout(name)
        credentials = Path(layout.config, CREDENTIALS)
        credentials.unlink(missing_ok=True)  # left by a container that was discarded
        credentials.symlink_to(self._account_file(person))
        link = Path(layout.home, GIT_HOME)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(self._git_folder(person))

    def start(self, name: str) -> None:
        Path(self.layout(name).run).mkdir(mode=0o700, exist_ok=True)
        (self.root / name / self.MARK_STOPPED).unlink(missing_ok=True)

    def stop(self, name: str) -> None:
        self._stop_tmux(name)
        (self.root / name / self.MARK_STOPPED).touch()
        shutil.rmtree(self.layout(name).run, ignore_errors=True)

    def _stop_tmux(self, name: str) -> None:
        """What a container's stop ends: its tmux server, and everything in its panes."""
        layout = self.layout(name)
        if Path(layout.tmux_socket).exists():
            subprocess.run(
                [layout.tmux, "-S", layout.tmux_socket, "kill-server"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        Path(layout.tmux_socket).unlink(missing_ok=True)  # as a container's tmpfs goes

    def remove(self, name: str) -> None:
        if (self.root / name).is_dir():
            self.stop(name)
            shutil.rmtree(self.root / name)

    def discard(self, name: str) -> None:
        if self.exists(name):
            self.stop(name)
            (self.root / name / ".labels").unlink(missing_ok=True)
            (self.root / name / self.MARK_DISCARDED).touch()

    def pull(
        self,
        image: str,
        progress: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        if cancel is not None and cancel.is_set():
            raise RuntimeFailed("the pull was stopped")
        if image not in self.images:
            raise RuntimeFailed(f"{image}: manifest unknown")
        self.pulled.append(image)
        if progress is not None:
            progress(f"Pulled {image}")

    def image_facts(self, image: str, cancel: threading.Event | None = None) -> ImageFacts:
        if cancel is not None and cancel.is_set():
            raise RuntimeFailed("reading the image was stopped")
        if image not in self.images:
            raise RuntimeFailed(f"{image}: image not known")
        return self.images[image]

    def has_image(self, image: str) -> bool:
        if image in self.lost_ids:
            return False
        return (
            image in self.images
            or image in self.extra_ids
            or (bool(self.default_id) and image == self.default_id)
            or any(facts.id == image for facts in self.images.values())
        )

    def default_image_id(self) -> str:
        return self.default_id

    def collect_images(self, keep_ids: set[str], keep_names: set[str]) -> list[str]:
        """Those of ``collectable`` that nothing keeps and no container of the host is made
        from: the images a test says were pulled or built by the runtime for someone."""
        used = {
            (spec.image_id, spec.image) for name, spec in self.specs.items() if self.exists(name)
        }
        used_ids = {i for i, _ in used if i} | {n for _, n in used if n}
        removed = []
        for name in sorted(self.collectable):
            facts = self.images.get(name)
            if facts is None or name in keep_names or name in used_ids:
                continue
            if facts.id and (facts.id in keep_ids or facts.id in used_ids):
                continue
            del self.images[name]
            self.collectable.discard(name)
            removed.append(name)
        return removed

    def clean_probes(self) -> None:
        """Nothing is made to read an image here, so nothing is left."""

    def remove_build(self, workspace_id: int, image: bool = False) -> None:
        self.builder_ids.discard(workspace_id)
        if image:
            name = self.built.pop(workspace_id, None)
            if name is not None:
                self.images.pop(name, None)

    def builders(self) -> set[int]:
        return set(self.builder_ids)

    def built_images(self) -> dict[int, str]:
        return dict(self.built)

    def _not_stopped(self, request: BuildRequest, cancel: threading.Event | None) -> None:
        if (cancel is not None and cancel.is_set()) or request.id not in self.builder_ids:
            raise RuntimeFailed("the build was stopped")

    def build_image(
        self,
        request: BuildRequest,
        progress: Callable[[str], None],
        cancel: threading.Event | None,
        vet: Callable[[Mapping[str, str]], None],
    ) -> str:
        self.remove_build(request.id, image=True)
        self.builds.append(request)
        self.builder_ids.add(request.id)
        kept = False
        try:
            self._not_stopped(request, cancel)
            script = self.build_script or (lambda request, progress, cancel: ImageFacts())
            facts = script(request, progress, cancel)
            self._not_stopped(request, cancel)
            vet(facts.labels)
            self._not_stopped(request, cancel)
            name = built_name(request.id)
            self.images[name], self.built[request.id] = facts, name
            kept = True
            return name
        finally:
            self.remove_build(request.id, image=not kept)

    def labelled(self) -> dict[str, dict[str, str]]:
        if not self.root.is_dir():
            raise RuntimeUnavailable(f"{self.root} is not there")
        found = {}
        for folder in sorted(self.root.iterdir()):
            with contextlib.suppress(OSError, ValueError):
                found[folder.name] = json.loads((folder / ".labels").read_text())
        return found

    def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float = PODMAN_TIMEOUT,
        cwd: str | None = None,
        keep: int | None = None,
        again: bool = False,
    ) -> Result:
        if not self.running(name):
            raise RuntimeFailed(f"the workspace {name} is not running")
        folder = cwd or self.layout(name).work
        try:
            if keep is None:
                proc = subprocess.run(
                    argv,
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    capture_output=True,
                    env={"PATH": "/usr/bin:/bin"},
                    cwd=folder,
                    timeout=timeout,
                )
                return Result(proc.returncode, proc.stdout, proc.stderr)
            code, out, err = _run_keeping_tail(
                argv, stdin, timeout, {"PATH": "/usr/bin:/bin"}, folder, keep
            )
            return Result(code, out, err)
        except subprocess.TimeoutExpired:
            raise RuntimeFailed("the command did not finish in time") from None
        except FileNotFoundError:
            return Result(127, b"", f"{argv[0]}: not found\n".encode())

    def channel(self, name: str) -> Channel:
        if not self.running(name):
            raise RuntimeFailed(f"the workspace {name} is not running")
        layout = self.layout(name)
        return Channel([layout.shell, "-s"], _host_tool, layout.run, env={"PATH": "/usr/bin:/bin"})

    def terminal_argv(self, name: str, layout: Layout, pidfile: str) -> Launch:
        command = terminal_command(layout, pidfile)
        return Launch([*_host_tool("setsid"), "--ctty", *command], {"PATH": "/usr/bin:/bin"})

    def ensure_account(self, person: int, slot: int) -> None:
        found = self._account_file(person)
        found.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder = self._git_folder(person)
        folder.mkdir(mode=0o700, exist_ok=True)
        for file in (found, folder / "credential"):
            os.close(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        made = folder / GH_CONFIG_FILE
        if not (made.exists() or made.is_symlink()):  # as ENSURE_SCRIPT: never replaced
            temporary = self._written_aside(folder, GH_CONFIG_FILE, GH_CONFIG.encode())
            try:
                os.link(temporary, made)
            except FileExistsError:
                pass  # another made it first
            finally:
                temporary.unlink(missing_ok=True)

    def read_account(self, person: int, slot: int) -> bytes:
        self.ensure_account(person, slot)
        return self._account_file(person).read_bytes()

    def clear_account(self, person: int, slot: int) -> None:
        self.ensure_account(person, slot)
        with open(self._account_file(person), "r+b") as handle:
            handle.truncate(0)

    def put_account(self, person: int, slot: int, name: str, data: bytes) -> None:
        _checked(name)
        self.ensure_account(person, slot)
        self._write_git_file(self._git_folder(person), name, data)

    @staticmethod
    def _written_aside(folder: Path, name: str, data: bytes) -> Path:
        """``data`` in a new owner-only file of ``folder`` of its own, named after ``name``,
        none left where it could not be written whole."""
        fd, made = tempfile.mkstemp(prefix=f"{name}.", dir=folder)  # owner-only
        temporary = Path(made)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    @classmethod
    def _write_git_file(cls, folder: Path, name: str, data: bytes) -> None:
        """``data`` as ``folder/name``, whole and owner-only, by a rename, as PUT_SCRIPT."""
        temporary = cls._written_aside(folder, name, data)
        try:
            os.replace(temporary, folder / name)
        except BaseException:
            temporary.unlink(missing_ok=True)  # as the container's script does
            raise

    def remove_account(self, person: int) -> None:
        shutil.rmtree(self._account_file(person).parent, ignore_errors=True)

    def start_sign_in(self, person: int, slot: int, seconds: float) -> str:
        name = sign_in_name(person)
        self.end_sign_in(name)
        layout = self.layout(name)
        for folder in (layout.work, layout.config, layout.home):
            Path(folder).mkdir(parents=True)
        self._link_account(name, person)
        self.start(name)
        return name

    def end_sign_in(self, name: str) -> None:
        self.remove(name)

    def apply_limits(self, person: int, workspace: int | None, held: Limits) -> None:
        if self.limits_error is not None:
            raise self.limits_error
        name = limits.slice_of(person, workspace)
        self.limits[name] = held
        self.applied.append((name, held))

    def usage(self, person: int, workspace: int | None) -> Usage:
        if self.limits_error is not None:
            raise self.limits_error
        return self.used.get(limits.slice_of(person, workspace), Usage())

    def caps(self, person: int, workspace: int | None) -> Caps:
        """What was last applied to the slice and not dropped since: ``limits`` is the slice as
        it is, which a test may change to make it differ from a table."""
        if self.limits_error is not None:
            raise self.limits_error
        held = self.limits.get(limits.slice_of(person, workspace), Limits())
        return Caps(
            None if held.memory_mib is None else held.memory_mib * limits.MEBIBYTE, held.pids
        )

    def drop_limits(self, person: int, workspace: int | None) -> None:
        if self.limits_error is not None:
            raise self.limits_error
        name = limits.slice_of(person, workspace)
        self.limits.pop(name, None)
        self.dropped.append(name)

    def end_canary(self, name: str) -> None:
        """The canary is a program run to its end here, never one left behind."""

    def run_canary(
        self, directory: Path, name: str, version: str, *, slot: int | None = None
    ) -> dict[str, dict]:
        if self.canary_script is not None:
            return self.canary_script(directory, name, version)
        with tempfile.TemporaryDirectory(prefix="rcman-canary-") as scratch:
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", f"{HELPERS_DIR.name}.claude_canary",
                     str(directory / CANDIDATE), version],
                    cwd=HELPERS_DIR.parent,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "TMPDIR": scratch,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=CANARY_SECONDS,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                raise RuntimeFailed("the canary did not finish in time") from None
        if proc.returncode != 0:
            raise RuntimeFailed(f"the canary ended with exit code {proc.returncode}")
        return parsed_canary_report(proc.stdout)
