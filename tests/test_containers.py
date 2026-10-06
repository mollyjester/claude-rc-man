"""tests/containers.py: which containers a run may remove as left by a killed run.

What needs docker itself is in tests/test_container_suite.py; this reads labels only."""

import os
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests import containers as ct
from tests import killed_runs as kr


def test_where_is_the_boot_and_the_folder_holders_are_made_in():
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    status = hf.TMP.stat()
    assert ct.where(hf.TMP / "rcct-anything") == f"{boot} {status.st_dev}:{status.st_ino}"


def test_labels_name_the_holder_the_owner_and_where(tmp_path):
    holder = tmp_path / "holder"
    labels = ct.labels(holder)
    assert labels == [
        "--label", f"{ct.HOLDER}={holder}",
        "--label", f"{ct.OWNER}={os.getuid()}",
        "--label", f"{ct.WHERE}={ct.where(holder)}",
    ]  # fmt: skip


@pytest.fixture
def stem(monkeypatch):
    """A stem, with this test's holders named after it, so that no other test's sweep
    takes them, nor this test's another's."""
    with kr.stem() as prefix:
        monkeypatch.setattr(ct, "HOLDERS", prefix + "rcct-")
        yield prefix


def _labels(holder: Path, **changed) -> dict[str, str]:
    labels = {ct.HOLDER: str(holder), ct.OWNER: str(os.getuid()), ct.WHERE: ct.where(holder)}
    return {**labels, **changed}


def test_only_a_container_whose_holder_nothing_holds_is_abandoned(stem):
    gone = hf.TMP / (ct.HOLDERS + "gone_abc")
    with hf.held(ct.HOLDERS) as live:
        found = {"gone": _labels(gone), "live": _labels(live)}
        assert ct.abandoned(found) == ["gone"]


@pytest.mark.parametrize(
    "changed",
    [
        {ct.OWNER: str(os.getuid() + 1)},
        {ct.OWNER: ""},
        {ct.WHERE: "another-boot 1:2"},
        {ct.WHERE: ""},
        {ct.HOLDER: "relative/holder"},
        {ct.HOLDER: ""},
    ],
    ids=["another owner", "no owner", "elsewhere", "nowhere", "relative holder", "no holder"],
)
def test_a_container_not_wholly_ours_is_never_abandoned(stem, changed):
    gone = hf.TMP / (ct.HOLDERS + "gone_abc")
    assert ct.abandoned({"x": _labels(gone)}) == ["x"]  # positive control
    assert ct.abandoned({"x": _labels(gone, **changed)}) == []


@pytest.mark.parametrize(
    "name", ["rcct-gone_abc", "{stem}rcct-gone_ab", "{stem}rcct-x-gone_abc", "{stem}other-gone_abc"]
)
def test_a_container_whose_holder_another_prefix_made_is_never_abandoned(stem, name):
    """As a held folder's sweep takes only its own prefix's folders."""
    mine = hf.TMP / (ct.HOLDERS + "gone_abc")
    assert ct.abandoned({"x": _labels(mine)}) == ["x"]  # positive control
    other = hf.TMP / name.format(stem=stem)
    assert ct.abandoned({"x": _labels(other)}) == []


def test_containers_listed_are_read_past_what_inspect_could_not_read(tmp_path):
    """docker inspect prints an empty line, or none, for a container gone meanwhile."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  ps) printf 'c1\\nc2\\nc3\\n' ;;\n"
        '  inspect) printf \'c1 {"a": "b"}\\n\\nc3 null\\n\'; exit 1 ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)
    assert ct._labelled([str(docker)]) == {"c1": {"a": "b"}, "c3": {}}


def test_a_container_without_our_labels_is_never_abandoned():
    assert ct.abandoned({"x": {}, "y": {"other": "label"}}) == []


def test_a_removal_waits_until_the_container_is_gone(tmp_path):
    """Another run removing it at the same time leaves it listed for a while."""
    state = tmp_path / "state"
    state.mkdir()
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"cd {state}\n"
        'case "$1" in\n'
        "  rm) touch removed ;;\n"
        "  ps) if [ -e removed ] && [ -e listed-twice ]; then exit 0; fi\n"
        "      [ -e removed ] && { [ -e listed ] && touch listed-twice; touch listed; }\n"
        "      echo c1 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    ct._remove([str(docker)], ["c1"])
    assert (state / "listed-twice").exists()  # it was still listed after the rm
    assert ct._listed([str(docker)]) == []
