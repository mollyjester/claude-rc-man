"""No test holds a fixed path under /tmp or a fixed port, which tests running at the same
time, in pytest-xdist's workers or in two runs, would share.

A test's files go under tmp_path; a folder that must be under /tmp, for a short socket
path, is made with a unique name (tempfile.mkdtemp(dir="/tmp")); a port is one the system
picks (port 0) or one held for the test. The guard reads the tests' own source.
tests/container/ is outside it: that suite runs as root in a container of its own.

It reads the frontend's TypeScript and JavaScript too, where the browser tests and their
configs are. What it catches in each language is in project-knowledge/tests.md.
"""

import ast
import ipaddress
import json
import re
import subprocess
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
        "leaks",
        f'"{TMP}/"',
    ): "searches the kept screens for the text; nothing is made there",
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


def _is_tmp(text: str) -> bool:
    """The folder itself or a path in it."""
    return text == TMP or text.startswith(TMP + "/")


def _port_number(value) -> bool:
    """An unprivileged port, as an int or a string of digits."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return type(value) is int and UNPRIVILEGED <= value <= 65535


def _fixed_tmp(node) -> bool:
    """A string constant that is the folder itself or a path in it."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_tmp(node.value)


def _gettempdir(node) -> bool:
    return isinstance(node, ast.Call) and _name(node) == "gettempdir"


def _port(node, names=frozenset()) -> bool:
    """A fixed unprivileged port: an int or a string of digits, a module-level name
    given one, or str() of either."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call) and _name(node) == "str" and len(node.args) == 1:
        return _port(node.args[0], names)
    return isinstance(node, ast.Constant) and _port_number(node.value)


def _published(text: str) -> bool:
    """What follows -p or --publish, written whole: a fixed port alone, or as the host
    port of a [address:]host:container mapping."""
    return _port_number(text.split(":")[-2] if ":" in text else text)


def _published_node(node, names) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _published(node.value)
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


def _host(value, named=True) -> bool:
    """A host as an address names one: any (""), localhost, an IP address or, unless
    ``named`` is false, a domain."""
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        domain = named and bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", value))
        return value in ("", "localhost") or domain
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
                if flag.value in PUBLISH_FLAGS and _published_node(value, names):
                    return "port"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            flag, _, value = node.value.partition("=")
            if flag in PORT_FLAGS and _port_number(value):
                return "port"
            if flag in PUBLISH_FLAGS and _published(value):
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


# The frontend's TypeScript and JavaScript: the browser tests, their configs and the rest.

FRONTEND = TESTS.parent / "frontend"
# Sites flagged on purpose: (file relative to frontend/, the function or test it is in, the
# site's source text) -> reason. Each covers exactly one site, as EXEMPT's do.
TS_EXEMPT = {
    ("e2e/world.ts", "", f'join("{TMP}", `rcman-e2e-${{randomUUID()}}`)'): (
        "a world no other run names, which the backend makes only where nothing is"
        " (project-knowledge/frontend.md)"
    ),
    ("vite.config.ts", "", '"http://127.0.0.1:8000"'): (
        "the development server's way to a backend a person runs by hand; no test runs either"
    ),
}
# The parser's language for each kind of source the guard reads.
SOURCES = {
    ".ts": "ts", ".mts": "ts", ".cts": "ts", ".tsx": "tsx",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "jsx",
}  # fmt: skip
# Calls that make a uniquely named folder at the path they are given: a prefix in a folder.
TS_UNIQUE_IN = {"mkdtemp", "mkdtempSync"}
# Vite's own parser, from the frontend's packages: each source given on stdin as
# {name: [language, source]}, as ESTree, with offsets in UTF-16 code units.
PARSE_TS = """
import { parseAst } from "vite";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const trees = {};
for (const [name, [lang, source]] of Object.entries(JSON.parse(input))) {
  try {
    trees[name] = parseAst(source, { lang });
  } catch (error) {
    throw new Error(`${name}: ${error.message}`);
  }
}
process.stdout.write(JSON.stringify(trees));
"""
FLAGS = "|".join(re.escape(f) for f in sorted(PORT_FLAGS | PUBLISH_FLAGS, key=len, reverse=True))
# A flag and what follows it, in a command written as one string; and a flag just before
# a port written as an expression.
FLAG_IN_TEXT = re.compile(rf"(?:^|\s)({FLAGS})(?: |=)(\S+)")
FLAG_BEFORE = re.compile(rf"(?:^|\s)(?:{FLAGS})(?: |=)$")
# An address's host and colon: in a URL, after its scheme and any user, or else alone.
ADDRESS = r"(?P<scheme>://(?:[^/@\s]*@)?)?(?P<host>\[[0-9A-Fa-f:.]*\]|[^\s/:@\[\],=]+):"
ADDRESS_IN_TEXT = re.compile(ADDRESS + r"(?P<port>[0-9]+)(?![0-9])")
ADDRESS_BEFORE = re.compile(ADDRESS + "$")
# What may stand just before a host written without a scheme.
BEFORE_HOST = " \t\n,=@"
# A name that holds a port: port itself, or one ending _PORT, as an environment's do.
PORT_NAME = re.compile(r"(?:^|_)port$", re.IGNORECASE)
FUNCTIONS = {"ArrowFunctionExpression", "FunctionExpression"}
WRAPPERS = {"TSAsExpression", "TSSatisfiesExpression", "TSNonNullExpression"}


def parse_ts(sources: dict[str, str], cwd: Path = FRONTEND) -> dict[str, dict]:
    """The ESTree of each source, by name, parsed as its suffix says, TypeScript where it
    says none. Raises when any does not parse, or the parser cannot be run."""
    given = {n: [SOURCES.get(Path(n).suffix, "ts"), s] for n, s in sources.items()}
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", PARSE_TS],
        cwd=cwd, input=json.dumps(given), capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def _segment(source: str, start: int, end: int) -> str:
    return source.encode("utf-16-le")[2 * start : 2 * end].decode("utf-16-le")


def _ts_name(node) -> str | None:
    """A call's function name, as tmpdir() or os.tmpdir() name it; an assignment's or a
    member's name, as process.env.PORT names it."""
    target = node.get("callee") or node.get("left") or node
    if target.get("type") == "MemberExpression":
        target = target["property"]
    return target.get("name") if target.get("type") == "Identifier" else _ts_string(target)


