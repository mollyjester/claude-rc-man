"""devtools/held_folders.py: folders a run holds under /tmp, and the clearing of those a
killed run left."""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests import killed_runs as kr

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
