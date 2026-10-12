"""No test holds a fixed path under /tmp or a fixed port, which tests running at the same
time, in pytest-xdist's workers or in two runs, would share.

A test's files go under tmp_path; a folder that must be under /tmp, for a short socket
path, is made with a unique name (tempfile.mkdtemp(dir="/tmp")); a port is one the system
picks (port 0) or one held for the test. The guard reads the tests' own source and the
frontend's TypeScript and JavaScript, where the browser tests and their configs are.
tests/container/ is outside it: that suite runs as root in a container of its own.

It catches the same forms in both languages: FORMS, in this module's last section, spells
each in both, and project-knowledge/tests.md describes them.
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
ROOT = TESTS.parent
DEVTOOLS = ROOT / "devtools"
OUTSIDE = "container"  # runs in a throwaway container of its own, never beside another run
TMP = "/" + "tmp"  # built, so that this module does not name it itself
# Ports below this need root to bind, so no other test can take one; a test may name one
# that nothing answers on.
UNPRIVILEGED = 1024
# Flags that take a port, and those that take one alone or as the host port of docker's
# [address:]host:container mapping.
PORT_FLAGS = {"--port"}
PUBLISH_FLAGS = {"-p", "--publish"}
FLAGS = "|".join(re.escape(f) for f in sorted(PORT_FLAGS | PUBLISH_FLAGS, key=len, reverse=True))
# A flag and what follows it up to a space or the shell's punctuation, in a command written
# as one string; and a flag just before a port written as an expression.
FLAG_IN_TEXT = re.compile(rf"(?:^|\s)({FLAGS})(?: |=)([^\s;&|<>()'\"]+)")
FLAG_BEFORE = re.compile(rf"(?:^|\s)(?:{FLAGS})(?: |=)$")
# An address's host and colon: in a URL, after its scheme and any user, or else alone.
ADDRESS = r"(?P<scheme>://(?:[^/@\s]*@)?)?(?P<host>\[[0-9A-Fa-f:.]*\]|[^\s/:@\[\],=]+):"
# Each one, overlapping, so that a host is not lost inside the match of a word before it.
ADDRESS_IN_TEXT = re.compile(rf"(?={ADDRESS}(?P<port>[0-9]+)(?![0-9]))")
ADDRESS_BEFORE = re.compile(ADDRESS + "$")
# What may stand just before a host written without a scheme.
BEFORE_HOST = " \t\n,=@:(\"'"
# A name that holds a port: port itself, or one ending _PORT, as an environment's do.
PORT_NAME = re.compile(r"(?:^|_)port$", re.IGNORECASE)
# Calls that read a value and give their fallback where it is missing, as os.environ.get()
# does, in either language: the fallback is the value, and the call is not a site of its own.
READS = {"get", "getenv"}
# What a formatted string shows, read as text: a fixed port given in it as a port, and
# anything else given in it as a space, which no host, flag or port takes in.
SHOWN_PORT = str(UNPRIVILEGED)
SHOWN_OTHER = " "


class Site(NamedTuple):
    line: int
    function: str
    kind: str
    text: str


# What both languages share: what a path in /tmp, a fixed port and an address are.


def _is_tmp(text: str) -> bool:
    """The folder itself or a path in it."""
    return text == TMP or text.startswith(TMP + "/")


def _port_number(value) -> bool:
    """An unprivileged port, as an int or a string of digits."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return type(value) is int and UNPRIVILEGED <= value <= 65535


def _port_named(name) -> bool:
    return isinstance(name, str) and bool(PORT_NAME.search(name))


def _published(text: str) -> bool:
    """What follows -p or --publish, written whole: a fixed port alone, or as the host
    port of a [address:]host:container mapping."""
    return _port_number(text.split(":")[-2] if ":" in text else text)


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


def _argument_host(value) -> bool:
    """A host given as a call's argument beside a port: an IP address or localhost, which
    no file's name or other argument is."""
    return value != "" and _host(value, named=False)


def _addressed(match) -> bool:
    """A host as an address names one: in a URL any host at all, and alone only one that
    cannot be a file's name, such as an IP address."""
    host = match["host"].removeprefix("[").removesuffix("]")
    if match["scheme"]:
        return bool(host)
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


def _side_by_side(args, host, port) -> bool:
    """A call's arguments holding a host and a fixed port next to each other, either first."""
    return any(
        _argument_host(host(a)) and port(b) or _argument_host(host(b)) and port(a)
        for a, b in zip(args, args[1:], strict=False)
    )


def _flag_given(elements, string, shown, port) -> bool:
    """A list of a command's arguments holding a fixed port after a flag that takes one;
    ``shown`` reads a string or a formatted one as text."""
    for flag, value in zip(elements, elements[1:], strict=False):
        flag = string(flag)
        if flag in PORT_FLAGS and port(value):
            return True
        text = shown(value)
        if text is not None and _port_in_text(text):
            continue  # a site itself, as an address
        if flag in PUBLISH_FLAGS and (_published(text) if text is not None else port(value)):
            return True
    return False


# Python: the tests' own source.

# tempfile's calls that make a uniquely named file or folder in the dir= they are given.
UNIQUE_IN = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
# Sites flagged on purpose: (file relative to the repository, the function it is in or ""
# at module level, the site's source text) -> reason. Each covers exactly one site: another
# site of the same text in the same function, or at module level, is flagged.
EXEMPT = {
    (
        "tests/test_record_claude.py",
        "",
        f'"{TMP}/"',
    ): "a leak mark the kept screens are searched for; nothing is made there",
    (
        "tests/test_rc_helper.py",
        "test_mkdir_refuses_a_parent_outside_the_home_folder",
        f'"{TMP}"',
    ): "gives rc-helper the absolute path to show it is refused; nothing is made there",
    ("devtools/held_folders.py", "", f'"{TMP}"'): (
        "the folder held folders are made in, named as the bound no removal may leave;"
        " nothing is made at the path itself"
    ),
}


def _name(call) -> str | None:
    return getattr(call.func, "attr", None) or getattr(call.func, "id", None)


