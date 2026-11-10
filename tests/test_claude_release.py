"""Claude Code's releases, as rcman and the installer fetch them (helpers/claude_release.py):
the manifest, a download checked against its checksum, and the version a release names as
the latest."""

import json

import pytest

from helpers import claude_release as cr
from tests import claude_download


@pytest.fixture
def releases(tmp_path):
    folder = tmp_path / "releases"
    folder.mkdir()
    return folder


def left_over(folder):
    return sorted(p.name for p in folder.iterdir())


def test_a_download_is_read_from_its_manifest_checked_and_put_in_place_whole_and_runnable(
    releases, tmp_path
):
    claude_download.release(releases, "9.8.7")
    written = json.loads((releases / "9.8.7" / "manifest.json").read_text())
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        assert cr.manifest(url, "9.8.7") == written  # as the release site publishes it
        cr.fetch(url, "9.8.7", target)
    expected = (releases / "9.8.7" / cr.platform_key() / "claude").read_bytes()
    assert target.read_bytes() == expected
    assert target.stat().st_mode & 0o777 == 0o755
    assert left_over(tmp_path) == ["claude", "releases"]  # no partial file


def test_a_checksum_that_does_not_match_is_refused_and_nothing_is_left(releases, tmp_path):
    claude_download.release(releases, "9.8.7", kind="failing")
    target = tmp_path / "claude"
    with claude_download.served(releases) as url:
        with pytest.raises(cr.ReleaseError) as raised:
            cr.fetch(url, "9.8.7", target)
    assert raised.value.kind == "checksum"
    assert left_over(tmp_path) == ["releases"]


def test_the_latest_version_is_what_the_release_site_names(releases):
    claude_download.release(releases, "9.8.7")
    claude_download.release(releases, "9.8.8")
    with claude_download.served(releases) as url:
        assert cr.latest(url) == "9.8.8"
        (releases / "latest").write_text("9.8.7\n")
        assert cr.latest(url) == "9.8.7"
