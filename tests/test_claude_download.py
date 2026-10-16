"""The fake claude as a Claude Code release (tests/claude_download.py): what the
installer downloads in the container suite, as it downloads the real one."""

import hashlib
import json
import os
import subprocess

from installer import install as ins
from tests import claude_download


def as_the_test_user(monkeypatch):
    """The store is made by rcman's account; here the same commands run as the test's user."""
    real = ins.run

    def run(argv, user=None, **kw):
        rest = argv[4:] if argv[:4] == ["runuser", "-u", ins.APP_USER, "--"] else argv
        command = [a for a in rest if "=" not in a and a not in ("/usr/bin/env", "-i")]
        return real(command if rest is not argv else argv, user=user, **kw)

    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "app_uid", lambda: 0)


def test_the_installer_puts_the_fake_release_in_the_store_and_it_runs(tmp_path, monkeypatch):
    as_the_test_user(monkeypatch)
    releases = tmp_path / "releases"
    claude_download.release(releases, "9.8.7")
    store = tmp_path / "store"
    with claude_download.served(releases) as url:
        assert ins.ensure_claude_store(store, url, "9.8.7") == ["claude 9.8.7"]
    unpacked = tmp_path / "tmp"
    unpacked.mkdir()
    ran = subprocess.run(
        [str(store / "bin" / "claude"), "--version"],
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(unpacked), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "9.8.7 (Claude Code)"  # the version it is released as
    assert os.listdir(unpacked)  # where it unpacked itself: under TMPDIR, not elsewhere


def test_a_release_whose_binary_changed_is_refused(tmp_path, monkeypatch):
    as_the_test_user(monkeypatch)
    releases = tmp_path / "releases"
    claude_download.release(releases, "9.8.7")
    binary = releases / "9.8.7" / ins.claude_platform() / "claude"
    binary.write_bytes(binary.read_bytes() + b"\n# changed\n")
    with claude_download.served(releases) as url:
        try:
            ins.ensure_claude_store(tmp_path / "store", url, "9.8.7")
        except ins.InstallError as error:
            assert "checksum" in str(error)
        else:
            raise AssertionError("a changed binary was taken")


def run_version(binary, tmp_path):
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir(exist_ok=True)
    ran = subprocess.run(
        ["/bin/sh", str(binary), "--version"],  # a release's file is not marked runnable
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(unpacked), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    return ran.stdout.strip()


def test_a_release_reports_the_version_it_is_released_as(tmp_path):
    releases = tmp_path / "releases"
    for version in ("3.1.4", "3.1.5"):
        claude_download.release(releases, version)
    reported = [
        run_version(releases / v / ins.claude_platform() / "claude", tmp_path)
        for v in ("3.1.4", "3.1.5")
    ]
    assert reported == ["3.1.4 (Claude Code)", "3.1.5 (Claude Code)"]


def test_the_kinds_of_release_differ_as_they_say(tmp_path):
    releases = tmp_path / "releases"
    for kind in claude_download.KINDS:
        claude_download.release(releases, f"4.0.{claude_download.KINDS.index(kind)}", kind)
    found = {}
    for number, kind in enumerate(claude_download.KINDS):
        folder = releases / f"4.0.{number}"
        program = folder / ins.claude_platform() / "claude"
        listed = json.loads((folder / "manifest.json").read_text())["platforms"]
        found[kind] = (
            run_version(program, tmp_path),
            listed[ins.claude_platform()]["checksum"]
            == hashlib.sha256(program.read_bytes()).hexdigest(),
        )
    assert found == {
        "good": ("4.0.0 (Claude Code)", True),
        "broken": (f"{claude_download.BROKEN_VERSION} (Claude Code)", True),
        "failing": ("4.0.2 (Claude Code)", False),
    }


def test_the_latest_file_names_the_release_made_last(tmp_path):
    releases = tmp_path / "releases"
    claude_download.release(releases, "4.0.0")
    assert (releases / "latest").read_text() == "4.0.0"
    claude_download.release(releases, "4.0.1")
    assert (releases / "latest").read_text() == "4.0.1"
    claude_download.release(releases, "4.0.2", latest=False)
    assert (releases / "latest").read_text() == "4.0.1"


def test_an_unknown_kind_of_release_is_refused(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        claude_download.release(tmp_path, "4.0.0", "perfect")
