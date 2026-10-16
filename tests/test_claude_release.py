"""Claude Code's releases, as rcman and the installer fetch them (helpers/claude_release.py):
the manifest, the platform, a download checked against its checksum and held to a size, and
the version a release names as the latest."""

import hashlib
import json

import pytest

from helpers import claude_release as cr
from tests import claude_download


@pytest.fixture
def releases(tmp_path):
    folder = tmp_path / "releases"
    folder.mkdir()
    return folder


def release(folder, version="9.8.7", **kwargs):
    claude_download.release(folder, version, **kwargs)


def failure(call, *args, **kwargs) -> cr.ReleaseError:
    with pytest.raises(cr.ReleaseError) as raised:
        call(*args, **kwargs)
    return raised.value


def left_over(folder):
    return sorted(p.name for p in folder.iterdir())


# --- the platform ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "machine,key",
    [
        ("x86_64", "linux-x64"),
        ("amd64", "linux-x64"),
        ("aarch64", "linux-arm64"),
        ("arm64", "linux-arm64"),
    ],
)
def test_the_platform_key_follows_the_machine(machine, key):
    assert cr.platform_key(machine) == key


def test_a_machine_with_no_build_is_told_so():
    assert failure(cr.platform_key, "riscv64").kind == "no_build"


# --- the manifest ----------------------------------------------------------------------


def test_the_manifest_of_a_release_is_read(releases):
    release(releases)
    with claude_download.served(releases) as url:
        found = cr.manifest(url, "9.8.7")
    assert found["platforms"][cr.platform_key()]["checksum"]


def test_a_version_the_release_site_does_not_have_is_no_such_version(releases):
    release(releases)
    with claude_download.served(releases) as url:
        assert failure(cr.manifest, url, "9.9.9").kind == "no_such_version"
        assert failure(cr.fetch, url, "9.9.9", releases / "claude").kind == "no_such_version"


def test_a_release_with_no_build_for_this_platform_is_told_so(releases, tmp_path):
    release(releases, platform="plan9-mips")
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        assert failure(cr.fetch, url, "9.8.7", target).kind == "no_build"
    assert not target.exists()
    assert left_over(tmp_path) == ["releases"]


def test_a_manifest_that_is_not_json_is_unreadable(releases):
    release(releases)
    (releases / "9.8.7" / "manifest.json").write_text("<html>nothing</html>")
    with claude_download.served(releases) as url:
        assert failure(cr.manifest, url, "9.8.7").kind == "network"


def test_an_address_that_answers_nothing_is_the_network(releases):
    release(releases)
    with claude_download.served(releases) as url:
        pass  # the server is gone: the address answers nothing
    assert failure(cr.manifest, url, "9.8.7").kind == "network"


def test_only_http_and_https_addresses_are_followed(tmp_path):
    (tmp_path / "9.8.7").mkdir()
    (tmp_path / "9.8.7" / "manifest.json").write_text("{}")
    assert failure(cr.manifest, tmp_path.as_uri(), "9.8.7").kind == "network"


# --- a download ---------------------------------------------------------------------------


def test_a_download_is_checked_and_put_in_place_whole_and_runnable(releases, tmp_path):
    release(releases)
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        cr.fetch(url, "9.8.7", target)
    expected = (releases / "9.8.7" / cr.platform_key() / "claude").read_bytes()
    assert target.read_bytes() == expected
    assert target.stat().st_mode & 0o777 == 0o755
    assert left_over(tmp_path) == ["claude", "releases"]  # no partial file


def test_a_checksum_that_does_not_match_is_refused_and_nothing_is_left(releases, tmp_path):
    release(releases, kind="failing")
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        assert failure(cr.fetch, url, "9.8.7", target).kind == "checksum"
    assert left_over(tmp_path) == ["releases"]


def test_a_download_larger_than_the_cap_is_refused_and_nothing_is_left(
    releases, tmp_path, monkeypatch
):
    release(releases)
    size = (releases / "9.8.7" / cr.platform_key() / "claude").stat().st_size
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        monkeypatch.setattr(cr, "MAX_BINARY_BYTES", size - 1)
        assert failure(cr.fetch, url, "9.8.7", target).kind == "too_big"
        assert left_over(tmp_path) == ["releases"]
        monkeypatch.setattr(cr, "MAX_BINARY_BYTES", size)  # positive control: at the cap, it goes
        cr.fetch(url, "9.8.7", target)
    assert target.exists()


