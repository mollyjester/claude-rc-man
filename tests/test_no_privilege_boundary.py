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
- **The services' accounts.** Every systemd service among the installer's templates
  names the account it runs as (``User=``), and not root. The one that is root is in
  ROOT_SERVICES, named, reasoned, and checked to still be there and still be root's.

Each layer has planted twins below: a scratch repository with the violation, the plant
confirmed, and the guard shown firing.
"""

import ast
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

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


def account_hits(rel: str, source: str) -> list[Hit]:
    hits, seen = [], set()
    for site in account_sites(source):
        key = (rel, site.function, site.text)
        if key in EXEMPT and key not in seen:
            seen.add(key)
            continue
        where = site.function or "the module"
        hits.append(Hit(rel, site.line, site.rule, f"{site.text} in {where}"))
    return hits


# --- the services' accounts -------------------------------------------------------------

SERVICE_TEMPLATES = "installer/templates/"
SERVICE_ACCOUNT = re.compile(r"^User=(\S*)\s*$", re.MULTILINE)
# Services that run as root: the template -> what makes it different. Each is checked to still
# be there and to still name no account
# (test_every_service_template_names_its_account_but_the_root_services).
ROOT_SERVICES = {
    "installer/templates/rcman-upgrade.service": (
        "the web upgrade: rcman asks for it by writing a file, and this oneshot runs the clone's "
        "install.sh as root, off by default and installed only when an administrator turns it "
        "on; as every run of the installer, it edits Caddy's configuration and restarts Caddy. "
        "Accepted risk: whoever can push to main runs code as root, through install.sh, "
        "installer/ and uv sync building the project as root; so a GitHub token stored in rcman "
        "that can push to the repository is a root credential, and while the option is on the "
        "clone owner's account is root without a password"
    ),
}


# What a root service may run and read, so that its exemption covers what was reasoned about
# and no more: its commands are exactly root's wrapper under the installation's bin folder, run
# in isolated mode with these arguments (nothing after them: `;` would add a command), it reads
# no file of settings, and it names nothing in rcman's home (the request file there is its path
# unit's, which is not a service).
WRAPPER = "/usr/bin/python3 -I /opt/rcman/bin/rcman-upgrade"
ROOT_SERVICE_COMMANDS = {
    "ExecStart": f"{WRAPPER} --clone @CLONE@ --caddy-user @CADDY_USER@",
    "ExecStopPost": f"{WRAPPER} --finish",
}
# The placeholders a root service may have the installer fill in.
ROOT_SERVICE_PLACEHOLDERS = {"CLONE", "CADDY_USER", "UPGRADE_ENVIRONMENT"}


def root_service_problems(text: str) -> list[str]:
    """What a root service holds beyond what its exemption allows."""
    found = []
    keys: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "[")):
            key, _, value = line.partition("=")
            keys.setdefault(key.strip(), []).append(value.strip())
    for key, command in ROOT_SERVICE_COMMANDS.items():
        if not keys.get(key):
            found.append(f"no {key}=")
        found += [f"{key}={v}" for v in keys.get(key, []) if v != command]
    exec_keys = {k for k in keys if k.startswith("Exec")} - set(ROOT_SERVICE_COMMANDS)
    found += [f"{key}=" for key in sorted(exec_keys)]
    if "EnvironmentFile" in keys:
        found.append("EnvironmentFile=")
    if "/var/lib/rcman" in text:
        found.append("a path under /var/lib/rcman")
    found += [f"@{name}@" for name in sorted(set(re.findall(r"@([A-Z0-9_]+)@", text)))
              if name not in ROOT_SERVICE_PLACEHOLDERS]  # fmt: skip
    return found


def is_service_template(rel: str) -> bool:
    return rel.startswith(SERVICE_TEMPLATES) and rel.endswith(".service")


def service_hits(rel: str, text: str, root_services: dict = ROOT_SERVICES) -> list[Hit]:
    """A service template that names no account, or root, unless it is a root service, which
    holds only what its exemption allows."""
    if not is_service_template(rel):
        return []
    if rel in root_services:
        beyond = root_service_problems(text)
        return [Hit(rel, 1, "root service beyond its exemption", p) for p in beyond]
    accounts = SERVICE_ACCOUNT.findall(text)
    if not accounts:
        return [Hit(rel, 1, "service without User=", "runs as root")]
    return [Hit(rel, 1, "service as root", f"User={a}") for a in accounts if a in ("", "root", "0")]


def stale_root_services(root: Path) -> list[str]:
    """The root services whose template is gone, or names an account now."""
    corpus = set(docs_guard.corpus_files(root))
    stale = []
    for rel, reason in ROOT_SERVICES.items():
        if not reason or rel not in corpus:
            stale.append(f"{rel}: not in the repository")
        elif SERVICE_ACCOUNT.search((root / rel).read_text(encoding="utf-8")):
            stale.append(f"{rel}: names an account now")
    return stale


# --- the guard -----------------------------------------------------------------------


def problems(root: Path, root_services: dict = ROOT_SERVICES) -> list[str]:
    """Everything the three layers find in the repository at ``root``, and a problem for
    each layer that read nothing, which would let it pass over an empty corpus."""
    corpus = docs_guard.corpus_files(root)
    texts = {rel: t for rel in corpus if (t := docs_guard._read_text(root, rel)) is not None}
    found = []
    for rel, text in texts.items():
        found += privilege_hits(rel, text)
    ast_files = [rel for rel in texts if rel.endswith(".py") and rel.startswith(AST_ROOTS)]
    for rel in ast_files:
        found += account_hits(rel, texts[rel])
    services = [rel for rel in texts if is_service_template(rel)]
    for rel in services:
        found += service_hits(rel, texts[rel], root_services)
    out = [str(hit) for hit in found]
    if not texts:
        out.append("the corpus holds no text file: nothing was read")
    for prefix in AST_ROOTS:
        if not any(rel.startswith(prefix) for rel in ast_files):
            out.append(f"no Python file under {prefix} was read by the account layer")
    if not services:
        out.append(f"no service template under {SERVICE_TEMPLATES} was read")
    return out


def test_the_repository_has_no_privilege_boundary():
    corpus = docs_guard.corpus_files(REPO)
    assert "README.md" in corpus and "backend/rcman/admin.py" in corpus  # the real corpus
    assert f"tests/{Path(__file__).name}" in corpus  # this module is read too
    assert problems(REPO) == []


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
    "installer/templates/ok.service": "[Service]\nUser=ok\nExecStart=/bin/true\n",
}


def planted(tmp_path: Path, extra: dict[str, str], root_services=None) -> list[str]:
    """The problems of the clean repository with ``extra`` files added, the plant
    confirmed to be in the corpus."""
    root = make_repo(tmp_path, {**CLEAN, **extra})
    corpus = docs_guard.corpus_files(root)
    for name, text in extra.items():
        assert name in corpus  # the plant took effect
        assert (root / name).read_text(encoding="utf-8") == text
    return problems(root, ROOT_SERVICES if root_services is None else root_services)


def test_an_empty_repository_fails_the_guard(tmp_path):
    root = make_repo(tmp_path, {})
    assert docs_guard.corpus_files(root) == []  # the plant took effect: nothing is there
    found = problems(root)
    assert any("nothing was read" in line for line in found), found


# the privilege command


def test_product_code_holds_the_command_in_no_form_at_all(tmp_path):
    assert problems(make_repo(tmp_path / "clean", CLEAN)) == []  # positive control
    found = planted(tmp_path / "planted", {"backend/rcman/x.py": f"# {PRIV} rcman-admin\n"})
    assert len(found) == 1 and "privilege command in product" in found[0], found


def test_a_form_nobody_listed_fails_outside_product_code(tmp_path):
    found = planted(tmp_path, {PAGE: f"{PRIV} rm -rf /\n"})
    assert len(found) == 1 and "form not allowed" in found[0], found


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


# the account layer


def test_an_account_call_is_found(tmp_path):
    rel = "helpers/planted.py"
    found = planted(tmp_path, {rel: "import pwd\n\n\ndef f():\n    return pwd.getpwnam('x')\n"})
    assert len(found) == 1 and found[0].startswith(f"{rel}:"), found


def test_the_exemptions_still_have_their_sites():
    """Stale check: each names a file, a function and a text that still exist."""
    assert EXEMPT  # non-vacuity
    for (rel, function, text), reason in EXEMPT.items():
        assert reason
        assert rel.startswith(AST_ROOTS), rel
        sites = account_sites((REPO / rel).read_text(encoding="utf-8"))
        assert [s for s in sites if (s.function, s.text) == (function, text)], (rel, function, text)


# the services' accounts

ROOT_SERVICE = "installer/templates/planted-root.service"
NO_ACCOUNT = "[Service]\nType=oneshot\nExecStart=/bin/true\n"
PINNED = (
    "[Service]\nType=oneshot\n"
    "ExecStart=/usr/bin/python3 -I /opt/rcman/bin/rcman-upgrade --clone @CLONE@"
    " --caddy-user @CADDY_USER@\n"
    "ExecStopPost=/usr/bin/python3 -I /opt/rcman/bin/rcman-upgrade --finish\n"
    "Environment=@UPGRADE_ENVIRONMENT@\n"
)


def test_every_service_template_names_its_account_but_the_root_services():
    """The real templates: the rule reads them, those that name an account are many, and each
    root service is still there, runs as root and holds only what its exemption allows."""
    corpus = docs_guard.corpus_files(REPO)
    services = [rel for rel in corpus if is_service_template(rel)]
    named = [rel for rel in services if SERVICE_ACCOUNT.search((REPO / rel).read_text())]
    assert len(named) >= 2 and set(ROOT_SERVICES) <= set(services)  # known positives
    assert set(services) - set(named) == set(ROOT_SERVICES)
    assert ROOT_SERVICES and stale_root_services(REPO) == []
    for rel in ROOT_SERVICES:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "ExecStart=" in text  # non-vacuity
        assert root_service_problems(text) == [], rel


def test_a_service_template_that_runs_as_root_is_found(tmp_path):
    found = planted(tmp_path, {ROOT_SERVICE: NO_ACCOUNT})
    assert len(found) == 1 and found[0].startswith(f"{ROOT_SERVICE}:"), found


def test_a_root_service_beyond_its_exemption_is_found(tmp_path):
    exempt = {ROOT_SERVICE: "planted"}
    assert planted(tmp_path / "a", {ROOT_SERVICE: PINNED}, root_services=exempt) == []
    twin = PINNED.replace("rcman-upgrade --finish", "rcman-upgrade --finish ; /bin/sh -c x")
    assert twin != PINNED  # the plant took effect
    found = planted(tmp_path / "b", {ROOT_SERVICE: twin}, root_services=exempt)
    assert found and all("beyond its exemption" in f for f in found), found