def _ts_string(node) -> str | None:
    if node and node["type"] == "Literal" and isinstance(node.get("value"), str):
        return node["value"]
    return None


def _ts_tmp(node) -> bool:
    """/tmp or a path in it, as a string, a template beginning with one, or tmpdir()."""
    if node["type"] == "TemplateLiteral":
        return _is_tmp(node["quasis"][0]["value"]["cooked"] or "")
    if node["type"] == "CallExpression":
        return _ts_name(node) == "tmpdir"
    text = _ts_string(node)
    return text is not None and _is_tmp(text)


def _ts_unique_dirs(tree) -> set[int]:
    """The join(<folder>, <prefix>) given to a call that makes a uniquely named folder,
    where the folder is /tmp itself or tmpdir()."""
    allowed = set()

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if node.get("type") == "CallExpression" and _ts_name(node) in TS_UNIQUE_IN:
                given = (node["arguments"] or [{}])[0]
                if given.get("type") == "CallExpression" and _ts_name(given) == "join":
                    folder, *rest = given["arguments"] or [{"type": ""}]
                    exact = _ts_string(folder) == TMP or _ts_name(folder) == "tmpdir"
                    if exact and len(rest) == 1:
                        allowed.add(id(given))
            for child in node.values():
                visit(child)

    visit(tree)
    return allowed


