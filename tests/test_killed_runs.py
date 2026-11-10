"""tests/killed_runs.py: the harness that runs the suite and kills it, and the safety
of everything it reads and removes."""

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests import killed_runs as kr
from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent
TMP = hf.TMP


@pytest.fixture
def reports(tmp_path, monkeypatch):
    folder = tmp_path / "reports"
    folder.mkdir()
    monkeypatch.setenv(kr.ENV_REPORTS, str(folder))
    return folder


# --- reports are written whole ------------------------------------------------------


def test_a_report_appears_only_complete(reports, monkeypatch):
    """Written under another name and renamed, so its own name never holds less."""
    seen, rename = [], os.rename

    def renaming(src, dst, *args, **kwargs):
        seen.append((os.path.exists(dst), Path(src).read_text(encoding="utf-8")))
        return rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", renaming)
    kr.report("one", {"pids": {"a": 7}})
    assert seen == [(False, json.dumps({"pids": {"a": 7}}))]
    assert sorted(p.name for p in reports.iterdir()) == ["one.json"]


# --- every path read from a report is checked ---------------------------------------


@pytest.fixture
def stem_prefix():
    with kr.stem() as prefix:
        yield prefix


def test_a_path_in_a_report_or_a_scrub_is_refused_unless_under_its_own_prefix_or_tmp_path(
    tmp_path, stem_prefix, monkeypatch
):
    good = [str(TMP / (stem_prefix + "rch-x") / "a.sock"), str(tmp_path / "x")]
    for path in good:  # positive control
        assert kr.checked(path, (stem_prefix,), tmp_path) == Path(path)
    home = str(Path.home())
    with pytest.raises(kr.UnsafePath):
        kr.checked(home, (stem_prefix,), tmp_path)
    for module, attr in ((shutil, "rmtree"), (os, "unlink"), (os, "rmdir"), (os, "chmod")):
        monkeypatch.setattr(module, attr, _refusing(attr))
    with pytest.raises(kr.UnsafePath):
        kr.scrub(home, (stem_prefix,), tmp_path)


def _refusing(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"would {name} {args!r}")

    return refuse


def test_scrub_removes_its_own_without_following_links(tmp_path, stem_prefix):
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "file").write_text("kept")
    left = TMP / (stem_prefix + "rch-left")
    (left / "deep").mkdir(parents=True)
    (left / "deep" / "out").symlink_to(kept)
    (left / "deep").chmod(0)
    sock = left / "x.sock"
    started = subprocess.run(
        [
            "tmux",
            "-S",
            str(sock),
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "sleep",
            f"{3 * HANG_GUARD:g}",
        ]
    )
    assert started.returncode == 0 and stat.S_ISSOCK(sock.lstat().st_mode)
    kr.scrub(str(left), (stem_prefix,), tmp_path)
    assert not os.path.lexists(left)
    assert (kept / "file").read_text() == "kept"
    alive = subprocess.run(["tmux", "-S", str(sock), "has-session"], capture_output=True)
    assert alive.returncode != 0


# --- stems ----------------------------------------------------------------------------


def test_a_killed_stem_is_cleared_with_what_was_made_beside_it(monkeypatch):
    """As a killed test leaves it: the stem and what is beside it, and nothing holding
    either; the next stem clears both, and leaves a stem still held alone. The stems
    here are named after one of their own, so no other test's stem takes them."""
    with kr.stem() as outer:
        monkeypatch.setattr(kr, "STEM", outer + kr.STEM)
        with kr.stem() as live:
            made = Path(tempfile.mkdtemp(prefix=kr.STEM, dir="/tmp"))
            (made / hf.LOCK).touch()
            beside = TMP / (made.name + "-rch-x")
            beside.mkdir()
            others = TMP / (live + "rch-x")
            others.mkdir()
            with kr.stem():
                assert not made.exists() and not beside.exists()
                assert others.is_dir()  # held by a stem that still runs


# --- the run ------------------------------------------------------------------------


TINY = """
import os
from tests import killed_runs as kr

def test_one():
    kr.report("one", {"pids": {"worker": os.getpid()}})
    kr.hold()

def test_two():
    kr.report("two", {"pids": {"worker": os.getpid()}})
    kr.hold()
"""


def test_a_killed_run_is_gone_with_its_workers(tmp_path):
    with kr.stem() as prefix, kr.KilledRun(tmp_path, {"test_tiny.py": TINY}, (prefix,)) as run:
        pids = {run.reports.wait(name)["pids"]["worker"] for name in ("one", "two")}
        assert len(pids) == 2 and all(Path(f"/proc/{p}").exists() for p in pids)
        run.kill(signal.SIGKILL)
        assert not run.alive()
        assert not any(kr.running(p) for p in pids)
