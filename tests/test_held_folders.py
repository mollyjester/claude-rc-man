"""devtools/held_folders.py: folders a run holds under /tmp, and the clearing of those a
killed run left."""

import os
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests import killed_runs as kr
from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent


# --- what a removal refuses ---------------------------------------------------------


@pytest.fixture
def nothing_removed(monkeypatch):
    """Every primitive a removal could use fails the test instead of removing anything,
    so a refusal that is missing shows as a failure, never as a removal."""

    def would(name):
        def refuse(*args, **kwargs):
            raise AssertionError(f"would {name} {args!r}")

        return refuse

    for module, name in (
        (shutil, "rmtree"),
        (os, "unlink"),
        (os, "remove"),
        (os, "rmdir"),
        (os, "chmod"),
        (os, "rename"),
    ):
        monkeypatch.setattr(module, name, would(name))


def test_a_removal_refuses_what_is_not_a_folder_under_tmp(nothing_removed):
    for path in ("/var/lib/x", str(Path.home()), str(REPO), "/" + "tmp/../etc"):
        for remover in (hf.remove, hf.remove_tree):
            with pytest.raises(hf.UnsafeRemoval):
                remover(Path(path))


# --- what a killed run leaves, and the next run clears ---------------------------------

USES_TMUX_SOCKETS = """
import subprocess
from tests import killed_runs as kr
from devtools.held_folders import tmux_sockets

def used(name):
    with tmux_sockets({prefix!r}) as folder:
        sock = folder / "x.sock"
        tmux = ["tmux", "-S", str(sock), "-f", "/dev/null"]
        subprocess.run([*tmux, "new-session", "-d", "sleep", "infinity"], check=True)
        out = subprocess.run(
            [*tmux, "display-message", "-p", "#{{pid}}"], check=True, capture_output=True, text=True
        ).stdout
        kr.report(name, {{"paths": {{"folder": str(folder)}}, "pids": {{"tmux": int(out)}}}})
        kr.hold()

def test_one():
    used("one")

def test_two():
    used("two")
"""


def _killed_with_tmux_sockets(tmp_path, stem, prefix):
    """Two folders and their tmux servers, as a run killed with SIGKILL left them."""
    source = USES_TMUX_SOCKETS.format(prefix=prefix)
    with kr.KilledRun(tmp_path, {"test_sockets.py": source}, (stem,)) as run:
        reported = [run.reports.wait(name) for name in ("one", "two")]
        run.kill(signal.SIGKILL)
    folders = [Path(r["paths"]["folder"]) for r in reported]
    pids = [r["pids"]["tmux"] for r in reported]
    assert all(f.is_dir() and f.name.startswith(prefix) for f in folders)  # positive control
    assert all(kr.running(pid) for pid in pids)  # the kill left them running
    return folders, pids


def test_the_next_run_clears_the_tmux_sockets_a_killed_run_left(tmp_path):
    with kr.stem() as stem:
        prefix = stem + "rch-"
        folders, pids = _killed_with_tmux_sockets(tmp_path, stem, prefix)
        with hf.tmux_sockets(prefix) as mine:
            assert mine.is_dir()
            assert not any(os.path.lexists(f) for f in folders)
            assert not any(kr.running(pid) for pid in pids)


# --- held folders and their sweep -----------------------------------------------------


def tmux(sock, *args):
    return subprocess.run(["tmux", "-S", str(sock), *args], capture_output=True, text=True)


def start_tmux(sock: Path) -> int:
    sock.parent.mkdir(parents=True, exist_ok=True)
    started = tmux(sock, "-f", "/dev/null", "new-session", "-d", "sleep", f"{3 * HANG_GUARD:g}")
    assert started.returncode == 0, started.stderr
    return int(tmux(sock, "display-message", "-p", "#{pid}").stdout)


