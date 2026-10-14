"""The fake claude as a Claude Code release (tests/claude_download.py): what the
installer downloads in the container suite, as it downloads the real one."""

import contextlib
import functools
import http.server
import os
import subprocess
import threading

from devtools import record_claude as rec
from installer import install as ins
from tests import claude_download


@contextlib.contextmanager
def served(folder):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_installer_puts_the_fake_release_in_the_store_and_it_runs(tmp_path):
    releases = tmp_path / "releases"
    claude_download.release(releases, "9.8.7")
    store = tmp_path / "store"
    with served(releases) as url:
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
    assert ran.stdout.strip() == f"{rec.load_manifest()['claude_version']} (Claude Code)"
    assert os.listdir(unpacked)  # where it unpacked itself: under TMPDIR, not elsewhere


def test_a_release_whose_binary_changed_is_refused(tmp_path):
    releases = tmp_path / "releases"
    claude_download.release(releases, "9.8.7")
    binary = releases / "9.8.7" / ins.claude_platform() / "claude"
    binary.write_bytes(binary.read_bytes() + b"\n# changed\n")
    with served(releases) as url:
        try:
            ins.ensure_claude_store(tmp_path / "store", url, "9.8.7")
        except ins.InstallError as error:
            assert "checksum" in str(error)
        else:
            raise AssertionError("a changed binary was taken")
