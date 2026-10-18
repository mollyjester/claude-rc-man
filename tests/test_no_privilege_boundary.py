"""rcman has no privilege boundary: in product code there is no command that raises
privilege, no root helper and no switching of accounts.

Two layers read the corpus (devtools/docs_guard.corpus_files: tracked files and
untracked files that are not ignored):

- **The privilege command.** Product code (PRODUCT_PREFIXES) holds it in no form at
  all. Everywhere else it must be followed by one of ALLOWED_FORMS or stand after a
  word of ALLOWED_PROSE: an allow-list, so that a form nobody listed fails, and each
  entry is checked to still be used. The command is built from pieces, so that this
  module, which the guard reads too, does not hold it itself.
- **Calls into the system's accounts.** An AST layer over backend/ and helpers/ finds
  reads of the user and group databases, changes of the process's identity and the
  programs that make or switch accounts. A site that is right is in EXEMPT: named by
  file, function and text, reasoned, and checked to still be there. installer/ is
  outside this layer: it is the one program that makes rcman's accounts.

Each layer has planted twins below: a scratch repository with the violation, the plant
confirmed, and the guard shown firing.
"""

import ast
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from devtools import docs_guard

REPO = Path(__file__).resolve().parent.parent


PAGE = "docs/page" + ".md"  # built, so that this module names no page the reference guard looks for


def j(*parts: str) -> str:
    """Pieces joined, so that the command is not written whole in this module."""
    return "".join(parts)


PRIV = j("su", "do")  # the command that raises privilege


class Hit(NamedTuple):
    file: str
    line: int
    rule: str
    text: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.rule}: {self.text}"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- the privilege command ---------------------------------------------------------

WORD = re.compile(rf"\b{PRIV}\b", re.IGNORECASE)
# What may stand between the command and what it runs: spaces, and the quotes and commas
# of a list of arguments written out.
GAP = r"[\s\"',]+"
# What the command may be followed by, with the reason for each. A form nobody listed
# fails.
ALLOWED_FORMS = {
    "rcman-admin": ("rcman-admin", "administrators run the administration command through it"),
    r"\./install\.sh": ("./install.sh", "the installer is run through it"),
    "systemctl": ("systemctl", "administrators manage the services through it"),
    "journalctl": ("journalctl", "administrators read the journal through it"),
    f"-u{GAP}caddy{GAP}caddy": ("-u caddy caddy", "Caddy's own check runs as Caddy's account"),
    "reboot": ("reboot", "the real-machine checklist restarts the host"),
}
# Sentences that name the command without running anything, as a word before it.
ALLOWED_PROSE = {
    "through": "the installer is run through it",
    "with": "the installer says to run it with it",
    "ran": "the installer finds the account that ran it",
}
FORM_RULES = {form: re.compile(GAP + form) for form in ALLOWED_FORMS}
PROSE_RULES = {word: re.compile(rf"\b{word}\s+$", re.IGNORECASE) for word in ALLOWED_PROSE}

# Product code is what the service ships and runs: the backend, the helpers it runs in a
# workspace, the web interface's sources (not its tests) and the installer's templates,
# workspace image and builder.
PRODUCT_PREFIXES = (
    "backend/",
    "helpers/",
    "frontend/src/",
    "installer/templates/",
    "installer/workspace/",
    "installer/builder/",
)


def is_product(rel: str) -> bool:
    if not rel.startswith(PRODUCT_PREFIXES):
        return False
    if rel.startswith("frontend/src/"):
        return not (rel.startswith("frontend/src/test/") or re.search(r"\.test\.tsx?$", rel))
    return True


def forms_used(text: str) -> set[str]:
    """The allowed forms and sentences the command is used in, in ``text``."""
    used = set()
    for m in WORD.finditer(text):
        after, before = text[m.end() :], text[: m.start()]
        used |= {f for f, rule in FORM_RULES.items() if rule.match(after)}
        used |= {w for w, rule in PROSE_RULES.items() if rule.search(before)}
    return used


