"""tests/podman_service.py: the tests' engine, the user's rootless Podman on a socket and a storage
of its own, and what is left of one a run did not end."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests import killed_runs as kr
from tests import podman_service as ps
from tests.hang_guard import tier_guard
from tests.until import until

REPO = Path(__file__).resolve().parent.parent


def busybox_tarball() -> bytes:
    """An image's root of the host's static busybox alone, for ``podman import``."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        tar.add("/bin/busybox", arcname="bin/busybox")
        link = tarfile.TarInfo("bin/sh")
        link.type, link.linkname = tarfile.SYMTYPE, "busybox"
        tar.addfile(link)
    return data.getvalue()


def test_the_engine_keeps_its_storage_and_state_in_its_own_folder(podman_service):
    found = podman_service.cli("info", "--format", "json")
    assert found.returncode == 0, found.stderr
    info = json.loads(found.stdout)
    assert info["host"]["security"]["rootless"] is True
    assert info["store"]["graphRoot"].startswith(str(podman_service.folder) + "/")
    assert info["store"]["runRoot"].startswith(str(podman_service.folder) + "/")
    assert info["store"]["graphDriverName"] == "overlay"
    assert info["host"]["cgroupManager"] == "cgroupfs"  # the shared configuration's
    assert info["host"]["eventLogger"] == "file"


def engine_left(prefix: str) -> tuple[subprocess.Popen, Path]:
    """An engine started by a process of its own, which is then killed, as a run killed by a
    signal is: what it reported, once its container runs. It is handed the test's tier's
    guard, and outlasts the test."""
    script = (
        "import sys, time\n"
        "from tests import podman_service as ps\n"
        f"with ps.started({tier_guard()!r}, {prefix!r}) as engine:\n"
        "    made = engine.cli('import', '-', 'localhost/bb', input=sys.stdin.buffer.read())\n"
        "    assert made.returncode == 0, made.stderr\n"
        "    ran = engine.cli('run', '-d', '--name=left', 'localhost/bb', 'busybox', 'sleep',\n"
        f"                     '{3 * tier_guard():g}')\n"
        "    assert ran.returncode == 0, ran.stderr\n"
        "    print(engine.folder, flush=True)\n"
        f"    time.sleep({3 * tier_guard()})\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    child.stdin.write(busybox_tarball())
    child.stdin.close()
    said = child.stdout.readline().decode().strip()
    if not said.startswith("/"):  # it failed before it reported
        os.killpg(child.pid, signal.SIGKILL)
        raise AssertionError(f"the engine's run said {said!r}")
    return child, Path(said)


def test_an_engine_a_killed_run_left_is_removed_by_the_next_with_its_container(podman_service):
    with kr.stem() as stem:
        prefix = stem + ps.PREFIX[:2]
        child, folder = engine_left(prefix)
        try:
            assert folder.name.startswith(prefix) and folder.is_dir()
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=tier_guard())
            # positive control: what the kill left, the service and the container's conmon
            left = ps._processes_naming(folder)
            assert left and folder.is_dir()
            commands = [Path(f"/proc/{p}/cmdline").read_bytes() for p in left]
            assert any(b"system\0service" in c for c in commands)
            assert any(b"conmon" in c for c in commands)
            with ps.started(tier_guard(), prefix):
                assert not folder.exists()
                assert ps._processes_naming(folder) == []
        finally:
            if folder.exists():
                ps.remove(folder)


def test_an_engine_that_ends_leaves_no_process_and_no_folder(podman_service):
    """Its own engine beside the session's; the fixture gives it the container marker."""
    with kr.stem() as stem:
        with ps.started(tier_guard(), stem + "rp") as engine:
            folder = engine.folder
            made = engine.cli("import", "-", "localhost/bb", input=busybox_tarball())
            assert made.returncode == 0, made.stderr
            ran = engine.cli(
                "run", "-d", "localhost/bb", "busybox", "sleep", f"{3 * tier_guard():g}"
            )
            assert ran.returncode == 0, ran.stderr
            assert ps._processes_naming(folder)  # positive control
        until(lambda: ps._processes_naming(folder) == [])
        assert not folder.exists()


# --- where the engine's processes run ---------------------------------------------------------


class Stop(Exception):
    """Raised in place of starting the engine's service, once what it was given is seen."""


def ran_in_engines_folder(kwargs: dict, folder: Path) -> bool:
    """Whether a process given ``kwargs`` runs in the engine's ``folder``: conmon writes a file
    named oom into its working folder when it sees a container killed for want of memory, and
    takes that folder from the service."""
    return "cwd" in kwargs and Path(kwargs["cwd"]) == folder and Path(kwargs["cwd"]) != REPO


def test_the_engines_service_and_its_podman_calls_run_in_its_own_folder(monkeypatch):
    seen: list[dict] = []

    def popen(argv, **kwargs):
        seen.append(kwargs)
        raise Stop

    monkeypatch.setattr(ps, "remove", lambda folder: hf.remove(folder))  # nothing of podman's
    monkeypatch.setattr(ps.subprocess, "Popen", popen)
    folder = None
    with pytest.raises(Stop), ps.started(tier_guard(), "rcoomtest-") as engine:
        folder = engine.folder  # never reached: the service is not started
    assert folder is None and len(seen) == 1
    held = Path(seen[0]["env"]["CONTAINERS_STORAGE_CONF"]).parent
    assert held.name.startswith("rcoomtest-")
    assert ran_in_engines_folder(seen[0], held)
    calls: list[dict] = []  # and so do its own podman calls
    monkeypatch.setattr(ps.subprocess, "run", lambda argv, **kwargs: calls.append(kwargs))
    ps.Engine(held).cli("version")
    assert len(calls) == 1 and ran_in_engines_folder(calls[0], held)
