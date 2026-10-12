"""Every rc-helper the tests run has GitHub pointed away from the real one.

tests/helper_env.py builds the one environment the tests run rc-helper with;
the guard here finds, in the tests' own source, every other place that names
rc-helper's home hook, and the hook check shows git really is sent to the
address the hook gives, before anything is fetched.
"""

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.github_server import UNREACHABLE
from tests.helper_env import assert_github_hooked, helper_env

TESTS = Path(__file__).resolve().parent
HOME_HOOK = "RC_HELPER_" + "HOME"  # built, so that this module does not name it itself
# Sites that name the home hook outside helper_env, each with why it may:
# (file relative to tests/, the function it is in) -> reason.
EXEMPT = {
    ("container/test_admin_in_container.py", "test_test_hooks_do_not_pass_through_sudo"): (
        "hands the hook to sudo to show that sudo drops it; the helper never sees it"
    ),
}


def hook_sites(source: str) -> list[tuple[int, str]]:
    """Where the source names the home hook other than to read it, with the function each
    is in: the name as a string anywhere - a dict's key, a subscript assigned to, the key
    given to setenv, setdefault or putenv, or held in a variable for any of them - or as
    "RC_HELPER_HOME=..." in an argv, or as a keyword, as dict() and update() take it.
    Reading it (env["RC_HELPER_HOME"]) and asking whether a mapping holds it are not."""
    tree = ast.parse(source)
    found = []
    reads = set()  # the string nodes that only read it

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            reads.add(id(node.slice))
        if isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            reads.add(id(node.left))

    def names_it(node) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(f"{HOME_HOOK}="):
                return True
            return node.value == HOME_HOOK and id(node) not in reads
        return isinstance(node, ast.keyword) and node.arg == HOME_HOOK

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if names_it(node):
            found.append((node.lineno if hasattr(node, "lineno") else 0, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


class ConfigSites(NamedTuple):
    """What config_sites reads in a module: each as (line, the function it is in), and
    an unreadable site with its source text as well."""

    made: list[tuple[int, str]]  # rc-helper's Config called
    without_github: list[tuple[int, str]]  # ... of those, not told where GitHub is
    unreadable: list[tuple[int, str, str]]  # rc-helper reached in a way not followed


# Calls that take a module and an attribute's name: given rc-helper and a written name,
# they reach that attribute as writing it would.
BY_NAME = {"getattr", "setattr", "hasattr", "delattr"}
# What a compound statement's header holds an expression in, besides the statement.
# A with item and a function's arguments have no position of their own, so the
# expression a site is in is taken as the one directly under them.
CLAUSES = (ast.stmt, ast.excepthandler, ast.match_case, ast.withitem, ast.arguments)
# The statements with a header and a body of their own, found among ast's.
COMPOUND = tuple(
    c
    for c in vars(ast).values()
    if isinstance(c, type)
    and issubclass(c, ast.stmt)
    and ("body" in c._fields or "cases" in c._fields)
)
# Unreadable sites that hand rc-helper's module on whole, each with why no Config is made
# through it: (file relative to tests/, the function it is in, the site's statement as
# written - of a compound statement, its header up to the end of the site's expression)
# -> reason. Each covers exactly one site: another in the same function, or the
# statement changed, is reported.
CONFIG_EXEMPT = {
    ("test_rules.py", "helpers_rules", "for module in (users_file, rc_helper, rc_admin)"): (
        "hands each helper module to held_rules, which reads the Rule attributes only"
    ),
    (
        "test_rc_helper.py",
        "test_every_rule_rc_helper_holds_is_reported_or_exempt",
        "held = held_rules(h)",
    ): "hands rc-helper to held_rules, which reads the Rule attributes only",
    (
        "test_rc_helper.py",
        "rule_code_problems",
        "names = {code: next(n for n, v in vars(h).items() if v is rule)"
        " for code, rule in codes.items()}",
    ): "reads vars(h) to find the name a rule is held under; nothing in it is called",
}


# rc-helper's module and its path, built, so that this module does not name them itself.
MODULE = "rc_" + "helper"
PATH = f"helpers.{MODULE}"


def names_rc_helper(value: str) -> bool:
    """Whether a string is a name, its parts joined by dots or colons and not a Python
    file's, with rc_helper among its parts."""
    parts = re.split(r"[.:]", value)
    if parts[-1] == "py":
        return False
    return MODULE in parts and all(part.isidentifier() or not part for part in parts)


def config_sites(source: str) -> ConfigSites:
    """Where the source makes rc-helper's Config, which of those leave out github= and so
    would leave an in-process helper pointed at the real GitHub, and where it reaches
    rc-helper in a way that could hide a Config from this reading.

    Read: how the source names rc-helper - the names its own imports bind to rc-helper's
    module, to the helpers package it is in and to what is imported from the module, the
    attributes written after them, and its strings. What a call returns is not read.
    Followed: the module and the package into a written attribute, or one given by its
    written name to one of BY_NAME (for the module, any name but Config); Config only into
    a call; any other name imported from the module into any use. Reported as unreadable:
    any other use of the module or the package; a dunder attribute, written or given by
    its written name to one of BY_NAME, of a chain of attributes from the module, the
    package or a name imported from the module, once for the chain; the module reached
    as an attribute of anything but the package; a star import from either; and any
    string names_rc_helper accepts."""
    tree = ast.parse(source)
    modules, packages, names, imported = set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "helpers":
            modules |= {a.asname or a.name for a in node.names if a.name == MODULE}
        elif isinstance(node, ast.ImportFrom) and node.module == PATH:
            names |= {a.asname or a.name for a in node.names if a.name == "Config"}
            imported |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == PATH and a.asname:
                    modules.add(a.asname)
                elif a.name == "helpers" or a.name.startswith("helpers."):
                    packages.add(a.asname or "helpers")

    def loaded(node, bound) -> bool:
        return isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in bound

    def is_module(node) -> bool:
        return loaded(node, modules) or (
            isinstance(node, ast.Attribute) and node.attr == MODULE and loaded(node.value, packages)
        )

    def is_config(node) -> bool:
        return loaded(node, names) or (
            isinstance(node, ast.Attribute) and node.attr == "Config" and is_module(node.value)
        )

    def dunder(name) -> bool:
        return isinstance(name, str) and name.startswith("__") and name.endswith("__")

    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    # Split only where Python ends a line, as ast counts lines.
    lines = source.encode().splitlines(keepends=True)

    def keyword_line(line: int, keyword: bytes) -> tuple[int, int]:
        """The line at or before ``line`` that starts with ``keyword``, and its indent."""
        while not lines[line - 1].lstrip().startswith(keyword):
            line -= 1
        return line, len(lines[line - 1]) - len(lines[line - 1].lstrip())

    def written(start: tuple[int, int], end: tuple[int, int]) -> str:
        """The source from one (line, byte offset) to another, as ast gives them."""
        text = b"".join(lines[start[0] - 1 : end[0]])
        cut = len(text) - len(lines[end[0] - 1]) + end[1]
        return text[start[1] : cut].decode()

    def statement_of(node) -> str:
        """The statement a site is in, as written; of a compound statement, its header -
        from its first decorator, or from the except or case clause the site is in - up
        to the end of the expression the site is in."""
        top = node
        while not isinstance(top, ast.stmt) and not isinstance(parents[id(top)], CLAUSES):
            top = parents[id(top)]
        clause = top if isinstance(top, ast.stmt) else parents[id(top)]
        while not isinstance(clause, (ast.stmt, ast.excepthandler, ast.match_case)):
            clause = parents[id(clause)]
        if isinstance(clause, ast.stmt) and not isinstance(clause, COMPOUND):
            ends = (clause.end_lineno, clause.end_col_offset)
            return written((clause.lineno, clause.col_offset), ends)
        if isinstance(clause, ast.match_case):
            start = keyword_line(clause.pattern.lineno, b"case")
        elif getattr(clause, "decorator_list", None):
            first = min(clause.decorator_list, key=lambda d: (d.lineno, d.col_offset))
            start = keyword_line(first.lineno, b"@")
        else:
            start = (clause.lineno, clause.col_offset)
        return written(start, (top.end_lineno, top.end_col_offset))

    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    followed = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    def from_rc_helper(node) -> bool:
        """Whether a chain of attributes starts at the module, the package or a name
        imported from the module."""
        while isinstance(node, ast.Attribute) and not is_module(node):
            node = node.value
        return is_module(node) or loaded(node, packages) or loaded(node, imported)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        name = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
        if func in BY_NAME and isinstance(name, str):
            if name != "Config" or not is_module(node.args[0]):
                followed.add(id(node.args[0]))  # a dunder name is reported as the call

    def dunder_reach(node) -> bool:
        """A dunder attribute, written or given by its written name to one of BY_NAME, of
        a chain of attributes from rc-helper."""
        if isinstance(node, ast.Attribute):
            return dunder(node.attr) and from_rc_helper(node.value)
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            name = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
            return func in BY_NAME and dunder(name) and from_rc_helper(node.args[0])
        return False

    def within_dunder_reach(node) -> bool:
        """Whether a dunder reach is the object of another, which is reported instead."""
        outer = parents.get(id(node))
        held = isinstance(outer, ast.Attribute) and outer.value is node
        given = isinstance(outer, ast.Call) and bool(outer.args) and outer.args[0] is node
        return (held or given) and dunder_reach(outer)

    sites = ConfigSites([], [], [])

    def reached(node) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return names_rc_helper(node.value)
        if isinstance(node, ast.ImportFrom) and node.module in {"helpers", PATH}:
            return any(a.name == "*" for a in node.names)
        if is_config(node):
            return id(node) not in called and not within_dunder_reach(node)
        if dunder_reach(node):
            return not within_dunder_reach(node)
        if isinstance(node, ast.Attribute) and node.attr == MODULE and not is_module(node):
            return True
        return (is_module(node) or loaded(node, packages)) and id(node) not in followed

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and is_config(node.func):
            sites.made.append((line, function))
            if not any(k.arg == "github" for k in node.keywords):
                sites.without_github.append((line, function))
        elif reached(node):
            sites.unreadable.append((line, function, statement_of(node)))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return sites


def where(sites: list[tuple]) -> list[tuple[int, str]]:
    """Sites by line and function alone."""
    return [(line, function) for line, function, *_ in sites]


def config_findings(rel: str, source: str) -> tuple[ConfigSites, list[str], set]:
    """The sites in ``source``, the file ``rel`` under tests/, the unreadable ones no
    exemption covers, and the exemptions that covered one."""
    sites = config_sites(source)
    flagged, used = [], set()
    for line, function, text in sites.unreadable:
        key = (rel, function, text)
        if key in CONFIG_EXEMPT and key not in used:
            used.add(key)
        else:
            flagged.append(f"{rel}:{line} in {function or 'the module'}: {text}")
    return sites, flagged, used


def test_every_in_process_config_says_where_github_is():
    made, without_github, unreadable = set(), [], []
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        sites, flagged, _ = config_findings(rel, path.read_text(encoding="utf-8"))
        made |= {(rel, function) for _, function in sites.made}
        without_github += [f"{rel}:{line}" for line, _ in sites.without_github]
        unreadable += flagged
    # Positive control: the hook check's own Config is read as one.
    assert ("helper_env.py", assert_github_hooked.__name__) in made, sorted(made)
    assert without_github == []
    assert unreadable == []


def test_the_config_exemptions_still_have_their_sites():
    for rel, function, text in CONFIG_EXEMPT:
        _, _, used = config_findings(rel, (TESTS / rel).read_text(encoding="utf-8"))
        assert (rel, function, text) in used, (rel, function, text)


@pytest.mark.parametrize("exemption", CONFIG_EXEMPT, ids=lambda key: key[1])
def test_another_reach_in_an_exempt_function_is_reported(exemption):
    """Planted twin: an exemption covers its own site, not the function it is in."""
    rel, function, text = exemption
    source = (TESTS / rel).read_text(encoding="utf-8")
    assert config_findings(rel, source)[1] == []  # positive control: the exempt site passes
    header = f"def {function}("
    assert source.count(header) == 1
    body = source.index(":\n", source.index(header)) + 2
    again = f"    {text}:\n        pass\n" if text.startswith("for ") else f"    {text}\n"
    for extra in (again, '    x = sys.modules["helpers.rc_helper"]\n'):
        planted = source[:body] + extra + source[body:]
        assert planted != source  # the plant took effect
        assert len(config_findings(rel, planted)[1]) == 1, extra


def test_a_changed_exempt_statement_is_reported():
    """Planted twin: the exempt statement rewritten to call Config through vars(h) is no
    longer the statement exempted."""
    [(rel, function, text)] = [key for key in CONFIG_EXEMPT if "vars(h)" in key[2]]
    source = (TESTS / rel).read_text(encoding="utf-8")
    # Split, so that this source holds no text read as a Markdown link.
    rewritten = 'vars(h)["Config"]' + "(home=x).items()"
    planted = source.replace("vars(h).items()", rewritten, 1)
    assert planted != source  # the plant took effect
    assert [f.split(": ")[0] for f in config_findings(rel, planted)[1]] == [
        f"{rel}:{source[: source.index(text)].count(chr(10)) + 1} in {function}"
    ]


@pytest.mark.parametrize(
    "planted",
    [
        "from helpers import rc_helper as h\ncfg = h.Config(home=x, claude=y, tmux_socket=None)",
        "from helpers import rc_helper\ncfg = rc_helper.Config(home=x, claude=y, tmux_socket=None)",
        "from helpers.rc_helper import Config\ncfg = Config(home=x, claude=y, tmux_socket=None)",
        "import helpers.rc_helper as rh\ncfg = rh.Config(home=x, claude=y, tmux_socket=None)",
        "import helpers.rc_helper\n"
        "cfg = helpers.rc_helper.Config(home=x, claude=y, tmux_socket=None)",
    ],
)
def test_the_config_guard_finds_a_config_with_no_github(planted):
    """Planted twins: each way of reaching rc-helper's Config is found without github=,
    and not with it; another module's Config is not rc-helper's."""
    sites = config_sites(planted)
    assert (sites.made, sites.without_github, where(sites.unreadable)) == (
        [(2, "")],
        [(2, "")],
        [],
    )
    told = config_sites(planted.replace("tmux_socket=None", "tmux_socket=None, github=g"))
    assert (told.made, told.without_github, where(told.unreadable)) == ([(2, "")], [], [])
    other = config_sites("from helpers import rc_admin as ra\ncfg = ra.Config(home=x)")
    assert other == ConfigSites([], [], [])


@pytest.mark.parametrize(
    "planted",
    [
        'import importlib\nrh = importlib.import_module("helpers.rc_helper")',
        'from importlib import import_module\nrh = import_module("helpers.rc_helper")',
        'import os\nrh = __import__("helpers.rc_helper", fromlist=["Config"])',
        'import os\nhelpers = __import__("helpers", fromlist=["rc_helper"])',
        'import sys\nrh = sys.modules["helpers.rc_helper"]',
        'import sys\nrh = sys.modules.get("helpers.rc_helper")',
        'import pkgutil\nmake = pkgutil.resolve_name("helpers.rc_helper:Config")',
        'import importlib\nrh = importlib.import_module("helpers").rc_helper',
        'import sys\nrh = sys.modules["helpers"].rc_helper',
        'import os\nrh = __import__("helpers").rc_helper',
        'from helpers import rc_helper as h\nmake = h.main.__globals__["Config"]',
        "from helpers import rc_helper as h\nwhere = h.main.__module__",
        'from helpers.rc_helper import main\nmake = main.__globals__["Config"]',
        "import helpers\nrh = helpers.__dict__",
        'from helpers.rc_helper import main\nmake = getattr(main, "__globals__")',
        'from helpers import rc_helper as h\nmake = getattr(h.main, "__globals__")',
        'import helpers.rc_helper\nmake = getattr(helpers.rc_helper.main, "__globals__")',
        "from helpers.rc_helper import Rule\nmake = Rule.__init__.__globals__",
        'from helpers.rc_helper import Rule\nmake = getattr(Rule.__init__, "__globals__")',
        "from helpers.rc_helper import Config\nmake = Config.__init__",
        "from helpers import rc_helper as h\nmake = h.Config.__init__",
        'from helpers.rc_helper import Config\nmake = getattr(Config, "__init__")',
        'import helpers\nrh = getattr(helpers, "rc_helper")',
        'from helpers import rc_helper as h\nmake = h.__dict__["Config"]',
        'from helpers import rc_helper as h\nmake = getattr(h, "__dict__")',
        'import os\nmonkeypatch.setattr("helpers.rc_helper.Config", fake)',
        "import os\nfrom helpers.rc_helper import *",
        "import os\nfrom helpers import *",
        'from helpers import rc_helper as h\nmake = getattr(h, "Config")',
        "from helpers import rc_helper as h\nmake = getattr(h, name)",
        "from helpers import rc_helper as h\nmake = vars(h)",
        "from helpers import rc_helper as h\nrh = h",
        "from helpers import rc_helper as h\nuse(h)",
        "import helpers\nuse(helpers)",
        "import helpers.rc_helper\nrh = helpers.rc_helper",
        "from helpers import rc_helper as h\nmake = h.Config",
        "from helpers import rc_helper as h\nmake = functools.partial(h.Config, github=g)",
        "from helpers.rc_helper import Config as C\nmake = C",
        "import helpers.rc_helper\nmake = helpers.rc_helper.Config",
    ],
)
def test_the_config_guard_reports_what_it_cannot_read(planted):
    """Planted twins: each way of reaching rc-helper that could hide a Config from the
    guard is reported where it is, inside a function too."""
    assert where(config_sites(planted).unreadable) == [(2, "")]
    first, second = planted.split("\n")
    assert where(config_sites(f"{first}\ndef f():\n    {second}\n").unreadable) == [(3, "f")]


@pytest.mark.parametrize(
    ("planted", "function", "statement"),
    [
        ("with use(h) as x:\n    pass", "", "with use(h)"),
        ("async def g():\n    async with use(h):\n        pass", "g", "async with use(h)"),
        ("def f(m=use(h)):\n    pass", "f", "def f(m=use(h)"),
        ("@use(h)\ndef f():\n    pass", "f", "@use(h)"),
        ("@first\n@use(h)\nclass C:\n    pass", "", "@first\n@use(h)"),
        ("try:\n    pass\nexcept use(h):\n    pass", "", "except use(h)"),
        ('match x:\n    case {"k": 1} if use(h):\n        pass', "", 'case {"k": 1} if use(h)'),
        ("for m in (use(h), 1):\n    pass", "", "for m in (use(h), 1)"),
        ("assert use(h), 'never'", "", "assert use(h), 'never'"),
        ("x = (\n    use(h)\n)", "", "x = (\n    use(h)\n)"),
        ('for m in ("é", use(h)):\n    pass', "", 'for m in ("é", use(h))'),
        ("\x0c\nfor m in (use(h), 1):\n    pass", "", "for m in (use(h), 1)"),
        ('x = "\u2028"\nfor m in (use(h), 1):\n    pass', "", "for m in (use(h), 1)"),
        ("@ use(h)\ndef f():\n    pass", "f", "@ use(h)"),
        ("@(\n    use(h)\n)\ndef f():\n    pass", "f", "@(\n    use(h)"),
        (
            "match x:\n    case (\n        1\n    ) if use(h):\n        pass",
            "",
            "case (\n        1\n    ) if use(h)",
        ),
    ],
)
def test_an_unreadable_site_is_shown_with_its_statement(planted, function, statement):
    """Of a compound statement, its header up to the end of the site's expression: from
    its first decorator, or the except or case clause the site is in."""
    sites = config_sites(f"from helpers import rc_helper as h\n{planted}\n")
    assert [(f, text) for _, f, text in sites.unreadable] == [(function, statement)]


def test_the_config_guard_leaves_other_reaches_alone():
    for source in (
        'import importlib\nj = importlib.import_module("json")',
        'from helpers import rc_helper as h\nm = getattr(h, "MESSAGES")',
        'from helpers import rc_helper as h\nmonkeypatch.setattr(h, "git_env", fake)',
        'from helpers import rc_admin as ra\nmake = getattr(ra, "Config")',
        "from helpers.rc_admin import *",
        "from helpers import rc_helper as h\nanswer = h.main(argv, env)",
        "import helpers.git_run\nrun = helpers.git_run.Git(env).run",
        'from helpers import rc_helper as h\nmessages = {"rc-helper": h.MESSAGES}',
        'import sys\nrun(["python", "-c", "import helpers.rc_helper"])',
        'from pathlib import Path\nsource = Path("helpers") / "rc_helper.py"',
        "from helpers.rc_helper import main\nanswer = main(argv, env)",
    ):
        assert config_sites(source) == ConfigSites([], [], []), source


def unhooked_sites() -> tuple[list[str], int]:
    """Every site naming the home hook outside tests/helper_env.py and EXEMPT, and how
    many sites there are in all."""
    flagged, total = [], 0
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        for line, function in hook_sites(path.read_text(encoding="utf-8")):
            total += 1
            if rel != "helper_env.py" and (rel, function) not in EXEMPT:
                flagged.append(f"{rel}:{line} in {function or 'the module'}")
    return flagged, total


def test_every_rc_helper_environment_is_built_by_helper_env():
    flagged, total = unhooked_sites()
    assert total >= 2  # non-vacuity: helper_env's own and the exempt one are found
    assert flagged == []


def test_the_exemptions_still_have_their_sites():
    for rel, function in EXEMPT:
        assert (rel, function) in {
            (rel, f) for _, f in hook_sites((TESTS / rel).read_text(encoding="utf-8"))
        }, (rel, function)


@pytest.mark.parametrize(
    "planted",
    [
        'env = {"PATH": "/usr/bin", "RC_HELPER_HOME": "/x"}',
        'env["RC_HELPER_HOME"] = "/x"',
        'monkeypatch.setenv("RC_HELPER_HOME", "/x")',
        'run(dict(PATH="/usr/bin"), {"RC_HELPER_HOME": home})',
        'env = dict(PATH="/usr/bin", RC_HELPER_HOME="/x")',
        'env.update(RC_HELPER_HOME="/x")',
        'env.setdefault("RC_HELPER_HOME", "/x")',
        'os.putenv("RC_HELPER_HOME", "/x")',
        'run(["env", "RC_HELPER_HOME=/x", "rc-helper", "list"])',
        'KEY = "RC_HELPER_HOME"',
    ],
)
def test_the_guard_finds_a_planted_environment(planted):
    """Planted twins: each way of naming the hook is found, inside a function too."""
    assert hook_sites(planted) != []
    assert hook_sites(f"def f():\n    {planted}\n") == [(2, "f")]


def test_the_guard_leaves_reading_the_hook_alone():
    for source in (
        "env = helper_env(home, claude, sock)",
        'home = env["RC_HELPER_HOME"]',
        'assert "RC_HELPER_HOME" not in seen',
    ):
        assert hook_sites(source) == [], source


def test_helper_env_always_sets_github():
    env = helper_env(Path("/h"), Path("/c"), Path("/s"))
    assert env["RC_HELPER_GITHUB"] == UNREACHABLE
    assert helper_env(Path("/h"), Path("/c"), Path("/s"), "http://127.0.0.1:9/")[
        "RC_HELPER_GITHUB"
    ] == ("http://127.0.0.1:9/")
    assert env["RC_HELPER_HOME"] == "/h"


def test_the_hook_check_passes_for_the_tests_github_and_the_unreachable_address(github, tmp_path):
    assert_github_hooked(tmp_path, github.url, github)
    assert_github_hooked(tmp_path, UNREACHABLE)


def test_the_hook_check_fails_without_the_hook_before_anything_is_fetched(
    github, tmp_path, monkeypatch
):
    """Planted twin: a git_env that drops the hook is caught by the address git would
    use, before git is asked to reach it."""
    from helpers import git_run
    from helpers import rc_helper as h

    plain = h.git_env
    monkeypatch.setattr(
        h, "git_env", lambda cfg: {k: v for k, v in plain(cfg).items() if "CONFIG" not in k}
    )
    reached = []
    monkeypatch.setattr(
        git_run.Git, "run", lambda self, cwd, args, **kw: reached.append(args) or "x"
    )
    with pytest.raises(AssertionError):
        assert_github_hooked(tmp_path, github.url, github)
    assert reached == []  # nothing went through the network path


# --- git given GitHub's address -------------------------------------------------------

# The tests' modules that give git GitHub's address on purpose, each keeping it away
# from the real one itself: the hook check, the stand-in and the fixtures. A literal
# address is all this guard sees; fixture git, which tests give addresses held in
# variables too, refuses every network transport (git_fixtures.fixture_env).
GITHUB_SANCTIONED = {"helper_env.py", "github_server.py", "git_fixtures.py"}
# Git calls given GitHub's address elsewhere, each with why it reaches no GitHub:
# (file relative to tests/, the function it is in) -> reason.
GITHUB_EXEMPT = {
    (
        "container/test_github_in_container.py",
        "test_the_token_is_stored_for_github_com_in_the_persons_home",
    ): "reads the configuration key naming GitHub's credential helper; git reaches nothing",
}
GITHUB_HOST = "github." + "com"  # built, so that this module does not name it itself


def github_git_calls(source: str) -> list[tuple[int, str]]:
    """Where the source runs git with GitHub's address among its arguments, with the
    function each is in: fixture_git, and any call whose first argument is "git" or a
    list or tuple starting with it, with a string anywhere in its arguments, an
    f-string's parts among them, that names GitHub's host."""
    found = []

    def runs_git(call: ast.Call) -> bool:
        if getattr(call.func, "id", None) == "fixture_git":
            return True
        first = call.args[0] if call.args else None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            first = first.elts[0]
        return isinstance(first, ast.Constant) and first.value == "git"

    def names_github(call: ast.Call) -> bool:
        return any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and GITHUB_HOST in node.value
            for arg in [*call.args, *(k.value for k in call.keywords)]
            for node in ast.walk(arg)
        )

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call) and runs_git(node) and names_github(node):
            found.append((node.lineno, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), "")
    return found


