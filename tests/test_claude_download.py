"""The fake claude as a Claude Code release (tests/claude_download.py): what the
installer downloads in the container suite, as it downloads the real one."""

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

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


def _repository_imports(rel: str, root: Path = claude_download.REPO) -> set[str]:
    """The repository's own files a Python file imports, read independently of the bundle."""
    found = set()
    for node in ast.walk(ast.parse((root / rel).read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            package = Path(rel).parent.as_posix().replace("/", ".")
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            names = [base, *(f"{base}.{a.name}" for a in node.names)]
        for name in names:
            path = root / Path(*name.split("."))
            for candidate in (path.with_suffix(".py"), path / "__init__.py"):
                if candidate.is_file():
                    found.add(candidate.relative_to(root).as_posix())
    return found


def test_the_bundle_holds_every_module_of_the_repository_its_python_imports():
    """The fake runs from the bundle alone: a module of the repository one of its files
    imports, and that one's own, must be in it."""
    python = [p for p in claude_download.PARTS if p.endswith(".py")]
    assert "devtools/fake_claude.py" in python and "helpers/clock.py" in python
    for rel in python:
        assert _repository_imports(rel) <= set(python), rel


def test_the_bundle_follows_an_import_planted_in_a_module_it_holds(tmp_path):
    """Planted twin: the parts are derived, so a new import is followed."""
    files = {
        "devtools/__init__.py": "",
        "devtools/fake_claude.py": "from devtools import first\n",
        "devtools/first.py": "import helpers.second\nfrom . import third\n",
        "devtools/third.py": "from .fourth import name\n",
        "devtools/fourth.py": "name = 1\n",
        "helpers/__init__.py": "",
        "helpers/second.py": "import json\n",
        "helpers/unused.py": "",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    parts = set(claude_download.python_parts(tmp_path))
    assert parts == set(files) - {"helpers/unused.py"}
    for rel in parts:  # the independent reading agrees on the planted tree, relative imports too
        assert _repository_imports(rel, tmp_path) <= parts, rel
