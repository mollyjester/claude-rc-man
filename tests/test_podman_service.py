"""tests/podman_service.py: the tests' engine, the user's rootless Podman on a socket and a storage
of its own, and what is left of one a run did not end."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

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


# --- an engine left with something mounted in its storage ---------------------------------------


def unshare(engine: ps.Engine, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """``argv`` in the user's rootless namespace, as root there, which is where every engine's
    mounts are made; with the environment of ``engine``, one that outlasts the engine looked at."""
    return subprocess.run([ps.PODMAN, "unshare", *argv], capture_output=True, text=True,
                          timeout=timeout, env=ps.env(engine.folder))  # fmt: skip


def mounts_under(engine: ps.Engine, folder: Path) -> list[str]:
    """The mount points at or below ``folder`` in that namespace, read there afresh."""
    shown = unshare(engine, "cat", "/proc/self/mountinfo")
    assert shown.returncode == 0, shown.stderr
    return ps.mounts_below(str(folder), shown.stdout)


def unmount_all(engine: ps.Engine, folder: Path) -> None:
    """What a failed test leaves mounted below ``folder``, unmounted, deepest first."""
    for point in sorted(mounts_under(engine, folder), key=len, reverse=True):
        unshare(engine, "umount", "-l", point)


def test_an_engine_left_with_a_mount_in_its_storage_is_removed_by_the_next(podman_service):
    """A mount the killed run's engine made in its storage, which nothing then unmounted, is in
    the user's rootless namespace, which every later ``podman unshare`` enters: rm, which keeps to
    one filesystem, cannot remove what is below it, so the clearing unmounts it first."""
    with kr.stem() as stem:
        prefix = stem + ps.PREFIX[:2]
        child, folder = engine_left(prefix)
        planted = folder / "g" / "planted"
        try:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=30)
            plant = (  # two, stacked on one path
                'mkdir -p "$1" && mount -t tmpfs tmpfs "$1" && touch "$1/file" && '
                'mount -t tmpfs tmpfs "$1"'
            )
            made = unshare(podman_service, "sh", "-c", plant, "sh", str(planted))
            assert made.returncode == 0, made.stderr
            found = mounts_under(podman_service, folder)
            assert found.count(str(planted)) == 2, found  # the plant took effect, and lasts
            with ps.started(prefix):
                assert not folder.exists()
                assert mounts_under(podman_service, folder) == []
        finally:
            unmount_all(podman_service, folder)
            if folder.exists():
                ps.remove(folder)


# The length of the sleep of a build's RUN that no other test runs, by which it is found.
BUILD_SLEEP = "4711"


def processes_sleeping(length: str) -> list[int]:
    """The user's processes that are busybox's sleep of ``length``, run by its name or a path."""
    found = []
    for entry in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.stat(f"/proc/{entry}").st_uid != os.getuid():
                continue
            line = Path(f"/proc/{entry}/cmdline").read_bytes()
            if line.endswith(f"busybox\0sleep\0{length}\0".encode()):  # by a path, or not
                found.append(int(entry))
        except OSError:
            continue
    return found


def where(pid: int) -> dict[str, str]:
    """What a process's root and working folder read as from here, and its parent: what a
    survivor is found by."""
    found = {}
    for name in ("root", "cwd"):
        try:
            found[name] = os.readlink(f"/proc/{pid}/{name}")
        except OSError as error:
            found[name] = str(error)
    with contextlib.suppress(OSError):
        found["ppid"] = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1]
    return found


def engine_building(prefix: str, context: Path) -> tuple[subprocess.Popen, Path]:
    """An engine started by a process of its own that then builds ``context``, whose RUN sleeps:
    what it reported, before the build."""
    script = (
        "import sys\n"
        "from tests import podman_service as ps\n"
        f"with ps.started({prefix!r}) as engine:\n"
        "    made = engine.cli('import', '-', 'localhost/bb', input=sys.stdin.buffer.read())\n"
        "    assert made.returncode == 0, made.stderr\n"
        "    print(engine.folder, flush=True)\n"
        "    engine.cli('build', '--network=none', '--pull=never', '-t', 'localhost/built',\n"
        f"               {str(context)!r})\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], cwd=REPO, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, start_new_session=True)  # fmt: skip
    child.stdin.write(busybox_tarball())
    child.stdin.close()
    said = child.stdout.readline().decode().strip()
    if not said.startswith("/"):
        os.killpg(child.pid, signal.SIGKILL)
        raise AssertionError(f"the engine's run said {said!r}")
    return child, Path(said)


def test_an_engine_killed_during_a_build_is_removed_by_the_next(podman_service, tmp_path):
    """The build's working container is mounted while its RUN runs; the run is killed then, and
    the next run's clearing removes the engine all the same."""
    (tmp_path / "Containerfile").write_text(
        f'FROM localhost/bb\nRUN ["busybox", "sleep", "{BUILD_SLEEP}"]\n'
    )
    assert processes_sleeping(BUILD_SLEEP) == []
    with kr.stem() as stem:
        prefix = stem + ps.PREFIX[:2]
        child, folder = engine_building(prefix, tmp_path)
        try:
            deadline = time.monotonic() + 300
            while not processes_sleeping(BUILD_SLEEP) and time.monotonic() < deadline:
                time.sleep(0.5)
            assert processes_sleeping(BUILD_SLEEP)  # the RUN runs
            merged = [p for p in mounts_under(podman_service, folder) if p.endswith("/merged")]
            assert merged, mounts_under(
                podman_service, folder
            )  # positive control: its layer is mounted
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=30)
            with ps.started(prefix):
                assert not folder.exists()
                assert mounts_under(podman_service, folder) == []
                left = processes_sleeping(BUILD_SLEEP)
                assert left == [], [where(pid) for pid in left]  # its RUN went with it
        finally:
            for pid in processes_sleeping(BUILD_SLEEP):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            unmount_all(podman_service, folder)
            if folder.exists():
                ps.remove(folder)