def privilege_hits(rel: str, text: str) -> list[Hit]:
    hits = []
    for m in WORD.finditer(text):
        after, before = text[m.end() :], text[: m.start()]
        allowed = any(rule.match(after) for rule in FORM_RULES.values()) or any(
            rule.search(before) for rule in PROSE_RULES.values()
        )
        line = _line_of(text, m.start())
        if is_product(rel):
            hits.append(Hit(rel, line, "privilege command in product", m.group(0)))
        elif not allowed:
            context = text[m.start() : m.end() + 20].splitlines()[0]
            hits.append(Hit(rel, line, "privilege command, form not allowed", context))
    return hits


# --- calls into the system's accounts ------------------------------------------------

# The attributes of a module that read the account databases or change who the process is.
ACCOUNT_CALLS = {
    "pwd": lambda attr: attr.startswith("getpw"),
    "grp": lambda attr: attr.startswith("getgr"),
    "os": lambda attr: attr
    in {
        "setuid", "setgid", "seteuid", "setegid", "setreuid", "setregid", "setresuid",
        "setresgid", "setgroups", "initgroups",
    },
}  # fmt: skip
# Programs that make or switch accounts or raise privilege, as the first word of an
# argument list or as the program a call is given.
ACCOUNT_PROGRAMS = {PRIV, "su", "useradd", "userdel", "usermod", "loginctl", "runuser"}
AST_ROOTS = ("backend/", "helpers/")
# Sites that are right: (file in the repository, the function it is in or "" at module
# level, the site's source text) -> what makes it different. Each covers exactly one
# site: another of the same text in the same function is flagged.
EXEMPT = {
    ("backend/rcman/admin.py", "as_account", "pwd.getpwnam"): (
        "rcman-admin run by root runs itself again as rcman, whose id and home it reads"
    ),
    ("backend/rcman/admin.py", "as_account", '"runuser"'): (
        "the program that runs rcman-admin again as rcman, with an argument list"
    ),
    ("backend/rcman/admin.py", "main", '"runuser"'): (
        "the call that replaces root's rcman-admin with the one run as rcman"
    ),
    ("backend/rcman/runtime.py", "layout", "pwd.getpwuid"): (
        "the host runtime, a test double, names the account its own process runs as"
    ),
}


class Site(NamedTuple):
    line: int
    function: str
    rule: str
    text: str


