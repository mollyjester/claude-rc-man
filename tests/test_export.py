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
]

# Files the deployment needs; they must not be export-ignored.
RUNTIME = ["README.md", "pyproject.toml", "uv.lock"]


def export_ignored(paths: list[str]) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "check-attr", "export-ignore", "--stdin"],
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


def test_every_development_file_is_export_ignored():
    corpus = docs_guard.corpus_files(REPO)
    dev_files = [p for p in corpus if under_dev_only(p)]
    # Non-vacuity: this very file is development-only.
    assert "tests/test_export.py" in dev_files
    assert set(dev_files) - export_ignored(dev_files) == set()


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