def test_the_mounts_below_a_folder_are_read_from_mountinfo_the_deepest_first():
    folder = "/x/rcpod-ab"
    lines = [
        "36 25 0:31 / / rw - overlay overlay rw",
        f"40 36 0:40 / {folder}/g/overlay rw - none none rw",
        f"41 40 0:41 / {folder}/g/overlay/l1/merged rw - overlay overlay rw",
        f"42 36 0:42 / {folder}/g/with\\040space rw - tmpfs tmpfs rw",
        f"43 36 0:43 / {folder}x/g rw - tmpfs tmpfs rw",  # a sibling whose name it begins
        f"44 36 0:44 / {folder} rw - tmpfs tmpfs rw",
    ]
    assert ps.mounts_below(folder, "\n".join(lines)) == [
        f"{folder}/g/overlay/l1/merged",
        f"{folder}/g/with space",
        f"{folder}/g/overlay",
        folder,
    ]
    assert ps.mounts_below(folder, "\n".join(lines[:1] + lines[4:5])) == []  # none of its own


def test_a_path_mounted_twice_is_below_the_folder_twice():
    """Two mounts stacked on one path are two lines of mountinfo, and a lazy unmount takes the top
    one only: each is one to unmount."""
    folder = "/x/rcpod-ab"
    lines = [
        f"40 36 0:40 / {folder}/g/planted rw - tmpfs tmpfs rw",
        f"41 40 0:41 / {folder}/g/planted rw - tmpfs tmpfs rw",
        f"42 36 0:42 / {folder}/g rw - tmpfs tmpfs rw",
    ]
    assert ps.mounts_below(folder, "\n".join(lines)) == [
        f"{folder}/g/planted",
        f"{folder}/g/planted",
        f"{folder}/g",
    ]


