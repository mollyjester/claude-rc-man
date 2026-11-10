"""Documentation guards: references resolve, and docs carry no history.

Planted documents in this module build file names with :func:`md` and links
with :func:`link`, so the module's own source never contains a dangling
reference for the guard to find when it scans this file as part of the real
corpus.
"""

import os
import shutil
import subprocess
from pathlib import Path

from devtools import docs_guard
from devtools.held_folders import held
from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent


def md(stem: str) -> str:
    return stem + ".md"


def link(label: str, target: str) -> str:
    return "[" + label + "](" + target + ")"


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


# --- references resolve -------------------------------------------------------


def test_real_repository_references_all_resolve():
    corpus = docs_guard.corpus_files(REPO)
    assert {"README.md", "tests/test_docs_guard.py"} <= set(corpus)
    refs = docs_guard.all_references(REPO, corpus)
    # Known positive: README links CONTRIBUTING; without it an empty scan would pass.
    assert any(r.file == "README.md" and r.target == "CONTRIBUTING.md" for r in refs)
    assert docs_guard.unresolved_references(REPO, corpus) == []


def test_dangling_link_is_reported(tmp_path):
    root = make_repo(tmp_path, {md("a"): "See " + link("it", md("missing")) + "."})
    corpus = docs_guard.corpus_files(root)
    assert md("a") in corpus  # the plant took effect

    bad = docs_guard.unresolved_references(root, corpus)

    assert [(r.file, r.target, r.kind) for r in bad] == [(md("a"), md("missing"), "link")]


def test_dangling_mention_is_reported(tmp_path):
    root = make_repo(tmp_path, {md("a"): f"Read {md('nowhere')} first."})
    bad = docs_guard.unresolved_references(root, docs_guard.corpus_files(root))
    assert [(r.target, r.kind) for r in bad] == [(md("nowhere"), "mention")]


# --- documentation describes the current state only --------------------------


def test_real_documentation_has_no_history():
    corpus = docs_guard.corpus_files(REPO)
    history = docs_guard.history_corpus(corpus)
    assert history  # non-vacuity: a scan over nothing would pass
    found = {f: docs_guard.history_phrases((REPO / f).read_text(encoding="utf-8")) for f in history}
    assert {f: hits for f, hits in found.items() if hits} == {}


def test_history_phrasing_is_reported():
    assert [line for line, _ in docs_guard.history_phrases("fine\nthis used to work\n")] == [2]


# --- every page is in CLAUDE.md's table ----------------------------------------------------


def test_claude_md_links_every_page_under_docs_and_project_knowledge():
    corpus = docs_guard.corpus_files(REPO)
    claude_md = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    pages = docs_guard.pages_of_the_contents(corpus)
    # Known positives: an empty listing, or a table that links nothing, would pass.
    assert {"docs/installation.md", "project-knowledge/tests.md"} <= set(pages)
    assert "docs/installation.md" in docs_guard.linked_pages(docs_guard.CONTENTS, claude_md)
    assert docs_guard.pages_not_linked(corpus, claude_md) == []


def test_a_page_the_table_does_not_link_is_reported():
    claude_md = "| Q | File |\n|---|---|\n| a | " + link("a", "docs/" + md("a")) + " |\n"
    corpus = ["CLAUDE.md", "docs/" + md("a"), "docs/" + md("b"), "project-knowledge/" + md("c")]
    assert docs_guard.pages_not_linked(corpus, claude_md) == [
        "docs/" + md("b"),
        "project-knowledge/" + md("c"),
    ]
    both = claude_md + link("b", "docs/" + md("b")) + link("c", "project-knowledge/" + md("c"))
    assert docs_guard.pages_not_linked(corpus, both) == []  # positive control


# --- the make targets tests.md documents ------------------------------------

TESTS_MD = REPO / "project-knowledge" / "tests.md"


def make_targets(makefile: str) -> set[str]:
    """The targets a Makefile defines, as make itself reads them: the files of make's
    database (make -p) that are targets, less special targets and target-specific
    variables."""
    with held("rcmk-") as scratch:
        path = scratch / "Makefile"
        path.write_text(makefile, encoding="utf-8")
        proc = subprocess.run(
            [shutil.which("make"), "-pRrq", "-f", str(path), ":"],
            cwd=scratch,
            env={"PATH": os.defpath, "LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=HANG_GUARD,
        )
    lines = proc.stdout.splitlines()
    assert "# Files" in lines, proc.stdout + proc.stderr  # make printed its database
    found, not_a_target = set(), False
    for line in lines[lines.index("# Files") + 1 :]:
        if line.startswith("# Finished Make data base"):
            break
        if line == "# Not a target:":
            not_a_target = True
            continue
        name, colon, rest = line.partition(":")
        rule = colon and name and line[0] not in "#\t ." and "=" not in rest
        if rule and not not_a_target:
            found.add(name)
        not_a_target = False
    return found


def documented_make_targets(markdown: str) -> set[str]:
    """The targets a table documents, one row each: | `make <target>` | ... |."""
    found = set()
    for line in markdown.splitlines():
        cell = line.split("|")[1].strip() if line.startswith("|") else ""
        if cell.startswith("`make ") and cell.endswith("`"):
            found.add(cell[len("`make ") : -1])
    return found


def test_tests_md_documents_every_make_target_and_no_other():
    targets = make_targets((REPO / "Makefile").read_text(encoding="utf-8"))
    assert {"setup", "test", "check"} <= targets  # non-vacuity: known targets are found
    assert documented_make_targets(TESTS_MD.read_text(encoding="utf-8")) == targets


def test_the_make_target_check_fails_on_a_drifted_table():
    makefile = ".PHONY: a b\n\na:  ## one\n\tdo a\n\nb: a\n\tdo b\n\nb: export X = 1\n"
    assert make_targets(makefile) == {"a", "b"}
    table = "| Command | What |\n|---|---|\n| `make a` | one |\n| `make b` | two |\n"
    assert documented_make_targets(table) == {"a", "b"}
    missing = table.replace("| `make b` | two |\n", "")
    assert missing != table  # the plant took effect
    assert documented_make_targets(missing) != make_targets(makefile)
    extra = table + "| `make c` | three |\n"
    assert documented_make_targets(extra) != make_targets(makefile)
