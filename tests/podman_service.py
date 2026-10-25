"""The tests' container engine: the current user's own rootless Podman, run as
``podman system service`` on a socket of its own, with storage of its own, in a held folder
under /tmp (devtools/held_folders.py), and the workspaces' shared configuration
(backend/rcman/podman/containers.conf and registries.conf).

Not a test module; tests/conftest.py makes ``podman_service`` of it, and a test that uses that
fixture carries the container marker (tests/container_marker.py).

Rootless Podman takes the storage it is given only from its flags (``graphroot`` and
``runroot`` of a storage.conf other than the user's own are not read), and refuses a run folder
longer than RUNROOT_MAX, so the folder is a short one under /tmp and its parts have short names.
Containers outlive the service (conmon holds each), and the storage holds files of the user's
subordinate ids, so removing an engine (``remove``) removes its containers and volumes, stops its
service and every process of the user's that names its folder with what they started, and, as root
of the user's namespace, unmounts what is still mounted below the folder and empties it before the
folder goes. A run killed by a signal leaves an engine that nothing holds; the next run's
``started`` removes it the same way.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from devtools import held_folders as hf

REPO = Path(__file__).resolve().parent.parent
SHARED = REPO / "backend" / "rcman" / "podman"
PREFIX = "rcpod-"
# What Podman refuses a longer run folder than.
RUNROOT_MAX = 50
# Seconds the service may take to answer, and a process to go once it is killed.
ANSWERS_WITHIN = 60
GONE_WITHIN = 30
PODMAN = "/usr/bin/podman"


def _parts(folder: Path) -> dict[str, Path]:
    return {
        "root": folder / "g",
        "runroot": folder / "r",
        "tmpdir": folder / "t",
        "socket": folder / "s.sock",
        "storage": folder / "storage.conf",
        "pid": folder / "service.pid",
        "log": folder / "service.log",
    }


def env(folder: Path) -> dict[str, str]:
    """The environment the engine's podman runs with: the shared configuration, a storage.conf of
    the folder's own (only its driver: the places are flags), and the user's runtime folder."""
    found = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "CONTAINERS_CONF": str(SHARED / "containers.conf"),
        "CONTAINERS_REGISTRIES_CONF": str(SHARED / "registries.conf"),
        "CONTAINERS_STORAGE_CONF": str(_parts(folder)["storage"]),
    }
    for key in ("HOME", "XDG_RUNTIME_DIR"):
        if os.environ.get(key):
            found[key] = os.environ[key]
    return found


def local(folder: Path) -> list[str]:
    """podman, not through the socket, on the engine's own storage."""
    parts = _parts(folder)
    return [PODMAN, "--root", str(parts["root"]), "--runroot", str(parts["runroot"]),
            "--tmpdir", str(parts["tmpdir"])]  # fmt: skip


@dataclass(frozen=True)
class Engine:
    folder: Path

    @property
    def socket(self) -> Path:
        return _parts(self.folder)["socket"]

    def argv(self, *args: str) -> list[str]:
        return ["podman", "--remote", "--url", f"unix://{self.socket}", *args]

    def cli(self, *args: str, timeout: float = 600, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.argv(*args),
            capture_output=True,
            timeout=timeout,
            env=env(self.folder),
            **{"cwd": self.folder, **kwargs},
        )

    def local(self, *args: str, timeout: float = 600, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*local(self.folder), *args],
            capture_output=True,
            timeout=timeout,
            env=env(self.folder),
            **{"cwd": self.folder, **kwargs},
        )


def holder_of(image: str) -> str:
    """The name of the container that holds ``image`` on an engine (``hold``)."""
    return "rcman-test-hold-" + "".join(c if c.isalnum() else "-" for c in image)