def github_git_sites() -> tuple[list[str], int]:
    """Every git call given GitHub's address outside GITHUB_SANCTIONED and GITHUB_EXEMPT,
    and how many such calls there are in all."""
    flagged, total = [], 0
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        for line, function in github_git_calls(path.read_text(encoding="utf-8")):
            total += 1
            if rel not in GITHUB_SANCTIONED and (rel, function) not in GITHUB_EXEMPT:
                flagged.append(f"{rel}:{line} in {function or 'the module'}")
    return flagged, total


def test_git_is_given_githubs_address_only_where_it_reaches_no_github():
    flagged, total = github_git_sites()
    assert total >= 1  # non-vacuity: the exempt site is found
    assert flagged == []


def test_the_github_exemptions_still_have_their_sites():
    for rel, function in GITHUB_EXEMPT:
        sites = github_git_calls((TESTS / rel).read_text(encoding="utf-8"))
        assert function in {f for _, f in sites}, (rel, function)


@pytest.mark.parametrize(
    "planted",
    [
        f'fixture_git(repo, "clone", "https://{GITHUB_HOST}/o/r")',
        f'fixture_git(repo, "fetch", f"https://{GITHUB_HOST}/{{owner}}/r")',
        f'subprocess.run(["git", "clone", "https://{GITHUB_HOST}/o/r"])',
        f'as_bob("git", "-C", repo, "ls-remote", "https://{GITHUB_HOST}/o/r")',
        f'run(("git", "push", url), env={{"X": "https://{GITHUB_HOST}/"}})',
    ],
)
def test_the_github_guard_finds_a_planted_git_call(planted):
    """Planted twins: each way of giving git the address is found, inside a function too."""
    assert github_git_calls(planted) != []
    assert github_git_calls(f"def f():\n    {planted}\n") == [(2, "f")]


