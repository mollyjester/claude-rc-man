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


def test_a_planted_scripts_report_appears_only_complete(reports, tmp_path):
    script = tmp_path / "script"
    script.write_text(
        "#!/bin/sh\n" + kr.shell_reporter(reports) + 'report one "{\\"pids\\": {\\"a\\": 7}}"\n'
    )
    script.chmod(0o755)
    traced = subprocess.run(["sh", "-x", str(script)], capture_output=True, text=True)
    assert traced.returncode == 0, traced.stderr
    assert json.loads((reports / "one.json").read_text()) == {"pids": {"a": 7}}
    assert sorted(p.name for p in reports.iterdir()) == ["one.json"]
    written = [line for line in traced.stderr.splitlines() if "mv" in line]
    assert written and ".partial" in written[0]  # written elsewhere first, then renamed


# --- only complete reports are read --------------------------------------------------


@pytest.mark.parametrize("text", ["", "{", '{"paths": {"a": "/x"', "[]", "null"])
def test_a_report_not_yet_complete_is_not_read(tmp_path, text):
    (tmp_path / "one.json").write_text(text)
    assert kr.complete_report(tmp_path / "one.json") is None


def test_a_complete_report_is_read(tmp_path):
    (tmp_path / "one.json").write_text('{"pids": {"a": 7}}')
    assert kr.complete_report(tmp_path / "one.json") == {"pids": {"a": 7}}


def test_a_report_that_is_a_link_or_a_folder_is_not_read(tmp_path):
    (tmp_path / "real.json").write_text('{"pids": {"a": 7}}')
    (tmp_path / "one.json").symlink_to(tmp_path / "real.json")
    (tmp_path / "two.json").mkdir()
    assert kr.complete_report(tmp_path / "real.json")  # positive control
    assert kr.complete_report(tmp_path / "one.json") is None
    assert kr.complete_report(tmp_path / "two.json") is None


# --- every path read from a report is checked ---------------------------------------


@pytest.fixture
def stem_prefix():
    with kr.stem() as prefix:
        yield prefix


def unsafe_paths(tmp_path, prefix):
    elsewhere = TMP / (prefix + "link")
    return {
        "empty": "",
        "dot": ".",
        "relative": "some/folder",
        "dot-dot inside": str(TMP / (prefix + "x") / ".." / ".." / "etc"),
        "root": "/",
        "home": str(Path.home()),
        "repository": str(REPO),
        "outside /tmp": "/var/lib/x",
        "/tmp itself": str(TMP),
        "another prefix in /tmp": str(TMP / "rch-abcdefgh"),
        "the prefix alone": str(TMP / prefix),
        "tmp_path itself": str(tmp_path),
        "a link out": str(elsewhere / "x"),
        "not a string": 7,
    }


@pytest.mark.parametrize(
    "name",
    list(unsafe_paths(Path("/x"), "p-")),
)
def test_a_path_in_a_report_is_refused_unless_under_its_own_prefix_or_tmp_path(
    tmp_path, stem_prefix, name
):
    (TMP / (stem_prefix + "link")).symlink_to(Path.home())
    good = [str(TMP / (stem_prefix + "rch-x") / "a.sock"), str(tmp_path / "x")]
    for path in good:  # positive control
        assert kr.checked(path, (stem_prefix,), tmp_path) == Path(path)
    with pytest.raises(kr.UnsafePath):
        kr.checked(unsafe_paths(tmp_path, stem_prefix)[name], (stem_prefix,), tmp_path)


@pytest.mark.parametrize("prefix", ["", "a/b", "/"])
def test_a_prefix_that_would_take_anything_is_refused(tmp_path, prefix):
    with pytest.raises(kr.UnsafePath):
        kr.checked(str(TMP / "rch-abcdefgh"), (prefix,), tmp_path)