def hold(engine: Engine, image: str) -> str:
    """``image`` kept on ``engine`` for as long as the engine lasts: a container made from it
    that is never started and is no workspace's. A service deleting a workspace collects the
    images nothing refers to but its own runtime's, and never removes one a container is made
    from. The image's name."""
    made = engine.cli(
        "create", f"--name={holder_of(image)}", "--pull=never", "--network=none",
        "--entrypoint=/bin/true", image,
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    return image


def usable() -> str | None:
    """Why the user's rootless Podman cannot be the engine, or None."""
    try:
        proc = subprocess.run([PODMAN, "unshare", "true"], capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return f"podman unshare: {error}"
    if proc.returncode != 0:
        return proc.stderr.decode("utf-8", "replace").strip() or "podman unshare failed"
    return None


def _processes_naming(folder: Path) -> list[int]:
    """The user's processes whose command line names ``folder``, but this one."""
    found = []
    needle = str(folder).encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != os.getuid():
                continue
            if needle in Path(f"/proc/{entry}/cmdline").read_bytes():
                found.append(int(entry))
        except OSError:
            continue
    return found


def _with_descendants(pids: list[int]) -> list[int]:
    """``pids`` and every process below them, by the parents /proc gives now: what an engine's
    service and conmon started, a build's RUN among them, whose own command line names nothing of
    the engine's folder and which no one reaps once its parent is killed. This process and those
    above it never."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                parent = int(Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append(int(entry))
    mine = set()
    pid = os.getpid()
    while pid > 1:  # this process and those above it
        mine.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    found, todo = set(), list(pids)
    while todo:
        pid = todo.pop()
        if pid in found or pid in mine:
            continue
        found.add(pid)
        todo.extend(children.get(pid, []))
    return sorted(found)


def _start_time(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot (field 22 of its stat), or None for one
    that is gone: with the pid, what names one process, since a pid is given again."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _found(pids: list[int]) -> dict[int, int | None]:
    """``pids`` with each one's start time, read now."""
    return {pid: _start_time(pid) for pid in pids}


def _stop(found: dict[int, int | None]) -> None:
    """Kill each process of ``found`` that is still the one found (its start time the same) and
    wait until none is running (held_folders.running); one the user may not signal is left out
    of the wait, and said."""
    killed = []
    for pid, started in found.items():
        if started is None or _start_time(pid) != started:
            continue  # gone, or its pid another's now
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            print(f"podman_service: left process {pid}: {error.strerror}", file=sys.stderr)
            continue
        killed.append(pid)
    deadline = time.monotonic() + GONE_WITHIN
    while [p for p in killed if hf.running(p) and _start_time(p) == found[p]]:
        if time.monotonic() > deadline:
            raise RuntimeError(f"processes not gone: {[p for p in killed if hf.running(p)]}")
        time.sleep(0.1)


# A mount point as /proc/self/mountinfo writes it: a space, a tab, a newline and a backslash in
# it as an octal escape.
_ESCAPED = re.compile(r"\\([0-7]{3})")
MNT_DETACH = 2


# How many times unmounting reads the mounts again before it gives up on what is left.
UNMOUNT_PASSES = 20


def mounts_below(folder: str, mountinfo: str) -> list[str]:
    """The mount points of ``mountinfo`` at or below ``folder``, the deepest first: a path once
    for each mount on it, since mounts stack and an unmount takes only the top one."""
    points = []
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            point = _ESCAPED.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
            if point == folder or point.startswith(folder.rstrip("/") + "/"):
                points.append(point)
    return sorted(points, key=lambda p: (p.count("/"), len(p)), reverse=True)


def unmount_all_below(
    folder: str, read: Callable[[], str], unmount: Callable[[str], int]
) -> list[str]:
    """Unmount with ``unmount`` (0, or an errno) each mount at or below ``folder`` in what ``read``
    gives, and read again, until nothing is below it, a pass unmounts nothing, or UNMOUNT_PASSES
    have run: the mount points left. Reading again takes what a pass uncovered or what was
    stacked beyond what one reading showed; one gone with another above it (EINVAL, ENOENT) is
    not progress, and is not left either once it no longer shows."""
    for _ in range(UNMOUNT_PASSES):
        points = mounts_below(folder, read())
        if not points:
            return []
        if not [point for point in points if unmount(point) == 0]:
            return points
    return mounts_below(folder, read())


def left_said(left: list[str], failed: dict[str, int]) -> list[str]:
    """A line for each mount point ``left``, with why its last unmount failed (an errno of
    ``failed``), or that it is still mounted where none did."""
    return [
        f"umount {point}: {os.strerror(failed[point]) if point in failed else 'still mounted'}"
        for point in left
    ]


def unmount_below(folder: str) -> None:
    """Unmount, lazily and the deepest first, every mount at or below ``folder`` in this process's
    mount namespace (``unmount_all_below``); run as root of the user's namespace by ``remove``.
    What is left is said on stderr, with why its last unmount failed, and the exit is then 1."""
    libc = ctypes.CDLL(None, use_errno=True)
    failed: dict[str, int] = {}

    def read() -> str:  # a mount point of no UTF-8 elsewhere must not stop it
        with open("/proc/self/mountinfo", errors="surrogateescape") as found:
            return found.read()

    def unmount(point: str) -> int:
        if libc.umount2(os.fsencode(point), MNT_DETACH) == 0:
            return 0
        failed[point] = ctypes.get_errno() or errno.EIO
        return failed[point]

    left = unmount_all_below(folder, read, unmount)
    for line in left_said(left, failed):
        print(line, file=sys.stderr)
    sys.exit(1 if left else 0)


# What ``podman unshare`` runs to unmount what is below a folder: unmount_below, from this module.
UNMOUNT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from tests import podman_service as ps; ps.unmount_below(sys.argv[2])"
)


def remove(folder: Path) -> None:
    """Remove the engine of ``folder``, which a run may have left: its containers and volumes, its
    service and every process of the user's that names the folder (conmon among them) with what
    they started (a build's RUN among them); then, as root of the user's namespace, every mount
    below the folder that is still there, and what its storage holds; then the folder. A killed
    engine leaves its mounts there, the layer of a build's working container among them, which
    ``rm --all`` does not remove, and rm, which keeps to one filesystem, cannot remove what is
    below a mount. A process of the engine's left without its service and conmon, which died
    before it, names nothing of the folder and is below nothing that does, so it is not found."""
    folder = hf.removable(folder)
    parts = _parts(folder)
    if parts["root"].exists():
        run = [*local(folder)]
        for args in (["rm", "--all", "--force", "--time=0"], ["volume", "rm", "--all", "--force"]):
            subprocess.run(
                [*run, *args], capture_output=True, timeout=300, env=env(folder), cwd=folder
            )
    _stop(_found(_with_descendants(_processes_naming(folder))))
    held = [str(parts[k]) for k in ("root", "runroot", "tmpdir") if parts[k].exists()]
    if held:
        unmounted = subprocess.run(
            [PODMAN, "unshare", sys.executable, "-c", UNMOUNT, str(REPO), str(folder)],
            capture_output=True,
            timeout=300,
            env=env(folder),
            cwd=folder,
        )
        if unmounted.returncode != 0:
            raise RuntimeError(unmounted.stderr.decode("utf-8", "replace"))
        emptied = subprocess.run(
            [PODMAN, "unshare", "rm", "-r", "-f", "--one-file-system", *held],
            capture_output=True,
            timeout=300,
            env=env(folder),
            cwd=folder,
        )
        if emptied.returncode != 0:
            raise RuntimeError(emptied.stderr.decode("utf-8", "replace"))
    hf.remove(folder)


@contextlib.contextmanager
def started(prefix: str = PREFIX) -> Iterator[Engine]:
    """A running engine in a held folder of ``prefix``; engines of the same prefix that a killed
    run left are removed first, and this one afterwards."""
    with hf.held(prefix, remover=remove) as folder:
        parts = _parts(folder)
        if len(str(parts["runroot"])) > RUNROOT_MAX:
            raise RuntimeError(f"{parts['runroot']} is longer than Podman takes")
        for key in ("root", "runroot", "tmpdir"):
            parts[key].mkdir(mode=0o700)
        parts["storage"].write_text('[storage]\ndriver="overlay"\n')
        with open(parts["log"], "wb") as log:
            service = subprocess.Popen(
                [*local(folder), "system", "service", "--time=0", f"unix://{parts['socket']}"],
                env=env(folder),
                # In the folder, never the repository: conmon, which the service starts, writes
                # a file named oom into the folder it was started in when it sees an OOM kill.
                cwd=folder,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        parts["pid"].write_text(f"{service.pid}\n")
        engine = Engine(folder)
        deadline = time.monotonic() + ANSWERS_WITHIN
        while engine.cli("version", timeout=30).returncode != 0:
            if service.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the engine did not answer: " + parts["log"].read_text())
            time.sleep(0.2)
        try:
            yield engine
        finally:
            service.kill()
            service.wait(timeout=GONE_WITHIN)