def _ts_port(node, names=frozenset()) -> bool:
    """A fixed unprivileged port: a number or a string of digits, a module-level name given
    one, String() or Number() of either, a template of either alone, either after ?? or ||,
    or either cast."""
    if not node:
        return False
    kind = node["type"]
    if kind in WRAPPERS or kind == "ParenthesizedExpression":
        return _ts_port(node["expression"], names)
    if kind == "Identifier":
        return node["name"] in names
    if kind == "Literal":
        return _port_number(node["value"])
    if kind == "CallExpression" and _ts_name(node) in ("String", "Number"):
        return len(node["arguments"]) == 1 and _ts_port(node["arguments"][0], names)
    if kind == "TemplateLiteral" and len(node["expressions"]) == 1:
        alone = all(q["value"]["cooked"] == "" for q in node["quasis"])
        return alone and _ts_port(node["expressions"][0], names)
    if kind == "LogicalExpression" and node["operator"] in ("??", "||"):
        return _ts_port(node["right"], names)
    return False


def _addressed(match) -> bool:
    """A host as an address names one: in a URL any _host(), and alone only one that
    cannot be a file's name, such as an IP address."""
    host = match["host"].removeprefix("[").removesuffix("]")
    if match["scheme"]:
        return _host(host)
    start = match.start()
    return (start == 0 or match.string[start - 1] in BEFORE_HOST) and _host(host, named=False)


def _port_in_text(text: str) -> bool:
    """A fixed port in text: after a flag that takes one, or after an address's host."""
    for flag, value in FLAG_IN_TEXT.findall(text):
        if _port_number(value) if flag in PORT_FLAGS else _published(value):
            return True
    return any(_addressed(m) and _port_number(m["port"]) for m in ADDRESS_IN_TEXT.finditer(text))


def _port_follows(text: str) -> bool:
    """Whether a port written next, as an expression, would be one: after a flag that
    takes one, or after an address's host and its colon."""
    address = ADDRESS_BEFORE.search(text)
    return bool(FLAG_BEFORE.search(text)) or bool(address and _addressed(address))


def _ts_ending(node) -> str | None:
    """The text a string, a template or a + of them ends with."""
    if node["type"] == "TemplateLiteral":
        return node["quasis"][-1]["value"]["cooked"]
    if node["type"] == "BinaryExpression" and node["operator"] == "+":
        return _ts_ending(node["right"])
    return _ts_string(node)


def _ts_port_names(tree) -> frozenset[str]:
    """The module-level names given a fixed port."""
    names = set()
    for node in tree["body"]:
        if node["type"] == "ExportNamedDeclaration":
            node = node.get("declaration") or {}
        if node.get("type") != "VariableDeclaration":
            continue
        for declarator in node["declarations"]:
            if declarator["id"]["type"] == "Identifier" and _ts_port(declarator["init"]):
                names.add(declarator["id"]["name"])
    return frozenset(names)


def _ts_what(node, names) -> str | None:
    kind = node["type"]
    text = _ts_string(node)
    if text is not None:
        return "path" if _is_tmp(text) else "port" if _port_in_text(text) else None
    if kind == "TemplateLiteral":
        quasis = [q["value"]["cooked"] or "" for q in node["quasis"]]
        if _is_tmp(quasis[0]):
            return "path"
        if any(_port_in_text(q) for q in quasis):
            return "port"
        for before, value in zip(quasis, node["expressions"], strict=False):
            if _port_follows(before) and _ts_port(value, names):
                return "port"
    if kind == "BinaryExpression" and node["operator"] == "+":
        before = _ts_ending(node["left"])
        if before is not None and _port_follows(before) and _ts_port(node["right"], names):
            return "port"
    if kind == "CallExpression":
        args = node["arguments"]
        if _ts_name(node) == "tmpdir" or args and _ts_tmp(args[0]):
            return "path"
        if node["callee"]["type"] == "MemberExpression" and _ts_name(node) == "listen":
            return "port" if args and _ts_port(args[0], names) else None
    if kind == "AssignmentExpression" and PORT_NAME.search(_ts_name(node) or ""):
        return "port" if _ts_port(node["right"], names) else None
    if kind == "Property" and PORT_NAME.search(_ts_name(node["key"]) or ""):
        return "port" if _ts_port(node["value"], names) else None
    if kind == "ArrayExpression":
        elements = node["elements"]
        for flag, value in zip(elements, elements[1:], strict=False):
            flag = _ts_string(flag)
            if flag in PORT_FLAGS and _ts_port(value, names):
                return "port"
            text = _ts_string(value)
            if text is not None and _port_in_text(text):
                continue  # a site itself, as an address
            published = _published(text) if text is not None else _ts_port(value, names)
            if flag in PUBLISH_FLAGS and published:
                return "port"
    return None