def test_a_download_that_stops_part_way_is_the_network_and_leaves_nothing(
    releases, tmp_path, monkeypatch
):
    release(releases)
    real = cr._open

    class Short:
        """A response that ends early, as a connection that drops does."""

        def __init__(self, response):
            self.response = response
            self.headers = response.headers
            self.sent = 0

        def read(self, size):
            if self.sent:
                raise ConnectionResetError("reset")
            self.sent = 1
            return self.response.read(10)

        def close(self):
            self.response.close()

    def opening(url, *args, **kwargs):
        opened = real(url, *args, **kwargs)
        return Short(opened) if url.endswith("/claude") else opened

    monkeypatch.setattr(cr, "_open", opening)
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        assert failure(cr.fetch, url, "9.8.7", target).kind == "network"
    assert left_over(tmp_path) == ["releases"]


def test_a_partial_file_an_earlier_download_left_is_replaced(releases, tmp_path):
    release(releases)
    target = tmp_path / "claude"
    stale = tmp_path / f".{target.name}{cr.PARTIAL_SUFFIX}"
    stale.write_bytes(b"half of something")
    with claude_download.served(releases) as url:
        cr.fetch(url, "9.8.7", target)
    assert not stale.exists()
    assert target.read_bytes().startswith(b"#!/bin/sh")


def test_a_failed_download_leaves_a_binary_already_in_place_as_it_was(releases, tmp_path):
    release(releases, kind="failing")
    target = tmp_path / "claude"
    target.write_bytes(b"the one in place")
    with claude_download.served(releases) as url:
        failure(cr.fetch, url, "9.8.7", target)
    assert target.read_bytes() == b"the one in place"


def test_bytes_read_from_an_address_are_what_it_answers_and_a_checksum_is_checked_apart(releases):
    (releases / "file").write_bytes(b"content")
    good = hashlib.sha256(b"content").hexdigest()
    with claude_download.served(releases) as url:
        assert cr.read(f"{url}/file") == b"content"
        assert failure(cr.read, f"{url}/nothing").kind == "no_such_version"
    cr.check(good, good, "the file")
    assert failure(cr.check, "0" * 64, good, "the file").kind == "checksum"


def test_text_beyond_the_cap_is_refused(releases, monkeypatch):
    (releases / "file").write_bytes(b"x" * 50)
    with claude_download.served(releases) as url:
        assert len(cr.read(f"{url}/file")) == 50  # positive control
        monkeypatch.setattr(cr, "MAX_TEXT_BYTES", 49)
        assert failure(cr.read, f"{url}/file").kind == "too_big"


# --- the latest version ------------------------------------------------------------------------


def test_the_latest_version_is_what_the_release_site_names(releases):
    release(releases, "9.8.7")
    release(releases, "9.8.8")
    with claude_download.served(releases) as url:
        assert cr.latest(url) == "9.8.8"
        (releases / "latest").write_text("9.8.7\n")
        assert cr.latest(url) == "9.8.7"


@pytest.mark.parametrize("text", ["", "latest", "1.2", "1.2.3; rm -rf /", "<html>1.2.3</html>"])
def test_a_latest_that_is_not_a_version_is_not_taken(releases, text):
    (releases / "latest").write_text(text)
    with claude_download.served(releases) as url:
        assert failure(cr.latest, url).kind == "network"


def test_no_latest_is_no_such_version(releases):
    with claude_download.served(releases) as url:
        assert failure(cr.latest, url).kind == "no_such_version"


def test_the_manifest_the_fake_publishes_is_what_the_helper_reads(releases):
    release(releases)
    written = json.loads((releases / "9.8.7" / "manifest.json").read_text())
    with claude_download.served(releases) as url:
        assert cr.manifest(url, "9.8.7") == written


@pytest.mark.parametrize("version", ["../x", "1.2", "", "latest", "1.2.3/../../x", "1.2.3\n"])
def test_a_version_that_is_not_one_is_refused_before_anything_is_asked(releases, tmp_path, version):
    with claude_download.served(releases) as url:
        pass  # nothing answers: a request would be the network, not this
    target = tmp_path / "claude"
    assert failure(cr.manifest, url, version).kind == "no_such_version"
    assert failure(cr.fetch, url, version, target).kind == "no_such_version"
    assert not target.exists()