def test_reported_checks_the_paths_in_a_report(tmp_path, stem_prefix):
    reports = tmp_path / "reports"
    reports.mkdir()
    ok = {"paths": {"a": str(TMP / (stem_prefix + "x"))}, "pids": {"p": 5}}
    (reports / "ok.json").write_text(json.dumps(ok))
    (reports / "bad.json").write_text(json.dumps({"paths": {"a": "."}}))
    (reports / "pid.json").write_text(json.dumps({"pids": {"p": 1}}))
    read = kr.Reports(reports, (stem_prefix,), tmp_path)
    assert read.wait("ok", deadline=5) == ok  # positive control
    with pytest.raises(kr.UnsafePath):
        read.wait("bad", deadline=5)
    with pytest.raises(kr.UnsafePath):
        read.wait("pid", deadline=5)


def test_reported_waits_for_an_empty_report_and_gives_up_at_its_deadline(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "one.json").write_text("")
    with pytest.raises(TimeoutError):
        kr.Reports(reports, ("p-",), tmp_path).wait("one", deadline=0.5)


# --- cleanup refuses anything but its own -------------------------------------------


@pytest.mark.parametrize("name", list(unsafe_paths(Path("/x"), "p-")))
def test_scrub_refuses_what_is_not_its_own(tmp_path, stem_prefix, name):
    (TMP / (stem_prefix + "link")).symlink_to(Path.home())
    path = unsafe_paths(tmp_path, stem_prefix)[name]
    with pytest.MonkeyPatch.context() as patch:
        for module, attr in ((shutil, "rmtree"), (os, "unlink"), (os, "rmdir"), (os, "chmod")):
            patch.setattr(module, attr, _refusing(attr))
        with pytest.raises(kr.UnsafePath):
            kr.scrub(path, (stem_prefix,), tmp_path)


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
        ["tmux", "-S", str(sock), "-f", "/dev/null", "new-session", "-d", "sleep", "600"]
    )
    assert started.returncode == 0 and stat.S_ISSOCK(sock.lstat().st_mode)
    kr.scrub(str(left), (stem_prefix,), tmp_path)
    assert not os.path.lexists(left)
    assert (kept / "file").read_text() == "kept"
    alive = subprocess.run(["tmux", "-S", str(sock), "has-session"], capture_output=True)
    assert alive.returncode != 0


# --- stems ----------------------------------------------------------------------------


def test_a_stem_is_a_held_folder_whose_name_prefixes_what_is_made_beside_it():
    with kr.stem() as prefix:
        folder = TMP / prefix.removesuffix("-")
        assert folder.is_dir() and not hf.unheld(folder)
        beside = TMP / (prefix + "rch-x")
        beside.mkdir()
    assert not folder.exists() and not beside.exists()


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


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_killed_run_is_gone_with_its_workers(tmp_path, sig):
    with kr.stem() as prefix, kr.KilledRun(tmp_path, {"test_tiny.py": TINY}, (prefix,)) as run:
        pids = {run.reports.wait(name)["pids"]["worker"] for name in ("one", "two")}
        assert len(pids) == 2 and all(Path(f"/proc/{p}").exists() for p in pids)
        run.kill(sig)
        assert not run.alive()
        assert not any(kr.running(p) for p in pids)


def test_a_released_run_ends_on_its_own(tmp_path):
    with kr.stem() as prefix, kr.KilledRun(tmp_path, {"test_tiny.py": TINY}, (prefix,)) as run:
        run.reports.wait("one")
        run.reports.wait("two")
        code, output = run.finish()
    assert code == 0, output
    assert "2 passed" in output


@pytest.mark.parametrize("given", [Path("/"), Path("relative/tmp_path"), Path("."), Path.home()])
def test_a_tmp_path_that_could_take_anything_is_refused(stem_prefix, given, monkeypatch):
    path = str(TMP / (stem_prefix + "x"))
    assert kr.checked(path, (stem_prefix,), Path("/" + "tmp/pytest-x"))  # positive control
    with pytest.raises(kr.UnsafePath):
        kr.checked("/etc/x" if given == Path("/") else path, (stem_prefix,), given)
    for module, attr in ((shutil, "rmtree"), (os, "unlink"), (os, "rmdir"), (os, "chmod")):
        monkeypatch.setattr(module, attr, _refusing(attr))
    with pytest.raises(kr.UnsafePath):
        kr.scrub(path, (stem_prefix,), given)