def ts_sites(source: str, tree: dict) -> list[Site]:
    """Where a source names a fixed path under /tmp or a fixed port, with the function or
    test each is in: a function's or a variable's name, or the title a call such as
    test() gives the function it is passed. A path given to a call is the call's site,
    so join() names it with the rest of the path."""
    names = _ts_port_names(tree)
    allowed = _ts_unique_dirs(tree)
    given = set()  # paths given to a call, whose site is the call
    found = []

    def visit(node, scope):
        if isinstance(node, list):
            for child in node:
                visit(child, scope)
            return
        if not isinstance(node, dict) or "type" not in node:
            return
        kind = node["type"]
        if kind == "FunctionDeclaration" and node.get("id"):
            scope = node["id"]["name"]
        if kind == "CallExpression" and node["arguments"] and _ts_tmp(node["arguments"][0]):
            given.add(id(node["arguments"][0]))
        what = None if id(node) in given | allowed else _ts_what(node, names)
        if what:
            line = _segment(source, 0, node["start"]).count("\n") + 1
            found.append(Site(line, scope, what, _segment(source, node["start"], node["end"])))
        for key, child in node.items():
            function = isinstance(child, dict) and child.get("type") in FUNCTIONS
            inner = scope
            if kind == "VariableDeclarator" and key == "init" and function:
                inner = node["id"].get("name", scope)
            if kind in ("CallExpression", "NewExpression") and key == "arguments":
                title = _ts_string(child[0]) if child else None
                for argument in child:
                    named = title is not None and argument.get("type") in FUNCTIONS
                    visit(argument, title if named else scope)
                continue
            visit(child, inner)

    visit(tree, "")
    return found


def ts_flagged(rel: str, source: str, tree: dict) -> list[str]:
    """The sites in ``source``, the file ``rel`` under frontend/, no exemption covers."""
    out, seen = [], set()
    for site in ts_sites(source, tree):
        key = (rel, site.function, site.text)
        if key in TS_EXEMPT and key not in seen:
            seen.add(key)
            continue
        where = site.function or "the module"
        out.append(f"frontend/{rel}:{site.line} in {where}: fixed {site.kind} {site.text}")
    return out


