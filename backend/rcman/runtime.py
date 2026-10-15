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

Python standard library and the backend's own channel only, so that the podman
host's suite can import it with the system interpreter.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import pwd
import re
import selectors
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rcman.channel import Channel, Result

# Where things are inside a workspace container.
TOOLS_MOUNT = "/opt/rcman"  # the static tools, read-only: bin/tmux, bin/busybox, terminfo
CLAUDE_MOUNT = "/opt/claude"  # the Claude Code store, read-only
RUN_MOUNT = "/run/rcman"  # a tmpfs: the tmux socket and the channel's files
VOLUME_MOUNT = "/rcman"  # the workspace's volume
# The folders of a workspace's volume: the one its server works in, Claude's
# configuration (CLAUDE_CONFIG_DIR) and the home of the programs rcman runs there.
WORK, CONFIG, HOME = "work", "claude", "home"
# Claude Code's credentials file, in its configuration folder: a person's sign-in.
CREDENTIALS = ".credentials.json"
# Labels every workspace container and volume carries: whose it is, and which row. A
# person's account volume and sign-in container carry only the person's.
LABEL_WORKSPACE = "rcman.workspace"
LABEL_PERSON = "rcman.person"
# Where a one-off container that makes or reads a person's credentials file mounts
# their account volume.
ACCOUNT_MOUNT = "/account"
# The folder of the account volume that holds the person's git configuration and
# credential, bound at HOME/GIT_HOME in every workspace of theirs; and the only files
# rcman puts in it.
GIT_DIR = "git"
GIT_HOME = ".config/git"
GIT_FILES = ("config", "credential")
# The scripts of an account container get the credentials file's path as $1, the git
# folder's as $2 and, for a write, the name of the file in it as $3.
# Makes the credentials file, owner-only, if it is not there, and never truncates it;
# makes the git folder and its credential file, which git's credential helper reads, so
# that there is always one to read.
ENSURE_SCRIPT = 'umask 077 && mkdir -p "$2" && : >> "$1" && : >> "$2/credential"'
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
# Each person's uid range: slot k maps the container's ids 0 to RANGE - 1 onto rcman's
# subordinate ids from k * RANGE. Slot 0 would be rcman's own ids, so no person has it.
RANGE = 65536
# podman's exit codes: 1 from `exists` for something missing; 125 when it could not
# reach Podman at all, or failed before running anything.
MISSING = 1
UNAVAILABLE = 125
PODMAN_TIMEOUT = 300.0
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
# How long one wait for Podman's socket polls, between tries.
READY_POLL_SECONDS = 1.0


def container_name(workspace_id: int) -> str:
    return f"rcman-ws-{workspace_id}"


def account_volume(person: int) -> str:
    """The volume that holds the person's credentials file, and nothing else."""
    return f"rcman-person-{person}"


def _checked(name: str) -> str:
    """``name``, a file of the git folder rcman writes; nothing else is."""
    if name not in GIT_FILES:
        raise ValueError(f"{name!r} is not a file rcman writes in the git folder")
    return name


def sign_in_name(person: int) -> str:
    """The person's short-lived sign-in container; one at a time."""
    return f"rcman-sign-in-{person}"


class RuntimeUnavailable(Exception):
    """Podman could not be reached."""


class RuntimeFailed(Exception):
    """Podman, or the host runtime, refused or failed; the message is what it said."""


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


@dataclass(frozen=True)
class ImageFacts:
    """What an image says about itself, read without running it: its passwd and group files
    (empty where it has none), the user it is set to run as, its environment, and its labels."""

    passwd: str = ""
    group: str = ""
    user: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)


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
    claude: str
    shell: str
    terminfo: str | None
    path: str
    user: str  # who the programs run as there
    # name -> the argv that runs it there: env, kill, cat, wc, rm, mkdir, chmod, mv, timeout
    tools: dict
    # The argv, but for the group's id, that sends SIGKILL to a whole process group:
    # busybox's kill refuses a "--" before it, and procps' kill needs one.
    kill_group: tuple = ()
    # What the workspace's own plan adds (``for_plan``): variables every program rcman runs
    # there gets besides its own, and the folder of rcman's own programs, which stays first
    # on a PATH the plan sets.
    env: Mapping[str, str] = field(default_factory=dict)
    path_prefix: str = ""

    def tool(self, name: str) -> list[str]:
        return list(self.tools[name])

    def for_plan(self, user: str, env: Mapping[str, str], path: str | None) -> Layout:
        """This layout for a workspace made from a plan: its programs run as ``user`` with
        ``env`` besides their own variables, and with ``path``, if there is one, after the
        folder of rcman's own programs."""
        tail = self.path if path is None else ":".join(p for p in (self.path_prefix, path) if p)
        return dataclasses.replace(self, user=user, env=dict(env), path=tail)

    def kill_group_of(self, pgid: int) -> list[str]:
        return [*self.kill_group, f"-{pgid}"]

    def with_env(self, env: dict[str, str], argv: list[str]) -> list[str]:
        """``argv`` run with exactly ``env``, and nothing of the environment it is run in."""
        return [*self.tool("env"), "-i", *(f"{k}={v}" for k, v in env.items()), *argv]


