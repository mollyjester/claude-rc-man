"""devtools/held_folders.py: folders a run holds under /tmp, and the clearing of those a
killed run left."""

import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
from pathlib import Path
from socket import SO_PEERCRED, SOL_SOCKET

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


UNSAFE = {
    "empty": "",
    "dot": ".",
    "relative": "some/folder",
    "dot-dot": "../x",
    "root": "/",
    "home": str(Path.home()),
    "repository": str(REPO),
    "inside the repository": str(REPO / "tests"),
    "outside /tmp": "/var/lib/x",
    "the system's temporary folder itself": "/" + "tmp",
    "out of /tmp through ..": "/" + "tmp/../etc",
}


@pytest.mark.parametrize("name", UNSAFE)
@pytest.mark.parametrize("remover", ["remove", "remove_tree"])
def test_a_removal_refuses_what_is_not_a_folder_under_tmp(nothing_removed, name, remover):
    with pytest.raises(hf.UnsafeRemoval):
        getattr(hf, remover)(Path(UNSAFE[name]))


@pytest.mark.parametrize("name", UNSAFE)
def test_removable_refuses_the_same(name):
    with pytest.raises(hf.UnsafeRemoval):
        hf.removable(Path(UNSAFE[name]))


def test_removable_refuses_a_path_through_a_link_to_outside(tmp_path):
    with hf.held("rchft-") as folder:
        (folder / "out").symlink_to(tmp_path)
        assert hf.removable(folder / "x") == folder / "x"  # positive control
        assert hf.removable(folder / "out", roots=(folder,))  # the link itself, not followed
        with pytest.raises(hf.UnsafeRemoval):
            hf.removable(folder / "out" / "x", roots=(folder,))


def test_removable_takes_a_folder_under_a_root_it_is_given(tmp_path):
    assert hf.removable(tmp_path / "x", roots=(tmp_path,)) == tmp_path / "x"
    with pytest.raises(hf.UnsafeRemoval):
        hf.removable(tmp_path, roots=(tmp_path,))  # the root itself
    with pytest.raises(hf.UnsafeRemoval):
        hf.removable(Path("/var/lib/x"), roots=(tmp_path,))


def test_removable_refuses_the_working_folder_and_what_holds_it(tmp_path, monkeypatch):
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    assert hf.removable(tmp_path / "other", roots=(tmp_path,))  # positive control
    for path in (inner, inner.parent):
        with pytest.raises(hf.UnsafeRemoval):
            hf.removable(path, roots=(tmp_path,))


# --- what a killed run leaves, and the next run clears ---------------------------------

USES_TMUX_SOCKETS = """
import subprocess
from tests import killed_runs as kr
from devtools.held_folders import tmux_sockets

def used(name):
    with tmux_sockets({prefix!r}) as folder:
        sock = folder / "x.sock"
        tmux = ["tmux", "-S", str(sock), "-f", "/dev/null"]
        subprocess.run([*tmux, "new-session", "-d", "sleep", "600"], check=True)
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


def _killed_with_tmux_sockets(tmp_path, stem, prefix, sig):
    """Two folders and their tmux servers, as a run killed with ``sig`` left them."""
    source = USES_TMUX_SOCKETS.format(prefix=prefix)
    with kr.KilledRun(tmp_path, {"test_sockets.py": source}, (stem,)) as run:
        reported = [run.reports.wait(name) for name in ("one", "two")]
        run.kill(sig)
    folders = [Path(r["paths"]["folder"]) for r in reported]
    pids = [r["pids"]["tmux"] for r in reported]
    assert all(f.is_dir() and f.name.startswith(prefix) for f in folders)  # positive control
    assert all(kr.running(pid) for pid in pids)  # the kill left them running
    return folders, pids


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_the_next_run_clears_the_tmux_sockets_a_killed_run_left(tmp_path, sig):
    with kr.stem() as stem:
        prefix = stem + "rch-"
        folders, pids = _killed_with_tmux_sockets(tmp_path, stem, prefix, sig)
        with hf.tmux_sockets(prefix) as mine:
            assert mine.is_dir()
            assert not any(os.path.lexists(f) for f in folders)
            assert not any(kr.running(pid) for pid in pids)


# --- held folders and their sweep -----------------------------------------------------


def tmux(sock, *args):
    return subprocess.run(["tmux", "-S", str(sock), *args], capture_output=True, text=True)


def start_tmux(sock: Path) -> int:
    sock.parent.mkdir(parents=True, exist_ok=True)
    started = tmux(sock, "-f", "/dev/null", "new-session", "-d", "sleep", "600")
    assert started.returncode == 0, started.stderr
    return int(tmux(sock, "display-message", "-p", "#{pid}").stdout)


def peer(sock: Path) -> tuple[int, int]:
    """The pid and uid the kernel gives for what listens on ``sock``: connected to without
    a byte sent, and closed."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_NONBLOCK)
    with probe:
        probe.connect(str(sock))
        size = struct.calcsize("3i")
        pid, uid, _ = struct.unpack("3i", probe.getsockopt(SOL_SOCKET, SO_PEERCRED, size))
    return pid, uid


