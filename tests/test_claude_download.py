"""The fake claude as a Claude Code release (tests/claude_download.py): what the
installer downloads in the container suite, as it downloads the real one."""

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