def ts_scanned(root: Path = FRONTEND) -> list[str]:
    """Every source of a kind in SOURCES under ``root`` that git does not ignore, relative
    to it, whatever its name."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
        cwd=root, check=True, capture_output=True,
    ).stdout.decode().split("\0")  # fmt: skip
    return sorted(r for r in listed if Path(r).suffix in SOURCES and (root / r).is_file())


TS_PLANTED = [
    (f'const root = "{TMP}/rcman-e2e";', "path"),
    (f'const root = "{TMP}";', "path"),
    (f"const root = `{TMP}/x-${{name}}`;", "path"),
    (f'const root = join("{TMP}", "x");', "path"),
    ('const root = join(tmpdir(), "x");', "path"),
    ('const root = join(os.tmpdir(), "x");', "path"),
    ("const root = tmpdir();", "path"),
    (f'const root = mkdtempSync(join("{TMP}/sub", "x-"));', "path"),
    ('const root = mkdtempSync(join(tmpdir(), "sub", "x-"));', "path"),
    ('mkdirSync(join(tmpdir(), "x-"));', "path"),
    ("export default { webServer: { port: 8765 } };", "port"),
    ('const server = { port: "8765" };', "port"),
    ('const server = { "port": 8765 };', "port"),
    ("const server = { port: 8765 as number };", "port"),
    ("const server = { port: process.env.PORT ?? 8765 };", "port"),
    ('const server = { port: Number(process.env.PORT) || "8765" };', "port"),
    ('run("uvicorn", ["app", "--port", "8765"]);', "port"),
    ('run("uvicorn", ["app", "--port", String(8765)]);', "port"),
    ('run("uvicorn", ["app", "--port=8765"]);', "port"),
    ('run("server", ["-p", "8765"]);', "port"),
    ('run("docker", ["run", "-p", "127.0.0.1:8765:443", "image"]);', "port"),
    ('run("docker", ["run", "--publish", "8765:80", "image"]);', "port"),
    ('const command = "uvicorn app --host 127.0.0.1 --port 8765";', "port"),
    ('const command = "docker run -p 8765:80 image";', "port"),
    ("const command = `uvicorn ${app} --port 8765`;", "port"),
    ("const command = `uvicorn app --port ${8765}`;", "port"),
    ("const command = `uvicorn app --port=${8765}`;", "port"),
    ("const url = `http://127.0.0.1:${8765}/api`;", "port"),
    ('const url = `http://localhost:${"8765"}`;', "port"),
    ("const rules = `MAP rcman.test:443 127.0.0.1:${process.env.PORT ?? 8765}`;", "port"),
    ('server.listen(8765, "127.0.0.1");', "port"),
    ('process.env.RCMAN_E2E_PORT ??= "8765";', "port"),
    ("process.env.PORT = 8765;", "port"),
    ('const env = { RCMAN_E2E_PORT: "8765" };', "port"),
    ('const url = "http://127.0.0.1:8765";', "port"),
    ('const config = { baseURL: "http://localhost:8765" };', "port"),
    ('const url = "https://rcman.test:44300/";', "port"),
    ('const url = "http://user@[::1]:8765/api";', "port"),
    ("const url = `http://127.0.0.1:8765/${path}`;", "port"),
    ('const rules = "MAP rcman.test:443 127.0.0.1:44300";', "port"),
    ('const url = "http://127.0.0.1:" + 8765;', "port"),
    ('const command = "uvicorn app --port " + "8765";', "port"),
]
TS_PORT_DECLARATIONS = [
    "const PORT = 8765;",
    'export const PORT = "8765";',
    'const PORT = process.env.PORT ?? "8765";',
]
TS_PORT_USES = [
    'run("uvicorn", ["--port", String(PORT)]);',
    "const command = `uvicorn app --port ${PORT}`;",
    "const url = `http://127.0.0.1:${PORT}`;",
    "const server = { port: PORT };",
    "server.listen(PORT);",
    'const url = "http://127.0.0.1:" + PORT;',
    "process.env.RCMAN_E2E_PORT ??= PORT;",
]
TS_CLEAN = [
    "const server = { port: 0 };",
    "const server = { port: process.env.PORT ?? 0 };",
    'server.listen(0, "127.0.0.1");',
    "const command = `uvicorn app --port ${process.env.PORT}`;",
    "const command = `uvicorn app --port ${port}`;",
    f'const text = "{TMP}x is another folder";',
    'run("mkdir", ["-p", "folder"]);',
    'run("tmux", ["display-message", "-p", "#{pid}"]);',
    'run("docker", ["run", "-p", `127.0.0.1:${port}:443`, "image"]);',
    'run("docker", ["run", "-p", "127.0.0.1::443", "image"]);',
    'parse(["--app-port", "8001"]);',
    'const UNREACHABLE = "http://127.0.0.1:1/";',
    "await page.setViewportSize({ width: 1200, height: 800 });",
    'const sizes = [["width", 1200]];',
    "const ratio = `${count}:${1200}`;",
    "const port = process.env.RCMAN_STACK_PORT;\nconst rules = `127.0.0.1:${port}`;",
    "function f(port: number) {\n  server.listen(port);\n}",
    "test('waits', async () => {\n  await vi.advanceTimersByTimeAsync(3500);\n});",
    'const root = mkdtempSync(join(tmpdir(), "x-"));',
    f'const root = mkdtempSync(join("{TMP}", "x-"));',
    'const root = await fs.mkdtemp(path.join(os.tmpdir(), "x-"));',
    "const size = { viewport: 1200 };",
    'process.env.TIMEOUT ??= "3000";',
    "process.env.RCMAN_E2E_PORT ??= String(await freePort());",
    'const where = "stack.spec.ts:1234";',
    'const ratio = "16:9000";',
    'const url = "https://rcman.test/";',
    'const url = "http://127.0.0.1:" + port;',
    'const rules = "MAP rcman.test:443 127.0.0.1:" + port;',
]


def _in_function(snippet: str) -> str:
    return f"function f() {{\n  {snippet}\n}}\n"


def _module_port(declaration: str, use: str) -> str:
    return f"{declaration}\nfunction f() {{\n  {use}\n}}\n"


@pytest.fixture(scope="module")
def ts_trees() -> dict[str, dict]:
    """Every TypeScript source these tests plant, parsed together: one node per module."""
    sources = [s for s, _ in TS_PLANTED] + [_in_function(s) for s, _ in TS_PLANTED]
    for declaration in TS_PORT_DECLARATIONS:
        for chosen in (declaration, declaration.replace("8765", "0")):
            sources += [_module_port(chosen, use) for use in TS_PORT_USES]
    sources += TS_CLEAN
    names = {f"planted{i}.ts": source for i, source in enumerate(sources)}
    return {names[name]: tree for name, tree in parse_ts(names).items()}


def test_no_frontend_source_holds_a_fixed_tmp_path_or_port():
    """The browser tests, their configs and everything else in the frontend's TypeScript."""
    rels = ts_scanned()
    sources = {rel: (FRONTEND / rel).read_text(encoding="utf-8") for rel in rels}
    trees = parse_ts(sources)
    assert set(trees) == set(sources)
    found, total = [], 0
    for rel, source in sources.items():
        total += len(ts_sites(source, trees[rel]))
        found += ts_flagged(rel, source, trees[rel])
    assert total >= len(TS_EXEMPT) > 0  # non-vacuity: the exempt sites are found
    assert found == []