def test_the_github_guard_leaves_other_calls_alone():
    for source in (
        f'fixture_git(repo, "clone", "{UNREACHABLE}o/r")',
        f'subprocess.run(["curl", "https://{GITHUB_HOST}/"])',
        f'helper(env, "git-clone", "--url=https://{GITHUB_HOST}/o/r")',
    ):
        assert github_git_calls(source) == [], source


def test_fixture_git_refuses_https_before_reaching_anything(tmp_path):
    """Whatever address a test gives fixture git, it never speaks https, so it cannot
    reach the real GitHub, whatever a rewrite would have sent there."""
    import subprocess

    from tests.git_fixtures import fixture_env

    address = "https://127.0.0.1:1/owner/repository"
    env = fixture_env(tmp_path / "home")
    refused = subprocess.run(["git", "ls-remote", address], env=env, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "transport 'https' not allowed" in refused.stderr, refused.stderr
    plain = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG_")}
    tried = subprocess.run(["git", "ls-remote", address], env=plain, capture_output=True, text=True)
    assert "not allowed" not in tried.stderr, tried.stderr  # positive control: git tried


@pytest.mark.parametrize(
    ("address", "transport"),
    [
        ("http://127.0.0.1:1/owner/repository", "http"),
        ("ssh://127.0.0.1:1/owner/repository", "ssh"),
        ("git@github.com:owner/repository.git", "ssh"),
        ("git://127.0.0.1:1/owner/repository", "git"),
    ],
)
def test_fixture_git_refuses_every_network_transport_before_connecting(
    tmp_path, address, transport
):
    """Fixture git reaches no network at all: each transport is refused before anything
    is connected to, an scp-like address too."""
    import subprocess

    from tests.git_fixtures import fixture_env

    env = fixture_env(tmp_path / "home")
    refused = subprocess.run(["git", "ls-remote", address], env=env, capture_output=True, text=True)
    assert refused.returncode != 0
    assert f"transport '{transport}' not allowed" in refused.stderr, refused.stderr


def test_fixture_git_still_reads_a_repository_by_its_path(tmp_path):
    """Positive control: the local repositories the fixtures make, and clone from by
    their path, are still read."""
    from tests.git_fixtures import fixture_git, make_repo

    source = make_repo(tmp_path / "source")
    fixture_git(tmp_path, "clone", "-q", "--", str(source), "copy")
    fixture_git(tmp_path, "clone", "-q", "--", f"file://{source}", "by-url")
    assert (tmp_path / "copy" / "README.md").is_file()
    assert (tmp_path / "by-url" / "README.md").is_file()
