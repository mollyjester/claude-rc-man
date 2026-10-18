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
import time
from pathlib import Path

from devtools import held_folders as hf
from tests import killed_runs as kr
from tests import podman_service as ps

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


def test_the_engines_runroot_is_one_podman_takes():
    assert ps.RUNROOT_MAX == 50
    longest = Path(hf.TMP) / (ps.PREFIX + "x" * 8) / "r"
    assert len(str(longest)) <= ps.RUNROOT_MAX


def engine_left(prefix: str) -> tuple[subprocess.Popen, Path]:
    """An engine started by a process of its own, which is then killed, as a run killed by a
    signal is: what it reported, once its container runs."""
    script = (
        "import sys, time\n"
        "from tests import podman_service as ps\n"
        f"with ps.started({prefix!r}) as engine:\n"
        "    made = engine.cli('import', '-', 'localhost/bb', input=sys.stdin.buffer.read())\n"
        "    assert made.returncode == 0, made.stderr\n"
        "    ran = engine.cli('run', '-d', '--name=left', 'localhost/bb', 'busybox', 'sleep',\n"
        "                     '600')\n"
        "    assert ran.returncode == 0, ran.stderr\n"
        "    print(engine.folder, flush=True)\n"
        "    time.sleep(600)\n"
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
            child.wait(timeout=30)
            # positive control: what the kill left, the service and the container's conmon
            left = ps._processes_naming(folder)
            assert left and folder.is_dir()
            commands = [Path(f"/proc/{p}/cmdline").read_bytes() for p in left]
            assert any(b"system\0service" in c for c in commands)
            assert any(b"conmon" in c for c in commands)
            with ps.started(prefix):
                assert not folder.exists()
                assert ps._processes_naming(folder) == []
        finally:
            if folder.exists():
                ps.remove(folder)


def test_an_engine_that_ends_leaves_no_process_and_no_folder(podman_service):
    """Its own engine beside the session's; the fixture gives it the container marker."""
    with kr.stem() as stem:
        with ps.started(stem + "rp") as engine:
            folder = engine.folder
            made = engine.cli("import", "-", "localhost/bb", input=busybox_tarball())
            assert made.returncode == 0, made.stderr
            ran = engine.cli("run", "-d", "localhost/bb", "busybox", "sleep", "600")
            assert ran.returncode == 0, ran.stderr
            assert ps._processes_naming(folder)  # positive control
        deadline = time.monotonic() + 10
        while ps._processes_naming(folder) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ps._processes_naming(folder) == []
        assert not folder.exists()