class Runtime(ABC):
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
        ``config``/CREDENTIALS."""

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
    def clean_probes(self) -> None:
        """Remove what ``image_facts`` left if rcman ended while it read an image."""

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
    ) -> Result:
        """``argv`` run once in the workspace, in its folder ``cwd`` (the runtime's own default
        if there is none: callers that need a folder name it) as the user its container runs as,
        with ``stdin`` on its standard input; RuntimeFailed if it has not ended within
        ``timeout`` seconds. With ``keep``, only the last ``keep`` bytes of each of its output
        streams are held, however much it prints."""

    @abstractmethod
    def channel(self, name: str) -> Channel:
        """A new channel to the workspace (channel.py), not yet opened."""

    @abstractmethod
    def ensure_account(self, person: int, slot: int) -> None:
        """Make the person's credentials file, empty and theirs alone, if it is not there."""

    @abstractmethod
    def read_account(self, person: int, slot: int) -> bytes:
        """The person's credentials file, made sure of first."""

    @abstractmethod
    def clear_account(self, person: int, slot: int) -> None:
        """Empty the person's credentials file in place: the same file, not a new one."""

    @abstractmethod
    def put_account(self, person: int, slot: int, name: str, data: bytes) -> None:
        """Write ``data`` as the file ``name`` (one of GIT_FILES) of the person's git
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


# --- Podman ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PodmanConfig:
    socket: Path
    image: str
    tools_dir: Path  # mounted read-only at TOOLS_MOUNT
    claude_store: Path  # mounted read-only at CLAUDE_MOUNT
    podman: str = "/usr/bin/podman"


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


def _busybox(name: str) -> list[str]:
    return [f"{TOOLS_MOUNT}/bin/busybox", name]


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
    path=f"{CLAUDE_MOUNT}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    user="root",
    tools={
        name: _busybox(name)
        for name in ("env", "kill", "cat", "wc", "rm", "mkdir", "chmod", "mv", "wget", "timeout")
    },
    kill_group=(*_busybox("kill"), "-KILL"),
    path_prefix=f"{CLAUDE_MOUNT}/bin",
)


class PodmanRuntime(Runtime):
    def __init__(self, config: PodmanConfig):
        self.config = config

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
        ]
        ids = self._ids(spec.slot, spec.uid, spec.gid)  # refuses what is wrong before anything
        # is made
        if self._podman("volume", "exists", spec.name, check=False).returncode != 0:
            self._podman("volume", "create", *labels, spec.name)
        self.ensure_account(spec.person, spec.slot)
        mapped = spec.uid != 0 or spec.gid != 0
        if mapped:
            self._prepare_folders(spec)
        self._podman(
            "run",
            "--detach",
            f"--name={spec.name}",
            f"--hostname={spec.name}",
            *labels,
            "--init",
            "--network=slirp4netns:allow_host_loopback=false",
            *ids,
            *([f"--user={spec.user}"] if spec.user else []),
            *(f"--group-add={g}" for g in spec.groups),
            *(f"--env={k}={v}" for k, v in spec.env.items()),
            f"--volume={spec.name}:{VOLUME_MOUNT}",
            *self._mounts(spec.person, owned=mapped),
            # Where the process starts is the volume's mount, which exists and is the user's;
            # the image's own working folder may be one the user cannot enter.
            f"--workdir={VOLUME_MOUNT}",
            # The image was pulled, or is here already; nothing is pulled implicitly.
            *(["--pull=never"] if spec.image else []),
            # The image's own entrypoint is not run: the process is rcman's.
            f"--entrypoint={TOOLS_MOUNT}/bin/busybox",
            spec.image or self.config.image,
            "sleep",
            "2147483647",
            timeout=IMAGE_CREATE_TIMEOUT if spec.image else PODMAN_TIMEOUT,
        )
        self._folders(spec.name)

    @staticmethod
    def _swap(base: int, own: int) -> list[str]:
        """The lines of a uid map (or gid map) of the person's range in which the container's
        id ``own`` is the host id root has in the person's ordinary map, and the container's
        root is the host id ``own`` has there: the two swap, every other id stays. ``own`` is
        an id of the range other than root's; a stretch that would be empty is left out."""
        stretches = [
            (0, base + own, 1),
            (1, base + 1, own - 1),
            (own, base, 1),
            (own + 1, base + own + 1, RANGE - own - 1),
        ]
        return [f"{inside}:{outside}:{length}" for inside, outside, length in stretches if length]

    @staticmethod
    def _ids(slot: int, uid: int = 0, gid: int = 0) -> list[str]:
        """The uid and gid maps of the person's range. Ordinarily the container's root is
        the range's first host id; with ``uid`` or ``gid`` not 0 the ids swap with root's
        (``_swap``), so that the user of that id owns what root owns in the person's other
        workspaces: the volume, the shared credentials file and the git folder."""
        if slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        if not (0 <= uid < RANGE and 0 <= gid < RANGE):
            raise RuntimeFailed("the user's ids are beyond the range of ids a workspace has")
        base = slot * RANGE
        return [
            *(
                f"--uidmap={m}"
                for m in (PodmanRuntime._swap(base, uid) if uid else [f"0:{base}:{RANGE}"])
            ),
            *(
                f"--gidmap={m}"
                for m in (PodmanRuntime._swap(base, gid) if gid else [f"0:{base}:{RANGE}"])
            ),
        ]

    def _isolated(self, slot: int) -> list[str]:
        """What every container a person's programs run in has: an init, the network
        with no way to the host's loopback, and the person's uid range."""
        return ["--init", "--network=slirp4netns:allow_host_loopback=false", *self._ids(slot)]

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
        self._podman("rm", "--force", "--ignore", "--time=0", name)

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
            self._podman("rm", "--force", "--ignore", "--time=0", name, check=False)
        return ImageFacts(
            passwd=passwd,
            group=group,
            user=inspected.get("User") or config.get("User") or "",
            env=env,
            labels=dict(inspected.get("Labels") or config.get("Labels") or {}),
        )

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

    def clean_probes(self) -> None:
        found = self._podman("ps", "--all", f"--filter=label={LABEL_PROBE}", "--format=json")
        for entry in json.loads(found.stdout or b"[]") or []:
            for name in entry.get("Names") or []:
                self._podman("rm", "--force", "--ignore", "--time=0", name, check=False)

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
    ) -> Result:
        args = [
            "exec",
            *(["--interactive"] if stdin is not None else []),
            *([f"--workdir={cwd}"] if cwd else []),
            name,
            *argv,
        ]
        for attempt in (1, 2):
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
            if code != 0 and CONMON_FAILED in said and attempt == 1:
                continue
            if code == UNAVAILABLE and SERVICE_UNREACHABLE in said.lower():
                raise RuntimeUnavailable(said.strip())
            return Result(code, out, err)
        raise AssertionError("unreachable")

    def channel(self, name: str) -> Channel:
        argv = self._argv("exec", "--interactive", name, f"{TOOLS_MOUNT}/bin/busybox", "sh", "-s")
        return Channel(argv, _busybox, RUN_MOUNT, env=podman_env())

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
        self._podman("rm", "--force", "--ignore", "--time=0", name)


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

    def __init__(self, root: Path, sockets: Path, claude: Path, tmux_conf: Path):
        self.root, self.sockets, self.claude, self.tmux_conf = root, sockets, claude, tmux_conf
        # There are no images here and no uids to map: the images a test plants, by name, what
        # was pulled, and the last spec each workspace was made from, so that a test can see
        # what reached ``create``.
        self.images: dict[str, ImageFacts] = {}
        self.pulled: list[str] = []
        self.specs: dict[str, Spec] = {}

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
            claude=str(self.claude),
            shell=_host_tool("sh")[0],
            terminfo=None,
            path="/usr/local/bin:/usr/bin:/bin",
            user=pwd.getpwuid(os.getuid()).pw_name,
            tools={
                n: _host_tool(n)
                for n in ("env", "kill", "cat", "wc", "rm", "mkdir", "chmod", "mv", "timeout")
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
        if not (0 <= spec.uid < RANGE and 0 <= spec.gid < RANGE):
            raise RuntimeFailed("the user's ids are beyond the range of ids a workspace has")
        if spec.image and spec.image not in self.images:
            raise RuntimeFailed(f"{spec.image}: image not known")
        if self.exists(spec.name):
            raise RuntimeFailed(f"the workspace {spec.name} is there already")
        layout = self.layout(spec.name)
        for folder in (layout.work, layout.config, layout.home):
            Path(folder).mkdir(parents=True, exist_ok=True)
        self._link_account(spec.name, spec.person)
        labels = {LABEL_WORKSPACE: str(spec.workspace), LABEL_PERSON: str(spec.person)}
        (self.root / spec.name / ".labels").write_text(json.dumps(labels))
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

    def clean_probes(self) -> None:
        """Nothing is made to read an image here, so nothing is left."""

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

    def ensure_account(self, person: int, slot: int) -> None:
        found = self._account_file(person)
        found.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._git_folder(person).mkdir(mode=0o700, exist_ok=True)
        for file in (found, self._git_folder(person) / "credential"):
            os.close(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))

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
        folder = self._git_folder(person)
        temporary = folder / f"{name}.new"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
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