def test_the_listener_of_a_tmux_servers_socket_is_its_server(stem):
    """What the sweep tells a tmux server by: the kernel's credentials of what listens on its
    socket are the server's own, its program is tmux, and a connection that sends nothing
    leaves it answering."""
    with hf.tmux_sockets(stem + "rch-") as folder:
        sock = folder / "x.sock"
        pid = start_tmux(sock)
        assert peer(sock) == (pid, os.getuid())
        assert Path(os.readlink(f"/proc/{pid}/exe")).name == "tmux"
        answered = tmux(sock, "display-message", "-p", "#{pid}")
        assert answered.returncode == 0 and int(answered.stdout) == pid, answered.stderr


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


def test_the_killed_run_sweep_passes_over_a_socket_another_program_listens_on_anywhere_in_it(
    listener, capsys
):
    """As a killed run's engine folder holds its engine's live socket: the next stem clears the
    folder, sends nothing to the socket, and says it left its listener."""
    left = Path(tempfile.mkdtemp(prefix=kr.STEM, dir=hf.TMP))
    (left / hf.LOCK).touch()  # a lock file nothing holds
    beside = Path(f"{left}-engine")
    sock = beside / "a" / "b" / "s.sock"
    other = listener(sock)
    heard(other, sock)
    with kr.stem():
        assert not os.path.lexists(left) and not os.path.lexists(beside)
    sent = other.close()
    assert not any(sent), sent
    assert f"left the listener on {sock}: " in capsys.readouterr().err


def test_a_socket_nothing_listens_on_starts_no_tmux(stem, monkeypatch):
    with hf.held(stem + "rch-") as folder:
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(folder / "dead.sock"))
        dead.close()
        pid = start_tmux(folder / "x.sock")
        ran = recording_runs(monkeypatch)
        hf.stop_tmux(folder)
        monkeypatch.undo()
        assert not kr.running(pid)
        assert ran and all(str(folder / "x.sock") in argv for argv, _ in ran), ran  # the control
        assert not [argv for argv, _ in ran if str(folder / "dead.sock") in argv], ran


def recording_runs(monkeypatch, *, timing_out: str | None = None) -> list[tuple[list, dict]]:
    """Every subprocess.run from now on, with its keywords; one whose argv holds
    ``timing_out`` raises TimeoutExpired instead of running."""
    run, ran = subprocess.run, []

    def recording(argv, *args, **kwargs):
        ran.append((list(argv), kwargs))
        if timing_out is not None and timing_out in argv:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    return ran


def test_a_kill_server_that_does_not_return_in_time_is_left_and_said(stem, monkeypatch, capsys):
    """No tmux the sweep runs may wait without end: kill-server is bounded, and one that does
    not return in time leaves its server, said."""
    with hf.held(stem + "rch-") as folder:
        sock = folder / "x.sock"
        pid = start_tmux(sock)
        ran = recording_runs(monkeypatch, timing_out="kill-server")
        try:
            hf.stop_tmux(folder)
        finally:
            monkeypatch.undo()
        try:
            assert [argv[-1] for argv, _ in ran] == ["kill-server"]  # the control: got that far
            assert all(kwargs.get("timeout") == hf.TMUX_GONE_WITHIN for _, kwargs in ran), ran
            assert f"held_folders: left the listener on {sock}: " in capsys.readouterr().err
            assert kr.running(pid)
        finally:
            tmux(sock, "kill-server")


def test_a_tmux_server_whose_program_was_replaced_on_disk_is_still_stopped(stem, monkeypatch):
    """A tmux upgraded while a killed run's server lived: its exe reads as ``tmux (deleted)``."""
    with hf.held(stem + "rch-") as folder:
        pid = start_tmux(folder / "x.sock")
        readlink = os.readlink
        monkeypatch.setattr(
            os,
            "readlink",
            lambda path, *a, **k: (
                readlink(path, *a, **k) + (" (deleted)" if str(path).endswith("/exe") else "")
            ),
        )
        assert Path(os.readlink(f"/proc/{pid}/exe")).name == "tmux (deleted)"  # the plant took
        try:
            hf.stop_tmux(folder)
        finally:
            monkeypatch.undo()
        assert not kr.running(pid)