def _string(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _gettempdir(node) -> bool:
    return isinstance(node, ast.Call) and _name(node) == "gettempdir"


def _port(node, names=frozenset()) -> bool:
    """A fixed unprivileged port: an int or a string of digits, a module-level name given
    one, str(), int() or cast() of either, an f-string showing one alone, or either as the
    fallback of ``or`` or of a call in READS."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call):
        name, args = _name(node), node.args
        if name in ("str", "int") and len(args) == 1:
            return _port(args[0], names)
        if name == "cast" and len(args) == 2:
            return _port(args[1], names)
        if name in READS:
            fallback = [k.value for k in node.keywords if k.arg == "default"] or args[1:2]
            return bool(fallback) and _port(fallback[0], names)
    if isinstance(node, ast.JoinedStr):
        return _port_number(_shown(node, names))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _port(node.values[-1], names)
    return isinstance(node, ast.Constant) and _port_number(node.value)


def _shown(node, names=frozenset()) -> str | None:
    """A string's text, or an f-string's, showing a fixed port given in it as SHOWN_PORT
    and anything else given in it as SHOWN_OTHER."""
    if not isinstance(node, ast.JoinedStr):
        return _string(node)
    return "".join(
        _string(part)
        if _string(part) is not None
        else SHOWN_PORT
        if isinstance(part, ast.FormattedValue) and _port(part.value, names)
        else SHOWN_OTHER
        for part in node.values
    )


def _ending(node) -> str | None:
    """The text a string, an f-string or a + of them ends with."""
    if isinstance(node, ast.JoinedStr):
        return (_string(node.values[-1]) or "") if node.values else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ending(node.right)
    return _string(node)


def _address(node, names) -> bool:
    """A (host, port) or (host, port, flowinfo, scope) tuple or list with a fixed port,
    as bind, connect and servers take one, wherever it is written."""
    return (
        isinstance(node, (ast.Tuple, ast.List))
        and len(node.elts) in (2, 4)
        and _host(_string(node.elts[0]))
        and _port(node.elts[1], names)
    )


def _target_name(target, at_module_level: bool) -> str | None:
    """What an assignment names: an attribute, a key, or a name of a function's own or a
    class's; a module-level name is followed to where it is used instead."""
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        return _string(target.slice)
    if isinstance(target, ast.Name) and not at_module_level:
        return target.id
    return None


def _parameters(function) -> list[ast.arg]:
    a = function.args
    return [*a.posonlyargs, *a.args, *a.kwonlyargs, *filter(None, (a.vararg, a.kwarg))]


def _port_defaults(tree) -> set[int]:
    """The defaults given to parameters named for a port."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            positional = [*a.posonlyargs, *a.args]
            given = zip(positional[len(positional) - len(a.defaults) :], a.defaults, strict=True)
            given = [*given, *zip(a.kwonlyargs, a.kw_defaults, strict=True)]
            found |= {id(value) for arg, value in given if value and _port_named(arg.arg)}
    return found


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


def _left_alone(tree) -> set[int]:
    """Strings that are no site of their own: one standing alone as a statement, as a
    docstring does, and the text of an f-string, which is read with the f-string."""
    left = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and _string(node.value) is not None:
            left.add(id(node.value))
        if isinstance(node, ast.JoinedStr):
            left |= {id(part) for part in node.values if _string(part) is not None}
    return left


def fixed_sites(source: str) -> list[Site]:
    """Where the source names a fixed path under /tmp or a fixed port, with the function
    each is in, what it is and its text. FORMS spells the forms it catches."""
    tree = ast.parse(source)
    skipped = _unique_dirs(tree) | _left_alone(tree)
    defaults = _port_defaults(tree)
    module_level = {id(node) for node in tree.body}
    found = []

    def what(node, names) -> str | None:
        def port(value) -> bool:
            return _port(value, names)

        if id(node) in defaults and port(node):
            return "port"
        text = _string(node)
        if text is not None:
            return "path" if _is_tmp(text) else "port" if _port_in_text(text) else None
        if _gettempdir(node):
            return "path"
        if isinstance(node, ast.JoinedStr):
            first = _string(node.values[0]) if node.values else None
            if first is not None and _is_tmp(first):
                return "path"
            if _port_in_text(_shown(node, names)):
                return "port"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            before = _ending(node.left)
            if before is not None and _port_follows(before) and port(node.right):
                return "port"
        if _address(node, names):
            return "port"
        if isinstance(node, (ast.List, ast.Tuple)):
            if _flag_given(node.elts, _string, lambda v: _shown(v, names), port):
                return "port"
        if isinstance(node, ast.Call):
            args = node.args
            if any(_port_named(k.arg) and port(k.value) for k in node.keywords):
                return "port"
            if isinstance(node.func, ast.Attribute) and node.func.attr == "listen":
                if args and port(args[0]):
                    return "port"
            if _name(node) not in READS and len(args) >= 2:
                if _port_named(_string(args[0])) and port(args[1]):
                    return "port"
            if _side_by_side(args, _string, port):
                return "port"
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            at_module_level = id(node) in module_level
            if any(_port_named(_target_name(t, at_module_level)) for t in targets):
                return "port" if port(node.value) else None
        if isinstance(node, ast.Dict):
            if any(
                _port_named(_string(k)) and port(v)
                for k, v in zip(node.keys, node.values, strict=True)
            ):
                return "port"
        return None

    def visit(node, function, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names = names - {arg.arg for arg in _parameters(node)}  # a parameter shadows one
        kind = None if id(node) in skipped else what(node, names)
        if kind:
            text = ast.get_source_segment(source, node) or ""
            found.append(Site(node.lineno, function, kind, text))
        for child in ast.iter_child_nodes(node):
            visit(child, function, names)

    visit(tree, "", _port_names(tree))
    return found


def flagged(rel: str, source: str) -> list[str]:
    """The sites in ``source``, the file ``rel`` in the repository, that no exemption
    covers."""
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
    """The tests but the container suite, and every devtools module, which the tests and
    the recorder run."""
    tests = [p for p in sorted(TESTS.rglob("*.py")) if p.relative_to(TESTS).parts[0] != OUTSIDE]
    return tests + sorted(DEVTOOLS.glob("*.py"))


def test_no_test_holds_a_fixed_tmp_path_or_port():
    found, total = [], 0
    for path in scanned():
        rel, source = path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")
        total += len(fixed_sites(source))
        found += flagged(rel, source)
    assert total >= len(EXEMPT) > 0  # non-vacuity: the exempt sites are found
    assert found == []


def test_the_scan_reads_every_devtools_module():
    """devtools holds what the tests run and what the recorder makes in /tmp."""
    rels = {p.relative_to(ROOT).as_posix() for p in scanned()}
    assert {"devtools/held_folders.py", "devtools/record_claude.py"} <= rels
    assert {f"devtools/{p.name}" for p in DEVTOOLS.glob("*.py")} <= rels


def test_the_scan_reads_the_tests_but_not_the_container_suite():
    rels = {p.relative_to(ROOT).as_posix() for p in scanned()}
    assert "tests/conftest.py" in rels and f"tests/{Path(__file__).name}" in rels
    assert (TESTS / OUTSIDE).is_dir()  # the exemption's subject still exists
    assert any((TESTS / OUTSIDE).rglob("*.py"))
    assert not any(r.startswith(f"tests/{OUTSIDE}/") for r in rels)


def test_the_exemptions_still_have_their_sites():
    for rel, function, text in EXEMPT:
        sites = fixed_sites((ROOT / rel).read_text(encoding="utf-8"))
        assert [s for s in sites if (s.function, s.text) == (function, text)], (rel, function)


def test_a_new_fixed_site_in_an_exempt_function_is_flagged():
    """Planted twin: an exemption covers its own site, not the function it is in."""
    in_functions = [key for key in EXEMPT if key[1]]
    assert in_functions  # non-vacuity: an exemption inside a function to plant beside
    rel, function, text = in_functions[0]
    source = (ROOT / rel).read_text(encoding="utf-8")
    assert flagged(rel, source) == []  # positive control: the exempt site passes
    header = f"def {function}("
    assert source.count(header) == 1
    body = source.index(":\n", source.index(header)) + 2
    for extra in ('    s.bind(("127.0.0.1", 8765))\n', f"    x = {text}\n"):
        planted = source[:body] + extra + source[body:]
        assert planted != source  # the plant took effect
        assert len(flagged(rel, planted)) == 1, extra


def test_a_new_fixed_site_beside_a_module_level_exemption_is_flagged():
    """Planted twin: a module-level exemption covers its own site, not the module."""
    at_module = [key for key in EXEMPT if not key[1]]
    assert at_module  # non-vacuity: an exemption at module level to plant beside
    for rel, _, text in at_module:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert flagged(rel, source) == []  # positive control: the exempt site passes
        for extra in ('s.bind(("127.0.0.1", 8765))\n', f"x = {text}\n"):
            planted = f"{source}\n{extra}"
            assert planted != source  # the plant took effect
            assert len(flagged(rel, planted)) == 1, (rel, extra)


# The frontend's TypeScript and JavaScript: the browser tests, their configs and the rest.

FRONTEND = TESTS.parent / "frontend"
# Sites flagged on purpose: (file relative to frontend/, the function or test it is in, the
# site's source text) -> reason. Each covers exactly one site, as EXEMPT's do.
TS_EXEMPT = {
    ("e2e/world.ts", "", f'join("{TMP}", `rcman-e2e-${{randomUUID()}}`)'): (
        "a world no other run names, which the backend makes only where nothing is"
        " (project-knowledge/frontend.md)"
    ),
    # Its port is filled in, as planted text's is below, so that this module does not hold it.
    ("vite.config.ts", "", '"http://127.0.0.1:<port>"'.replace("<port>", "8000")): (
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
FUNCTIONS = {"ArrowFunctionExpression", "FunctionExpression"}
WRAPPERS = {"TSAsExpression", "TSSatisfiesExpression", "TSNonNullExpression", "TSTypeAssertion"}


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


def _segment(encoded: bytes, start: int, end: int) -> str:
    """The text between two offsets in UTF-16 code units of a source encoded as UTF-16."""
    return encoded[2 * start : 2 * end].decode("utf-16-le")


def _ts_name(node) -> str | None:
    """A call's function name, as tmpdir() or os.tmpdir() name it; an assignment's or a
    member's name, as process.env.PORT names it; a computed member's only where it is a
    string, as process.env["PORT"] names it."""
    target = node.get("callee") or node.get("left") or node
    if target.get("type") == "MemberExpression":
        if target.get("computed"):
            return _ts_string(target["property"])
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
    one, String() or Number() of either, a template showing one alone, either after ?? or
    || or as the fallback of a call in READS, or either cast."""
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
    if kind == "CallExpression" and _ts_name(node) in READS:
        return len(node["arguments"]) >= 2 and _ts_port(node["arguments"][1], names)
    if kind == "TemplateLiteral":
        return _port_number(_ts_shown(node, names))
    if kind == "LogicalExpression" and node["operator"] in ("??", "||"):
        return _ts_port(node["right"], names)
    return False


def _ts_shown(node, names=frozenset()) -> str | None:
    """A string's text, or a template's, showing a fixed port given in it as SHOWN_PORT
    and anything else given in it as SHOWN_OTHER."""
    if not node or node["type"] != "TemplateLiteral":
        return _ts_string(node)
    shown = [node["quasis"][0]["value"]["cooked"] or ""]
    for value, after in zip(node["expressions"], node["quasis"][1:], strict=True):
        shown += [SHOWN_PORT if _ts_port(value, names) else SHOWN_OTHER]
        shown += [after["value"]["cooked"] or ""]
    return "".join(shown)


def _ts_parameters(function) -> set[str]:
    """The names a function's parameters give, with or without a default."""
    given = (
        p.get("left", p) if p["type"] == "AssignmentPattern" else p for p in function["params"]
    )
    return {p["name"] for p in given if p["type"] == "Identifier"}


def _ts_ending(node) -> str | None:
    """The text a string, a template or a + of them ends with."""
    if node["type"] == "TemplateLiteral":
        return node["quasis"][-1]["value"]["cooked"]
    if node["type"] == "BinaryExpression" and node["operator"] == "+":
        return _ts_ending(node["right"])
    return _ts_string(node)


def _ts_module_declarations(tree) -> list[dict]:
    """What gives module-level names a value: a declaration, exported or not, and a plain
    assignment to a name standing as a statement of its own."""
    found = []
    for node in tree["body"]:
        if node["type"] == "ExportNamedDeclaration":
            node = node.get("declaration") or {}
        if node.get("type") == "VariableDeclaration":
            found += node["declarations"]
        given = node.get("expression") if node.get("type") == "ExpressionStatement" else None
        if given and given["type"] == "AssignmentExpression" and given["operator"] == "=":
            found += [given] if given["left"]["type"] == "Identifier" else []
    return found


def _ts_port_names(tree) -> frozenset[str]:
    """The module-level names given a fixed port."""
    names = set()
    for given in _ts_module_declarations(tree):
        name, value = (
            (given["id"], given["init"]) if "id" in given else (given["left"], given["right"])
        )
        if name["type"] == "Identifier" and _ts_port(value):
            names.add(name["name"])
    return frozenset(names)


def _ts_what(node, names, module_level) -> str | None:
    kind = node["type"]
    text = _ts_string(node)

    def port(value) -> bool:
        return _ts_port(value, names)

    if text is not None:
        return "path" if _is_tmp(text) else "port" if _port_in_text(text) else None
    if kind == "TemplateLiteral":
        if _is_tmp(node["quasis"][0]["value"]["cooked"] or ""):
            return "path"
        if _port_in_text(_ts_shown(node, names)):
            return "port"
    if kind == "BinaryExpression" and node["operator"] == "+":
        before = _ts_ending(node["left"])
        if before is not None and _port_follows(before) and port(node["right"]):
            return "port"
    if kind == "ArrayExpression":
        elements = node["elements"]
        if len(elements) in (2, 4) and _host(_ts_string(elements[0])) and port(elements[1]):
            return "port"
        if _flag_given(elements, _ts_string, lambda v: _ts_shown(v, names), port):
            return "port"
    if kind == "CallExpression":
        args = node["arguments"]
        if _ts_name(node) == "tmpdir" or args and _ts_tmp(args[0]):
            return "path"
        if node["callee"]["type"] == "MemberExpression" and _ts_name(node) == "listen":
            if args and port(args[0]):
                return "port"
    if kind in ("CallExpression", "NewExpression") and _ts_name(node) not in READS:
        args = node["arguments"]
        if len(args) >= 2 and _port_named(_ts_string(args[0])) and port(args[1]):
            return "port"
        if _side_by_side(args, _ts_string, port):
            return "port"
    if kind in ("AssignmentExpression", "AssignmentPattern") and id(node) not in module_level:
        if _port_named(_ts_name(node)):
            return "port" if port(node["right"]) else None
    if kind == "VariableDeclarator" and id(node) not in module_level:
        if _port_named(_ts_name(node["id"])):
            return "port" if port(node["init"]) else None
    if kind in ("Property", "PropertyDefinition"):
        key = node["key"]
        if _port_named(_ts_string(key) if node.get("computed") else _ts_name(key)):
            return "port" if port(node["value"]) else None
    return None


def ts_sites(source: str, tree: dict) -> list[Site]:
    """Where a source names a fixed path under /tmp or a fixed port, with the function or
    test each is in: a function's or a variable's name, or the title a call such as
    test() gives the function it is passed. A path given to a call is the call's site,
    so join() names it with the rest of the path. FORMS spells the forms it catches."""
    encoded = source.encode("utf-16-le")
    module_level = {id(d) for d in _ts_module_declarations(tree)}
    # Paths given to a call, whose site is the call; strings standing alone as a statement,
    # as a directive does; and the folders that _ts_unique_dirs() allows.
    skipped = _ts_unique_dirs(tree)
    found = []

    def visit(node, scope, names):
        if isinstance(node, list):
            for child in node:
                visit(child, scope, names)
            return
        if not isinstance(node, dict) or "type" not in node:
            return
        kind = node["type"]
        if kind == "FunctionDeclaration" and node.get("id"):
            scope = node["id"]["name"]
        if kind == "FunctionDeclaration" or kind in FUNCTIONS:
            names = names - _ts_parameters(node)  # a parameter shadows a module-level name
        if kind == "CallExpression" and node["arguments"] and _ts_tmp(node["arguments"][0]):
            skipped.add(id(node["arguments"][0]))
        if kind == "ExpressionStatement" and _ts_string(node["expression"]) is not None:
            skipped.add(id(node["expression"]))
        what = None if id(node) in skipped else _ts_what(node, names, module_level)
        if what:
            line = _segment(encoded, 0, node["start"]).count("\n") + 1
            found.append(Site(line, scope, what, _segment(encoded, node["start"], node["end"])))
        for key, child in node.items():
            function = isinstance(child, dict) and child.get("type") in FUNCTIONS
            inner = scope
            if kind == "VariableDeclarator" and key == "init" and function:
                inner = node["id"].get("name", scope)
            if kind in ("CallExpression", "NewExpression") and key == "arguments":
                title = _ts_string(child[0]) if child else None
                for argument in child:
                    named = title is not None and argument.get("type") in FUNCTIONS
                    visit(argument, title if named else scope, names)
                continue
            visit(child, inner, names)

    visit(tree, "", _ts_port_names(tree))
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


# The forms the guard catches, each spelled in both languages: the one list of them. Planted
# text writes its path and port as <tmp> and <port>, which _spelled() fills in, so that this
# module, which the guard reads, does not hold them itself.

PLANTED_PORT = "8765"


class Form(NamedTuple):
    kind: str
    python: tuple[str, ...]
    typescript: tuple[str, ...]


class Twins(NamedTuple):
    python: tuple[str, ...]
    typescript: tuple[str, ...]


FORMS = {
    "/tmp or a path in it, as a string": Form(
        "path",
        (
            'root = "<tmp>"',
            'root = Path("<tmp>/rcman-e2e")',
            'root = Path("<tmp>") / f"x-{name}"',
            'sock = os.path.join("<tmp>", "a.sock")',
            'd = tmp_path_factory.mktemp("x", dir="<tmp>")',
            'd = tempfile.mkdtemp(dir="<tmp>/sub")',
        ),
        (
            'const root = "<tmp>";',
            'const root = "<tmp>/rcman-e2e";',
            'const root = join("<tmp>", "x");',
            'const root = mkdtempSync(join("<tmp>/sub", "x-"));',
        ),
    ),
    "a formatted string beginning with one": Form(
        "path",
        ('root = f"<tmp>/x-{name}"',),
        ("const root = `<tmp>/x-${name}`;",),
    ),
    "the system's temporary folder": Form(
        "path",
        (
            "root = tempfile.gettempdir()",
            'root = Path(tempfile.gettempdir()) / "fixed"',
            'sock = os.path.join(tempfile.gettempdir(), "a.sock")',
            'os.mkdir(os.path.join(tempfile.gettempdir(), "x-"))',
            'd = tempfile.mkdtemp(dir=os.path.join(tempfile.gettempdir(), "sub"))',
        ),
        (
            "const root = tmpdir();",
            'const root = join(tmpdir(), "x");',
            'const root = join(os.tmpdir(), "x");',
            'mkdirSync(join(tmpdir(), "x-"));',
            'const root = mkdtempSync(join(tmpdir(), "sub", "x-"));',
        ),
    ),
    "an address: a host and a port in a tuple, a list or an array": Form(
        "port",
        (
            's.bind(("127.0.0.1", <port>))',
            's.bind(["127.0.0.1", <port>])',
            'server = HTTPServer(("", <port>), Handler)',
            'socket.create_connection(("::1", "<port>"))',
            'address = ("::1", <port>, 0, 0)',
            'address = ("rcman.test", <port>)',
            'address = ("localhost", "<port>")',
        ),
        (
            'const address = ["127.0.0.1", <port>];',
            'const address = ["", <port>];',
            'const address = ["::1", "<port>"];',
            'const address = ["::1", <port>, 0, 0];',
            'const address = ["rcman.test", <port>];',
            'const address = ["localhost", "<port>"];',
        ),
    ),
    "a host and a port side by side as a call's arguments": Form(
        "port",
        (
            'connection = asyncio.open_connection("127.0.0.1", <port>)',
            'server = asyncio.start_server(handle, "127.0.0.1", <port>)',
            'connection = http.client.HTTPConnection("localhost", "<port>")',
        ),
        (
            'const socket = net.connect(<port>, "127.0.0.1");',
            'const socket = net.createConnection(<port>, "localhost");',
            'const site = new Site("::1", "<port>");',
        ),
    ),
    "a name, key or keyword argument for a port: port, or one ending _PORT": Form(
        "port",
        (
            "github.start(root, token, port=<port>)",
            "start(app_port=<port>)",
            'config = {"port": <port>}',
            'env = {"RCMAN_E2E_PORT": "<port>"}',
            'os.environ["RCMAN_E2E_PORT"] = "<port>"',
            "server.port = <port>",
            "server.port: int = <port>",
            "class Server:\n    port = <port>",
            'monkeypatch.setenv("RCMAN_E2E_PORT", "<port>")',
            'os.environ.setdefault("RCMAN_E2E_PORT", "<port>")',
        ),
        (
            "export default { webServer: { port: <port> } };",
            'const server = { port: "<port>" };',
            'const server = { "port": <port> };',
            'const env = { RCMAN_E2E_PORT: "<port>" };',
            'process.env["RCMAN_E2E_PORT"] = "<port>";',
            "process.env.PORT = <port>;",
            'process.env.RCMAN_E2E_PORT ??= "<port>";',
            "class Server {\n  port = <port>;\n}",
            'vi.stubEnv("RCMAN_E2E_PORT", "<port>");',
        ),
    ),
    "a parameter named for a port, given one as its default": Form(
        "port",
        (
            "def serve(port=<port>):\n    pass",
            "def serve(*, app_port: int = <port>):\n    pass",
            "serve = lambda port=<port>: port",
        ),
        (
            "function serve(port = <port>) {}",
            "const serve = (appPort, app_port = <port>) => app_port;",
            "const { port = <port> } = options;",
        ),
    ),
    "the first argument of listen": Form(
        "port",
        ("app.listen(<port>)", 'app.listen(<port>, address="127.0.0.1")'),
        ("server.listen(<port>);", 'server.listen(<port>, "127.0.0.1");'),
    ),
    "after --port": Form(
        "port",
        (
            'run(["uvicorn", "app", "--port", "<port>"])',
            'run(["uvicorn", "app", "--port=<port>"])',
            'command = "uvicorn app --host 127.0.0.1 --port <port>"',
            'command = f"uvicorn {app} --port <port>"',
            'command = f"uvicorn app --port {<port>}"',
            'command = f"uvicorn app --port={<port>}"',
            'command = "uvicorn app --port " + "<port>"',
            'command = "uvicorn app --port <port>; echo ok"',
            "command = \"sh -c 'uvicorn app --port <port>'\"",
        ),
        (
            'run("uvicorn", ["app", "--port", "<port>"]);',
            'run("uvicorn", ["app", "--port=<port>"]);',
            'const command = "uvicorn app --host 127.0.0.1 --port <port>";',
            "const command = `uvicorn ${app} --port <port>`;",
            "const command = `uvicorn app --port ${<port>}`;",
            "const command = `uvicorn app --port=${<port>}`;",
            'const command = "uvicorn app --port " + "<port>";',
            'const command = "uvicorn app --port <port>; echo ok";',
            "const command = \"sh -c 'uvicorn app --port <port>'\";",
        ),
    ),
    "after -p or --publish, alone or as the host port of docker's mapping": Form(
        "port",
        (
            'run(["server", "-p", "<port>"])',
            'run(["docker", "run", "-p", "<port>:80", "image"])',
            'run(["docker", "run", "-p", "127.0.0.1:<port>:443", "image"])',
            'run(["docker", "run", "--publish", "<port>:80", "image"])',
            'run(["docker", "run", "--publish=127.0.0.1:<port>:80", "image"])',
            'command = "docker run -p <port>:80 image"',
            'command = f"docker run -p {<port>}:80 image"',
            'run(["docker", "run", "-p", f"{<port>}:80", "image"])',
            'run(["docker", "run", "-p", f"127.0.0.1:{<port>}:80", "image"])',
        ),
        (
            'run("server", ["-p", "<port>"]);',
            'run("docker", ["run", "-p", "<port>:80", "image"]);',
            'run("docker", ["run", "-p", "127.0.0.1:<port>:443", "image"]);',
            'run("docker", ["run", "--publish", "<port>:80", "image"]);',
            'run("docker", ["run", "--publish=127.0.0.1:<port>:80", "image"]);',
            'const command = "docker run -p <port>:80 image";',
            "const command = `docker run -p ${<port>}:80 image`;",
            'run("docker", ["run", "-p", `${<port>}:80`, "image"]);',
            'run("docker", ["run", "-p", `127.0.0.1:${<port>}:80`, "image"]);',
        ),
    ),
    "after a host and its colon: in a URL, or after an IP address or localhost": Form(
        "port",
        (
            'url = "http://127.0.0.1:<port>/api"',
            'httpx.get("http://localhost:<port>")',
            'url = "https://rcman.test:<port>/"',
            'url = "http://web:<port>/"',
            'url = "http://RCMAN.test:<port>/"',
            'url = "http://user@[::1]:<port>/api"',
            'rules = "MAP rcman.test:443 127.0.0.1:<port>"',
            'url = f"http://127.0.0.1:<port>/{path}"',
            'url = f"http://127.0.0.1:{<port>}/api"',
            "url = f\"http://localhost:{'<port>'}\"",
            "rules = f\"MAP rcman.test:443 127.0.0.1:{os.environ.get('PORT') or <port>}\"",
            'url = "http://127.0.0.1:" + "<port>"',
            'url = "http://127.0.0.1:" + str(<port>)',
            'command = "socat TCP:127.0.0.1:<port> -"',
            'body = \'{"addr": "127.0.0.1:<port>"}\'',
            "command = \"serve --bind '127.0.0.1:<port>'\"",
        ),
        (
            'const url = "http://127.0.0.1:<port>/api";',
            'const config = { baseURL: "http://localhost:<port>" };',
            'const url = "https://rcman.test:<port>/";',
            'const url = "http://web:<port>/";',
            'const url = "http://RCMAN.test:<port>/";',
            'const url = "http://user@[::1]:<port>/api";',
            'const rules = "MAP rcman.test:443 127.0.0.1:<port>";',
            "const url = `http://127.0.0.1:<port>/${path}`;",
            "const url = `http://127.0.0.1:${<port>}/api`;",
            'const url = `http://localhost:${"<port>"}`;',
            "const rules = `MAP rcman.test:443 127.0.0.1:${process.env.PORT || <port>}`;",
            'const url = "http://127.0.0.1:" + "<port>";',
            'const url = "http://127.0.0.1:" + String(<port>);',
            'const command = "socat TCP:127.0.0.1:<port> -";',
            'const body = \'{"addr": "127.0.0.1:<port>"}\';',
            "const command = \"serve --bind '127.0.0.1:<port>'\";",
        ),
    ),
    "a port converted or cast": Form(
        "port",
        (
            'start(port=int("<port>"))',
            'run(["uvicorn", "app", "--port", str(<port>)])',
            "start(port=cast(int, <port>))",
        ),
        (
            'const server = { port: Number("<port>") };',
            'run("uvicorn", ["app", "--port", String(<port>)]);',
            "const server = { port: <port> as number };",
            "const server = { port: <number><port> };",
        ),
    ),
    "a port as the fallback of a value that may be missing": Form(
        "port",
        (
            'start(port=os.environ.get("PORT", <port>))',
            'start(port=os.getenv("PORT", default="<port>"))',
            'start(port=int(os.environ.get("PORT") or "<port>"))',
        ),
        (
            "const server = { port: process.env.PORT ?? <port> };",
            'const server = { port: Number(process.env.PORT) || "<port>" };',
            'const server = { port: settings.get("PORT", <port>) };',
        ),
    ),
    "a port formatted alone": Form(
        "port",
        ('start(port=f"{<port>}")', 'env = {"RCMAN_E2E_PORT": f"<port>"}'),
        ("const server = { port: `${<port>}` };", "const env = { RCMAN_E2E_PORT: `<port>` };"),
    ),
}
# A module-level name given a fixed port, and the forms that use it in a function. The name
# is followed to where it is used; it is not a site itself.
PORT_DECLARATIONS = {
    "a number": Twins(("PORT = <port>",), ("const PORT = <port>;",)),
    "a string of digits": Twins(('PORT: str = "<port>"',), ('export const PORT = "<port>";',)),
    "a fallback": Twins(
        ('PORT = int(os.environ.get("PORT", "<port>"))',),
        ('const PORT = process.env.PORT ?? "<port>";',),
    ),
}
PORT_USES = {
    "an address": Twins(
        ('s.bind(("127.0.0.1", PORT))',), ('const address = ["127.0.0.1", PORT];',)
    ),
    "a call's arguments": Twins(
        ('connection = asyncio.open_connection("127.0.0.1", PORT)',),
        ('const socket = net.connect(PORT, "127.0.0.1");',),
    ),
    "a port's name": Twins(("start(port=PORT)",), ("const server = { port: PORT };",)),
    "an environment's port": Twins(
        ('os.environ.setdefault("RCMAN_E2E_PORT", str(PORT))',),
        ("process.env.RCMAN_E2E_PORT ??= PORT;",),
    ),
    "listen": Twins(("app.listen(PORT)",), ("server.listen(PORT);",)),
    "after --port": Twins(
        ('run(["uvicorn", "--port", str(PORT)])', 'run(["uvicorn", "--port", PORT])'),
        ('run("uvicorn", ["--port", String(PORT)]);',),
    ),
    "a formatted command": Twins(
        ('command = f"uvicorn app --port {PORT}"',),
        ("const command = `uvicorn app --port ${PORT}`;",),
    ),
    "a formatted URL": Twins(
        ('url = f"http://localhost:{PORT}"',), ("const url = `http://localhost:${PORT}`;",)
    ),
    "a joined URL": Twins(
        ('url = "http://127.0.0.1:" + str(PORT)',), ('const url = "http://127.0.0.1:" + PORT;',)
    ),
}
# A name for a port that is a function's own: a site, as a module-level one is not.
LOCAL_PORT = Twins(("port = <port>",), ("const port = <port>;", "let port;\nport = <port>;"))
# What the guard leaves alone, in both languages.
CLEAN = {
    "a uniquely named folder made in /tmp itself": Twins(
        (
            'root = Path(tempfile.mkdtemp(prefix="rcb-", dir="<tmp>"))',
            'with tempfile.TemporaryDirectory(dir="<tmp>") as d: pass',
            'd = tempfile.mkdtemp(dir=Path("<tmp>"))',
            "d = tempfile.mkdtemp(dir=tempfile.gettempdir())",
            "d = tempfile.mkdtemp(dir=Path(tempfile.gettempdir()))",
        ),
        (
            'const root = mkdtempSync(join("<tmp>", "x-"));',
            'const root = mkdtempSync(join(tmpdir(), "x-"));',
            'const root = await fs.mkdtemp(path.join(os.tmpdir(), "x-"));',
        ),
    ),
    "a test's own folder": Twins(
        ('root = tmp_path / "x"',), ('const root = join(testInfo.outputDir, "x");',)
    ),
    "a port the system picks": Twins(
        (
            's.bind(("127.0.0.1", 0))',
            "github.start(root, token, port=0)",
            'start(port=os.environ.get("PORT", 0))',
            'connection = asyncio.open_connection("127.0.0.1", 0)',
        ),
        (
            "const server = { port: 0 };",
            "const server = { port: process.env.PORT ?? 0 };",
            'server.listen(0, "127.0.0.1");',
            'const address = ["127.0.0.1", 0];',
        ),
    ),
    "a port held in a variable, read from the environment or computed": Twins(
        (
            's.bind(("127.0.0.1", port))',
            "def f(port):\n    s.bind(('', port))",
            'run(["uvicorn", "app", "--port", str(port)])',
            "command = f\"uvicorn app --port {os.environ['PORT']}\"",
            'run(["docker", "run", "-p", f"127.0.0.1:{port}:443", "image"])',
            'url = "http://127.0.0.1:" + str(port)',
            'rules = "MAP rcman.test:443 127.0.0.1:" + port',
            'port = os.environ["RCMAN_STACK_PORT"]\nrules = f"127.0.0.1:{port}"',
            'os.environ.setdefault("RCMAN_E2E_PORT", str(free_port()))',
        ),
        (
            "server.listen(port);",
            "function f(port: number) {\n  server.listen(port);\n}",
            "const command = `uvicorn app --port ${port}`;",
            "const command = `uvicorn app --port ${process.env.PORT}`;",
            'run("docker", ["run", "-p", `127.0.0.1:${port}:443`, "image"]);',
            'const url = "http://127.0.0.1:" + port;',
            'const rules = "MAP rcman.test:443 127.0.0.1:" + port;',
            "const port = process.env.RCMAN_STACK_PORT;\nconst rules = `127.0.0.1:${port}`;",
            "process.env.RCMAN_E2E_PORT ??= String(await freePort());",
        ),
    ),
    "a key computed from a variable, whatever the variable's name": Twins(
        ("def f(port):\n    settings[port] = <port>", "def f(port):\n    d = {port: <port>}"),
        (
            "function f(port: string) {\n  settings[port] = <port>;\n}",
            "function f(port: string) {\n  const d = { [port]: <port> };\n}",
        ),
    ),
    "a parameter that shadows a module-level name for a port": Twins(
        ('PORT = <port>\ndef f(PORT):\n    s.bind(("127.0.0.1", PORT))',),
        ("const PORT = <port>;\nfunction f(PORT: number) {\n  server.listen(PORT);\n}",),
    ),
    "a privileged port, which no other test can take": Twins(
        ('UNREACHABLE = "http://127.0.0.1:1/"', 'socket.create_connection(("127.0.0.1", 9))'),
        (
            'const UNREACHABLE = "http://127.0.0.1:1/";',
            'const socket = net.connect(9, "127.0.0.1");',
        ),
    ),
    "text that only looks like a path or a port": Twins(
        (
            'text = "<tmp>x is another folder"',
            'run(["mkdir", "-p", "folder"])',
            'run(["tmux", "display-message", "-p", "#{pid}"])',
            'run(["docker", "run", "-p", "127.0.0.1::443", "image"])',
            'parse(["--app-port", "8001"])',
            'sizes = [("width", 1200), ("height", 1200)]',
            'size = {"viewport": 1200}',
            'write("out.txt", 1200)',
            'ratio = f"{count}:{1200}"',
            'ratio = "16:9000"',
            'where = "stack.spec.ts:1234"',
            'url = "https://rcman.test/"',
            'os.environ.setdefault("TIMEOUT", "3000")',
            'start(os.environ.get("PORT", "<port>"))',
            "clock.advance(3500)",
        ),
        (
            'const text = "<tmp>x is another folder";',
            'run("mkdir", ["-p", "folder"]);',
            'run("tmux", ["display-message", "-p", "#{pid}"]);',
            'run("docker", ["run", "-p", "127.0.0.1::443", "image"]);',
            'parse(["--app-port", "8001"]);',
            'const sizes = [["width", 1200]];',
            "const size = { viewport: 1200 };",
            'write("out.txt", 1200);',
            "const ratio = `${count}:${1200}`;",
            'const ratio = "16:9000";',
            'const where = "stack.spec.ts:1234";',
            'const url = "https://rcman.test/";',
            'process.env.TIMEOUT ??= "3000";',
            'start(settings.get("PORT", "<port>"));',
            "await vi.advanceTimersByTimeAsync(3500);",
            "await page.setViewportSize({ width: 1200, height: 800 });",
        ),
    ),
    "a string standing alone, as a docstring does": Twins(
        (
            '"""Serves on http://127.0.0.1:<port>."""',
            'def f():\n    """Serves on http://127.0.0.1:<port>."""',
        ),
        (
            '"Serves on http://127.0.0.1:<port>";',
            'function f() {\n  "Serves on http://127.0.0.1:<port>";\n}',
        ),
    ),
}
TABLES = {
    "FORMS": FORMS,
    "PORT_DECLARATIONS": PORT_DECLARATIONS,
    "PORT_USES": PORT_USES,
    "CLEAN": CLEAN,
}


def _spelled(planted: str) -> str:
    return planted.replace("<tmp>", TMP).replace("<port>", PLANTED_PORT)


def _one_sided(table: dict) -> list[str]:
    """The entries of a table that one language does not spell."""
    return [name for name, twins in table.items() if not twins.python or not twins.typescript]


def _python_in_function(snippet: str) -> str:
    return "def f():\n" + "".join(f"    {line}\n" for line in snippet.splitlines())


def _in_function(snippet: str) -> str:
    return f"function f() {{\n  {snippet}\n}}\n"


def _module_port(declaration: str, use: str, python: bool) -> str:
    inside = _python_in_function(use) if python else _in_function(use)
    return f"{_spelled(declaration)}\n{inside}"


def _twins(language: str, table: dict) -> list:
    """Each spelling in one language of a table's entries, with the entry's name as its id."""
    return [
        pytest.param(entry, spelling, id=f"{name}: {spelling}")
        for name, entry in table.items()
        for spelling in getattr(entry, language)
    ]


def _module_ports(language: str) -> list:
    return [
        pytest.param(declaration, use, id=f"{d} / {u}: {use}")
        for d, declarations in PORT_DECLARATIONS.items()
        for declaration in getattr(declarations, language)
        for u, uses in PORT_USES.items()
        for use in getattr(uses, language)
    ]


def test_every_form_and_allowance_is_spelled_in_both_languages():
    """What keeps the two languages uniform: an entry one of them does not spell fails."""
    assert all(TABLES.values())  # non-vacuity: each table has entries
    for name, table in TABLES.items():
        assert _one_sided(table) == [], name
    assert LOCAL_PORT.python and LOCAL_PORT.typescript
    planted = {**FORMS, "only in Python": Form("port", ("x = 1",), ())}
    planted["only in TypeScript"] = Form("port", (), ("const x = 1;",))
    assert _one_sided(planted) == ["only in Python", "only in TypeScript"]


def test_planted_text_names_no_fixed_path_or_port_until_spelled():
    """The placeholders stand for the path and port, so the guard reads none in this module."""
    entries = [*(e for table in TABLES.values() for e in table.values()), LOCAL_PORT]
    planted = [s for e in entries for s in (*e.python, *e.typescript)]
    assert any("<port>" in s for s in planted) and any("<tmp>" in s for s in planted)
    assert all(TMP not in s and PLANTED_PORT not in s for s in planted)


@pytest.mark.parametrize(("form", "planted"), _twins("python", FORMS))
def test_the_guard_finds_each_form_planted_in_python(form, planted):
    """Planted twins: each spelling is found, at module level and in a function."""
    source = _spelled(planted)
    sites = fixed_sites(source)
    assert [s.kind for s in sites] == [form.kind]
    inside = fixed_sites(_python_in_function(source))
    assert [s[:3] for s in inside] == [(sites[0].line + 1, sites[0].function or "f", form.kind)]


@pytest.mark.parametrize(("declaration", "use"), _module_ports("python"))
def test_the_guard_finds_a_fixed_port_held_in_a_module_constant(declaration, use):
    """Planted twin: a port named once at module level and used in a function."""
    source = _module_port(declaration, use, python=True)
    assert [s[:3] for s in fixed_sites(source)] == [(3, "f", "port")]
    chosen = _module_port(declaration.replace("<port>", "0"), use, python=True)
    assert fixed_sites(chosen) == []  # positive control: port 0


def test_a_functions_own_port_name_is_a_site_and_a_modules_is_followed():
    (planted,) = LOCAL_PORT.python
    assert fixed_sites(_spelled(planted)) == []
    inside = _python_in_function(_spelled(planted))
    assert [s[:3] for s in fixed_sites(inside)] == [(2, "f", "port")]


@pytest.mark.parametrize(("allowance", "source"), _twins("python", CLEAN))
def test_the_guard_leaves_unique_names_and_chosen_ports_alone(allowance, source):
    assert fixed_sites(_spelled(source)) == []


@pytest.fixture(scope="module")
def ts_trees() -> dict[str, dict]:
    """Every TypeScript source these tests plant, parsed together: one node per module."""
    sources = []
    for form in FORMS.values():
        for planted in map(_spelled, form.typescript):
            sources += [planted, _in_function(planted)]
    for declarations in PORT_DECLARATIONS.values():
        for declaration in declarations.typescript:
            for uses in PORT_USES.values():
                for use in uses.typescript:
                    for chosen in (declaration, declaration.replace("<port>", "0")):
                        sources.append(_module_port(chosen, use, python=False))
    for local in map(_spelled, LOCAL_PORT.typescript):
        sources += [local, _in_function(local)]
    sources += [_spelled(s) for twins in CLEAN.values() for s in twins.typescript]
    names = {f"planted{i}.ts": source for i, source in enumerate(sources)}
    return {names[name]: tree for name, tree in parse_ts(names).items()}


@pytest.mark.parametrize(("form", "planted"), _twins("typescript", FORMS))
def test_the_guard_finds_each_form_planted_in_typescript(ts_trees, form, planted):
    """Planted twins: each spelling is found, at module level and in a function."""
    source = _spelled(planted)
    sites = ts_sites(source, ts_trees[source])
    assert [s.kind for s in sites] == [form.kind]
    inside = _in_function(source)
    assert [s[:3] for s in ts_sites(inside, ts_trees[inside])] == [
        (sites[0].line + 1, sites[0].function or "f", form.kind)
    ]


@pytest.mark.parametrize(("declaration", "use"), _module_ports("typescript"))
def test_the_guard_finds_a_fixed_port_held_in_a_typescript_module_constant(
    ts_trees, declaration, use
):
    """Planted twin: a port named once at module level and used in a function."""
    source = _module_port(declaration, use, python=False)
    assert [s[:3] for s in ts_sites(source, ts_trees[source])] == [(3, "f", "port")]
    chosen = _module_port(declaration.replace("<port>", "0"), use, python=False)
    assert ts_sites(chosen, ts_trees[chosen]) == []  # positive control: port 0


@pytest.mark.parametrize("planted", LOCAL_PORT.typescript)
def test_a_functions_own_port_name_is_a_typescript_site_and_a_modules_is_followed(
    ts_trees, planted
):
    planted = _spelled(planted)
    assert ts_sites(planted, ts_trees[planted]) == []
    line = 2 + next(i for i, text in enumerate(planted.splitlines()) if PLANTED_PORT in text)
    inside = _in_function(planted)
    assert [s[:3] for s in ts_sites(inside, ts_trees[inside])] == [(line, "f", "port")]


@pytest.mark.parametrize(("allowance", "source"), _twins("typescript", CLEAN))
def test_the_guard_leaves_unique_names_and_chosen_ports_in_typescript_alone(
    ts_trees, allowance, source
):
    source = _spelled(source)
    assert ts_sites(source, ts_trees[source]) == []
