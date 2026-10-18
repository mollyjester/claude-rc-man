"""tests/containers.py: the labels a run gives the containers it starts, and their removal.

What needs the engine itself is in tests/test_container_suite.py; this reads labels only."""

from pathlib import Path

from tests import containers as ct


def test_labels_name_the_holder(tmp_path):
    holder = tmp_path / "holder"
    assert ct.labels(holder) == ["--label", f"{ct.HOLDER}={holder}"]


def test_containers_listed_are_read_past_what_inspect_could_not_read(tmp_path):
    """podman inspect prints an empty line, or none, for a container gone meanwhile."""
    engine = tmp_path / "podman"
    engine.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  ps) printf 'c1\\nc2\\nc3\\n' ;;\n"
        '  inspect) printf \'c1 {"a": "b"}\\n\\nc3 null\\n\'; exit 1 ;;\n'
        "esac\n"
    )
    engine.chmod(0o755)
    assert ct._labelled([str(engine)]) == {"c1": {"a": "b"}, "c3": {}}


def test_a_removal_waits_until_the_container_and_its_anonymous_volumes_are_gone(tmp_path):
    """Another run removing it at the same time leaves it listed for a while. Its
    anonymous volumes go with it."""
    state = tmp_path / "state"
    state.mkdir()
    engine = tmp_path / "podman"
    engine.write_text(
        "#!/bin/sh\n"
        f"cd {state}\n"
        'case "$1" in\n'
        '  rm) echo "$*" > removal; touch removed ;;\n'
        "  ps) if [ -e removed ] && [ -e listed-twice ]; then exit 0; fi\n"
        "      [ -e removed ] && { [ -e listed ] && touch listed-twice; touch listed; }\n"
        "      echo c1 ;;\n"
        "esac\n"
    )
    engine.chmod(0o755)
    ct._remove([str(engine)], ["c1"])
    assert (state / "listed-twice").exists()  # it was still listed after the rm
    assert ct._listed([str(engine)]) == []
    assert (state / "removal").read_text().split() == ["rm", "-f", "-v", "c1"]


# --- the images the installer builds from, by digest -----------------------------------------


def test_the_images_a_containerfile_starts_from_by_digest_are_listed_in_order_and_once(tmp_path):
    digest = "sha256:" + "a" * 64
    other = "sha256:" + "b" * 64
    file = tmp_path / "Containerfile"
    file.write_text(
        "# FROM docker.io/library/commented:1@" + digest + "\n"
        f"FROM docker.io/library/node:24@{digest} AS cli\n"
        "RUN true\n"
        f"FROM --platform=linux/amd64 docker.io/library/ubuntu:24.04@{other}\n"
        "FROM cli AS later\n"  # a stage of the file itself
        "FROM scratch\n"
        "FROM docker.io/library/unpinned:1\n"
        f"FROM docker.io/library/node:24@{digest}\n"  # again
    )
    assert ct.digest_references(file) == [
        f"docker.io/library/node:24@{digest}",
        f"docker.io/library/ubuntu:24.04@{other}",
    ]


def test_the_installers_containerfiles_name_images_by_digest_for_the_suite_to_have_ready():
    root = Path(__file__).resolve().parent.parent
    found = [
        ref
        for name in ("workspace", "builder")
        for ref in ct.digest_references(root / "installer" / name / "Containerfile")
    ]
    assert found and all("@sha256:" in ref for ref in found)  # non-vacuity: the list is not empty