def test_a_socket_gone_before_it_is_looked_at_starts_no_tmux_and_is_not_said(
    stem, monkeypatch, capsys
):
    """A socket removed between the listing of the folder and the look at it, as a run that
    ends meanwhile removes its own: nothing to stop, nothing to say."""
    with hf.held(stem + "rch-") as folder:
        ran = recording_runs(monkeypatch)
        hf._stop_server(folder / "gone.sock")
        monkeypatch.undo()
        assert ran == [] and capsys.readouterr().err == ""
        start_tmux(folder / "x.sock")
        ran = recording_runs(monkeypatch)
        hf._stop_server(folder / "x.sock")
        monkeypatch.undo()
        assert ran  # the control: a live one is stopped through tmux


def abandoned(parent: Path, prefix: str, *, tmux_in: str | None = "x.sock") -> tuple[Path, int]:
    """A folder as a killed run leaves one: its lock file, which nothing holds, and a tmux
    server still running on a socket in it."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    (folder / hf.LOCK).touch()
    pid = start_tmux(folder / tmux_in) if tmux_in else 0
    return folder, pid


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


def test_a_held_folder_is_held_while_in_use(stem):
    with hf.held(stem + "rch-") as folder:
        assert folder.parent == hf.TMP and hf.made_with(stem + "rch-")(folder.name)
        assert (folder / hf.LOCK).is_file() and not hf.unheld(folder)
    assert hf.unheld(folder)  # gone


def test_a_live_runs_folder_is_left_alone(stem):
    prefix = stem + "rch-"
    with hf.tmux_sockets(prefix) as live:
        pid = start_tmux(live / "x.sock")
        with hf.tmux_sockets(prefix) as other:
            assert other != live  # positive control: the second swept the prefix
            assert live.is_dir() and kr.running(pid)


def test_the_next_run_of_a_prefix_clears_only_that_prefix(stem):
    prefix = stem + "rch-"
    left, pid = abandoned(hf.TMP, prefix)
    longer, longer_pid = abandoned(hf.TMP, prefix + "x-")
    other, other_pid = abandoned(hf.TMP, stem + "rcb-")
    shorter, shorter_pid = abandoned(hf.TMP, stem + "rc")
    with hf.tmux_sockets(prefix):
        assert not left.exists() and not kr.running(pid)
        for folder, server in ((longer, longer_pid), (other, other_pid), (shorter, shorter_pid)):
            assert folder.is_dir() and kr.running(server), folder


@pytest.mark.parametrize(
    ("name", "taken"),
    [
        ("rch-abcd_123", True),
        ("rch-abcd_12", False),
        ("rch-abcd_1234", False),
        ("rch-ABCD_123", False),
        ("rch-abcd-123", False),
        ("rch-x-abcd_123", False),
        ("xrch-abcd_123", False),
        ("rch-", False),
    ],
)
def test_made_with_takes_only_what_mkdtemp_makes_with_the_prefix(name, taken):
    assert hf.made_with("rch-")(name) is taken


def test_made_with_takes_every_name_mkdtemp_makes(tmp_path):
    for _ in range(50):
        made = Path(tempfile.mkdtemp(prefix="rchmw-", dir=tmp_path))
        assert hf.made_with("rchmw-")(made.name), made.name


def test_a_folder_without_a_lock_file_is_cleared_only_once_old(stem):
    prefix = stem + "rch-"
    young = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    old = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    then = old.stat().st_mtime - hf.UNLOCKED_FOR - 1
    os.utime(old, (then, then))
    hf.clear_abandoned(hf.TMP, hf.made_with(prefix), label="test")
    assert young.is_dir() and not old.exists()
    young.rmdir()


def test_a_folder_another_sweep_removed_first_is_passed_over_in_silence(stem, capsys):
    prefix = stem + "rch-"
    left, _ = abandoned(hf.TMP, prefix, tmux_in=None)

    def already_gone(folder):
        hf.remove(folder)
        folder.rmdir()  # as though another sweep had removed it a moment before

    hf.clear_abandoned(hf.TMP, hf.made_with(prefix), remover=already_gone, label="test")
    assert not left.exists()
    assert capsys.readouterr().err == ""


def test_what_cannot_be_removed_is_left_and_said(stem, capsys):
    prefix = stem + "rch-"
    left, _ = abandoned(hf.TMP, prefix, tmux_in=None)
    stuck = left / "stuck"
    stuck.mkdir()
    (stuck / "file").touch()
    stuck.chmod(0o500)
    later, _ = abandoned(hf.TMP, prefix, tmux_in=None)
    hf.clear_abandoned(hf.TMP, hf.made_with(prefix), label="the label")
    assert f"the label: left {left}" in capsys.readouterr().err
    assert (stuck / "file").exists()
    assert not later.exists()  # it went on past the one it could not remove
    stuck.chmod(0o700)


def test_a_removal_that_fails_in_its_own_way_is_left_and_said_and_the_others_cleared(stem, capsys):
    """A remover of a caller's own, as the tests' engine's is, may fail with an error of its own
    (RuntimeError, say): that folder is left and said, and the clearing goes on past it."""
    prefix = stem + "rch-"
    first, _ = abandoned(hf.TMP, prefix, tmux_in=None)
    second, _ = abandoned(hf.TMP, prefix, tmux_in=None)
    refused = min(first, second)  # cleared in the order of their names

    def remover(folder):
        if folder == refused:
            raise RuntimeError("rm: skipping a mount, since it's on a different device")
        hf.remove(folder)

    try:
        hf.clear_abandoned(hf.TMP, hf.made_with(prefix), remover=remover, label="the label")
        said = capsys.readouterr().err
        assert f"the label: left {refused}: RuntimeError: rm: skipping a mount" in said, said
        assert refused.is_dir()  # left
        assert not max(first, second).exists()  # and the other cleared
    finally:
        for folder in (first, second):
            if folder.exists():
                hf.remove(folder)


def test_unheld_says_whether_anything_holds_a_folder(tmp_path):
    folder = tmp_path / "f"
    assert hf.unheld(folder)  # gone
    folder.mkdir()
    assert not hf.unheld(folder)  # being made: no lock file yet
    then = folder.stat().st_mtime - hf.UNLOCKED_FOR - 1
    os.utime(folder, (then, then))
    assert hf.unheld(folder)  # long without one
    held_file = hf.lock(folder)
    assert not hf.unheld(folder)
    held_file.close()
    assert hf.unheld(folder)
    assert not hf.unheld(folder, uid=os.getuid() + 1)  # someone else's


def test_a_process_whose_first_thread_has_ended_runs_while_another_thread_does(tmp_path):
    """A process whose first thread has ended reads Z while another thread runs, and holds its
    files, and with them a held folder's lock, until the last thread has ended and been
    released: only then does it read Z with one thread."""
    folder = tmp_path / "held"
    folder.mkdir()
    with kr.FirstThreadEnded(tmp_path, holding=hf.lock(folder)) as helper:
        pid = helper.start()  # which closes this process's copy of the lock
        assert hf.state_and_threads(pid) == kr.FIRST_THREAD_ENDED
        assert not hf.unheld(folder)  # the control: it still holds the lock
        assert hf.running(pid)
        helper.release()
        kr.wait_for_state(pid, kr.ENDED)  # ended, not yet reaped
        assert not hf.running(pid)
        assert hf.unheld(folder)


def test_remove_takes_the_lock_file_last(stem, monkeypatch):
    with hf.held(stem + "rch-") as folder:
        (folder / "a").mkdir()
        (folder / "b").touch()
        unlink, seen = os.unlink, []

        def unlinking(path, *args, **kwargs):
            if os.path.basename(path) == hf.LOCK:
                seen.append(sorted(os.listdir(folder)))
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlinking)
    assert seen == [[hf.LOCK]] and not folder.exists()


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


def test_remove_refuses_a_link(stem, tmp_path):
    (tmp_path / "keep").touch()
    link = hf.TMP / (stem + "link")
    link.symlink_to(tmp_path)
    with pytest.raises(hf.UnsafeRemoval):
        hf.remove(link)
    assert (tmp_path / "keep").exists()


def test_stop_tmux_stops_only_sockets_of_its_own_in_the_folder_itself(stem, tmp_path):
    victim = start_tmux(tmp_path / "victim" / "v.sock")
    with hf.held(stem + "rch-") as folder:
        mine = start_tmux(folder / "x.sock")
        (folder / "y.sock").symlink_to(tmp_path / "victim" / "v.sock")
        hf.stop_tmux(folder)
        assert not kr.running(mine) and kr.running(victim)
    tmux(tmp_path / "victim" / "v.sock", "kill-server")


def test_a_sweep_that_is_refused_a_removal_leaves_it_says_so_and_goes_on(
    tmp_path, monkeypatch, capsys
):
    """Fails closed: an abandoned folder outside the roots removals may reach is left,
    and the sweep goes on to the next."""
    monkeypatch.setattr(hf, "TMP", tmp_path / "the-only-root")
    first, _ = abandoned(tmp_path, "rchref-", tmux_in=None)
    second, _ = abandoned(tmp_path, "rchref-", tmux_in=None)
    hf.clear_abandoned(tmp_path, hf.made_with("rchref-"), label="the label")
    err = capsys.readouterr().err
    assert first.is_dir() and second.is_dir()
    for folder in (first, second):
        assert f"the label: left {folder}: refused" in err
