"""No test holds a fixed path under /tmp or a fixed port, which tests running at the same
time, in pytest-xdist's workers or in two runs, would share.

A test's files go under tmp_path; a folder that must be under /tmp, for a short socket
path, is made with a unique name (tempfile.mkdtemp(dir="/tmp")); a port is one the system
picks (port 0) or one held for the test. The guard reads the tests' own source.
tests/container/ is outside it: that suite runs as root in a container of its own.
"""

import ast
import ipaddress
import re
from pathlib import Path
from typing import NamedTuple

import pytest

TESTS = Path(__file__).resolve().parent
OUTSIDE = "container"  # runs in a throwaway container of its own, never beside another run
TMP = "/" + "tmp"  # built, so that this module does not name it itself
# tempfile's calls that make a uniquely named file or folder in the dir= they are given.
UNIQUE_IN = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
# Ports below this need root to bind, so no other test can take one; a test may name one
# that nothing answers on.
UNPRIVILEGED = 1024
# Sites flagged on purpose: (file relative to tests/, the function it is in, the site's
# source text) -> reason. Each covers exactly one site: another site of the same text in
# the same function is flagged.
EXEMPT = {
    (
        "test_record_claude.py",
        "test_no_fixture_contains_an_unsanitised_secret_or_path",
        f'"{TMP}/"',
    ): "searches the recorded fixtures for the text; nothing is made there",
    (
        "test_rc_helper.py",
        "test_mkdir_refuses_a_parent_outside_the_home_folder",
        f'"{TMP}"',
    ): "gives rc-helper the absolute path to show it is refused; nothing is made there",
}
# Flags that take a port, and those that take one alone or as the host port of docker's
# [address:]host:container mapping.
PORT_FLAGS = {"--port"}
PUBLISH_FLAGS = {"-p", "--publish"}


class Site(NamedTuple):
    line: int
    function: str
    kind: str
    text: str


def _name(call) -> str | None:
    return getattr(call.func, "attr", None) or getattr(call.func, "id", None)


def _fixed_tmp(node) -> bool:
    """A string constant that is the folder itself or a path in it."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == TMP or node.value.startswith(TMP + "/"))
    )


def _gettempdir(node) -> bool:
    return isinstance(node, ast.Call) and _name(node) == "gettempdir"


def _port(node, names=frozenset()) -> bool:
    """A fixed unprivileged port: an int or a string of digits, a module-level name
    given one, or str() of either."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call) and _name(node) == "str" and len(node.args) == 1:
        return _port(node.args[0], names)
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return False
    value = node.value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return isinstance(value, int) and UNPRIVILEGED <= value <= 65535