def test_unmounting_goes_on_until_nothing_is_below_or_a_pass_does_nothing():
    """Read again after every pass: a mount a pass uncovered, stacked beyond what the reading
    showed, is taken by the next; a pass that unmounts nothing ends it, with what is left."""
    folder = "/x/rcpod-ab"
    line = f"40 36 0:40 / {folder}/g rw - tmpfs tmpfs rw"
    stacked, reads = 3, []

    def read() -> str:  # shows the top mount only, as though the rest were hidden below it
        reads.append(stacked)
        return line if stacked else ""

    def unmount(point: str) -> int:
        nonlocal stacked
        assert point == f"{folder}/g"
        stacked -= 1
        return 0

    assert ps.unmount_all_below(folder, read, unmount) == []
    assert stacked == 0 and reads == [3, 2, 1, 0]  # one pass each, then nothing below
    busy = ps.unmount_all_below(folder, lambda: line, lambda point: errno.EBUSY)
    assert busy == [f"{folder}/g"]  # no progress: what is left is said
    bounded = []
    left = ps.unmount_all_below(folder, lambda: line, lambda point: bounded.append(point) or 0)
    assert left == [f"{folder}/g"] and len(bounded) == ps.UNMOUNT_PASSES  # never endless


def test_what_is_left_mounted_is_said_with_why_its_last_unmount_failed():
    folder = "/x/rcpod-ab"
    said = ps.left_said([f"{folder}/g", f"{folder}/h"], {f"{folder}/g": errno.EBUSY})
    assert said == [
        f"umount {folder}/g: {os.strerror(errno.EBUSY)}",
        f"umount {folder}/h: still mounted",  # one whose unmount did not fail, and stayed
    ]


def test_what_a_named_process_started_is_stopped_with_it_and_nothing_above(tmp_path):
    """A build's RUN names nothing of the engine's folder, and its root and working folder read as
    / from outside its namespaces; it is found below the service that started it."""
    parent = subprocess.Popen(["sh", "-c", "sleep 60 & wait"], start_new_session=True)
    beside = subprocess.Popen(["sleep", "60"])
    try:
        deadline = time.monotonic() + 30
        while not (below := set(ps._with_descendants([parent.pid])) - {parent.pid}) and (
            time.monotonic() < deadline
        ):
            time.sleep(0.1)
        found = ps._with_descendants([parent.pid])
        assert parent.pid in found and below and beside.pid not in found
        assert os.getpid() not in ps._with_descendants([os.getppid()])  # never this one
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait(timeout=30)
        beside.kill()
        beside.wait(timeout=30)


def test_a_process_that_may_not_be_signalled_is_left_and_said_and_the_rest_stopped(
    monkeypatch, capsys
):
    """One the user may not signal (a setuid one, say) is left out of the wait and said; the
    removal goes on."""
    others = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]
    refused, stopped = others
    real_kill = os.kill

    def kill(pid, sig):
        if pid == refused.pid:
            raise PermissionError(1, "Operation not permitted")
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    try:
        ps._stop(ps._found([refused.pid, stopped.pid]))
        assert stopped.wait(timeout=30) == -signal.SIGKILL
        assert refused.poll() is None  # left
        said = capsys.readouterr().err
        assert f"left process {refused.pid}" in said and "not permitted" in said, said
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        for child in others:
            child.kill()
            child.wait(timeout=30)


def test_a_process_whose_first_thread_has_ended_is_waited_on_while_another_runs(
    tmp_path, monkeypatch
):
    """A process whose first thread has ended still holds its files while another thread runs:
    the wait goes on until it has ended. The kill is withheld, so it never does."""
    with kr.FirstThreadEnded(tmp_path) as helper:
        pid = helper.start()
        # the control: its first thread has ended
        assert hf.state_and_threads(pid) == kr.FIRST_THREAD_ENDED
        real_kill = os.kill
        with monkeypatch.context() as patched:  # undone before the helper is ended and reaped
            patched.setattr(os, "kill", lambda p, sig: None if p == pid else real_kill(p, sig))
            patched.setattr(ps, "GONE_WITHIN", 0.5)
            with pytest.raises(RuntimeError, match=f"processes not gone: \\[{pid}\\]"):
                ps._stop(ps._found([pid]))


def test_a_pid_given_again_since_it_was_found_is_not_signalled():
    """Found with its start time, and signalled only while that is still its start time."""
    child = subprocess.Popen(["sleep", "60"])
    try:
        found = ps._found([child.pid])
        assert found[child.pid] == ps._start_time(child.pid) is not None  # the positive
        ps._stop({child.pid: found[child.pid] + 1})  # as though another had the pid now
        assert child.poll() is None  # not signalled
        ps._stop(found)
        assert child.wait(timeout=30) == -signal.SIGKILL  # the control: its own is
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
