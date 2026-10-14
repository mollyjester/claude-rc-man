"""Where workspaces run: rootless Podman, or folders on the host for the tests.

A workspace is one container, named after its row (``container_name``), with a
volume of its own and the person's uid range. rcman reaches Podman only through
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
# Labels every workspace container and volume carries: whose it is, and which row.
LABEL_WORKSPACE = "rcman.workspace"
LABEL_PERSON = "rcman.person"
# Each person's uid range: slot k maps the container's ids 0 to RANGE - 1 onto rcman's
# subordinate ids from k * RANGE. Slot 0 would be rcman's own ids, so no person has it.
RANGE = 65536
# podman's exit codes: 1 from `exists` for something missing; 125 when it could not
# reach Podman at all, or failed before running anything.
MISSING = 1
UNAVAILABLE = 125
PODMAN_TIMEOUT = 300.0
STOP_TIMEOUT_SECONDS = 10
# What conmon says when it could not start an exec at all: nothing ran, so it is tried
# once more (channel.CONMON_FAILED).
CONMON_FAILED = "no logs from conmon"
# How long one wait for Podman's socket polls, between tries.
READY_POLL_SECONDS = 1.0


def container_name(workspace_id: int) -> str:
    return f"rcman-ws-{workspace_id}"


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
    tools: dict  # name -> the argv that runs it there: env, kill, cat, wc, rm, mkdir, chmod
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
        """Make the workspace and its volume, start it, and make its folders."""

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
    def exec(self, name: str, argv: list[str], stdin: bytes | None = None) -> Result:
        """``argv`` run once in the workspace, with ``stdin`` on its standard input."""

    @abstractmethod
    def channel(self, name: str) -> Channel:
        """A new channel to the workspace (channel.py), not yet opened."""


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
        for name in ("env", "kill", "cat", "wc", "rm", "mkdir", "chmod", "wget")
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

    def _podman(self, *args: str, check: bool = True, timeout: float = PODMAN_TIMEOUT):
        try:
            proc = subprocess.run(
                self._argv(*args),
                capture_output=True,
                env=podman_env(),
                timeout=timeout,
                stdin=subprocess.DEVNULL,
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
        if spec.slot < 1:
            raise RuntimeFailed("slot 0 is rcman's own uid range")
        base = spec.slot * RANGE
        if self._podman("volume", "exists", spec.name, check=False).returncode != 0:
            self._podman("volume", "create", *labels, spec.name)
        self._podman(
            "run",
            "--detach",
            f"--name={spec.name}",
            f"--hostname={spec.name}",
            *labels,
            "--init",
            "--network=slirp4netns:allow_host_loopback=false",
            f"--uidmap=0:{base}:{RANGE}",
            f"--gidmap=0:{base}:{RANGE}",
            f"--volume={spec.name}:{VOLUME_MOUNT}",
            f"--volume={self.config.tools_dir}:{TOOLS_MOUNT}:ro",
            f"--volume={self.config.claude_store}:{CLAUDE_MOUNT}:ro",
            f"--tmpfs={RUN_MOUNT}:mode=0700",
            self.config.image,
            f"{TOOLS_MOUNT}/bin/busybox",
            "sleep",
            "2147483647",
        )
        self._folders(spec.name)

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

    def exec(self, name: str, argv: list[str], stdin: bytes | None = None) -> Result:
        args = ["exec", *(["--interactive"] if stdin is not None else []), name, *argv]
        for attempt in (1, 2):
            try:
                proc = subprocess.run(
                    self._argv(*args),
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    capture_output=True,
                    env=podman_env(),
                    timeout=PODMAN_TIMEOUT,
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


# --- the host -------------------------------------------------------------------------


def _host_tool(name: str) -> list[str]:
    found = shutil.which(name, path="/usr/bin:/bin")
    if found is None:
        raise RuntimeFailed(f"{name} is not installed")
    return [found]


class HostRuntime(Runtime):
    """A workspace is a folder under ``root``, holding what its volume would and a runtime
    folder, with its tmux socket in ``sockets``, whose path must stay short; a stopped
    one is marked so. Programs run on the host, as the user rcman runs as."""

    MARK_STOPPED = ".stopped"

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
            tools={n: _host_tool(n) for n in ("env", "kill", "cat", "wc", "rm", "mkdir", "chmod")},
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
        labels = {LABEL_WORKSPACE: str(spec.workspace), LABEL_PERSON: str(spec.person)}
        (self.root / spec.name / ".labels").write_text(json.dumps(labels))
        self.start(spec.name)

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

    def exec(self, name: str, argv: list[str], stdin: bytes | None = None) -> Result:
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
                timeout=PODMAN_TIMEOUT,
            )
        except FileNotFoundError:
            return Result(127, b"", f"{argv[0]}: not found\n".encode())
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def channel(self, name: str) -> Channel:
        if not self.running(name):
            raise RuntimeFailed(f"the workspace {name} is not running")
        layout = self.layout(name)
        return Channel([layout.shell, "-s"], _host_tool, layout.run, env={"PATH": "/usr/bin:/bin"})
