"""Documentation guards: references resolve, and docs carry no history.

Planted documents in this module build file names with :func:`md` and links
with :func:`link`, so the module's own source never contains a dangling
reference for the guard to find when it scans this file as part of the real
corpus.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from devtools import docs_guard

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


# --- corpus -----------------------------------------------------------------


def test_corpus_includes_tracked_and_untracked_files(tmp_path):
    root = make_repo(tmp_path, {md("a"): "x", "sub/" + md("b"): "y", ".gitignore": "ignored/\n"})
    (root / "ignored").mkdir()
    (root / "ignored" / md("c")).write_text("z", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", md("a")], check=True)

    corpus = docs_guard.corpus_files(root)

    assert md("a") in corpus  # tracked
    assert "sub/" + md("b") in corpus  # untracked, not ignored
    assert "ignored/" + md("c") not in corpus


def test_real_corpus_is_not_empty():
    corpus = docs_guard.corpus_files(REPO)
    assert "README.md" in corpus
    assert "tests/test_docs_guard.py" in corpus


# --- references resolve -------------------------------------------------------


def test_real_repository_references_all_resolve():
    corpus = docs_guard.corpus_files(REPO)
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


def test_link_resolves_relative_to_the_file_it_is_written_in(tmp_path):
    root = make_repo(
        tmp_path,
        {
            md("b"): "root b",
            "sub/" + md("a"): link("b", md("b")) + " and " + link("up", "../" + md("b")),
        },
    )
    bad = docs_guard.unresolved_references(root, docs_guard.corpus_files(root))
    # The bare link points at sub/b, which does not exist; the ../ link is fine.
    assert [(r.file, r.target) for r in bad] == [("sub/" + md("a"), md("b"))]


def test_mention_resolves_by_root_path_writer_path_or_basename(tmp_path):
    root = make_repo(
        tmp_path,
        {
            "docs/" + md("guide"): "g",
            "sub/" + md("local"): "l",
            "sub/" + md("a"): f"docs/{md('guide')}, {md('local')}, {md('guide')}",
        },
    )
    assert docs_guard.unresolved_references(root, docs_guard.corpus_files(root)) == []


def test_link_to_existing_non_markdown_file_and_directory_resolves(tmp_path):
    root = make_repo(
        tmp_path,
        {
            "pyproject.toml": "",
            "docs/" + md("x"): "",
            md("a"): link("p", "pyproject.toml") + " " + link("d", "docs/"),
        },
    )
    assert docs_guard.unresolved_references(root, docs_guard.corpus_files(root)) == []


def test_link_to_missing_non_markdown_file_is_reported(tmp_path):
    root = make_repo(tmp_path, {md("a"): link("p", "nothing" + ".toml")})
    bad = docs_guard.unresolved_references(root, docs_guard.corpus_files(root))
    assert [r.target for r in bad] == ["nothing" + ".toml"]


def test_external_links_and_pure_anchors_are_ignored(tmp_path):
    root = make_repo(
        tmp_path,
        {
            md("a"): (
                f"[x](https://example.com/{md('remote')}) "
                f"https://example.com/{md('bare')} [y](#section) "
                "[m](mailto:a@example.com)"
            )
        },
    )
    assert docs_guard.unresolved_references(root, docs_guard.corpus_files(root)) == []


def test_anchor_is_stripped_before_resolving(tmp_path):
    root = make_repo(tmp_path, {md("b"): "", md("a"): link("b", md("b") + "#part")})
    assert docs_guard.unresolved_references(root, docs_guard.corpus_files(root)) == []


def test_references_are_found_in_code_files_too(tmp_path):
    root = make_repo(tmp_path, {"tool.py": f'"""See {md("gone")}."""\n'})
    bad = docs_guard.unresolved_references(root, docs_guard.corpus_files(root))
    assert [(r.file, r.line) for r in bad] == [("tool.py", 1)]


def test_binary_files_are_skipped(tmp_path):
    root = make_repo(tmp_path, {md("a"): "fine"})
    (root / "blob.bin").write_bytes(b"\xff\xfe" + md("gone").encode() + b"\x00")
    assert docs_guard.unresolved_references(root, docs_guard.corpus_files(root)) == []


# --- documentation describes the current state only --------------------------


def test_history_corpus_is_docs_readme_and_claude_md():
    corpus = docs_guard.corpus_files(REPO)
    history = docs_guard.history_corpus(corpus)
    assert "README.md" in history
    assert "CLAUDE.md" in history
    # The rule files quote the forbidden phrasings in order to forbid them.
    assert "CONTRIBUTING.md" not in history
    assert "project-knowledge/working-rules.md" not in history


def test_history_corpus_includes_every_markdown_file_under_docs():
    files = ["docs/" + md("a"), "docs/deep/" + md("b"), "docs/image.png", md("other")]
    assert docs_guard.history_corpus(files) == ["docs/" + md("a"), "docs/deep/" + md("b")]


def test_real_documentation_has_no_history():
    corpus = docs_guard.corpus_files(REPO)
    history = docs_guard.history_corpus(corpus)
    assert history  # non-vacuity: a scan over nothing would pass
    found = {f: docs_guard.history_phrases((REPO / f).read_text(encoding="utf-8")) for f in history}
    assert {f: hits for f, hits in found.items() if hits} == {}


@pytest.mark.parametrize(
    "sentence",
    [
        "The API previously accepted a token.",
        "This used to be a flag.",
        "The option is no longer supported.",
        "The legacy endpoint stays.",
        "This field is deprecated.",
        "The parameter was removed in favour of another.",
        "Both modes were removed.",
        "It was replaced by the helper.",
        "It has been replaced by the helper.",
        "Formerly this ran as root.",
        "It was originally designed to mitigate drift.",
        "Kept for backward compatibility.",
        "Kept for backwards-compat.",
        "Until recently the server ran here.",
        "In the past the server ran here.",
    ],
)
def test_history_phrasing_is_reported(sentence):
    assert docs_guard.history_phrases(sentence) != []


def test_history_hits_carry_line_numbers():
    text = "fine\nthis used to work\n"
    assert [line for line, _ in docs_guard.history_phrases(text)] == [2]


@pytest.mark.parametrize(
    "sentence",
    [
        "The server is used to run sessions.\n",  # not history: "is used to <verb>"
        "The tool was focused on speed.\n",  # substring of a word is not a match
        "Run `git log --previously` to see.\n",  # inline code
        "```\nthis used to be shown in a code block\n```\n",  # fenced code
    ],
)
def test_non_history_text_is_not_reported(sentence):
    assert docs_guard.history_phrases(sentence) == []


# --- the make targets tests.md documents ------------------------------------

TESTS_MD = REPO / "project-knowledge" / "tests.md"


def make_targets(makefile: str) -> set[str]:
    """The targets a Makefile defines, as make itself reads them: the files of make's
    database (make -p) that are targets, less special targets and target-specific
    variables."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "Makefile"
        path.write_text(makefile, encoding="utf-8")
        proc = subprocess.run(
            [shutil.which("make"), "-pRrq", "-f", str(path), ":"],
            cwd=scratch,
            env={"PATH": os.defpath, "LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=60,
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
    assert documented_make_targets("") == set()


def test_a_makefile_comment_with_a_colon_is_not_a_target():
    """A comment line is not a rule, whatever it holds; the rule beside it still is."""
    makefile = "a:  ## one\n\tdo a\n"
    assert make_targets(makefile) == {"a"}  # positive control
    commented = "# Why: a reason\n" + makefile
    assert "# Why:" in commented  # the plant took effect
    assert make_targets(commented) == {"a"}


@pytest.mark.parametrize(
    "variable",
    [
        "NAME := value\n",
        "NAME ::= value\n",
        "export NAME := value\n",
        "override NAME := value\n",
        "define BLOCK\nnot-a-target: x\nendef\n",
        "ifeq (1,2)\nnot-a-target:\n\tdo it\nendif\n",
    ],
)
def test_a_makefile_variable_or_unused_text_with_a_colon_is_not_a_target(variable):
    """Variable assignments, a define block's body and a branch make does not take are
    not rules; the rule beside them still is."""
    makefile = "a:  ## one\n\tdo a\n"
    assert make_targets(makefile) == {"a"}  # positive control
    planted = variable + makefile
    assert ":" in variable and planted != makefile  # the plant took effect
    assert make_targets(planted) == {"a"}


def test_a_double_colon_rule_and_a_rule_with_several_targets_are_targets():
    """Positive controls for the forms a variable must not be mistaken for."""
    assert make_targets("a:: b\n\tdo a\nb c: \n\tdo it\n") == {"a", "b", "c"}
    assert make_targets("a: missing\n\tdo a\n") == {"a"}  # a prerequisite only is not