def test_the_scan_reads_every_typescript_and_javascript_file_in_the_frontend():
    """Derived from git: every such file in frontend/ that is not ignored."""
    rels = set(ts_scanned())
    named = {"playwright.config.ts", "playwright.stack.config.ts", "vite.config.ts"}
    named |= {"e2e/people.ts", "e2e/world.ts", "src/api.ts"}
    named |= {p.relative_to(FRONTEND).as_posix() for p in (FRONTEND / "e2e-stack").glob("*.ts")}
    assert any(r.startswith("e2e-stack/") for r in named)
    assert named <= rels
    for ignored in ("node_modules", "dist", "test-results"):
        assert not any(r.startswith(ignored + "/") for r in rels), ignored
    # Positive control: node_modules holds TypeScript that the scan leaves out.
    assert next((FRONTEND / "node_modules" / "vite").rglob("*.d.ts"), None) is not None


def test_the_scan_takes_every_name_and_kind_of_source_git_lists(tmp_path):
    """Planted twin: names with spaces and beyond ASCII, each kind of source, and what
    git ignores, in a repository of its own."""
    sources = {f"src/kind{suffix}" for suffix in SOURCES} | {"a b.ts", "über/ünï.tsx"}
    for rel in [*sources, "ignored/x.ts", "notes.txt"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("const a = 1;\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "a b.ts", "src"], check=True)
    assert set(ts_scanned(tmp_path)) == sources


def test_a_typescript_file_that_does_not_parse_fails_the_guard():
    with pytest.raises(RuntimeError, match="broken.ts"):
        parse_ts({"fine.ts": "const a = 1;", "broken.ts": "const = ;"})


def test_the_guard_fails_without_the_frontends_packages(tmp_path):
    """The parser is Vite's, from the frontend's packages; without them nothing is read."""
    assert parse_ts({"fine.ts": "const a = 1;"})["fine.ts"]["body"]  # positive control
    with pytest.raises(RuntimeError, match="vite"):
        parse_ts({"fine.ts": "const a = 1;"}, cwd=tmp_path)


def test_the_typescript_exemptions_still_have_their_sites():
    for rel, scope, text in TS_EXEMPT:
        source = (FRONTEND / rel).read_text(encoding="utf-8")
        sites = ts_sites(source, parse_ts({rel: source})[rel])
        assert [s for s in sites if (s.function, s.text) == (scope, text)], (rel, scope)


def test_a_new_fixed_site_beside_a_typescript_exemption_is_flagged():
    """Planted twin: an exemption covers its own site, not the scope it is in."""
    rel, scope, text = next(iter(TS_EXEMPT))
    source = (FRONTEND / rel).read_text(encoding="utf-8")
    assert scope == ""  # the exempt site is at module level, where the plants go
    plants = {"exempt": source}
    for extra in ("server.listen(8765);\n", f"const again = {text};\n"):
        plants[extra] = source + extra
        assert plants[extra] != source  # the plant took effect
    trees = parse_ts({f"p{i}.ts": s for i, s in enumerate(plants.values())})
    flags = [ts_flagged(rel, s, trees[f"p{i}.ts"]) for i, s in enumerate(plants.values())]
    assert flags[0] == []  # positive control: the exempt site passes
    assert [len(f) for f in flags[1:]] == [1, 1], flags


def test_the_world_named_without_a_unique_name_is_flagged():
    """Planted twin: the exemption covers the world's unique name, not any name in /tmp."""
    rel, _, text = next(iter(TS_EXEMPT))
    source = (FRONTEND / rel).read_text(encoding="utf-8")
    fixed = source.replace(text, f'join("{TMP}", "rcman-e2e")')
    assert fixed != source  # the plant took effect
    assert len(ts_flagged(rel, fixed, parse_ts({rel: fixed})[rel])) == 1


@pytest.mark.parametrize(("planted", "kind"), TS_PLANTED)
def test_the_guard_finds_a_planted_fixed_resource_in_typescript(ts_trees, planted, kind):
    """Planted twins: each way of naming a fixed path or port is found, in a function too."""
    assert [s.kind for s in ts_sites(planted, ts_trees[planted])] == [kind]
    inside = _in_function(planted)
    assert [s[:3] for s in ts_sites(inside, ts_trees[inside])] == [(2, "f", kind)]


@pytest.mark.parametrize("declaration", TS_PORT_DECLARATIONS)
@pytest.mark.parametrize("use", TS_PORT_USES)
def test_the_guard_finds_a_fixed_port_held_in_a_typescript_module_constant(
    ts_trees, declaration, use
):
    """Planted twin: a port named once at module level and used in a function."""
    source = _module_port(declaration, use)
    assert [s[:3] for s in ts_sites(source, ts_trees[source])] == [(3, "f", "port")]
    chosen = _module_port(declaration.replace("8765", "0"), use)
    assert chosen != source and ts_sites(chosen, ts_trees[chosen]) == []  # control: port 0


@pytest.mark.parametrize("source", TS_CLEAN)
def test_the_guard_leaves_unique_names_and_chosen_ports_in_typescript_alone(ts_trees, source):
    assert ts_sites(source, ts_trees[source]) == []


def test_a_typescript_site_is_named_by_the_function_or_test_it_is_in():
    snippet = f'join("{TMP}", "x")'
    source = (
        f"const top = {snippet};\n"
        f"function named() {{\n  {snippet};\n}}\n"
        f"const arrow = () => {snippet};\n"
        f'test("a test title", async () => {{\n  {snippet};\n}});\n'
    )
    sites = ts_sites(source, parse_ts({"scopes.ts": source})["scopes.ts"])
    assert [(s.line, s.function) for s in sites] == [
        (1, ""), (3, "named"), (5, "arrow"), (7, "a test title"),
    ]  # fmt: skip
