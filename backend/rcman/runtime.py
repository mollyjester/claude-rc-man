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
import json
import os
import pwd
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
STOP_TIMEOUT_SECONDS = 10
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
    """A workspace container to make."""

    name: str
    slot: int
    workspace: int
    person: int


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

    def tool(self, name: str) -> list[str]:
        return list(self.tools[name])

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
    def labelled(self) -> dict[str, dict[str, str]]:
        """Every workspace there is, by name, with its labels."""

    @abstractmethod
    def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes | None = None,
        timeout: float = PODMAN_TIMEOUT,
    ) -> Result:
        """``argv`` run once in the workspace, with ``stdin`` on its standard input;
        RuntimeFailed if it has not ended within ``timeout`` seconds."""

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
        if proc.returncode == UNAVAILABLE and "connect" in said.lower():
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
        self._ids(spec.slot)  # refuses slot 0 before anything is made
        if self._podman("volume", "exists", spec.name, check=False).returncode != 0:
            self._podman("volume", "create", *labels, spec.name)
        self.ensure_account(spec.person, spec.slot)
        self._podman(
            "run",
            "--detach",
            f"--name={spec.name}",
            f"--hostname={spec.name}",
            *labels,
            *self._isolated(spec.slot),
            f"--volume={spec.name}:{VOLUME_MOUNT}",
            *self._mounts(spec.person),
            self.config.image,
            f"{TOOLS_MOUNT}/bin/busybox",
            "sleep",
            "2147483647",
        )
        self._folders(spec.name)

    @staticmethod
    def _ids(slot: int) -> list[str]:
        if slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        base = slot * RANGE
        return [f"--uidmap=0:{base}:{RANGE}", f"--gidmap=0:{base}:{RANGE}"]

    def _isolated(self, slot: int) -> list[str]:
        """What every container a person's programs run in has: an init, the network
        with no way to the host's loopback, and the person's uid range."""
        return ["--init", "--network=slirp4netns:allow_host_loopback=false", *self._ids(slot)]

    def _mounts(self, person: int) -> list[str]:
        """The person's credentials file and git folder, bound by their paths, as the
        volume's ``subpath`` option is refused by the Podman this runs on; the tools; the
        Claude Code store; and the tmpfs for the runtime folder."""
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
            f"--tmpfs={RUN_MOUNT}:mode=0700",
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

    def remove(self, name: str) -> None:
        self._podman("rm", "--force", "--ignore", "--time=0", name)
        if self._podman("volume", "exists", name, check=False).returncode == 0:
            self._podman("volume", "rm", "--force", name)

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
    ) -> Result:
        args = ["exec", *(["--interactive"] if stdin is not None else []), name, *argv]
        for attempt in (1, 2):
            try:
                proc = subprocess.run(
                    self._argv(*args),
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    capture_output=True,
                    env=podman_env(),
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeFailed("podman exec did not finish in time") from None
            said = proc.stderr.decode("utf-8", "replace")
            if proc.returncode != 0 and CONMON_FAILED in said and attempt == 1:
                continue
            if proc.returncode == UNAVAILABLE and "connect" in said.lower():
                raise RuntimeUnavailable(said.strip())
            return Result(proc.returncode, proc.stdout, proc.stderr)
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
    ACCOUNTS = ".accounts"

    def __init__(self, root: Path, sockets: Path, claude: Path, tmux_conf: Path):
        self.root, self.sockets, self.claude, self.tmux_conf = root, sockets, claude, tmux_conf

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
        return (self.root / name).is_dir()

    def running(self, name: str) -> bool:
        if not self.exists(name):
            raise RuntimeFailed(f"no such workspace: {name}")
        return not (self.root / name / self.MARK_STOPPED).exists()

    def create(self, spec: Spec) -> None:
        if spec.slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        if self.exists(spec.name):
            raise RuntimeFailed(f"the workspace {spec.name} is there already")
        layout = self.layout(spec.name)
        for folder in (layout.work, layout.config, layout.home):
            Path(folder).mkdir(parents=True)
        self._link_account(spec.name, spec.person)
        labels = {LABEL_WORKSPACE: str(spec.workspace), LABEL_PERSON: str(spec.person)}
        (self.root / spec.name / ".labels").write_text(json.dumps(labels))
        self.start(spec.name)

    def _account_file(self, person: int) -> Path:
        return self.root / self.ACCOUNTS / str(person) / CREDENTIALS

    def _git_folder(self, person: int) -> Path:
        return self.root / self.ACCOUNTS / str(person) / GIT_DIR

    def _link_account(self, name: str, person: int) -> None:
        self.ensure_account(person, 1)
        layout = self.layout(name)
        Path(layout.config, CREDENTIALS).symlink_to(self._account_file(person))
        link = Path(layout.home, GIT_HOME)
        link.parent.mkdir(parents=True, exist_ok=True)
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
    ) -> Result:
        if not self.running(name):
            raise RuntimeFailed(f"the workspace {name} is not running")
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                env={"PATH": "/usr/bin:/bin"},
                cwd=self.layout(name).work,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeFailed("the command did not finish in time") from None
        except FileNotFoundError:
            return Result(127, b"", f"{argv[0]}: not found\n".encode())
        return Result(proc.returncode, proc.stdout, proc.stderr)

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
