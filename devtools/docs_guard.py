"""Documentation guards.

Two checks, driven by ``tests/test_docs_guard.py``:

- **References resolve.** Every relative link and every ``*.md`` mention in any
  text file of the repository names a file or directory that exists in the
  repository. Links resolve relative to the file they are written in; mentions
  also resolve from the repository root or by bare file name.
- **Documentation describes the current state only.** Pages read by users of
  the service carry no historical phrasing. The phrase list is a regression net
  for known phrasings, not a completeness claim; review still enforces the rule.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
MENTION = re.compile(r"(?<![\w./-])(\w[\w./-]*\.md)\b")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

HISTORY_PATTERNS = [
    r"previously",
    r"(?<!\bis )(?<!\bare )(?<!\bbe )(?<!\bbeing )(?<!\bbeen )(?<!\bgets )used to",
    r"no longer",
    r"legacy",
    r"deprecated",
    r"(?:was|were) removed",
    r"(?:was|were|has been|have been) replaced",
    r"formerly",
    r"originally",
    r"backwards?[- ]compat(?:ible|ibility)?",
    r"until recently",
    r"in the past",
]
HISTORY = re.compile(r"\b(?:" + "|".join(HISTORY_PATTERNS) + r")\b", re.IGNORECASE)
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")


@dataclass(frozen=True)
class Ref:
    file: str
    line: int
    target: str
    kind: str  # "link" or "mention"


def corpus_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored, repo-relative."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted({p for p in out.splitlines() if (root / p).is_file()})


def _read_text(root: Path, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_references(rel: str, text: str) -> list[Ref]:
    refs: list[Ref] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in LINK.finditer(line):
            target = match.group(1)
            if target.startswith(("#", "<")) or SCHEME.match(target):
                continue
            refs.append(Ref(rel, number, target, "link"))
        rest = URL.sub(" ", LINK.sub(" ", line))
        for match in MENTION.finditer(rest):
            refs.append(Ref(rel, number, match.group(1), "mention"))
    return refs


def all_references(root: Path, corpus: list[str]) -> list[Ref]:
    refs: list[Ref] = []
    for rel in corpus:
        text = _read_text(root, rel)
        if text is not None:
            refs.extend(find_references(rel, text))
    return refs


def _resolves(ref: Ref, files: set[str], dirs: set[str], basenames: set[str]) -> bool:
    target = ref.target.split("#", 1)[0]
    if not target:
        return True
    writer_dir = posixpath.dirname(ref.file)
    candidates = [posixpath.normpath(posixpath.join(writer_dir, target))]
    if ref.kind == "mention":
        candidates.append(posixpath.normpath(target))
        if "/" not in target and target in basenames:
            return True
    return any(c in files or c.rstrip("/") in dirs for c in candidates)


def unresolved_references(root: Path, corpus: list[str]) -> list[Ref]:
    files = set(corpus)
    dirs = {posixpath.dirname(p) for p in files}
    dirs |= {d for p in list(dirs) for d in _parents(p)}
    basenames = {posixpath.basename(p) for p in files}
    return [r for r in all_references(root, corpus) if not _resolves(r, files, dirs, basenames)]


def _parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def history_corpus(corpus: list[str]) -> list[str]:
    """The documentation users of the service read."""
    return [
        p
        for p in corpus
        if p in ("README.md", "CLAUDE.md") or (p.startswith("docs/") and p.endswith(".md"))
    ]


def history_phrases(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in HISTORY.finditer(INLINE_CODE.sub(" ", line)):
            hits.append((number, match.group(0)))
    return hits
