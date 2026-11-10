"""tests/containers.py: the labels a run gives the containers it starts, and the images the
installer's Containerfiles start from, by digest.

What needs the engine itself is in tests/test_container_suite.py."""

from pathlib import Path

from tests import containers as ct


def test_labels_name_the_holder(tmp_path):
    holder = tmp_path / "holder"
    assert ct.labels(holder) == ["--label", f"{ct.HOLDER}={holder}"]


def test_the_installers_containerfiles_name_images_by_digest_for_the_suite_to_have_ready():
    root = Path(__file__).resolve().parent.parent
    found = [
        ref
        for name in ("workspace", "builder")
        for ref in ct.digest_references(root / "installer" / name / "Containerfile")
    ]
    assert found and all("@sha256:" in ref for ref in found)  # non-vacuity: the list is not empty
