"""No test holds a fixed path under /tmp or a fixed port, which tests running at the same
time, in pytest-xdist's workers or in two runs, would share.

A test's files go under tmp_path; a folder that must be under /tmp, for a short socket
path, is made with a unique name (tempfile.mkdtemp(dir="/tmp")); a port is one the system
picks (port 0) or one held for the test. The guard reads the tests' own source.
tests/container/ is outside it: that suite runs as root in a container of its own.
"""

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
OUTSIDE = "container"  # runs in a throwaway container of its own, never beside another run
TMP = "/" + "tmp"  # built, so that this module does not name it itself
# tempfile's calls that make a uniquely named file or folder in the dir= they are given.
UNIQUE_IN = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
# Ports below this need root to bind, so no other test can take one; a test may name one
# that nothing answers on.
UNPRIVILEGED = 1024
# Sites flagged on purpose: (file relative to tests/, the function it is in) -> reason.
EXEMPT = {
    ("test_record_claude.py", "test_no_fixture_contains_an_unsanitised_secret_or_path"): (
        "searches the recorded fixtures for the text; nothing is made there"
    ),
    ("test_rc_helper.py", "test_mkdir_refuses_a_parent_outside_the_home_folder"): (
        "gives rc-helper the absolute path to show it is refused; nothing is made there"
    ),
}


def _fixed_tmp(node) -> bool:
    """A string constant that is the folder itself or a path in it."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == TMP or node.value.startswith(TMP + "/"))
    )


def _port(node) -> bool:
    """A constant naming an unprivileged port: an int, or a string of digits."""
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return False
    value = node.value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return isinstance(value, int) and UNPRIVILEGED <= value <= 65535


def _address(node) -> bool:
    """A (host, port) tuple with a fixed port, as bind, connect and servers take one."""
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) >= 2
        and isinstance(node.elts[0], ast.Constant)
        and isinstance(node.elts[0].value, str)
        and _port(node.elts[1])
    )


def fixed_sites(source: str) -> list[tuple[int, str, str]]:
    """Where the source names a fixed path under /tmp or a fixed port, with the function
    each is in and what it is: a string that is /tmp or a path in it - an f-string's
    first part too - other than as the dir= of a tempfile call that makes a unique name;
    a (host, port) tuple given to a call; port= given a fixed port; or a fixed port after
    "--port" in an argv, or as "--port=...". Privileged ports are not fixed in this sense."""
    tree = ast.parse(source)
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in UNIQUE_IN:
                allowed |= {id(k.value) for k in node.keywords if k.arg == "dir"}
    found = []

    def what(node) -> str | None:
        if isinstance(node, ast.JoinedStr) and node.values and _fixed_tmp(node.values[0]):
            return "path"
        if _fixed_tmp(node) and id(node) not in allowed:
            return "path"
        if isinstance(node, ast.Call):
            if any(_address(a) for a in node.args):
                return "port"
            if any(k.arg == "port" and _port(k.value) for k in node.keywords):
                return "port"
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
            for flag, value in zip(elts, elts[1:], strict=False):
                if isinstance(flag, ast.Constant) and flag.value == "--port" and _port(value):
                    return "port"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            flag, _, value = node.value.partition("=")
            if flag == "--port" and _port(ast.Constant(value)):
                return "port"
        return None

    def visit(node, function, inside_fstring=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        kind = None if inside_fstring else what(node)
        if kind:
            found.append((node.lineno, function, kind))
        for child in ast.iter_child_nodes(node):
            visit(child, function, inside_fstring or isinstance(node, ast.JoinedStr))

    visit(tree, "")
    return found


def scanned() -> list[Path]:
    return [p for p in sorted(TESTS.rglob("*.py")) if p.relative_to(TESTS).parts[0] != OUTSIDE]


def test_no_test_holds_a_fixed_tmp_path_or_port():
    flagged, total = [], 0
    for path in scanned():
        rel = path.relative_to(TESTS).as_posix()
        for line, function, kind in fixed_sites(path.read_text(encoding="utf-8")):
            total += 1
            if (rel, function) not in EXEMPT:
                flagged.append(f"{rel}:{line} in {function or 'the module'}: fixed {kind}")
    assert total >= len(EXEMPT) > 0  # non-vacuity: the exempt sites are found
    assert flagged == []


def test_the_scan_reads_the_tests_but_not_the_container_suite():
    rels = {p.relative_to(TESTS).as_posix() for p in scanned()}
    assert "conftest.py" in rels and Path(__file__).name in rels
    assert (TESTS / OUTSIDE).is_dir()  # the exemption's subject still exists
    assert any((TESTS / OUTSIDE).rglob("*.py"))
    assert not any(r.startswith(OUTSIDE + "/") for r in rels)


def test_the_exemptions_still_have_their_sites():
    for rel, function in EXEMPT:
        sites = fixed_sites((TESTS / rel).read_text(encoding="utf-8"))
        assert function in {f for _, f, _ in sites}, (rel, function)


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
    ],
)
def test_the_guard_finds_a_planted_fixed_resource(planted, kind):
    """Planted twins: each way of naming a fixed path or port is found, in a function too."""
    assert [k for _, _, k in fixed_sites(planted)] == [kind]
    assert fixed_sites(f"def f():\n    {planted}\n") == [(2, "f", kind)]


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
    ):
        assert fixed_sites(source) == [], source