class Listener:
    """Another program's socket at ``path``: it takes each connection, records what is sent on
    it, and closes it, so a tmux client that speaks to it is answered with an end, not left
    waiting. Its thread ends when close() is called."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.received: list[bytes] = []
        self.got = threading.Event()
        self._stop_r, self._stop_w = os.pipe()
        self._thread = threading.Thread(target=self._serve)
        self._thread.start()

    def _take(self, conn: socket.socket) -> None:
        with conn:
            ready, _, _ = select.select([conn, self._stop_r], [], [])
            data = conn.recv(65536) if conn in ready else b""
            self.received.append(data)
            if data:
                self.got.set()

    def _serve(self) -> None:
        while True:
            ready, _, _ = select.select([self.sock, self._stop_r], [], [])
            if self._stop_r in ready:
                return
            self._take(self.sock.accept()[0])

    def close(self) -> list[bytes]:
        """End the thread, take what is still queued without waiting, and give what every
        connection sent."""
        os.write(self._stop_w, b"x")
        self._thread.join()
        self.sock.setblocking(False)
        while True:
            try:
                conn = self.sock.accept()[0]
            except BlockingIOError:
                break
            with conn:
                conn.setblocking(False)
                try:
                    self.received.append(conn.recv(65536))
                except BlockingIOError:
                    self.received.append(b"")
        self.sock.close()
        os.close(self._stop_r)
        os.close(self._stop_w)
        return self.received


@pytest.fixture
def listener():
    """Listener(path), each closed after the test."""
    made: list[Listener] = []

    def make(path: Path) -> Listener:
        made.append(Listener(path))
        return made[-1]

    yield make
    for each in made:
        if each._thread.is_alive():
            each.close()


def heard(listener: Listener, sock: Path) -> None:
    """The positive control: the listener records what is sent to it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(sock))
        client.sendall(b"ping")
        assert listener.got.wait(HANG_GUARD)
    assert listener.received[-1] == b"ping"
    listener.received.clear()


def test_stop_tmux_passes_over_a_socket_another_program_listens_on(stem, listener, capsys):
    with hf.held(stem + "rch-") as folder:
        sock = folder / "s.sock"
        other = listener(sock)
        heard(other, sock)
        hf.stop_tmux(folder)
        sent = other.close()
        assert not any(sent), sent
        assert f"held_folders: left the listener on {sock}: " in capsys.readouterr().err


def abandoned(prefix: str) -> tuple[Path, int]:
    """A folder as a killed run leaves one in /tmp: its lock file, which nothing holds, and a
    tmux server still running on a socket in it."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=hf.TMP))
    (folder / hf.LOCK).touch()
    return folder, start_tmux(folder / "x.sock")


@pytest.fixture
def stem():
    with kr.stem() as prefix:
        yield prefix


def test_a_sockets_folder_is_removed_with_its_tmux_servers_after_the_test(stem):
    with hf.tmux_sockets(stem + "rct-") as one, hf.tmux_sockets(stem + "rct-") as two:
        assert one != two and one.name.startswith(stem + "rct-")
        sock = one / "x.sock"
        pid = start_tmux(sock)
        assert one.is_dir() and sock.exists() and kr.running(pid)  # positive control
    assert not one.exists() and not two.exists()
    assert not kr.running(pid)


def test_the_next_run_of_a_prefix_clears_only_that_prefix_and_leaves_a_live_run_alone(stem):
    prefix = stem + "rch-"
    left, pid = abandoned(prefix)
    other, other_pid = abandoned(stem + "rcb-")
    with hf.tmux_sockets(prefix) as live:
        live_pid = start_tmux(live / "x.sock")
        with hf.tmux_sockets(prefix):
            assert not left.exists() and not kr.running(pid)
            assert other.is_dir() and kr.running(other_pid)  # another prefix's
            assert live.is_dir() and kr.running(live_pid)  # a folder whose lock is held


def test_remove_tree_stops_tmux_anywhere_and_follows_no_link(stem, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").touch()
    victim_pid = start_tmux(outside / "v.sock")
    with hf.held(stem + "rce2e-test-", remover=hf.remove_tree) as folder:
        deep_pid = start_tmux(folder / "a" / "b" / "deep.sock")
        (folder / "a" / "out").symlink_to(outside)
        (folder / "a" / "v.sock").symlink_to(outside / "v.sock")
        (folder / "a").chmod(0)
    assert not folder.exists() and not kr.running(deep_pid)
    assert (outside / "keep").exists() and kr.running(victim_pid)
    tmux(outside / "v.sock", "kill-server")