def account_sites(source: str) -> list[Site]:
    """Where the source reads the account databases, changes the process's identity or
    names a program that makes or switches accounts, with the function each is in."""
    tree = ast.parse(source)
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        site = None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            check = ACCOUNT_CALLS.get(node.value.id)
            if check and check(node.attr):
                site = ("account database or identity", f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module in ACCOUNT_CALLS:
            check = ACCOUNT_CALLS[node.module]
            for alias in node.names:
                if check(alias.name):
                    found.append(
                        Site(node.lineno, function, "account database or identity",
                             f"{node.module}.{alias.name}")
                    )  # fmt: skip
        elif isinstance(node, ast.Constant) and node.value in ACCOUNT_PROGRAMS:
            site = ("program that makes or switches accounts", ast.get_source_segment(source, node))
        if site:
            found.append(Site(node.lineno, function, *site))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


def account_hits(rel: str, source: str, exempt: dict = EXEMPT) -> list[Hit]:
    hits, seen = [], set()
    for site in account_sites(source):
        key = (rel, site.function, site.text)
        if key in exempt and key not in seen:
            seen.add(key)
            continue
        where = site.function or "the module"
        hits.append(Hit(rel, site.line, site.rule, f"{site.text} in {where}"))
    return hits


# --- the guard -----------------------------------------------------------------------


def problems(root: Path, exempt: dict = EXEMPT) -> list[str]:
    """Everything the two layers find in the repository at ``root``, and a problem for
    each layer that read nothing, which would let it pass over an empty corpus."""
    corpus = docs_guard.corpus_files(root)
    texts = {rel: t for rel in corpus if (t := docs_guard._read_text(root, rel)) is not None}
    found = []
    for rel, text in texts.items():
        found += privilege_hits(rel, text)
    ast_files = [rel for rel in texts if rel.endswith(".py") and rel.startswith(AST_ROOTS)]
    for rel in ast_files:
        found += account_hits(rel, texts[rel], exempt)
    out = [str(hit) for hit in found]
    if not texts:
        out.append("the corpus holds no text file: nothing was read")
    for prefix in AST_ROOTS:
        if not any(rel.startswith(prefix) for rel in ast_files):
            out.append(f"no Python file under {prefix} was read by the account layer")
    return out


def test_the_repository_has_no_privilege_boundary():
    assert problems(REPO) == []


def test_the_scan_reads_the_real_corpus_and_each_layer_has_something_to_read():
    corpus = docs_guard.corpus_files(REPO)
    assert "README.md" in corpus and "backend/rcman/admin.py" in corpus
    assert f"tests/{Path(__file__).name}" in corpus  # this module is read too
    assert any(is_product(rel) for rel in corpus)
    assert any(rel.startswith("helpers/") and rel.endswith(".py") for rel in corpus)
    assert any(rel.startswith("frontend/src/") and not is_product(rel) for rel in corpus)


# --- planted twins -------------------------------------------------------------------


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


# A repository the guard passes: text for the privilege layer and a Python file under each
# root of the account layer.
CLEAN = {
    "README.md": "A service.\n",
    "backend/rcman/ok.py": "x = 1\n",
    "helpers/ok.py": "y = 2\n",
}


def planted(tmp_path: Path, extra: dict[str, str], exempt: dict | None = None) -> list[str]:
    """The problems of the clean repository with ``extra`` files added, the plant
    confirmed to be in the corpus."""
    root = make_repo(tmp_path, {**CLEAN, **extra})
    corpus = docs_guard.corpus_files(root)
    for name, text in extra.items():
        assert name in corpus  # the plant took effect
        assert (root / name).read_text(encoding="utf-8") == text
    return problems(root, EXEMPT if exempt is None else exempt)


def test_the_clean_repository_passes(tmp_path):
    root = make_repo(tmp_path, CLEAN)
    assert problems(root) == []  # positive control: the plants below are what fail it


@pytest.mark.parametrize("form", sorted(ALLOWED_FORMS))
def test_an_allowed_form_of_the_privilege_command_passes_outside_product_code(tmp_path, form):
    text = f"run {PRIV} {ALLOWED_FORMS[form][0]} now\n"
    assert planted(tmp_path, {PAGE: text}) == []


def test_the_command_in_a_written_out_list_of_arguments_passes(tmp_path):
    text = f'argv = ["{PRIV}", "systemctl", "restart", "rcman"]\n'
    assert planted(tmp_path, {"tests/test_x.py": text}) == []


def test_docker_after_the_command_is_a_form_nobody_listed(tmp_path):
    """The tests reach no container through the command: their engine is the user's own."""
    found = planted(tmp_path, {"tests/test_x.py": f'prefix = ["{PRIV}", "-n", "docker"]\n'})
    assert len(found) == 1 and "form not allowed" in found[0], found


@pytest.mark.parametrize("word", sorted(ALLOWED_PROSE))
def test_the_command_named_in_an_allowed_sentence_passes(tmp_path, word):
    assert planted(tmp_path, {"install.sh": f'die "run it {word} {PRIV}"\n'}) == []


@pytest.mark.parametrize(
    "text",
    [
        f"{PRIV} rm -rf /",
        f"{PRIV} caddy validate",  # not as Caddy's account
        f"{PRIV} -u rcman podman ps",
        f"**{PRIV}** for your account",
        f"{PRIV}\n",
        f"{PRIV}: a colon",
    ],
)
def test_a_form_nobody_listed_fails_outside_product_code(tmp_path, text):
    found = planted(tmp_path, {PAGE: text + "\n"})
    assert len(found) == 1 and "form not allowed" in found[0], found


def test_the_variable_of_the_account_that_ran_it_is_not_the_command(tmp_path):
    assert planted(tmp_path, {"install.sh": f"u={j('$', PRIV.upper(), '_USER')}\n"}) == []


@pytest.mark.parametrize(
    "rel",
    [
        "backend/rcman/x.py",
        "helpers/x.py",
        "frontend/src/Page.tsx",
        "frontend/src/messages.ts",
        "installer/templates/rcman.service",
        "installer/workspace/Containerfile",
        "installer/builder/Containerfile",
    ],
)
@pytest.mark.parametrize("form", ["rcman-admin", "./install.sh", "systemctl", "reboot"])
def test_product_code_holds_the_command_in_no_form_at_all(tmp_path, rel, form):
    found = planted(tmp_path, {rel: f"# {PRIV} {form}\n"})
    assert len(found) == 1 and "privilege command in product" in found[0], found


def test_product_code_holds_it_in_prose_too(tmp_path):
    found = planted(tmp_path, {"backend/rcman/x.py": f'"""Run it through {PRIV}."""\n'})
    assert len(found) == 1 and "in product" in found[0], found


@pytest.mark.parametrize(
    "rel",
    [
        "frontend/src/Page.test.tsx",
        "frontend/src/api.test.ts",
        "frontend/src/test/fakeApi.ts",
        "installer/install.py",
        "install.sh",
        "tests/test_x.py",
        PAGE,
    ],
)
def test_what_is_not_product_code_may_use_the_allowed_forms(tmp_path, rel):
    assert not is_product(rel)
    assert planted(tmp_path, {rel: f"# {PRIV} rcman-admin add\n"}) == []


def test_every_allowed_form_and_sentence_is_still_used_in_the_corpus():
    """Stale check: an entry nobody uses is removed, so the list is no wider than the
    repository needs."""
    used = set()
    for rel in docs_guard.corpus_files(REPO):
        text = docs_guard._read_text(REPO, rel)
        used |= forms_used(text) if text is not None else set()
    assert used >= set(ALLOWED_FORMS) | set(ALLOWED_PROSE), (
        set(ALLOWED_FORMS) | set(ALLOWED_PROSE)
    ) - used


# account layer

ACCOUNT_SAMPLES = {
    "pwd.getpwnam": "import pwd\n\n\ndef f():\n    return pwd.getpwnam('x')\n",
    "pwd.getpwuid": "import pwd\n\n\ndef f():\n    return pwd.getpwuid(0)\n",
    "grp.getgrnam": "import grp\n\n\ndef f():\n    return grp.getgrnam('x')\n",
    "grp.getgrgid": "import grp\n\n\ndef f():\n    return grp.getgrgid(0)\n",
    "os.setuid": "import os\n\n\ndef f():\n    os.setuid(0)\n",
    "os.setgid": "import os\n\n\ndef f():\n    os.setgid(0)\n",
    "os.initgroups": "import os\n\n\ndef f():\n    os.initgroups('x', 0)\n",
    "from pwd import getpwnam": "from pwd import getpwnam\n",
    "from grp import getgrnam": "from grp import getgrnam\n",
    **{
        f'argv starting "{program}"': f'def f():\n    return ["{program}", "-x"]\n'
        for program in sorted(ACCOUNT_PROGRAMS)
    },
}


@pytest.mark.parametrize("sample", sorted(ACCOUNT_SAMPLES), ids=str)
@pytest.mark.parametrize("root", ["backend/rcman/planted.py", "helpers/planted.py"])
def test_every_account_call_is_found(tmp_path, root, sample):
    found = planted(tmp_path, {root: ACCOUNT_SAMPLES[sample]}, exempt={})
    found = [line for line in found if "privilege command in product" not in line]
    assert len(found) == 1 and found[0].startswith(f"{root}:"), found


def test_the_account_layer_reads_the_roots_and_only_them(tmp_path):
    source = ACCOUNT_SAMPLES["pwd.getpwnam"]
    assert planted(tmp_path, {"installer/planted.py": source, "tests/planted.py": source}) == []


def test_every_account_program_and_call_is_covered_by_a_sample():
    for module, check in ACCOUNT_CALLS.items():
        assert any(s.startswith(f"{module}.") and check(s.split(".")[1]) for s in ACCOUNT_SAMPLES)
    assert {s for s in ACCOUNT_PROGRAMS if f'argv starting "{s}"' not in ACCOUNT_SAMPLES} == set()


def test_an_exemption_covers_its_own_site_only(tmp_path):
    """Planted twin: the exemption, its function and its file are planted, so the twin
    holds whatever EXEMPT lists."""
    rel = "backend/rcman/planted.py"
    key = (rel, "f", "pwd.getpwnam")
    source = ACCOUNT_SAMPLES["pwd.getpwnam"]
    exempt = {key: "planted"}
    assert planted(tmp_path / "a", {rel: source}, exempt=exempt) == []  # positive control
    twin_same = source + "    return pwd.getpwnam('y')\n"
    twin_other = source + "    return pwd.getpwuid(0)\n"
    twin_function = source + "\n\ndef g():\n    return pwd.getpwnam('x')\n"
    twin_module = source + "\n\nz = pwd.getpwnam('x')\n"
    for n, twin in enumerate([twin_same, twin_function, twin_module]):
        assert twin != source  # the plant took effect
        assert len(planted(tmp_path / f"b{n}", {rel: twin}, exempt=exempt)) == 1, twin
    assert twin_other != source  # the plant took effect
    assert len(planted(tmp_path / "c", {rel: twin_other}, exempt=exempt)) == 1


def test_an_exemption_covers_its_own_file_only(tmp_path):
    key = ("backend/rcman/planted.py", "f", "pwd.getpwnam")
    other = "backend/rcman/other.py"
    found = planted(tmp_path, {other: ACCOUNT_SAMPLES["pwd.getpwnam"]}, exempt={key: "planted"})
    assert len(found) == 1 and found[0].startswith(other)


def test_the_exemptions_still_have_their_sites():
    """Stale check: each names a file, a function and a text that still exist."""
    assert EXEMPT  # non-vacuity
    for (rel, function, text), reason in EXEMPT.items():
        assert reason
        assert rel.startswith(AST_ROOTS), rel
        sites = account_sites((REPO / rel).read_text(encoding="utf-8"))
        assert [s for s in sites if (s.function, s.text) == (function, text)], (rel, function, text)


def test_every_site_of_the_real_roots_is_found_or_exempt():
    """Known positives: the exempt sites are found, or an empty scan would pass."""
    total = 0
    for rel in docs_guard.corpus_files(REPO):
        if rel.endswith(".py") and rel.startswith(AST_ROOTS):
            total += len(account_sites((REPO / rel).read_text(encoding="utf-8")))
    assert total >= len(EXEMPT) > 0


# an empty corpus


def test_an_empty_repository_fails_the_guard(tmp_path):
    root = make_repo(tmp_path, {})
    assert docs_guard.corpus_files(root) == []  # the plant took effect: nothing is there
    found = problems(root)
    assert any("nothing was read" in line for line in found), found


def test_a_repository_without_python_under_the_roots_fails_the_guard(tmp_path):
    root = make_repo(tmp_path, {"README.md": "A service.\n", "tests/ok.py": "x = 1\n"})
    found = problems(root)
    assert [line for line in found if "no Python file under" in line]
    assert len(found) == len(AST_ROOTS)


def test_a_repository_with_only_one_root_fails_the_guard(tmp_path):
    root = make_repo(tmp_path, {"README.md": "A.\n", "backend/rcman/ok.py": "x = 1\n"})
    found = problems(root)
    assert len(found) == 1 and "helpers/" in found[0]