def _published(node, names) -> bool:
    """What follows -p or --publish: a fixed port alone, or as the host port of a
    [address:]host:container mapping."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and ":" in node.value:
        return _port(ast.Constant(node.value.split(":")[-2]))
    return _port(node, names)


def _address(node, names) -> bool:
    """A (host, port) or (host, port, flowinfo, scope) tuple or list with a fixed port,
    as bind, connect and servers take one, wherever it is written."""
    return (
        isinstance(node, (ast.Tuple, ast.List))
        and len(node.elts) in (2, 4)
        and isinstance(node.elts[0], ast.Constant)
        and _host(node.elts[0].value)
        and _port(node.elts[1], names)
    )


def _host(value) -> bool:
    """A host as an address names one: any (""), localhost, an IP address or a domain."""
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value in ("", "localhost") or bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", value))
    return True


def _port_names(tree) -> frozenset[str]:
    """The module-level names given a fixed port."""
    names = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        if targets and _port(node.value):
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return frozenset(names)


def _unique_dirs(tree) -> set[int]:
    """The dir= arguments of tempfile calls that make a unique name in the folder itself:
    /tmp or gettempdir(), as written or inside Path() or str()."""
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node) in UNIQUE_IN:
            for value in (k.value for k in node.keywords if k.arg == "dir"):
                if isinstance(value, ast.Call) and _name(value) in ("Path", "str"):
                    value = value.args[0] if len(value.args) == 1 else value
                if value.__class__ is ast.Constant and value.value == TMP or _gettempdir(value):
                    allowed.add(id(value))
    return allowed


def fixed_sites(source: str) -> list[Site]:
    """Where the source names a fixed path under /tmp or a fixed port, with the function
    each is in, what it is and its text. See the guard's description in tests.md for the
    forms it catches."""
    tree = ast.parse(source)
    allowed = _unique_dirs(tree)
    names = _port_names(tree)
    found = []

    def what(node) -> str | None:
        if isinstance(node, ast.JoinedStr) and node.values and _fixed_tmp(node.values[0]):
            return "path"
        if (_fixed_tmp(node) or _gettempdir(node)) and id(node) not in allowed:
            return "path"
        if _address(node, names):
            return "port"
        if isinstance(node, ast.Call):
            if any(k.arg == "port" and _port(k.value, names) for k in node.keywords):
                return "port"
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
            for flag, value in zip(elts, elts[1:], strict=False):
                if not isinstance(flag, ast.Constant):
                    continue
                if flag.value in PORT_FLAGS and _port(value, names):
                    return "port"
                if flag.value in PUBLISH_FLAGS and _published(value, names):
                    return "port"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            flag, _, value = node.value.partition("=")
            if flag in PORT_FLAGS and _port(ast.Constant(value)):
                return "port"
            if flag in PUBLISH_FLAGS and _published(ast.Constant(value), names):
                return "port"
        return None

    def visit(node, function, inside_fstring=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        kind = None if inside_fstring else what(node)
        if kind:
            text = ast.get_source_segment(source, node) or ""
            found.append(Site(node.lineno, function, kind, text))
        for child in ast.iter_child_nodes(node):
            visit(child, function, inside_fstring or isinstance(node, ast.JoinedStr))

    visit(tree, "")
    return found


def flagged(rel: str, source: str) -> list[str]:
    """The sites in ``source``, the file ``rel`` under tests/, that no exemption covers."""
    out, seen = [], set()
    for site in fixed_sites(source):
        key = (rel, site.function, site.text)
        if key in EXEMPT and key not in seen:
            seen.add(key)
            continue
        where = site.function or "the module"
        out.append(f"{rel}:{site.line} in {where}: fixed {site.kind} {site.text}")
    return out


def scanned() -> list[Path]:
    return [p for p in sorted(TESTS.rglob("*.py")) if p.relative_to(TESTS).parts[0] != OUTSIDE]


def test_no_test_holds_a_fixed_tmp_path_or_port():
    found, total = [], 0
    for path in scanned():
        rel, source = path.relative_to(TESTS).as_posix(), path.read_text(encoding="utf-8")
        total += len(fixed_sites(source))
        found += flagged(rel, source)
    assert total >= len(EXEMPT) > 0  # non-vacuity: the exempt sites are found
    assert found == []


def test_the_scan_reads_the_tests_but_not_the_container_suite():
    rels = {p.relative_to(TESTS).as_posix() for p in scanned()}
    assert "conftest.py" in rels and Path(__file__).name in rels
    assert (TESTS / OUTSIDE).is_dir()  # the exemption's subject still exists
    assert any((TESTS / OUTSIDE).rglob("*.py"))
    assert not any(r.startswith(OUTSIDE + "/") for r in rels)


def test_the_exemptions_still_have_their_sites():
    for rel, function, text in EXEMPT:
        sites = fixed_sites((TESTS / rel).read_text(encoding="utf-8"))
        assert [s for s in sites if (s.function, s.text) == (function, text)], (rel, function)


def test_a_new_fixed_site_in_an_exempt_function_is_flagged():
    """Planted twin: an exemption covers its own site, not the function it is in."""
    rel, function, text = next(iter(EXEMPT))
    source = (TESTS / rel).read_text(encoding="utf-8")
    assert flagged(rel, source) == []  # positive control: the exempt site passes
    header = f"def {function}("
    assert source.count(header) == 1
    body = source.index(":\n", source.index(header)) + 2
    for extra in ('    s.bind(("127.0.0.1", 8765))\n', f"    x = {text}\n"):
        planted = source[:body] + extra + source[body:]
        assert planted != source  # the plant took effect
        assert len(flagged(rel, planted)) == 1, extra


@pytest.mark.parametrize(
    ("planted", "kind"),
    [
        (f'root = Path("{TMP}") / f"x-{{name}}"', "path"),
        (f'root = Path("{TMP}/rcman-e2e")', "path"),
        (f'root = f"{TMP}/x-{{name}}"', "path"),
        (f'sock = os.path.join("{TMP}", "a.sock")', "path"),
        (f'd = tmp_path_factory.mktemp("x", dir="{TMP}")', "path"),
        ('s.bind(("127.0.0.1", 8765))', "port"),
        ('server = HTTPServer(("", 8765), Handler)', "port"),
        ('socket.create_connection(("::1", "2019"))', "port"),
        ("github.start(root, token, port=8765)", "port"),
        ('run(["uvicorn", "app", "--port", "8765"])', "port"),
        ('run(["uvicorn", "app", "--port=8765"])', "port"),
        ('address = ("127.0.0.1", 8765)', "port"),
        ('address = ["127.0.0.1", 8765]', "port"),
        ('s.bind(["127.0.0.1", 8765])', "port"),
        ('address = ("::1", 8765, 0, 0)', "port"),
        ('address = ("rcman.test", 8765)', "port"),
        ('address = ("localhost", "8765")', "port"),
        ('run(["server", "-p", "8765"])', "port"),
        ('run(["docker", "run", "-p", "8765:80", "image"])', "port"),
        ('run(["docker", "run", "-p", "127.0.0.1:8765:443", "image"])', "port"),
        ('run(["docker", "run", "--publish", "8765:80", "image"])', "port"),
        ('run(["docker", "run", "--publish=127.0.0.1:8765:80", "image"])', "port"),
        ('root = Path(tempfile.gettempdir()) / "fixed"', "path"),
        ('sock = os.path.join(tempfile.gettempdir(), "a.sock")', "path"),
        (f'd = tempfile.mkdtemp(dir="{TMP}/sub")', "path"),
        ('d = tempfile.mkdtemp(dir=os.path.join(tempfile.gettempdir(), "sub"))', "path"),
    ],
)
def test_the_guard_finds_a_planted_fixed_resource(planted, kind):
    """Planted twins: each way of naming a fixed path or port is found, in a function too."""
    assert [s.kind for s in fixed_sites(planted)] == [kind]
    assert [s[:3] for s in fixed_sites(f"def f():\n    {planted}\n")] == [(2, "f", kind)]


PORT_CONSTANT = "PORT = 8765\n"


@pytest.mark.parametrize(
    "use",
    [
        's.bind(("127.0.0.1", PORT))',
        "github.start(root, token, port=PORT)",
        'run(["uvicorn", "app", "--port", str(PORT)])',
        'run(["uvicorn", "app", "--port", PORT])',
    ],
)
def test_the_guard_finds_a_fixed_port_held_in_a_module_constant(use):
    """Planted twin: a port named once at module level and used in a function."""
    source = f"{PORT_CONSTANT}def f():\n    {use}\n"
    assert [s[:3] for s in fixed_sites(source)] == [(3, "f", "port")]
    chosen = source.replace(PORT_CONSTANT, "PORT = 0\n")
    assert chosen != source and fixed_sites(chosen) == []  # positive control: port 0


def test_the_guard_leaves_unique_names_and_chosen_ports_alone():
    for source in (
        f'root = Path(tempfile.mkdtemp(prefix="rcb-", dir="{TMP}"))',
        f'with tempfile.TemporaryDirectory(dir="{TMP}") as d: pass',
        'root = tmp_path / "x"',
        's.bind(("127.0.0.1", 0))',
        "github.start(root, token, port=0)",
        'UNREACHABLE = "http://127.0.0.1:1/"',
        'socket.create_connection(("127.0.0.1", 9))',
        'run(["uvicorn", "app", "--port", str(port)])',
        f'text = "{TMP}x is another folder"',
        "d = tempfile.mkdtemp(dir=tempfile.gettempdir())",
        "d = tempfile.mkdtemp(dir=Path(tempfile.gettempdir()))",
        f'd = tempfile.mkdtemp(dir=Path("{TMP}"))',
        'run(["tmux", "display-message", "-p", "#{pid}"])',
        'run(["mkdir", "-p", "folder"])',
        'run(["docker", "run", "-p", f"127.0.0.1:{port}:443", "image"])',
        'run(["docker", "run", "-p", "127.0.0.1::443", "image"])',
        's.bind(("127.0.0.1", port))',
        'parse(["--app-port", "8001"])',
        'sizes = [("width", 1200), ("height", 1200)]',
        "def f(port):\n    s.bind(('', port))\n",
    ):
        assert fixed_sites(source) == [], source
