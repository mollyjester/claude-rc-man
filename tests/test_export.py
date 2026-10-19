"""What a deployment receives: `git archive` minus everything marked export-ignore.

`install.sh` deploys with `git archive`, so `.gitattributes` decides what reaches
the deployed copy. Tests and development tooling must never reach it.
"""

import io
import subprocess
import tarfile
from pathlib import Path

from devtools import docs_guard

REPO = Path(__file__).resolve().parent.parent

# Development-only locations; every file under a directory entry, or the file
# itself, must be export-ignored.
DEV_ONLY = [
    "tests/",
    "devtools/",
    "frontend/",
    "installer/",
    "install.sh",
    ".claude/",
    "project-knowledge/",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "Makefile",
    ".devcontainer/",
]

# Files the deployment needs; they must not be export-ignored.
RUNTIME = ["README.md", "pyproject.toml", "uv.lock"]


def export_ignored(paths: list[str], repo: Path = REPO) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "check-attr", "export-ignore", "--stdin"],
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ignored = set()
    for line in out.splitlines():
        path, _, value = line.rpartition(": ")
        if value == "set":
            ignored.add(path.removesuffix(": export-ignore"))
    return ignored


def under_dev_only(path: str) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in DEV_ONLY
    )


def unmarked_development_files(repo: Path, files: list[str]) -> set[str]:
    """The development-only files among ``files`` that ``repo`` does not export-ignore."""
    dev_files = [p for p in files if under_dev_only(p)]
    return set(dev_files) - export_ignored(dev_files, repo)


def test_every_development_file_is_export_ignored():
    corpus = docs_guard.corpus_files(REPO)
    # Non-vacuity: this very file is development-only.
    assert "tests/test_export.py" in [p for p in corpus if under_dev_only(p)]
    assert unmarked_development_files(REPO, corpus) == set()


def test_runtime_files_are_exported():
    corpus = set(docs_guard.corpus_files(REPO))
    assert set(RUNTIME) <= corpus
    assert export_ignored(RUNTIME) == set()


def test_git_archive_honours_the_patterns_used(tmp_path):
    """Probe the mechanism itself, with this repository's own .gitattributes."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_bytes((REPO / ".gitattributes").read_bytes())
    for name in ["tests/test_x.py", "devtools/tool.py", "CLAUDE.md", "README.md"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "probe"], check=True)

    archive = subprocess.run([*git, "archive", "HEAD"], capture_output=True, check=True).stdout
    names = tarfile.open(fileobj=io.BytesIO(archive)).getnames()

    assert "README.md" in names  # positive control
    assert not [n for n in names if n.startswith(("tests", "devtools")) or n == "CLAUDE.md"]


def test_the_check_reports_a_development_file_that_is_not_export_ignored(tmp_path):
    """Planted twin: a repository whose .gitattributes no longer marks devtools/."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    planted = "".join(
        line for line in attributes.splitlines(keepends=True) if not line.startswith("/devtools")
    )
    assert planted != attributes  # the plant took effect: devtools/ is no longer marked
    (tmp_path / ".gitattributes").write_text(planted, encoding="utf-8")
    files = ["devtools/tool.py", "tests/test_x.py"]
    assert unmarked_development_files(tmp_path, files) == {"devtools/tool.py"}
    (tmp_path / ".gitattributes").write_text(attributes, encoding="utf-8")
    assert unmarked_development_files(tmp_path, files) == set()  # and clean when marked


# --- no tracked link ----------------------------------------------------------------------
# The upgrade from the web interface refuses a clone whose tracked paths hold a link
# (installer/upgrade.py), and a release is built from what git tracks: so none is tracked.

LINK_MODE = "120000"


def tracked_links(repo: Path) -> list[str]:
    """The tracked paths of ``repo`` that are symbolic links, as git's index has them."""
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    entries = [entry for entry in listed.split("\0") if entry]
    assert all("\t" in entry for entry in entries), entries[:3]  # the listing is as read
    return [entry.split("\t", 1)[1] for entry in entries if entry.split(" ", 1)[0] == LINK_MODE]


def test_no_tracked_file_is_a_link():
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "install.sh" in listed  # non-vacuity: the index is read
    assert tracked_links(REPO) == []


def test_a_tracked_link_is_found(tmp_path):
    """Planted twin: a link committed in a scratch repository, beside a file."""
    from tests import git_fixtures

    repo = git_fixtures.make_repo(tmp_path / "repo")
    assert tracked_links(repo) == []  # positive control: a file is not a link
    (repo / "installer").mkdir()
    (repo / "installer" / "planted").symlink_to("../README.md")
    git_fixtures.fixture_git(repo, "add", "installer/planted")
    git_fixtures.fixture_git(repo, "commit", "-q", "-m", "a link")
    assert (repo / "installer" / "planted").is_symlink()  # the plant took effect
    assert tracked_links(repo) == ["installer/planted"]
