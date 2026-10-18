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
subordinate ids, so removing an engine (``remove``) removes its containers and volumes, stops
its service and every process of the user's that names its folder, and empties the folder as
root of the user's namespace before the folder goes. A run killed by a signal leaves an engine
that nothing holds; the next run's ``started`` removes it the same way.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Iterator
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
            self.argv(*args), capture_output=True, timeout=timeout, env=env(self.folder), **kwargs
        )

    def local(self, *args: str, timeout: float = 600, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*local(self.folder), *args],
            capture_output=True,
            timeout=timeout,
            env=env(self.folder),
            **kwargs,
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


def _stop(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + GONE_WITHIN
    while [p for p in pids if _running(p)]:
        if time.monotonic() > deadline:
            raise RuntimeError(f"processes not gone: {[p for p in pids if _running(p)]}")
        time.sleep(0.1)


def _running(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] != "Z"


def remove(folder: Path) -> None:
    """Remove the engine of ``folder``, which a run may have left: its containers and volumes,
    its service and every process of the user's that names the folder (conmon among them), then
    what its storage holds, as root of the user's namespace, then the folder."""
    folder = hf.removable(folder)
    parts = _parts(folder)
    if parts["root"].exists():
        run = [*local(folder)]
        for args in (["rm", "--all", "--force", "--time=0"], ["volume", "rm", "--all", "--force"]):
            subprocess.run([*run, *args], capture_output=True, timeout=300, env=env(folder))
    _stop(_processes_naming(folder))
    held = [str(parts[k]) for k in ("root", "runroot", "tmpdir") if parts[k].exists()]
    if held:
        emptied = subprocess.run(
            [PODMAN, "unshare", "rm", "-r", "-f", "--one-file-system", *held],
            capture_output=True,
            timeout=300,
            env=env(folder),
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
