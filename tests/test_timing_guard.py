"""The timing guard: no test waits on, or races, real time.

A static scan of the test code (project-knowledge/tests.md, "Time in tests"). It imports no
scanned module and runs nothing: each file is parsed once, and its tree walked once for its
parents, owners and bindings. It flags

- P1, elapsed real time compared: a comparison with a clock read, or with a name derived from
  one through arithmetic, min or max, and a difference of two such;
- P2, a sleep: time.sleep, asyncio.sleep, anyio.sleep and trio.sleep, resolved through imports,
  with any argument but 0, and threading.Timer, loop.call_later and loop.call_at;
- P2s, a sleep or a timeout written in a child program's source: the shell's ``sleep N`` and
  ``timeout [options] N``, Python's ``time.sleep(N)``, the numbers of an argv of the ``timeout``
  tool, ``sleep`` and the length after it in an argv, a call's arguments or a child's Python
  (``["sleep", "600"]``, ``engine.cli(..., "busybox", "sleep", "600")``, ``str(60)`` there, or a
  number a helper's caller gives the helper's parameter that is the length), and a child's loop
  on a clock, or one that sleeps on a count; an interpolated one too, unless it is a multiple of
  the guard or RUNS_OUT (a sleep: twice the guard or more). An argv compared runs nothing;
- P3, a timeout that is not allowed: a keyword of a timing name; a known positional, where a
  name or a call is seconds unless shown to be something else; a value at a position the
  parameter table or a parameter's flow gives (at a position of a name whose definitions
  disagree, only a value shown to be seconds); a parameter's default; a module's or a class's
  constant of a timing name; and pytest.mark.timeout;
- P4, a product's deadline, grace or pace shortened: monkeypatch's, mock's and the builtin
  setattr of a timing constant, a plain ``module.NAME = value``, a patch of the time module
  itself, of the guard, of a name that cannot be resolved, a restoring setattr, and runs_out
  given anything but 0 or a product's floor;
- a module defining HANG_GUARD, CONTAINER_HANG_GUARD or tier_guard other than
  tests/hang_guard.py;
- the tier rule, TIER: HANG_GUARD in what the container tests run, CONTAINER_HANG_GUARD in what
  the host tests run, and either in what both run, where only tier_guard() gives the asking
  test's (``tier_reach``).

An allowed value is the tier's guard or a multiple of it of at least 1, plus a number of at least 0
or through int(), float() or round(), tier_guard() where a test reads it, None, -1 (the standard
library's "no limit") and 0; a constant whose single binding, at module or class level, is
allowed; and a parameter of a helper (not a test, a fixture or a lambda) whose default is allowed
and whose every caller gives an allowed value, which the scan checks at each call. A timing name
is a word of the name (a keyword's or a parameter's, ``guard`` among them, or a constant's, its
longer words also run together, as IDLETIMEOUT); a string that reads as a number is seconds.
The only exemptions are the named patterns of NAMED, each with its reason and a stale check,
the derived allowances: a double (a call on one, its bound method as a value, the keywords of
a helper of the module that returns one it builds, and a helper's parameter typed as one where
every call of the helper in the module gives one), a turn of 0, the benchmark and a child's
wait loop; and STAND_INS.
"""

from __future__ import annotations

import ast
import functools
import operator
import posixpath
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.hang_guard import HANG_GUARD
from tests.typescript import FRONTEND, parse_ts, ts_scanned

REPO = Path(__file__).resolve().parent.parent
GUARD_MODULE = "tests.hang_guard"
GUARD_NAMES = {"HANG_GUARD", "CONTAINER_HANG_GUARD"}
PROJECT = {"tests", "devtools", "rcman", "helpers", "backend"}
PRODUCT = {"rcman", "helpers"}

TIMING_KEYWORD = re.compile(
    r"^(timeout|within|deadline|seconds|wait|quiet|interval|delay|every|grace|guard)$"
    r"|_(timeout|seconds|within|interval|delay|wait|grace|after)$"
)
# A word of a constant's name, between underscores: WORKSPACE is not PACE, nor RELAYS DELAY. The
# longer words are also seen run together with what comes before them, as IDLETIMEOUT and MAXWAIT.
TIMING_CONSTANT = re.compile(
    r"(^|_)(TIMEOUT|SECONDS|WITHIN|DEADLINE|GRACE|WAIT|PACE|TICK|POLL|INTERVAL|DELAY|QUIET)S?(_|$)"
    r"|(^|_)[A-Z0-9]+(TIMEOUT|SECONDS|DEADLINE|GRACE|WAIT|INTERVAL|DELAY)S?(_|$)"
    r"|_FOR$|_LIMIT$"
)


def timing_constant(name: str) -> bool:
    return name.isupper() and bool(TIMING_CONSTANT.search(name))


CLOCKS = {f"time.{n}{ns}" for n in ("monotonic", "perf_counter", "time") for ns in ("", "_ns")}
DATETIME_CLOCKS = {
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
    "datetime.date.today",
}  # fmt: skip
SLEEPS = {"time.sleep", "asyncio.sleep", "anyio.sleep", "trio.sleep"}
TIMERS = {"threading.Timer"}
LOOP_TIMERS = {"call_later", "call_at"}
# A positional timeout: the method's name and the position. A call resolved to a module is one
# of RESOLVED_POSITIONAL's or none.
POSITIONAL = {
    "wait": 0, "join": 0, "settimeout": 0, "result": 0, "poll": 0, "select": 0,
    "wait_for": 1, "communicate": 1, "acquire": 1,
}  # fmt: skip
RESOLVED_POSITIONAL = {
    "signal.alarm": 0, "socket.setdefaulttimeout": 0, "socket.create_connection": 1,
    "select.select": 3, "urllib.request.urlopen": 2, "asyncio.wait_for": 1,
    "anyio.fail_after": 0, "anyio.move_on_after": 0, "trio.fail_after": 0,
    "trio.move_on_after": 0,
}  # fmt: skip
QUEUE_NAME = re.compile(r"queue|inbox|outbox|_q$|^q$", re.I)
DOUBLES = {"FakeTime", "WatchedCondition", "ControlledEvent", "RecordingLock", "Time", "Clock"}
GUARD_ATTRIBUTES = GUARD_NAMES | {"tier", "tier_guard"}

SHELL = re.compile(
    r"(?P<what>\bsleep\s+|\btime\.sleep\(\s*"
    r"|\btimeout\s+(?:(?:--?[a-z][a-z-]*(?:=\S+|\s+-?\w+)?|-[sk]\S+)\s+)*)"
    r"(?P<n>\d*\.?\d+|\{[^}]*\})"
)
SHELL_LOOP = re.compile(r"\bwhile\b(?P<cond>[^;]*?);\s*do\b(?P<body>.*?)\bdone\b", re.S)
CHILD_CLOCK = re.compile(r"\bdate\b|\$SECONDS|\btime\.(monotonic|time|perf_counter)\b")
CHILD_COUNTER = re.compile(r"-(lt|le|gt|ge|eq)\s+\S*\d")
CHILD_SLEEP = re.compile(r"\bsleep\b")
# A sleep given as an argv, in a child's Python source: ['sleep', '600'].
ARGV_SLEEP = re.compile(
    r"""(?P<q>['"])(?P<what>(?:/(?:usr/)?bin/)?sleep)(?P=q)\s*,\s*(?P<r>['"])"""
    r"(?P<n>\d*\.?\d+|\{[^}]*\})(?P=r)"
)
SLEEP_TOOLS = {"sleep", "/bin/sleep", "/usr/bin/sleep"}
NUMERIC = re.compile(r"\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*")


@dataclass(frozen=True, order=True)
class Site:
    path: str
    line: int
    form: str
    text: str
    where: str = ""  # the qualified name of the function it is in, "" at module level


def number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = number(node.operand)
        return None if inner is None else -inner
    return None


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCTIONS, ast.Lambda)


# --- the parameter table (P3) --------------------------------------------------------------------


@dataclass
class Table:
    """For each function name, the positions of its parameters with a timing name where every
    definition agrees (``positions``), and, for a name whose definitions disagree, the union of
    those positions (``disagreeing``)."""

    positions: dict[str, frozenset[int]] = field(default_factory=dict)
    disagreeing: dict[str, frozenset[int]] = field(default_factory=dict)


def positional_parameters(fn: ast.AST) -> list[str]:
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if args and args[0] in ("self", "cls"):
        args = args[1:]
    return args


def defined_functions(body: list[ast.stmt]):
    """The functions defined in ``body``, at any depth, walking statements only."""
    for node in body:
        if isinstance(node, FUNCTIONS):
            yield node
        for name in ("body", "orelse", "finalbody", "handlers"):
            inner = getattr(node, name, None)
            if inner:
                yield from defined_functions(inner)
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from defined_functions(case.body)


def build_table(trees: Iterable[ast.AST]) -> Table:
    seen: dict[str, set[frozenset[int]]] = {}
    for tree in trees:
        for node in defined_functions(tree.body):
            positions = frozenset(
                i
                for i, name in enumerate(positional_parameters(node))
                if TIMING_KEYWORD.search(name)
            )
            seen.setdefault(node.name, set()).add(positions)
    table = Table()
    for name, kinds in seen.items():
        union = frozenset().union(*kinds)
        if len(kinds) > 1 and union:
            table.disagreeing[name] = union
        elif len(kinds) == 1 and union:
            table.positions[name] = union
    return table


# --- the tiers ----------------------------------------------------------------------------------

CONTAINER_ROOT = "tests/container/"
# Each tier's guard. A test of the container tier is one that uses the tests' engine, as the
# container marker is derived (tests/container_marker.py), and all of tests/container/.
TIER_GUARDS = {"host": "HANG_GUARD", "container": "CONTAINER_HANG_GUARD"}
ENGINE_FIXTURES = {"podman_service"}


def own_tier(path: str) -> str:
    return "container" if path.startswith(CONTAINER_ROOT) else "host"


def top_names(stmt: ast.AST) -> set[str]:
    """The names a top-level statement defines."""
    if isinstance(stmt, (*FUNCTIONS, ast.ClassDef)):
        return {stmt.name}
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
        targets = [stmt.target]
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def is_fixture_def(fn: ast.AST) -> bool:
    for d in getattr(fn, "decorator_list", []):
        target = d.func if isinstance(d, ast.Call) else d
        if getattr(target, "attr", None) == "fixture" or getattr(target, "id", None) == "fixture":
            return True
    return False


def is_autouse(fn: ast.AST) -> bool:
    return any(
        isinstance(d, ast.Call)
        and any(k.arg == "autouse" and getattr(k.value, "value", False) for k in d.keywords)
        for d in getattr(fn, "decorator_list", [])
    )


def tier_reach(trees: dict[str, ast.AST]) -> dict[str, dict[str, set[str]]]:
    """For each file, the tiers whose tests reach each of its top-level names. The roots are
    every top-level name of tests/container/, a test elsewhere, of the container tier where it
    asks for the tests' engine through its fixtures, and tests/conftest.py's autouse fixtures,
    which every test runs. A name is reached through the names its statement uses: of its own
    module, imported from another of the project's, or a fixture its function asks for. Each
    statement is walked once."""
    by_module = {module_name(p): p for p in trees}
    defs: dict[str, dict[str, list[ast.AST]]] = {}
    imports: dict[str, dict[str, tuple[str, str | None]]] = {}
    fixtures: dict[str, set[str]] = {}
    # each top-level statement's names loaded, attributes of a name, and parameters
    uses: dict[int, tuple[set[str], set[tuple[str, str]], set[str]]] = {}
    for path, tree in trees.items():
        defs[path], fixtures[path] = {}, set()
        found = imports[path] = {}
        for stmt in tree.body:
            for name in top_names(stmt):
                defs[path].setdefault(name, []).append(stmt)
            if isinstance(stmt, FUNCTIONS) and is_fixture_def(stmt):
                fixtures[path].add(stmt.name)
            names, attributes, parameters = set(), set(), set()
            uses[id(stmt)] = (names, attributes, parameters)
            for node in ast.walk(stmt):
                kind = type(node)
                if kind is ast.Name:
                    if type(node.ctx) is ast.Load:
                        names.add(node.id)
                elif kind is ast.Attribute:
                    if type(node.value) is ast.Name:
                        attributes.add((node.value.id, node.attr))
                elif kind is ast.FunctionDef or kind is ast.AsyncFunctionDef:
                    parameters.update(FileScan.parameter_names(node))
                elif kind is ast.ImportFrom:
                    if node.module and not node.level:
                        for a in node.names:
                            whole = f"{node.module}.{a.name}"
                            if whole in by_module:
                                found[a.asname or a.name] = (by_module[whole], None)
                            elif node.module in by_module:
                                found[a.asname or a.name] = (by_module[node.module], a.name)
                elif kind is ast.Import:
                    for a in node.names:
                        if a.asname and a.name in by_module:
                            found[a.asname] = (by_module[a.name], None)

    def fixture_of(path: str, name: str) -> str | None:
        """Where the fixture ``name`` a function of ``path`` asks for is defined."""
        for where in (path, "tests/container/conftest.py" if own_tier(path) == "container"
                      else None, "tests/conftest.py"):  # fmt: skip
            if where in fixtures and name in fixtures[where]:
                return where
        return None

    def asks_for_the_engine(path: str, fn: ast.AST, seen: set) -> bool:
        for a in FileScan.parameter_names(fn):
            if a in ENGINE_FIXTURES:
                return True
            where = fixture_of(path, a)
            if where is not None and (where, a) not in seen:
                seen.add((where, a))
                if any(
                    isinstance(d, FUNCTIONS) and asks_for_the_engine(where, d, seen)
                    for d in defs[where].get(a, [])
                ):
                    return True
        return False

    reached: dict[str, dict[str, set[str]]] = {p: {} for p in trees}
    todo: list[tuple[str, str, str]] = []

    def reach(path: str, name: str, tier: str) -> None:
        tiers = reached[path].setdefault(name, set())
        if tier not in tiers:
            tiers.add(tier)
            todo.append((path, name, tier))

    for path, tree in trees.items():
        for stmt in tree.body:
            if path.startswith(CONTAINER_ROOT):
                for name in top_names(stmt):
                    reach(path, name, "container")
            elif isinstance(stmt, FUNCTIONS) and path == "tests/conftest.py" and is_autouse(stmt):
                for tier in TIER_GUARDS:
                    reach(path, stmt.name, tier)
            elif Path(path).name.startswith("test_") and path.startswith("tests/"):
                tests = []
                if isinstance(stmt, FUNCTIONS) and stmt.name.startswith("test"):
                    tests = [stmt]
                elif isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Test"):
                    tests = [f for f in stmt.body if isinstance(f, FUNCTIONS)]
                for fn in tests:
                    container = asks_for_the_engine(path, fn, set())
                    reach(path, stmt.name, "container" if container else "host")
    while todo:
        path, name, tier = todo.pop()
        local, imported = defs[path], imports[path]
        for stmt in local.get(name, []):
            names, attributes, parameters = uses[id(stmt)]
            for n in names:
                if n in local:
                    reach(path, n, tier)
                elif n in imported and imported[n][1] is not None:
                    reach(*imported[n], tier)
            for value, attr in attributes:
                if value in imported and imported[value][1] is None:
                    reach(imported[value][0], attr, tier)
            for a in parameters:
                where = fixture_of(path, a)
                if where is not None:
                    reach(where, a, tier)
    return reached


# --- the allowances, each a switch the mutation run turns off ------------------------------------

ALLOWANCES = {
    "none": True, "zero": True, "runs out": True, "minus one": True, "guard": True,
    "tier_guard": True, "not seconds": True, "double in a closure": True,
    "positional not seconds": True, "seconds only at a disagreeing slot": True,
    "constant": True, "parameter": True, "double": True, "a double's method": True,
    "a double's helper": True, "typed double": True, "loop variable not seconds": True,
    "only seconds through a loose flow": True,
    "turn": True, "child loop": True, "benchmark": True,
}  # fmt: skip


# --- the scan of one file ------------------------------------------------------------------------


class FileScan:
    """The sites of one file. ``modules`` maps a module of the project to its tree, to resolve
    a constant imported from it and a fixture of tests/conftest.py."""

    def __init__(
        self,
        path: str,
        tree: ast.AST,
        source: str,
        modules: dict | None = None,
        reached: dict[str, set[str]] | None = None,
    ):
        self.path = path
        # the tiers whose tests reach each top-level name of the file (``tier_reach``)
        self.reached = reached or {}
        self.source = source
        self.tree = tree
        self.modules = modules or {}
        self.sites: list[Site] = []
        self.derived: list[tuple[str, Site]] = []
        # (function, index, name, method, loose): a loose flow's callers are checked only
        # where they give a value shown to be seconds
        self.flows: set[tuple[str, int, str, bool, bool]] = set()
        self.assignments: list[tuple[ast.AST, ast.AST, ast.AST]] = []
        self.module_aliases: dict[str, str] = {}
        self.imported: dict[str, str] = {}
        self.parents: dict[ast.AST, ast.AST] = {}
        self.owner: dict[ast.AST, ast.AST | None] = {}
        self.local_binds: dict[ast.AST, dict[str, int]] = {}  # function -> name -> bindings
        self.module_binds: dict[str, list[ast.AST]] = {}  # name -> binding statements
        self.calls: list[ast.Call] = []
        self.docstrings: set[ast.AST] = set()
        self.flagged_calls: set[int] = set()
        self._assigned: dict[ast.AST, dict[str, list[ast.AST]]] = {}
        self.globals: set[str] = set()
        self._proving: set[tuple[int, str]] = set()
        self.lines = source.splitlines()
        self._walk()
        self.calls.sort(key=operator.attrgetter("lineno", "col_offset"))
        self.functions = {n.name: n for n in self.tree.body if isinstance(n, FUNCTIONS)}
        self.fixtures = dict(self._fixtures(self.tree))
        if path != "tests/conftest.py" and "tests.conftest" in self.modules:
            self.fixtures = {
                **dict(self._fixtures(self.modules["tests.conftest"])),
                **self.fixtures,
            }
        self.benchmark_only = self._benchmark_only()
        self.may_double = any(q.startswith("tests.time_doubles.") for q in self.imported.values())
        if not self.may_double:
            used = {a.arg for n in self.parents if isinstance(n, FUNCTIONS) for a in n.args.args}
            self.may_double = any(
                name in used and self.returns_double(f) for name, f in self.fixtures.items()
            )

    # -- the one walk ---------------------------------------------------------------------------

    def _walk(self) -> None:
        """The one walk: each node's parent and owner (the function, lambda or class it is in),
        the imports, the calls, the bindings and the docstrings."""
        parents, owners, calls = self.parents, self.owner, self.calls
        scope_types = {ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef}
        bodied = {ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef}
        AST = ast.AST
        stack: list[tuple[ast.AST, ast.AST | None, ast.AST | None]] = [(self.tree, None, None)]
        pop, push = stack.pop, stack.append
        while stack:
            node, parent, owner = pop()
            kind = type(node)
            if parent is not None:
                parents[node] = parent
            owners[node] = owner
            if kind is ast.Call:
                calls.append(node)
            elif kind is ast.Name:
                if type(node.ctx) is ast.Store:
                    if owner is None or type(owner) is ast.ClassDef:
                        self.module_binds.setdefault(node.id, []).append(parent)
                    else:
                        counts = self.local_binds.setdefault(owner, {})
                        counts[node.id] = counts.get(node.id, 0) + 1
                continue  # a name has no child but its context
            elif kind is ast.Constant:
                continue
            elif kind is ast.Global:
                self.globals.update(node.names)
            elif kind is ast.Import:
                for a in node.names:
                    top = a.name.split(".")[0]
                    self.module_aliases[a.asname or top] = a.name if a.asname else top
            elif kind is ast.ImportFrom and node.module and not node.level:
                for a in node.names:
                    self.imported[a.asname or a.name] = f"{node.module}.{a.name}"
            if kind in bodied and node.body:
                first = node.body[0]
                if type(first) is ast.Expr and type(first.value) is ast.Constant:
                    self.docstrings.add(first.value)
            if kind in scope_types:
                # a function's defaults and decorators belong to where it is defined
                outer = {id(d) for d in getattr(node, "decorator_list", ())}
                if kind is not ast.ClassDef:
                    outer.add(id(node.args))
                for name in node._fields:
                    value = getattr(node, name, None)
                    for child in value if type(value) is list else (value,):
                        if isinstance(child, AST):
                            push((child, node, owner if id(child) in outer else node))
                continue
            for name in node._fields:
                value = getattr(node, name, None)
                if type(value) is list:
                    for child in value:
                        if isinstance(child, AST):
                            push((child, node, owner))
                elif isinstance(value, AST):
                    push((value, node, owner))

    def function_of(self, node: ast.AST) -> ast.AST | None:
        owner = self.owner.get(node)
        while isinstance(owner, ast.ClassDef):
            owner = self.owner.get(owner)
        return owner

    def where(self, node: ast.AST) -> str:
        names = []
        owner = self.owner.get(node)
        while owner is not None:
            if not isinstance(owner, ast.Lambda):
                names.append(owner.name)
            owner = self.owner.get(owner)
        return ".".join(reversed(names))

    def text(self, node: ast.AST) -> str:
        """The source of ``node``, on one line."""
        if not self.source or not hasattr(node, "lineno"):
            return ""
        lines = self.lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if not lines:
            return ""
        if len(lines) == 1:
            segment = lines[0][node.col_offset : node.end_col_offset]
        else:
            segment = " ".join([lines[0][node.col_offset :], *lines[1:-1],
                                lines[-1][: node.end_col_offset]])  # fmt: skip
        return " ".join(segment.split())[:120]

    def site(self, node: ast.AST, form: str, text: str | None = None) -> None:
        if isinstance(node, ast.Call):
            self.flagged_calls.add(id(node))
        self.sites.append(
            Site(self.path, node.lineno, form, text or self.text(node), self.where(node))
        )

    def allow(self, key: str, node: ast.AST, form: str) -> bool:
        """Whether the derived allowance ``key`` is on; recorded where it allows a site."""
        if not ALLOWANCES[key]:
            return False
        self.derived.append((key, Site(self.path, node.lineno, form, self.text(node))))
        return True

    # -- names ----------------------------------------------------------------------------------

    def qual(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            fn = self.function_of(node)
            if fn is not None and (
                self.local_binds.get(fn, {}).get(node.id) or node.id in self.parameter_names(fn)
            ):
                return None  # a local name shadows the module's import
            if self.path == "tests/hang_guard.py" and node.id in GUARD_NAMES:
                return f"{GUARD_MODULE}.{node.id}"  # the guard's own module: a value made of it
            return self.imported.get(node.id) or self.module_aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            inner = self.qual(node.value)
            return f"{inner}.{node.attr}" if inner else None
        return None

    def is_guard(self, node: ast.AST) -> bool:
        return self.qual(node) in {f"{GUARD_MODULE}.{n}" for n in GUARD_NAMES}

    def is_tier_guard_call(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and not node.args
            and self.qual(node.func) == f"{GUARD_MODULE}.tier_guard"
        )

    def is_runs_out(self, node: ast.AST) -> bool:
        return self.qual(node) == f"{GUARD_MODULE}.RUNS_OUT"

    def outlasts_or_runs_out(self, node: ast.AST) -> bool:
        """A child's time: RUNS_OUT (rule (a)), or a multiple of the guard (rule (g))."""
        node = self.child_time(node)
        if self.is_runs_out(node):
            return ALLOWANCES["runs out"]
        return self.guard_multiple(node) and ALLOWANCES["guard"]

    def outlasts(self, node: ast.AST) -> bool:
        """A child's sleep that outlasts the test: twice the guard or more (rule (g))."""
        least = 2 if ALLOWANCES.get("P2s twice the guard", True) else 1
        return self.guard_multiple(self.child_time(node), least) and ALLOWANCES["guard"]

    def child_time(self, node: ast.AST, depth: int = 0) -> ast.AST:
        """What a child's time is written from: an f-string's one value, str()'s argument, and a
        name's one binding, in the function or the module."""
        if depth > 3:
            return node
        if isinstance(node, ast.JoinedStr):
            inner = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
            texts = [v for v in node.values if isinstance(v, ast.Constant) and v.value]
            if len(inner) == 1 and not texts:
                return self.child_time(inner[0], depth + 1)
            return node
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "str" and node.args:
            return self.child_time(node.args[0], depth + 1)
        if isinstance(node, ast.Name) and ALLOWANCES.get("P2s a name's binding", True):
            fn = self.function_of(node)
            binds = self.bindings_in(fn, node.id) if fn is not None else []
            if not binds and not (fn is not None and node.id in self.parameter_names(fn)):
                value = self.single_binding(self.module_binds.get(node.id, []), node.id)
                binds = [value] if value is not None and node.id not in self.globals else []
            if len(binds) == 1:
                return self.child_time(binds[0], depth + 1)
        return node

    def is_product(self, node: ast.AST) -> bool:
        q = self.qual(node)
        return q is not None and q.split(".")[0] in PRODUCT

    def guard_multiple(self, node: ast.AST, least: float = 1) -> bool:
        """The guard, a multiple of it of at least ``least``, either plus a number of at least
        0, and int(), float() or round() of one, as ``int(2 * tier_guard())``."""
        if self.is_guard(node) or self.is_tier_guard_call(node):
            return least <= 1
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) in ("int", "float", "round")
            and len(node.args) == 1
            and not node.keywords
            and ALLOWANCES.get("guard through int", True)
        ):
            return self.guard_multiple(node.args[0], least)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult | ast.Add):
            times = isinstance(node.op, ast.Mult)
            for k, g in ((node.left, node.right), (node.right, node.left)):
                n = number(k)
                if n is None or n < (1 if times else 0):
                    continue
                if self.guard_multiple(g, least / n if times else least):
                    return True
        return False

    @staticmethod
    def is_fixture(fn: ast.AST) -> bool:
        for d in getattr(fn, "decorator_list", []):
            target = d.func if isinstance(d, ast.Call) else d
            if (isinstance(target, ast.Attribute) and target.attr == "fixture") or (
                isinstance(target, ast.Name) and target.id == "fixture"
            ):
                return True
        return False

    def is_test(self, fn: ast.AST) -> bool:
        return isinstance(fn, FUNCTIONS) and fn.name.startswith("test")

    def allowed(
        self,
        node: ast.AST,
        at: ast.AST,
        read_at_test_time: bool = True,
        product_ok: bool = False,
        depth: int = 0,
    ) -> bool:
        """Whether ``node``, used at ``at``, is an allowed value."""
        if isinstance(node, ast.Constant) and node.value is None:
            return ALLOWANCES["none"]
        if number(node) == 0:
            return ALLOWANCES["zero"]
        if number(node) == -1:
            return ALLOWANCES["minus one"]
        if self.is_tier_guard_call(node):
            return ALLOWANCES["tier_guard"] and read_at_test_time
        if self.guard_multiple(node):
            uses_tier_guard = any(self.is_tier_guard_call(n) for n in ast.walk(node))
            return ALLOWANCES["guard"] and (read_at_test_time or not uses_tier_guard)
        if self.is_product(node):
            return False  # a product's value is the product's, not the test's bound
        if depth > 3:
            return False
        if isinstance(node, ast.Name):
            fn = self.function_of(at)
            if fn is not None and node.id in self.parameter_names(fn):
                return self.parameter_allowed(fn, node.id)
            if fn is not None and self.local_binds.get(fn, {}).get(node.id):
                return False  # a local binding: a site where it is used
            return self.constant_allowed(node.id, depth)
        if (
            isinstance(node, ast.Attribute)
            and self.a_double(node.value, at)
            and self.allow("a double's method", node, "P3")
        ):
            return True  # a double's bound method, as the monitor's ``wait=times.wait``
        if isinstance(node, ast.Attribute):
            q = self.qual(node)
            if q and q.split(".")[0] in ("tests", "devtools"):
                module, _, name = q.rpartition(".")
                return self.imported_constant_allowed(module, name, depth)
        return False

    @staticmethod
    def parameter_names(fn: ast.AST) -> list[str]:
        a = fn.args
        return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [
            x.arg for x in (a.vararg, a.kwarg) if x
        ]

    def parameter_allowed(self, fn: ast.AST, name: str, loose: bool = False) -> bool:
        """A helper's parameter: not a test's, a fixture's or a lambda's, with an allowed default
        if it has one; its callers are checked at each call through the flow it records."""
        if not ALLOWANCES["parameter"]:
            return False
        if isinstance(fn, ast.Lambda) or self.is_test(fn) or self.is_fixture(fn):
            return False
        default = self.default_of(fn, name)
        if default is not None and not self.allowed(default, fn, read_at_test_time=False):
            return False
        params = positional_parameters(fn)
        method = isinstance(self.owner.get(fn), ast.ClassDef)
        index = params.index(name) if name in params else -1
        self.flows.add((fn.name, index, name, method, loose))
        return True

    @staticmethod
    def default_of(fn: ast.AST, name: str) -> ast.AST | None:
        a = fn.args
        positional = a.posonlyargs + a.args
        for p, d in zip(positional[len(positional) - len(a.defaults) :], a.defaults, strict=True):
            if p.arg == name:
                return d
        for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True):
            if p.arg == name:
                return d
        return None

    def constant_allowed(self, name: str, depth: int) -> bool:
        if not ALLOWANCES["constant"]:
            return False
        if name in self.imported:
            module, _, attr = self.imported[name].rpartition(".")
            if module.split(".")[0] in ("tests", "devtools"):
                return self.imported_constant_allowed(module, attr, depth)
            return False
        if name in self.globals and ALLOWANCES.get("P3 global", True):
            return False  # rebound in a function, through global
        value = self.single_binding(self.module_binds.get(name, []), name)
        return value is not None and self.allowed(
            value, value, read_at_test_time=False, depth=depth + 1
        )

    @staticmethod
    def single_binding(binds: list[ast.AST], name: str) -> ast.AST | None:
        """The value of a name's one binding, a plain assignment of it alone; None otherwise."""
        if len(binds) != 1:
            return None
        bind = binds[0]
        if isinstance(bind, ast.Assign) and len(bind.targets) == 1:
            target = bind.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return bind.value
        if isinstance(bind, ast.AnnAssign) and bind.value is not None:
            return bind.value
        return None

    def imported_constant_allowed(self, module: str, name: str, depth: int) -> bool:
        if not ALLOWANCES["constant"]:
            return False
        if module == GUARD_MODULE and name in GUARD_NAMES:
            return ALLOWANCES["guard"]
        other = self.modules.get(module)
        if other is None:
            return False
        scan = scan_of(module, self.modules)
        value = scan.single_binding(scan.module_binds.get(name, []), name)
        return value is not None and scan.allowed(
            value, value, read_at_test_time=False, depth=depth + 1
        )

    # -- doubles --------------------------------------------------------------------------------

    def makes_double(self, node: ast.AST, depth: int = 1) -> bool:
        """An expression that builds a double of tests/time_doubles.py: its constructor, a list
        or tuple holding one, or a call of a helper or fixture of this module (one level) that
        returns one."""
        if isinstance(node, ast.List | ast.Tuple):
            return any(self.makes_double(e, depth) for e in node.elts)
        if not isinstance(node, ast.Call):
            return False
        q = self.qual(node.func) or ""
        if q.startswith("tests.time_doubles.") and q.rsplit(".", 1)[1] in DOUBLES:
            return True
        if depth and isinstance(node.func, ast.Name):
            helper = self.functions.get(node.func.id) or self.fixtures.get(node.func.id)
            return helper is not None and self.returns_double(helper, depth - 1)
        return False

    @functools.cache  # noqa: B019 - one scan per file, freed with it
    def returns_double(self, fn: ast.AST, depth: int = 0) -> bool:
        built = self.double_names(fn, depth)
        for node in ast.walk(fn):
            if isinstance(node, ast.Return | ast.Yield) and node.value is not None:
                values = node.value.elts if isinstance(node.value, ast.Tuple | ast.List) else [
                    node.value]  # fmt: skip
                for v in values:
                    if self.makes_double(v, depth) or self.names_double(v, built):
                        return True
        return False

    @staticmethod
    def names_double(node: ast.AST, built: set[str]) -> bool:
        if isinstance(node, ast.Tuple | ast.List):
            return any(isinstance(e, ast.Name) and e.id in built for e in node.elts)
        return isinstance(node, ast.Name) and node.id in built

    @functools.cache  # noqa: B019
    def double_names(self, fn: ast.AST, depth: int = 1) -> frozenset[str]:
        """Names ``fn`` binds to a double, its own or a helper's, and its parameters that are
        fixtures (of this module or tests/conftest.py) returning one."""
        found: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and self.makes_double(node.value, depth):
                for target in node.targets:
                    found |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        if isinstance(fn, FUNCTIONS):
            for a in fn.args.args:
                fixture = self.fixtures.get(a.arg)
                if fixture is not None and fixture is not fn and self.returns_double(fixture):
                    found.add(a.arg)
                elif self.typed_double(a.annotation) and self.given_a_double(fn, a.arg):
                    found.add(a.arg)
        return frozenset(found)

    def typed_parameter(self, fn: ast.AST, name: str) -> bool:
        """Whether ``name`` is a parameter of ``fn`` typed as a double, and given one."""
        return isinstance(fn, FUNCTIONS) and any(
            a.arg == name and self.typed_double(a.annotation) and self.given_a_double(fn, name)
            for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        )

    def given_a_double(self, fn: ast.AST, name: str) -> bool:
        """Whether every call of ``fn`` in this module gives its parameter ``name`` a double, as
        a literal argument is resolved: its annotation alone is not trusted. A function with no
        such call, or named other than where it is called, is given none that is shown."""
        if not ALLOWANCES.get("P3 typed double given", True):
            return True
        key = (id(fn), name)
        if key in self._proving:
            return False  # a call of its own: shown by its other callers or not at all
        self._proving.add(key)
        try:
            calls = self.calls_of(fn)
            if not calls:
                return False
            params = positional_parameters(fn)
            index = params.index(name) if name in params else None
            for call in calls:
                given = call.args[index] if index is not None and index < len(call.args) else None
                for kw in call.keywords:
                    if kw.arg == name:
                        given = kw.value
                if given is None or not (
                    self.makes_double(given)
                    or (isinstance(given, ast.Name) and self.a_double(given, call))
                ):
                    return False
            return True
        finally:
            self._proving.discard(key)

    def calls_of(self, fn: ast.AST) -> list[ast.Call] | None:
        """The calls of ``fn`` in this module: of its name, of a method's name on any object, of
        a class for its __init__; None where its name is also used as a value."""
        owner = self.owner.get(fn)
        method = isinstance(owner, ast.ClassDef)
        name = owner.name if method and fn.name == "__init__" else fn.name
        by_attribute = method and fn.name != "__init__"
        found, called = [], set()
        for call in self.calls:
            f = call.func
            if (by_attribute and isinstance(f, ast.Attribute) and f.attr == name) or (
                not by_attribute and isinstance(f, ast.Name) and f.id == name
            ):
                found.append(call)
                called.add(id(f))
        if not by_attribute:
            for node in self.parents:
                if (
                    isinstance(node, ast.Name)
                    and node.id == name
                    and isinstance(node.ctx, ast.Load)
                    and id(node) not in called
                ):
                    return None  # handed on as a value, given what is not seen here
        return found

    def typed_double(self, annotation: ast.AST | None) -> bool:
        """A parameter's annotation that names a double of tests/time_doubles.py, as a helper
        handed the test's FakeTime declares it."""
        if annotation is None or not ALLOWANCES["typed double"]:
            return False
        q = self.qual(annotation) or ""
        return q.startswith("tests.time_doubles.") and q.rsplit(".", 1)[1] in DOUBLES

    def on_a_double(self, call: ast.Call) -> bool:
        if not isinstance(call.func, ast.Attribute):
            return False
        receiver = call.func.value
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        return self.a_double(receiver, call)

    def a_double(self, receiver: ast.AST, at: ast.AST) -> bool:
        """Whether ``receiver``, used at ``at``, is a name bound to a double: the innermost
        function's first, then the functions it is defined in."""
        if (
            not self.may_double
            or not isinstance(receiver, ast.Name)
            or receiver.id in self.module_aliases
        ):
            return False
        fn = self.function_of(at)
        name = receiver.id
        scope = fn
        while scope is not None:  # bound here or outside, or a fixture: else no walk is needed
            if self.local_binds.get(scope, {}).get(name) or (
                (name in self.fixtures or self.typed_parameter(scope, name))
                and name in self.parameter_names(scope)
            ):
                break
            scope = self.function_of(scope)
        else:
            return False
        return name in self.double_names(fn) or name in self.doubles_in_scope(fn)

    @functools.cache  # noqa: B019
    def doubles_in_scope(self, fn: ast.AST) -> frozenset[str]:
        """The double names of ``fn`` and of the functions it is defined in (a closure's)."""
        found = set(self.double_names(fn))
        outer = self.function_of(fn)
        while outer is not None and ALLOWANCES.get("double in a closure", True):
            found |= self.double_names(outer)
            outer = self.function_of(outer)
        return frozenset(found)

    def builds_a_double(self, call: ast.Call) -> bool:
        """A call of a helper of this module that returns a double it builds, as the monitor's
        ``looping`` builds the virtual Time its intervals are given to."""
        f = call.func
        if not self.may_double or not isinstance(f, ast.Name):
            return False
        helper = self.functions.get(f.id)
        return helper is not None and self.returns_double(helper)

    @functools.cache  # noqa: B019
    def doubled(self, fn: ast.AST) -> frozenset[str]:
        """The objects whose ``time`` (or seams for time) ``fn`` replaces with a double."""
        found = set()
        doubles = self.double_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and (got := self.setattr_args(node)):
                obj, name, value = got
                if name in ("time", "_changed", "_closing") or (name or "").endswith("lock"):
                    if self.makes_double(value) or self.names_double(value, doubles):
                        found.add(ast.dump(obj))
        return frozenset(found)

    # -- the benchmark --------------------------------------------------------------------------

    @staticmethod
    def is_benchmark(fn: ast.AST) -> bool:
        return any(
            isinstance(d, ast.Attribute) and d.attr == "benchmark"
            for d in getattr(fn, "decorator_list", [])
        )

    def _benchmark_only(self) -> set[str]:
        """Functions of this module that only benchmark tests call."""
        only = {n for n, f in self.functions.items() if self.is_benchmark(f)}
        if not only:
            return only
        callers: dict[str, set[str]] = {name: set() for name in self.functions}
        for node in self.parents:
            if isinstance(node, ast.Name) and node.id in callers:
                fn = self.function_of(node)
                while fn is not None and self.owner.get(fn) is not None:
                    fn = self.owner.get(fn)
                caller = fn.name if isinstance(fn, FUNCTIONS) else "<module>"
                if caller != node.id:
                    callers[node.id].add(caller)
        changed = True
        while changed:
            changed = False
            for name, by in callers.items():
                if name not in only and by and by <= only:
                    only.add(name)
                    changed = True
        return only

    # -- the scan -------------------------------------------------------------------------------

    def scan(self) -> FileScan:
        self._clocks()
        self._defining_the_guard()
        self._tier_rule()
        for node in self.calls:
            self._call(node)
        for node in self.parents:
            if isinstance(node, FUNCTIONS):
                self._defaults(node)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                self._assign(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node not in self.docstrings and not isinstance(
                    self.parents.get(node), ast.JoinedStr
                ):
                    self._child_source(node, node.value, [])
            elif isinstance(node, ast.JoinedStr):
                self._joined(node)
            elif isinstance(node, ast.List | ast.Tuple):
                self._timeout_argv(node)
                if not isinstance(self.parents.get(node), ast.Compare):
                    self._sleep_argv(node.elts, node)  # one compared runs nothing: it is expected
        self._setattrs()
        return self

    def finish(self, table: Table, flows: set[tuple[str, int, str, bool, bool]]) -> list[Site]:
        """The calls checked against the parameter table and the flows of every file, then the
        sites, the benchmark's left out."""
        self._table_calls(table, flows)
        kept = []
        for s in sorted(set(self.sites)):
            head = s.where.split(".")[0] if s.where else ""
            if head in self.benchmark_only and ALLOWANCES["benchmark"]:
                self.derived.append(("benchmark", s))
            else:
                kept.append(s)
        self.sites = kept
        return kept

    # P1 ---------------------------------------------------------------------------------------

    def clock_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        q = self.qual(node.func)
        if q in DATETIME_CLOCKS:
            return True
        if node.args:
            return False
        if q in CLOCKS:
            return True
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "time":  # loop.time()
            inner = f.value
            if isinstance(inner, ast.Name) and "loop" in inner.id:
                return True
            if isinstance(inner, ast.Call) and (self.qual(inner.func) or "").endswith(
                ("get_running_loop", "get_event_loop")
            ):
                return True
        return False

    def clocky(self, node: ast.AST, derived: set[str]) -> bool:
        """A clock read, a name derived from one, or arithmetic, min or max of such."""
        if self.clock_read(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in derived
        if isinstance(node, ast.BinOp):
            return self.clocky(node.left, derived) or self.clocky(node.right, derived)
        if isinstance(node, ast.UnaryOp):
            return self.clocky(node.operand, derived)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
            "min", "max"):  # fmt: skip
            return any(self.clocky(a, derived) for a in node.args)
        return False

    def _clocks(self) -> None:
        if not ALLOWANCES.get("P1 branch", True):
            return
        by_owner: dict[ast.AST | None, list[ast.AST]] = {}
        for node in self.parents:
            if isinstance(
                node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Compare | ast.BinOp
            ):
                by_owner.setdefault(self.function_of(node), []).append(node)
        for nodes in by_owner.values():
            derived: set[str] = set()
            changed = True
            while changed:
                changed = False
                for n in nodes:
                    for target, value in self.pairs(n):
                        if self.clocky(value, derived) and target not in derived:
                            derived.add(target)
                            changed = True
            reported = set()
            for n in nodes:
                if isinstance(n, ast.Compare):
                    if all(isinstance(op, ast.Is | ast.IsNot) for op in n.ops):
                        continue
                    if any(self.clocky(x, derived) for x in [n.left, *n.comparators]):
                        self.site(n, "P1")
                        reported.add(n.lineno)
            for n in nodes:
                if (
                    isinstance(n, ast.BinOp)
                    and isinstance(n.op, ast.Sub)
                    and n.lineno not in reported
                    and self.clocky(n.left, derived)
                    and self.clocky(n.right, derived)
                ):
                    self.site(n, "P1")
                    reported.add(n.lineno)

    @staticmethod
    def pairs(node: ast.AST) -> list[tuple[str, ast.AST]]:
        """(name, value) for each name an assignment binds, a tuple paired element by element."""
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            return []
        found = []
        for t in targets:
            if isinstance(t, ast.Name):
                found.append((t.id, value))
            elif isinstance(t, ast.Tuple) and isinstance(value, ast.Tuple):
                for te, ve in zip(t.elts, value.elts, strict=False):
                    if isinstance(te, ast.Name):
                        found.append((te.id, ve))
        return found

    # P2, P3 ----------------------------------------------------------------------------------

    def _call(self, node: ast.Call) -> None:
        q = self.qual(node.func)
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        self._sleep_argv(node.args, node)
        if ALLOWANCES.get("P2 branch", True):
            if q in SLEEPS:
                if node.args and number(node.args[0]) == 0 and self.allow("turn", node, "P2"):
                    pass
                else:
                    self.site(node, "P2")
            if q in TIMERS or name in LOOP_TIMERS:
                self.site(node, "P2")
        if q == f"{GUARD_MODULE}.runs_out":
            self._runs_out(node)
            return
        if q == f"{GUARD_MODULE}.pace":
            return
        if self.on_a_double(node):
            if (node.args or node.keywords) and self.allow("double", node, "P3"):
                return
        keywords = node.keywords
        if keywords and self.builds_a_double(node):
            if self.allow("a double's helper", node, "P3"):
                keywords = []  # its keywords go to the double; its flows are still checked
        for kw in keywords:
            if kw.arg and TIMING_KEYWORD.search(kw.arg) and not self.allowed(kw.value, node):
                if ALLOWANCES.get("P3 keywords", True):
                    self.site(node, "P3", f"{kw.arg}={self.text(kw.value)} in {self.text(f)}")
        position = self.position_of(node, q, name)
        if position is not None and len(node.args) > position:
            value = node.args[position]
            if self.seconds_like(value, node) and not self.allowed(value, node):
                self.site(node, "P3", f"{name}(…{self.text(value)}…)")
        if q == "pytest.mark.timeout" and node.args and not self.allowed(node.args[0], node):
            if ALLOWANCES.get("P3 mark", True):
                self.site(node, "P3")

    def position_of(self, node: ast.Call, q: str | None, name: str | None) -> int | None:
        if q in RESOLVED_POSITIONAL:
            return RESOLVED_POSITIONAL[q] if ALLOWANCES.get(f"P3 positional {q}", True) else None
        if not ALLOWANCES.get(f"P3 positional {name}", True):
            return None
        if q is not None:
            return None  # a module's function, not a method of a waiting object
        f = node.func
        if not isinstance(f, ast.Attribute) or isinstance(f.value, ast.Constant):
            return None
        if name == "join" and isinstance(f.value, ast.Call) and getattr(
            f.value.func, "id", None) in ("chr", "str"):  # fmt: skip
            return None  # str.join, as chr(10).join(lines)
        if name == "get":
            first = node.args[0] if node.args else None
            blocking = isinstance(first, ast.Constant) and isinstance(first.value, bool)
            return 1 if blocking or QUEUE_NAME.search(self.text(f.value).split(".")[-1]) else None
        return POSITIONAL.get(name)

    def _runs_out(self, node: ast.Call) -> None:
        """runs_out's ``at``: 0, or the product's floor, a product's constant."""
        if not ALLOWANCES.get("P4 runs_out", True):
            return
        at = node.args[3] if len(node.args) > 3 else None
        for kw in node.keywords:
            if kw.arg == "at":
                at = kw.value
        named = at.attr if isinstance(at, ast.Attribute) else getattr(at, "id", "")
        if at is None or number(at) == 0 or "FLOOR" in named:
            return
        self.site(node, "P4", f"runs_out at {self.text(at)}: not 0 or a floor")

    def _table_calls(self, table: Table, flows: set[tuple[str, int, str, bool, bool]]) -> None:
        if not ALLOWANCES.get("P3 table", True):
            return
        by_function: dict[str, list[tuple[int, str, bool, bool]]] = {}
        for fname, index, pname, method, loose in sorted(flows):
            by_function.setdefault(fname, []).append((index, pname, method, loose))
        for node in self.calls:
            if id(node) in self.flagged_calls and ALLOWANCES.get("P3 one site", True):
                continue  # one call, one site
            f, args, keywords = self.unwrapped(node)
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name is None:
                continue
            q = self.qual(f)
            if q is not None and q.split(".")[0] not in PROJECT:
                continue  # the standard library's, or another package's
            if q in (f"{GUARD_MODULE}.runs_out", f"{GUARD_MODULE}.pace"):
                continue
            if self.on_a_double(node) and ALLOWANCES["double"]:
                continue
            if (
                not ALLOWANCES.get("P3 flow past a double's helper", True)
                and self.builds_a_double(node)
                and ALLOWANCES["a double's helper"]
            ):
                continue
            before = len(self.sites)
            checked: set[int] = set()
            for i in sorted(table.positions.get(name, ())):
                checked.add(i)
                if i < len(args) and not self.allowed(args[i], node):
                    self.site(node, "P3", f"{name} at {i}: {self.text(args[i])}")
            for i in sorted(table.disagreeing.get(name, ())):
                if i in checked or i >= len(args):
                    continue
                checked.add(i)
                value = args[i]
                if not ALLOWANCES.get("P3 every value", True) and not self.timeish(value):
                    continue
                if ALLOWANCES["seconds only at a disagreeing slot"]:
                    # a name of no agreed meaning: only what is shown to be seconds; a helper's
                    # parameter is checked at its callers, through a loose flow
                    helper = self.helper_given(value, node)
                    if helper is not None and ALLOWANCES.get("P3 disagreeing parameter", True):
                        if not self.parameter_allowed(helper, value.id, loose=True):
                            self.site(node, "P3", f"{name} at {i}: {self.text(value)}")
                        continue
                    if not self.positively_seconds(value, node):
                        continue
                elif self.not_seconds(value, node):
                    continue
                if not self.allowed(value, node):
                    self.site(node, "P3", f"{name} at {i}: {self.text(value)}")
            for index, pname, method, loose in by_function.get(name, ()):
                if not method and isinstance(f, ast.Attribute):
                    # a module's function called through its module: only one of the project's
                    if q is None or not ALLOWANCES.get("P3 flow through a module", True):
                        continue
                value = args[index] if 0 <= index < len(args) else None
                for kw in keywords:
                    if kw.arg == pname:
                        value = kw.value
                if value is None or index in checked:
                    continue
                checked.add(index)
                if (
                    loose
                    and ALLOWANCES["only seconds through a loose flow"]
                    and not self.positively_seconds(value, node)
                ):
                    continue  # given to a slot of no agreed meaning: only seconds are checked
                if method and (
                    self.not_seconds(value, node)
                    or (not ALLOWANCES.get("P3 every value", True) and not self.timeish(value))
                ):
                    continue  # a method's name is matched loosely: what is not seconds is not
                if not self.allowed(value, node):
                    self.site(node, "P3", f"{name}'s {pname}: {self.text(value)}")
            del self.sites[before + 1 :]  # one site per call

    def helper_given(self, value: ast.AST, at: ast.AST) -> ast.AST | None:
        """The helper (not a test, a fixture or a lambda) whose parameter ``value`` is."""
        helper = self.function_of(at)
        if (
            isinstance(value, ast.Name)
            and isinstance(helper, FUNCTIONS)
            and value.id in self.parameter_names(helper)
            and not self.is_test(helper)
            and not self.is_fixture(helper)
        ):
            return helper
        return None

    def table_flows(self, table: Table) -> None:
        """Before the flows of every file are put together: a helper's parameter given at a
        position of the table flows to the helper's callers, loosely where the name's
        definitions disagree."""
        for node in self.calls:
            f, args, _ = self.unwrapped(node)
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            agreed = table.positions.get(name, ())
            if not ALLOWANCES.get("P3 table position flow", True):
                agreed = ()
            for i, loose in [(i, False) for i in agreed] + [
                (i, True) for i in table.disagreeing.get(name, ())
            ]:
                if i < len(args) and (
                    not loose or ALLOWANCES.get("P3 disagreeing parameter", True)
                ):
                    helper = self.helper_given(args[i], node)
                    if helper is not None:
                        self.parameter_allowed(helper, args[i].id, loose=loose)

    def unwrapped(self, node: ast.Call):
        """The function, arguments and keywords a call gives: functools.partial's own."""
        if (
            self.qual(node.func) == "functools.partial"
            and node.args
            and ALLOWANCES.get("P3 partial", True)
        ):
            return node.args[0], node.args[1:], node.keywords
        return node.func, node.args, node.keywords

    NOT_A_TIME = (ast.List, ast.Tuple, ast.Dict, ast.Set, ast.JoinedStr, ast.Lambda,
                   ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)  # fmt: skip

    def not_seconds(self, node: ast.AST, at: ast.AST) -> bool:
        """Provably not seconds: a str, bytes or bool constant, a container, an f-string, a
        lambda or a comprehension, or a name whose single binding is one of those."""
        if not ALLOWANCES["not seconds"]:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes | bool):
            return True
        if isinstance(node, self.NOT_A_TIME):
            return True
        if isinstance(node, ast.Name):
            looped = self.loop_iterable(node, at)
            if looped is not None and ALLOWANCES["loop variable not seconds"]:
                return (
                    isinstance(looped, ast.Tuple | ast.List)
                    and bool(looped.elts)
                    and all(self.not_seconds(e, at) for e in looped.elts)
                )
            fn = self.function_of(at)
            binds = self.bindings_in(fn, node.id) if fn is not None else []
            if not binds:
                value = self.single_binding(self.module_binds.get(node.id, []), node.id)
                binds = [value] if value is not None else []
            return len(binds) == 1 and self.not_seconds(binds[0], at)
        return False

    def words(self, node: ast.AST) -> bool:
        """A str constant, or a list, tuple or set of them only: a constant whose name holds a
        timing word by chance (WORKSPACE, INTERVALS) but whose value is words, not seconds. A
        string that reads as a number, as '0.01', is seconds written as words, and is not."""
        if not ALLOWANCES["not seconds"]:
            return False
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            return bool(node.elts) and all(self.words(e) for e in node.elts)
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and not (NUMERIC.fullmatch(node.value) and ALLOWANCES.get("P3 numeric words", True))
        )

    def seconds_like(self, node: ast.AST, at: ast.AST) -> bool:
        """At a known positional slot: a number, arithmetic, a name or a call that
        ``not_seconds`` does not show to be something else (a parameter, a fixture, a call's
        result), or an attribute of a timing name."""
        if not ALLOWANCES["positional not seconds"]:
            return True
        if number(node) is not None or isinstance(node, ast.BinOp | ast.UnaryOp):
            return True
        if isinstance(node, ast.Name | ast.Call) and ALLOWANCES.get("P3 positional name", True):
            return not self.not_seconds(node, at)
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if timing_constant(name) or TIMING_KEYWORD.search(name) or name in GUARD_NAMES:
                return True
            if isinstance(node, ast.Name):
                fn = self.function_of(at)
                binds = self.bindings_in(fn, node.id) if fn is not None else []
                if not binds:
                    value = self.single_binding(self.module_binds.get(node.id, []), node.id)
                    binds = [value] if value is not None else []
                return any(number(b) is not None or isinstance(b, ast.BinOp) for b in binds)
        return False

    def positively_seconds(self, node: ast.AST, at: ast.AST, depth: int = 0) -> bool:
        """Shown to be seconds: a number, arithmetic on one, a timing name, or a name whose
        binding is one, in the function, the module, or a module of the project it is imported
        from."""
        if number(node) is not None:
            return True
        if isinstance(node, ast.UnaryOp):
            return self.positively_seconds(node.operand, at, depth)
        if isinstance(node, ast.BinOp):
            return self.positively_seconds(node.left, at, depth) or self.positively_seconds(
                node.right, at, depth
            )
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if timing_constant(name) or TIMING_KEYWORD.search(name) or name in GUARD_NAMES:
                return True
        if depth > 3:
            return False
        if isinstance(node, ast.Attribute):
            q = self.qual(node)
            if q and q.split(".")[0] in ("tests", "devtools"):
                module, _, attr = q.rpartition(".")
                return self.imported_seconds(module, attr, depth)
            return False
        if not isinstance(node, ast.Name):
            return False
        fn = self.function_of(at)
        if fn is not None:
            binds = self.bindings_in(fn, node.id)
            if binds:
                return any(self.positively_seconds(b, b, depth + 1) for b in binds)
            if node.id in self.parameter_names(fn):
                return False  # its callers are checked through its flow
        return self.module_seconds(node.id, depth)

    def module_seconds(self, name: str, depth: int) -> bool:
        if name in self.imported:
            module, _, attr = self.imported[name].rpartition(".")
            return self.imported_seconds(module, attr, depth)
        value = self.single_binding(self.module_binds.get(name, []), name)
        return value is not None and self.positively_seconds(value, value, depth + 1)

    def imported_seconds(self, module: str, name: str, depth: int) -> bool:
        if not ALLOWANCES.get("P3 seconds through an import", True) or module not in self.modules:
            return False
        return scan_of(module, self.modules).module_seconds(name, depth + 1)

    @staticmethod
    def timeish(node: ast.AST) -> bool:
        for n in ast.walk(node):
            if (v := number(n)) is not None and v not in (0, -1):
                return True
            if isinstance(n, ast.Name) and (timing_constant(n.id) or TIMING_KEYWORD.search(n.id)):
                return True
            if isinstance(n, ast.Attribute) and timing_constant(n.attr):
                return True
        return False

    def _defaults(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not ALLOWANCES.get("P3 defaults", True):
            return
        a = fn.args
        positional = a.posonlyargs + a.args
        pairs = list(zip(positional[len(positional) - len(a.defaults) :], a.defaults, strict=True))
        pairs += [(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
        for param, default in pairs:
            if TIMING_KEYWORD.search(param.arg) and not self.allowed(
                default, fn, read_at_test_time=False
            ):
                self.site(default, "P3", f"{fn.name}({param.arg}={self.text(default)})")

    def _assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        if node.value is None:
            return
        at_module_or_class = self.function_of(node) is None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            names = [(t, node.value)]
            if isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                names = list(zip(t.elts, node.value.elts, strict=False))
            for target, value in names:
                if (
                    ALLOWANCES.get("P3 constants", True)
                    and isinstance(target, ast.Name)
                    and at_module_or_class
                    and timing_constant(target.id)
                    and not self.words(value)
                    and not self.allowed(value, value, read_at_test_time=False)
                ):
                    self.site(node, "P3", f"{target.id} = {self.text(value)}")
            if (
                isinstance(t, ast.Name)
                and at_module_or_class
                and self.guard_multiple(node.value)
                and self.allowed(node.value, node.value, read_at_test_time=False)
                and ALLOWANCES.get("P3 alias", True)
                and self.path != "tests/hang_guard.py"  # where the guard's values are made
            ):
                self.site(node, "P3", f"{t.id} = {self.text(node.value)}: an alias of the guard")
            if isinstance(t, ast.Attribute) and timing_constant(t.attr):
                self.assignments.append((node, t, node.value))

    def _defining_the_guard(self) -> None:
        if self.path == "tests/hang_guard.py" or not ALLOWANCES.get("guard names", True):
            return
        for node in self.parents:
            defined = []
            if isinstance(node, ast.Assign):
                defined = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined = [node.target.id]
            elif isinstance(node, FUNCTIONS):
                defined = [node.name]
            if set(defined) & (GUARD_NAMES | {"tier_guard"}):
                self.site(node, "GUARD", f"defines {', '.join(defined)}")

    # the tier rule -----------------------------------------------------------------------------

    def tiers_at(self, node: ast.AST) -> set[str]:
        """The tiers whose tests run ``node``: those that reach the top-level statement it is
        in, or, where none is known to, the tier of its file's folder."""
        stmt = node
        while (parent := self.parents.get(stmt)) is not None and parent is not self.tree:
            stmt = parent
        tiers = set().union(*(self.reached.get(n, set()) for n in top_names(stmt)))
        return tiers or {own_tier(self.path)}

    def _tier_rule(self) -> None:
        """A guard of the wrong tier: HANG_GUARD in what the container tests run,
        CONTAINER_HANG_GUARD in what the host tests run, and either in what both run, where
        only tier_guard() gives the asking test's."""
        if self.path == "tests/hang_guard.py" or not ALLOWANCES.get("tier rule", True):
            return
        for node in self.parents:
            if not (isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load)):
                continue
            q = self.qual(node)
            if q is None or not q.startswith(GUARD_MODULE + "."):
                continue
            name = q.rpartition(".")[2]
            if name not in GUARD_NAMES:
                continue
            tiers = self.tiers_at(node)
            if len(tiers) > 1:
                self.site(node, "TIER", f"{name} in what both tiers run: tier_guard()")
            elif name != TIER_GUARDS[next(iter(tiers))]:
                self.site(node, "TIER", f"{name} in what the {next(iter(tiers))} tests run")

    # P2s --------------------------------------------------------------------------------------

    def _child_source(self, node: ast.AST, text: str, values: list[ast.AST]) -> None:
        """Sleeps and timeouts in a string a child program runs; ``values`` are an f-string's
        interpolated expressions, written {0}, {1}, … in ``text``."""
        if not ALLOWANCES.get("P2s branch", True):
            return
        if "sleep" not in text and "timeout" not in text and "while" not in text:
            return
        loops = []
        for m in SHELL_LOOP.finditer(text):
            cond, body = m.group("cond"), m.group("body")
            clock = bool(CHILD_CLOCK.search(cond))
            counted_sleep = bool(CHILD_COUNTER.search(cond)) and bool(CHILD_SLEEP.search(body))
            if clock or counted_sleep:
                self.site(node, "P1", f"a child's loop on a clock or a count: {cond.strip()}")
            loops.append((m.start(), m.end(), clock or counted_sleep))
        python_loops = self._python_loop_lines(text)
        found = [*SHELL.finditer(text)]
        if ALLOWANCES.get("P2s sleep argv", True):
            found += ARGV_SLEEP.finditer(text)
        for m in found:
            n, what = m.group("n"), m.group("what").strip()
            if n.startswith("{"):
                index = n[1:-1]
                if index.isdigit() and int(index) < len(values):
                    value = values[int(index)]
                    timed = self.outlasts if "sleep" in what else self.outlasts_or_runs_out
                    if timed(value):
                        continue
                self.site(node, "P2s", f"{what} {n}: interpolated, not the guard or RUNS_OUT")
                continue
            if float(n) == 0:
                continue
            in_loop = any(s <= m.start() < e and not bad for s, e, bad in loops)
            line = text.count("\n", 0, m.start())
            if "sleep" in what and (in_loop or line in python_loops):
                if self.allow("child loop", node, "P2s"):
                    continue
            self.site(node, "P2s", f"{m.group(0)!r} in a child's source")

    @staticmethod
    def _python_loop_lines(text: str) -> set[int]:
        if "while" not in text:
            return set()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        lines: set[int] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.While):
                test = ast.unparse(n.test)
                if (
                    CHILD_CLOCK.search(test)
                    or isinstance(n.test, ast.Compare)
                    and any(number(c) is not None for c in n.test.comparators)
                ):
                    continue
                for b in n.body:
                    lines |= set(range(b.lineno - 1, b.end_lineno or b.lineno))
        return lines

    def _joined(self, node: ast.JoinedStr) -> None:
        parts, values = [], []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value).replace("{", "{{").replace("}", "}}"))
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + str(len(values)) + "}")
                values.append(v.value)
        if node not in self.docstrings:
            self._child_source(node, "".join(parts), values)

    def the_timeout_tool(self, node: ast.AST, depth: int = 0) -> bool:
        """``"timeout"``, ``*x.tool("timeout")``, or ``*name`` bound to an argv holding one."""
        if isinstance(node, ast.Constant):
            return node.value == "timeout"
        if isinstance(node, ast.Starred):
            inner = node.value
            if isinstance(inner, ast.Call) and any(
                isinstance(a, ast.Constant) and a.value == "timeout" for a in inner.args
            ):
                return True
            if isinstance(inner, ast.Name) and depth < 2:
                fn = self.function_of(node)
                for bound in self.bindings_in(fn, inner.id):
                    if isinstance(bound, ast.List | ast.Tuple) and any(
                        self.the_timeout_tool(e, depth + 1) for e in bound.elts
                    ):
                        return True
        return False

    def bindings_in(self, fn: ast.AST | None, name: str) -> list[ast.AST]:
        """The values ``name`` is assigned in ``fn`` (or the module), worked out once per scope."""
        scope = fn if fn is not None else self.tree
        found = self._assigned.get(scope)
        if found is None:
            found = {}
            for n in ast.walk(scope):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            found.setdefault(t.id, []).append(n.value)
            self._assigned[scope] = found
        return found.get(name, [])

    def _timeout_argv(self, node: ast.List | ast.Tuple) -> None:
        if not ALLOWANCES.get("P2s argv", True):
            return
        elts = node.elts
        for i, e in enumerate(elts):
            if not self.the_timeout_tool(e):
                continue
            for later in elts[i + 1 :]:
                if isinstance(later, ast.Constant) and isinstance(later.value, str):
                    if re.fullmatch(r"\d*\.?\d+", later.value):
                        if float(later.value):
                            self.site(node, "P2s", f"timeout argv: {self.text(node)}")
                            return
                        continue
                    if later.value.startswith("-") or re.fullmatch(r"[A-Z]+", later.value):
                        continue
                    return  # the command begins
                if isinstance(later, ast.Starred):
                    return  # the command, or another argv
                value = later
                if isinstance(later, ast.JoinedStr):
                    inner = [v.value for v in later.values if isinstance(v, ast.FormattedValue)]
                    value = inner[0] if len(inner) == 1 else later
                elif (
                    isinstance(later, ast.Call)
                    and isinstance(later.func, ast.Name)
                    and later.func.id == "str"
                    and later.args
                ):
                    value = later.args[0]
                if not self.outlasts_or_runs_out(value):
                    self.site(node, "P2s", f"timeout argv: {self.text(node)}")
                return  # the length is given: the command follows

    def _sleep_argv(self, elts: list[ast.AST], node: ast.AST) -> None:
        """``sleep`` and the length it is given, in an argv or a call's arguments, as
        ``["sleep", "600"]`` and ``engine.cli("run", image, "busybox", "sleep", "600")``: a
        length that is not twice the guard or more (rule (g)), a number through str() among
        them; where a helper's parameter is the length, what each call of it in the module
        gives. ``infinity`` outlasts anything."""
        if not ALLOWANCES.get("P2s sleep argv", True):
            return
        for i, e in enumerate(elts[:-1]):
            if not (isinstance(e, ast.Constant) and e.value in SLEEP_TOOLS):
                continue
            later = elts[i + 1]
            written = self.child_time(later)
            helper = self.helper_given(later, node)
            if helper is not None and ALLOWANCES.get("P2s sleep argv parameter", True):
                # its callers in the module give the length: each is checked where it does
                for call, value in self.given_to(helper, later.id):
                    if self.a_sleeps_length(self.child_time(value), value):
                        self.site(call, "P2s", f"a sleep's length: {self.text(call)}")
                return
            if self.a_sleeps_length(written, later):
                self.site(node, "P2s", f"a sleep's argv: {self.text(node)}")
                return

    def given_to(self, helper: ast.AST, name: str) -> list[tuple[ast.Call, ast.AST]]:
        """Each call of the module's ``helper`` and what it gives its parameter ``name``."""
        params = positional_parameters(helper)
        index = params.index(name) if name in params else -1
        found = []
        for call in self.calls:
            if getattr(call.func, "id", None) != helper.name:
                continue
            value = call.args[index] if 0 <= index < len(call.args) else None
            for kw in call.keywords:
                if kw.arg == name:
                    value = kw.value
            if value is not None:
                found.append((call, value))
        return found

    def a_sleeps_length(self, written: ast.AST, later: ast.AST) -> bool:
        """Whether ``later``, written as ``written``, is a length given ``sleep`` that does
        not outlast the test: a number, or a string of one, but 0, and an f-string or str() of
        what is not twice the guard. ``infinity`` outlasts anything."""
        if isinstance(written, ast.Constant):
            value = written.value
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                return False
            if isinstance(value, str) and not NUMERIC.fullmatch(value):
                return False  # infinity, or what is no length an argv gives
            return float(value) != 0
        if not (
            isinstance(later, ast.JoinedStr)
            or (isinstance(later, ast.Call) and getattr(later.func, "id", None) == "str")
        ):
            return False  # what is not written as a string here is no argv's length
        return not self.outlasts(later)

    # P4 ---------------------------------------------------------------------------------------

    def setattr_args(self, node: ast.Call):
        """(object, name or None, value) of a setattr-like call, or None."""
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        q = self.qual(f) or ""
        is_mock_object = q.endswith("mock.patch.object") or (
            isinstance(f, ast.Attribute) and f.attr == "object"
            and (self.qual(f.value) or "").endswith("mock.patch"))  # fmt: skip
        is_mock = q.endswith("mock.patch") and not is_mock_object
        if fname == "setattr" or is_mock_object:
            args = node.args
            if len(args) >= 3:
                name = args[1].value if isinstance(args[1], ast.Constant) else None
                return args[0], name if isinstance(name, str) else None, args[2]
            if (
                len(args) == 2
                and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str)
            ):
                module, _, name = args[0].value.rpartition(".")
                return ast.Name(id=module), name, args[1]
            return None
        if is_mock and node.args and isinstance(node.args[0], ast.Constant):
            module, _, name = str(node.args[0].value).rpartition(".")
            value = node.args[1] if len(node.args) > 1 else ast.Constant(value=None)
            for kw in node.keywords:
                if kw.arg == "new":
                    value = kw.value
            return ast.Name(id=module), name, value
        return None

    def patches_time(self, obj: ast.AST) -> bool:
        q = self.qual(obj)
        if q == "time" or (isinstance(obj, ast.Name) and obj.id in ("time",)):
            return True
        return isinstance(obj, ast.Attribute) and obj.attr == "time"

    def _setattrs(self) -> None:
        if not ALLOWANCES.get("P4 branch", True):
            return
        seen: dict[tuple, int] = {}
        for node in self.calls:
            if not (got := self.setattr_args(node)):
                continue
            obj, name, value = got
            fn = self.function_of(node)
            if self.patches_time(obj):
                self.site(node, "P4", f"the time module patched: {self.text(node)}")
                continue
            if name in GUARD_ATTRIBUTES:
                self.site(node, "P4", f"the guard patched: {self.text(node)}")
                continue
            names = [name] if name is not None else self.resolve_name(node.args[1], node)
            if names is None:
                self.site(node, "P4", f"a name that cannot be resolved: {self.text(node)}")
                continue
            timing = [n for n in names if timing_constant(n)]
            if not timing:
                continue
            back = []
            for resolved in timing:
                key = (id(fn), ast.dump(obj), resolved)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 2:
                    back.append(resolved)
            if back:
                self.site(node, "P4", f"{', '.join(back)} given back by a second setattr")
            if self.allowed(value, node):
                continue
            if fn is not None and ast.dump(obj) in self.doubled(fn):
                if self.allow("double", node, "P4"):
                    continue
            self.site(node, "P4", f"{', '.join(timing)} = {self.text(value)}")
        for node, target, value in self.assignments:
            if not self.allowed(value, node):
                self.site(node, "P4", f"{self.text(target)} = {self.text(value)}")

    def resolve_name(self, node: ast.AST, at: ast.AST) -> list[str] | None:
        """A non-literal setattr name: a loop variable over a literal tuple or list, itself or
        as a part of a tuple the loop unpacks; a test's parameter that pytest.mark.parametrize
        gives literals; or a module-local helper's parameter given a literal at every call."""
        if not isinstance(node, ast.Name):
            return None
        fn = self.function_of(at)
        if self.reassigned(fn, node.id):
            return None  # what it was given is not what it is here
        values = self.loop_values(node, at)
        if values is None and isinstance(fn, FUNCTIONS):
            values = self.parametrized(fn, node.id)
        if values is not None:
            if values and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                              for e in values):  # fmt: skip
                return [e.value for e in values]
            return None
        while isinstance(fn, FUNCTIONS):  # the function's parameter, or a closure's
            if node.id in self.parameter_names(fn):
                return self.literal_arguments(fn, node.id)
            if self.local_binds.get(fn, {}).get(node.id) or not ALLOWANCES.get("P4 closure", True):
                return None
            fn = self.function_of(fn)
            if self.reassigned(fn, node.id):
                return None
        return None

    def reassigned(self, fn: ast.AST | None, name: str) -> bool:
        """Whether ``name`` is assigned in ``fn`` other than as a loop's target."""
        if fn is None or not ALLOWANCES.get("P4 reassigned", True):
            return False
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                targets = n.targets
            elif isinstance(n, ast.AugAssign | ast.AnnAssign | ast.NamedExpr):
                targets = [n.target]
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == name for x in targets for t in ast.walk(x)):
                return True
        return False

    @staticmethod
    def _part(items: ast.AST, at: int, of: int) -> list[ast.AST] | None:
        """Item ``at`` of each literal tuple or list of ``of`` items in the literal ``items``."""
        if not isinstance(items, ast.Tuple | ast.List):
            return None
        if of == 1:
            return list(items.elts)
        parts = []
        for e in items.elts:
            if not (isinstance(e, ast.Tuple | ast.List) and len(e.elts) == of):
                return None
            parts.append(e.elts[at])
        return parts

    def loop_values(self, node: ast.Name, at: ast.AST) -> list[ast.AST] | None:
        """The values ``node`` takes, where ``at`` is in a for loop or a comprehension whose
        target is ``node``, or a tuple that holds it, over a literal tuple or list (of literal
        tuples as long as the target); [] where it is a loop variable over anything else; None
        where it is no loop variable there."""
        parent = self.parents.get(at)
        while parent is not None:
            if isinstance(parent, ast.For):
                loops = [(parent.target, parent.iter)]
            elif isinstance(parent, self.COMPREHENSIONS):
                loops = [(g.target, g.iter) for g in parent.generators]
            else:
                loops = []
            for target, it in loops:
                names = [target] if isinstance(target, ast.Name) else (
                    target.elts if isinstance(target, ast.Tuple) else [])  # fmt: skip
                for i, t in enumerate(names):
                    if isinstance(t, ast.Name) and t.id == node.id:
                        return self._part(it, i, len(names)) or []
            parent = self.parents.get(parent)
        return None

    def parametrized(self, fn: ast.AST, name: str) -> list[ast.AST] | None:
        """The literal values pytest.mark.parametrize gives the parameter ``name`` of ``fn``;
        None where it gives it none, or none that is literal."""
        for d in getattr(fn, "decorator_list", []):
            if not (isinstance(d, ast.Call) and len(d.args) > 1
                    and self.text(d.func).endswith("mark.parametrize")):  # fmt: skip
                continue
            names = d.args[0]
            if isinstance(names, ast.Constant) and isinstance(names.value, str):
                argnames = [n.strip() for n in names.value.split(",")]
            elif isinstance(names, ast.Tuple | ast.List) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in names.elts
            ):
                argnames = [e.value for e in names.elts]
            else:
                continue
            if name in argnames:
                return self._part(d.args[1], argnames.index(name), len(argnames))
        return None

    COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

    def loop_iterable(self, node: ast.Name, at: ast.AST) -> ast.AST | None:
        """What ``node`` loops over, where ``at`` is in a for loop or a comprehension whose
        target it is; None where it is no loop variable there."""
        parent = self.parents.get(at)
        while parent is not None:
            if isinstance(parent, ast.For):
                loops = [(parent.target, parent.iter)]
            elif isinstance(parent, self.COMPREHENSIONS):
                loops = [(g.target, g.iter) for g in parent.generators]
            else:
                loops = []
            for target, it in loops:
                if isinstance(target, ast.Name) and target.id == node.id:
                    return it
            parent = self.parents.get(parent)
        return None

    def literal_arguments(self, fn: ast.AST, param: str) -> list[str] | None:
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        index = params.index(param) if param in params else None
        found: list[str] = []
        for node in self.calls:
            if isinstance(node.func, ast.Name) and node.func.id == fn.name:
                given = node.args[index] if index is not None and index < len(node.args) else None
                for kw in node.keywords:
                    if kw.arg == param:
                        given = kw.value
                if not (isinstance(given, ast.Constant) and isinstance(given.value, str)):
                    return None
                found.append(given.value)
        return found or None

    # fixtures -------------------------------------------------------------------------------

    def _fixtures(self, tree: ast.AST):
        for n in getattr(tree, "body", []):
            if isinstance(n, FUNCTIONS) and self.is_fixture(n):
                yield n.name, n


# A FileScan of a module the scan resolves names into, once per run of the scan.
_SCANS: dict[tuple[str, int], FileScan] = {}


def scan_of(module: str, modules: dict) -> FileScan:
    tree = modules[module]
    key = (module, id(tree))
    if key not in _SCANS:
        _SCANS[key] = FileScan(module.replace(".", "/") + ".py", tree, "", modules)
    return _SCANS[key]


def scan_source(
    path: str,
    source: str,
    table: Table | None = None,
    modules: dict | None = None,
    reached: dict[str, set[str]] | None = None,
) -> FileScan:
    """One file scanned alone: its own flows are the only ones its calls are checked against."""
    table = table or Table()
    scan = FileScan(path, ast.parse(source), source, modules, reached).scan()
    scan.table_flows(table)
    scan.finish(table, set(scan.flows))
    return scan


# --- the scope -----------------------------------------------------------------------------------

# Whole files that run only as another program, imitating a program rcman drives, or programs whose
# deadlines are their behaviour and decide no test. What races them is scanned in
# the tests that run them; ``stand_in_problems`` is their stale check.
STAND_INS = {
    "devtools/fake_claude.py": "runs as `claude` in a workspace, imitating Claude Code's timing",
    "devtools/pre_test_commit.py": "the pre-test hook: its DEADLINE and GRACE are its behaviour",
    "tests/container/stand_ins.py": "runs as systemctl, loginctl and rcman's podman in the "
    "container suites, imitating their timing (a unit's stop)",
    "tests/container/github_stand_in.py": "serves the tests' GitHub in a container",
    "tests/container/oidc_stub.py": "runs as Google's sign-in in the whole stack",
}
GUARD_FILE = "tests/test_timing_guard.py"

# The roots of the Python scope: tests/, tests/container/ among them, and devtools/. The
# frontend's TypeScript is its own scope (``ts_scope``, below).
SCOPE = ("tests", "devtools")
TABLE_ROOTS = ("backend", "helpers", "tests", "devtools")


def listed(roots: Iterable[str], repo: Path = REPO) -> list[str]:
    roots = list(roots)
    if not roots:
        return []
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "--", *roots],
        cwd=repo, capture_output=True, text=True, check=True, timeout=HANG_GUARD,
    ).stdout.split()  # fmt: skip
    return sorted(f for f in out if f.endswith(".py") and (repo / f).exists())


def scoped_files(roots: Iterable[str] = SCOPE, repo: Path = REPO) -> list[str]:
    """The Python files of ``roots`` that git does not ignore, but the stand-ins. No root: no
    file."""
    return [f for f in listed(roots, repo) if f not in STAND_INS]


def module_name(path: str) -> str:
    """The module a file of tests/ or devtools/ is imported as; backend's drop their root."""
    name = path[: -len(".py")].replace("/", ".")
    return name.removeprefix("backend.")


@dataclass
class Tree:
    sites: list[Site]
    derived: list[tuple[str, Site]]
    table: Table


def scan_tree(files: Iterable[str] | None = None) -> Tree:
    """Every site of the scope, before the named patterns: each file parsed once, the parameter
    table and the modules names resolve into built from the same trees."""
    paths = listed(TABLE_ROOTS)
    sources = {p: (REPO / p).read_text(encoding="utf-8") for p in paths}
    trees = {p: ast.parse(s) for p, s in sources.items()}
    table = build_table(trees.values())
    modules = {module_name(p): t for p, t in trees.items()}
    reached = tier_reach({p: t for p, t in trees.items() if p.startswith(("tests/", "devtools/"))})
    _SCANS.clear()
    scans = [
        FileScan(f, trees[f], sources[f], modules, reached.get(f)).scan()
        for f in (scoped_files() if files is None else files)
    ]
    for s in scans:
        s.table_flows(table)
    flows = set().union(*(s.flows for s in scans)) if scans else set()
    sites, derived = [], []
    for s in scans:
        sites += s.finish(table, flows)
        derived += s.derived
    return Tree(sites, derived, table)


# --- the TypeScript scope ------------------------------------------------------------------------

# The frontend's tests and their configs, under frontend/: the component tests vite.config.ts's
# ``include`` names (TS_TESTS, which a test holds to it), everything under TS_DIRS, and TS_CONFIGS.
TS_DIRS = ("src/test/",)
TS_TESTS = re.compile(r"^src/(.+/)?[^/]+\.test\.tsx?$")
TS_INCLUDE = "src/**/*.test.{ts,tsx}"
TS_CONFIGS = ("vite.config.ts",)
# The frontend's hang guards, of its one tier, the component tests' (Vitest in jsdom): the only
# names a timeout there may be given, besides 0.
TS_GUARD_FILE = "src/test/hangGuard.ts"
# The keys the config sets (P3c): Vitest's in vite.config.ts.
VITEST_KEYS = ("test.testTimeout", "test.hookTimeout", "test.teardownTimeout")
TS_CLOCKS = {("Date", "now"), ("performance", "now")}
TS_COMPARE = {"<", ">", "<=", ">=", "==", "===", "!=", "!=="}
# Matchers that compare what expect() was given, as a comparison does.
TS_MATCHERS = {"toBeLessThan", "toBeGreaterThan", "toBeLessThanOrEqual", "toBeGreaterThanOrEqual"}
TS_GLOBALS = {"window", "globalThis", "self"}
TS_FUNCTIONS = {"ArrowFunctionExpression", "FunctionExpression", "FunctionDeclaration"}
TS_WRAPPERS = {
    "TSAsExpression", "TSSatisfiesExpression", "TSNonNullExpression", "TSTypeAssertion",
    "ParenthesizedExpression",
}  # fmt: skip


def ts_in_scope(rel: str) -> bool:
    return rel in TS_CONFIGS or rel.startswith(TS_DIRS) or bool(TS_TESTS.match(rel))


def ts_scope(frontend: Path = FRONTEND) -> list[str]:
    """The TypeScript of the scope, relative to ``frontend``: what git does not ignore."""
    return [rel for rel in ts_scanned(frontend) if ts_in_scope(rel)]


def _ts_unwrap(node: dict) -> dict:
    while node and node.get("type") in TS_WRAPPERS:
        node = node["expression"]
    return node


def _ts_zero(node: dict) -> bool:
    value = node.get("value")
    return node.get("type") == "Literal" and type(value) in (int, float) and value == 0


def _ts_null(node: dict) -> bool:
    return node.get("type") == "Literal" and node.get("raw") == "null"


def _ts_key(prop: dict) -> str | None:
    key = prop.get("key") or {}
    if prop.get("computed"):
        return key.get("value") if key.get("type") == "Literal" else None
    return key.get("name") if key.get("type") == "Identifier" else key.get("value")


def _ts_member(node: dict) -> tuple[str | None, str | None]:
    """A call's callee as (object, property): ``vi.waitFor`` as ("vi", "waitFor"), a bare
    ``setTimeout`` as (None, "setTimeout"); an object that is not a plain name as ""."""
    callee = _ts_unwrap(node.get("callee") or {})
    if callee.get("type") == "Identifier":
        return None, callee["name"]
    if callee.get("type") == "MemberExpression" and not callee.get("computed"):
        obj = _ts_unwrap(callee["object"])
        return (obj["name"] if obj.get("type") == "Identifier" else ""), callee["property"].get(
            "name"
        )
    return "", None


def _ts_children(node: dict):
    for key, child in node.items():
        if key in ("type", "start", "end", "range", "loc"):
            continue
        if isinstance(child, dict) and "type" in child:
            yield key, child
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict) and "type" in item:
                    yield key, item


def _ts_resolve(rel: str, given: str) -> str | None:
    """A relative import's file under frontend/, without its suffix."""
    if not given.startswith("."):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), given))
    return re.sub(r"\.(ts|tsx|js|mjs)$", "", path)


class TsScan:
    """The sites of one TypeScript file, ``rel`` under frontend/."""

    def __init__(self, rel: str, source: str, tree: dict):
        self.rel, self.source, self.tree = rel, source, tree
        self.encoded = source.encode("utf-16-le")
        self.sites: list[Site] = []
        self.derived: list[tuple[str, Site]] = []
        # local name -> the name it imports from hangGuard.ts
        self.guards: dict[str, str] = {}
        for node in tree["body"]:
            if node["type"] != "ImportDeclaration":
                continue
            target = _ts_resolve(rel, node["source"]["value"])
            for spec in node["specifiers"]:
                local = spec["local"]["name"]
                if (
                    target == TS_GUARD_FILE.removesuffix(".ts")
                    and spec["type"] == "ImportSpecifier"
                ):
                    self.guards[local] = spec["imported"].get("name") or spec["imported"]["value"]

    @property
    def path(self) -> str:
        return f"frontend/{self.rel}"

    def site(self, node: dict, form: str, where: str, text: str | None = None) -> Site:
        line = self.encoded[: 2 * node["start"]].decode("utf-16-le").count("\n") + 1
        if text is None:
            text = self.encoded[2 * node["start"] : 2 * node["end"]].decode("utf-16-le")
        return Site(self.path, line, form, " ".join(text.split())[:120], where)

    def flag(self, node: dict, form: str, where: str, text: str | None = None) -> None:
        self.sites.append(self.site(node, form, where, text))

    def allow(self, key: str, node: dict, where: str) -> bool:
        if ALLOWANCES[key]:
            self.derived.append((key, self.site(node, "P3", where)))
            return True
        return False

    def value(self, node: dict | None, where: str, site: dict) -> None:
        """A timeout's value: 0, or a guard of hangGuard.ts; otherwise flagged, at ``site``,
        as P3."""
        node = _ts_unwrap(node or {})
        if _ts_zero(node) and self.allow("zero", site, where):
            return
        if (
            node.get("type") == "Identifier"
            and node["name"] in self.guards
            and self.allow("guard", site, where)
        ):
            return
        self.flag(site, "P3", where)

    def scan(self) -> TsScan:
        self._visit(self.tree, "", set(), None)
        if self.rel in TS_CONFIGS:
            self._config_keys()
        return self

    # P1: a clock read, or a name given one in this function or one around it.
    def _clocked(self, node: dict, derived: set[str]) -> bool:
        node = _ts_unwrap(node)
        kind = node.get("type")
        if kind in TS_FUNCTIONS:
            return False
        if kind == "CallExpression" and _ts_member(node) in TS_CLOCKS:
            return True
        if kind == "Identifier":
            return node["name"] in derived
        for key, child in _ts_children(node):
            if kind == "MemberExpression" and key == "property" and not node.get("computed"):
                continue
            if kind == "Property" and key == "key" and not node.get("computed"):
                continue
            if self._clocked(child, derived):
                return True
        return False

    def _visit(self, node: dict, where: str, derived: set[str], parent: dict | None):
        """``derived`` holds the clock-derived names of the function the node is in, which a
        function inside starts from."""
        kind = node.get("type")
        if kind in TS_FUNCTIONS:
            derived = set(derived)
        if kind == "FunctionDeclaration" and node.get("id"):
            where = node["id"]["name"]
        if kind == "VariableDeclarator":
            target = node["id"]
            if target.get("type") == "Identifier":
                name = target["name"]
                if name.endswith("_GUARD_MS") and self.rel != TS_GUARD_FILE:
                    self.flag(node, "GUARD", where)
                if node.get("init") and self._clocked(node["init"], derived):
                    derived.add(name)
        if kind == "AssignmentExpression" and node["left"].get("type") == "Identifier":
            if self._clocked(node["right"], derived):
                derived.add(node["left"]["name"])
        if kind == "BinaryExpression":
            left, right = (
                self._clocked(node["left"], derived),
                self._clocked(node["right"], derived),
            )
            if node["operator"] in TS_COMPARE and (left or right):
                self.flag(node, "P1", where)
            elif node["operator"] == "-" and left and right:
                self.flag(node, "P1", where)
        if kind == "CallExpression":
            self._call(node, where, derived)
        if kind == "Property" and (parent or {}).get("type") != "ObjectPattern":
            key = _ts_key(node)
            if key == "shouldAdvanceTime":
                value = _ts_unwrap(node["value"])
                if not (value.get("type") == "Literal" and value.get("value") is False):
                    self.flag(node, "P1", where)
            elif key is not None and (key == "timeout" or key.endswith("Timeout")):
                self.value(node["value"], where, node)
        for key, child in _ts_children(node):
            inner = where
            if kind == "VariableDeclarator" and key == "init" and child["type"] in TS_FUNCTIONS:
                inner = node["id"].get("name", where)
            if kind == "CallExpression" and key == "arguments" and child["type"] in TS_FUNCTIONS:
                title = _ts_unwrap(node["arguments"][0])
                if title.get("type") == "Literal" and isinstance(title.get("value"), str):
                    inner = title["value"]
            self._visit(child, inner, derived, node)

    def _call(self, node: dict, where: str, derived: set[str]) -> None:
        obj, name = _ts_member(node)
        args = node["arguments"]
        timer = obj is None or obj in TS_GLOBALS
        if name == "waitForTimeout" and obj is not None:
            self.flag(node, "P2", where)
        elif name == "setTimeout" and timer:
            delay = _ts_unwrap(args[1]) if len(args) > 1 else None
            if delay is None or _ts_zero(delay):
                if ALLOWANCES["turn"]:
                    self.derived.append(("turn", self.site(node, "P2", where)))
                    return
            self.flag(node, "P2", where)
        elif name == "setInterval" and timer:
            self.flag(node, "P2", where)
        elif name == "setTimeout":  # test.setTimeout(n)
            self.value(args[0] if args else None, where, node)
        elif name == "slow" and obj is not None:  # test.slow(), testInfo.slow(), and so on
            self.flag(node, "P3", where)
        elif obj == "userEvent" and name == "setup" and args:  # a delay is a sleep per action
            options = _ts_unwrap(args[0])
            for prop in options.get("properties", []):
                value = _ts_unwrap(prop.get("value") or {})
                if _ts_key(prop) == "delay" and not (_ts_zero(value) or _ts_null(value)):
                    self.flag(prop, "P2", where)
        elif obj == "vi" and name in ("waitFor", "waitUntil"):
            options = _ts_unwrap(args[1]) if len(args) > 1 else None
            if options is None:
                self.flag(node, "P3", where)
            elif options.get("type") == "ObjectExpression":
                if not any(_ts_key(p) == "timeout" for p in options["properties"]):
                    self.flag(node, "P3", where)
            else:
                self.value(options, where, node)
        elif name in TS_MATCHERS and obj == "":
            target = _ts_unwrap(node["callee"]["object"])
            given = target.get("arguments", []) if target.get("type") == "CallExpression" else []
            if any(self._clocked(a, derived) for a in [*given, *args]):
                self.flag(node, "P1", where)

    # P3c: the config sets every key.
    def _exported(self, tree: dict) -> dict | None:
        for node in tree["body"]:
            if node["type"] == "ExportDefaultDeclaration":
                value = _ts_unwrap(node["declaration"])
                if value.get("type") == "CallExpression" and value["arguments"]:
                    value = _ts_unwrap(value["arguments"][0])
                return value if value.get("type") == "ObjectExpression" else None
        return None

    def _flat(self, obj: dict, prefix: str = "") -> dict[str, dict]:
        """The keys an object literal sets, as dotted paths."""
        found: dict[str, dict] = {}
        for prop in obj["properties"]:
            key = _ts_key(prop)
            if key is None:
                continue
            value = _ts_unwrap(prop["value"])
            found[prefix + key] = prop
            if value.get("type") == "ObjectExpression":
                found |= self._flat(value, f"{prefix}{key}.")
        return found

    def _config_keys(self) -> None:
        exported = self._exported(self.tree)
        node = next(n for n in self.tree["body"] if n["type"] == "ExportDefaultDeclaration")
        found = {} if exported is None else self._flat(exported)
        for key in VITEST_KEYS:
            if key not in found:
                self.flag(node, "P3c", "", f"{key} is not set")


@dataclass
class TsTree:
    sites: list[Site]
    derived: list[tuple[str, Site]]


def ts_scan_sources(sources: dict[str, str], trees: dict[str, dict] | None = None) -> TsTree:
    """The sites of TypeScript files, by ``rel`` under frontend/, parsed together once."""
    trees = parse_ts(sources) if trees is None else trees
    sites, derived = [], []
    for rel, source in sources.items():
        scan = TsScan(rel, source, trees[rel]).scan()
        sites += scan.sites
        derived += scan.derived
    return TsTree(sites, derived)


def ts_scan_tree(frontend: Path = FRONTEND) -> TsTree:
    return ts_scan_sources(
        {rel: (frontend / rel).read_text(encoding="utf-8") for rel in ts_scope(frontend)}
    )


# --- the named patterns --------------------------------------------------------------------------


@dataclass(frozen=True)
class Named:
    """A named exemption: the sites of ``forms`` in ``path``, in the functions ``where`` names
    (the qualified name or a prefix of one; "" for module level)."""

    key: str
    path: str
    where: tuple[str, ...]
    forms: frozenset[str]
    reason: str

    def covers(self, site: Site) -> bool:
        return (
            site.path == self.path
            and site.form in self.forms
            and any(site.where == w or (w and site.where.startswith(w + ".")) for w in self.where)
        )


NAMED = (
    Named("until", "tests/until.py", ("until", "until_async"), frozenset({"P1", "P2"}),
          "until's pace between looks and its bound at the tier's guard, which only ends a hang"),
    Named("until", "tests/until.py", ("",), frozenset({"P3"}),
          "until's PACE and SLOW, the paces between looks"),
    Named("doubles", "tests/time_doubles.py",
          ("_Double._hang_left", "FakeTime._poll", "FakeTime.sleep", "WatchedCondition.wait_for",
           "ControlledEvent.wait", "_Double._until", "FakeTime._park", "_Double.wait_waiting",
           "_Double._expire", "RecordingLock.acquire", "RecordingLock.wait_blocked"),
          frozenset({"P1", "P2", "P3"}),
          "the doubles' hang bound, which only turns a frozen wait into DoubleHung, and their "
          "wait records, whose parameters are named wait"),
    Named("doubles", "tests/time_doubles.py", ("",), frozenset({"P3"}),
          "the doubles' POLL_CAP and POLL_GAP"),
    Named("hang guard", "tests/hang_guard.py", ("tier_guard", "runs_out", "pace", "mark"),
          frozenset({"P3", "P4"}),
          "the tier's guard, runs_out and pace, which set the name a test gives them, and the "
          "container tests' guard given to each of them"),
    Named("hang guard", "tests/conftest.py",
          ("pytest_timeout_set_timer", "pytest_collection_modifyitems"),
          frozenset({"P2", "P3"}),
          "the hang guard's timer, and the container tests' guard read from pytest's ini"),
    Named("guard tests", "tests/test_time_doubles.py",
          ("frozen_waits",
           "test_a_polling_loop_that_no_advance_reaches_raises_double_hung_at_the_bound"),
          frozenset({"P3"}),
          "the short hang bounds given to the doubles whose ending of a hang the test asserts"),
    Named("guard tests", "tests/test_until.py",
          ("test_a_condition_that_never_holds_fails_at_the_tiers_guard_with_what_it_last_gave",
           "test_a_container_tests_tier_guard_is_the_container_hang_guard",
           "test_runs_out_puts_a_deadline_at_the_floor_the_product_takes",
           "test_runs_out_refuses_a_deadline_that_is_not_shortened"),
          frozenset({"P4"}),
          "until's, tier_guard's and runs_out's own tests: a guard of 0 that until has run out "
          "against, the container tier read from the registry, and runs_out given a floor and "
          "a deadline it refuses"),
    Named("guard tests", "tests/test_until.py",
          ("test_a_container_tests_tier_guard_is_the_container_hang_guard",
           "test_a_program_the_tests_start_takes_the_guard_it_is_handed",
           "test_a_tests_own_tier_comes_before_a_guard_handed_by_a_fixture"),
          frozenset({"TIER"}),
          "tier_guard's own tests, in a host test: that it gives the container tier's guard "
          "where the container tier asks"),
    Named("guard tests", "tests/test_hang_guard.py",
          ("test_each_tiers_hang_guard_is_outlived_by_its_guard_whose_method_is_thread",),
          frozenset({"TIER"}), "the hang guard's own test of each tier's value, in a host test"),
    Named("recorder's watch", "devtools/tmuxctl.py",
          ("", "Pane.wait_for", "Pane.wait_exit", "Pane.wait_stable", "Pane.close"),
          frozenset({"P1", "P2", "P3"}),
          "tmuxctl's look at the screen every POLL_SECONDS, and its give-up at the timeout a "
          "caller passes: RealWatch's, or a test's, which passes its tier's guard, which only "
          "ends a hang, or 0 (rule (a))"),
    Named("recorder's watch", "devtools/record_claude.py", ("RealWatch",),
          frozenset({"P1", "P2", "P3"}),
          "the recorder's command line's watch, against the real claude with a person at hand: "
          "no test reaches it but a module marked real_claude, which is run by hand against the "
          "real claude as the command line is, and is exempt (test_record_claude.py's "
          "test_no_test_reaches_the_command_lines_watch)"),
    Named("killed run", "tests/killed_runs.py", ("hold", "shell_hold"),
          frozenset({"P1", "P2", "P2s"}),
          "a killed run's own hang bound: hold() and shell_hold() give up at the deadline the run "
          "is handed, the tier's guard from its start, which only ends a run nothing killed or "
          "released"),
    Named("held folders", "devtools/held_folders.py", ("", "_stop_server", "unheld", "_clear"),
          frozenset({"P1", "P2", "P3"}),
          "the sweep of held folders: UNLOCKED_FOR and TMUX_GONE_WITHIN are its behaviour, and "
          "decide no test"),
)  # fmt: skip


def named_problems(sites: list[Site], named: Iterable[Named] = NAMED) -> list[str]:
    """Stale checks: each named pattern's subject exists and holds a site it covers."""
    problems = []
    for n in named:
        if not (REPO / n.path).exists():
            problems.append(f"{n.key}: {n.path} is gone")
        elif not any(n.covers(s) for s in sites):
            problems.append(f"{n.key}: {n.path} {n.where} holds no site of {sorted(n.forms)}")
    return problems


def stand_in_problems(files: Iterable[str] | None = None) -> list[str]:
    """Stale checks of STAND_INS: each exists, is a program (``__main__`` or a ``main``), and
    its file is named in a string of a scanned file other than this guard's own, as an argv or
    a path."""
    problems = []
    texts = {
        f: (REPO / f).read_text(encoding="utf-8")
        for f in (scoped_files() if files is None else files)
        if f != GUARD_FILE
    }
    for path in STAND_INS:
        full = REPO / path
        if not full.exists():
            problems.append(f"{path} is gone")
            continue
        text = full.read_text(encoding="utf-8")
        if "__main__" not in text and not re.search(r"^def main\(", text, re.M):
            problems.append(f"{path} is no program")
        named = re.compile(rf"""["'][^"'\n]*{re.escape(Path(path).name)}["']""")
        if not any(named.search(t) for t in texts.values()):
            problems.append(f"{path} is named in the string of no scanned file")
    return problems


def unexempted(sites: list[Site]) -> list[Site]:
    return [s for s in sites if not any(n.covers(s) for n in NAMED)]


# --- the planted twins ---------------------------------------------------------------------------


def placed(source: str) -> str:
    """A planted source with its placeholders put in place: this module writes $SLEEP, $TIMEOUT,
    $DATE and $LT for the words, so that its own strings hold no planted form."""
    for placeholder, word in (("$SLEEP", "sleep"), ("$TIMEOUT", "timeout"), ("$DATE", "date"),
                              ("$LT", "-lt")):  # fmt: skip
        source = source.replace(placeholder, word)
    return source


TABLE_DEFS = (
    "def gone(path, within):\n    pass\n"
    "class S:\n    def recording_held(self, seconds):\n        pass\n"
    "def run(argv, timeout):\n    pass\n"
    "def run(cmd, env, wait_timeout=None):\n    pass\n"
    "class FakeTime:\n    def advance(self, seconds):\n        pass\n"
    "def held(path, seconds):\n    pass\n"
    "def go(argv, seconds):\n    pass\n"
    "def go(seconds, argv):\n    pass\n"
    "def handed(guard):\n    pass\n"
    "def started(guard, prefix=None):\n    pass\n"
)


def planted_table() -> Table:
    return build_table([ast.parse(TABLE_DEFS)])


# A module the planted twins import their constants from.
PLANTED_MODULES = {"tests.planted_constants": ast.parse("SHORT = 0.5\nNAME = 'x'\n")}


def flagged(source: str, table: Table | None = None, path: str = "tests/test_planted.py"):
    scan = scan_source(
        path, placed(source), planted_table() if table is None else table, PLANTED_MODULES
    )
    return [(s.form, s.line) for s in scan.sites]


def fn(body: str, params: str = "t") -> str:
    """A test whose body is ``body``, indented."""
    return f"def test_x({params}):\n" + "".join(f"    {line}\n" for line in body.splitlines())


# name: (form, source, line, the branch that flags it)
PLANTED = {
    # P1
    "a deadline loop": ("P1", "import time\ndeadline = time.monotonic() + 5\n"
                        "while time.monotonic() < deadline:\n    pass\n", 3, "P1 branch"),
    "a difference stored": ("P1", "import time\nstarted = time.perf_counter()\n"
                            "took = time.perf_counter() - started\n", 3, "P1 branch"),
    "time.time": ("P1", "import time\ndef f(t):\n    return time.time() > t\n", 3, "P1 branch"),
    "monotonic_ns": ("P1", "import time\ndef f(t):\n    return time.monotonic_ns() > t\n", 3,
                     "P1 branch"),
    "perf_counter_ns": ("P1", "import time\ndef f(t):\n    return time.perf_counter_ns() > t\n",
                        3, "P1 branch"),
    "time_ns": ("P1", "import time\ndef f(t):\n    return time.time_ns() > t\n", 3, "P1 branch"),
    "datetime.now compared": ("P1", "from datetime import datetime as dt\n"
                              "def f(t):\n    return dt.now() > t\n", 3, "P1 branch"),
    "datetime.now with a zone": ("P1", "import datetime\ndef f(t):\n"
                                 "    return datetime.datetime.now(datetime.UTC) > t\n", 3,
                                 "P1 branch"),
    "utcnow": ("P1", "import datetime\ndef f(t):\n    return datetime.datetime.utcnow() > t\n",
               3, "P1 branch"),
    "date.today": ("P1", "from datetime import date\ndef f(t):\n    return date.today() > t\n",
                   3, "P1 branch"),
    "an aliased clock": ("P1", "import time as t\ndef f(x):\n    return t.monotonic() > x\n", 3,
                         "P1 branch"),
    "loop.time": ("P1", "def f(loop, end):\n    return loop.time() < end\n", 2, "P1 branch"),
    "a clock through max": ("P1", "import time\ndef f(a):\n    late = max(time.monotonic(), a)\n"
                            "    return late > 3\n", 4, "P1 branch"),
    # P2
    "time.sleep": ("P2", "import time\ntime.$SLEEP(1)\n", 2, "P2 branch"),
    "an aliased sleep": ("P2", "from time import $SLEEP as nap\nnap(0.5)\n", 2, "P2 branch"),
    "asyncio.sleep": ("P2", "import asyncio\nasync def f():\n    await asyncio.$SLEEP(0.1)\n", 3,
                      "P2 branch"),
    "anyio.sleep": ("P2", "import anyio\nasync def f():\n    await anyio.$SLEEP(1)\n", 3,
                    "P2 branch"),
    "trio.sleep": ("P2", "import trio\nasync def f():\n    await trio.$SLEEP(1)\n", 3, "P2 branch"),
    "a Timer": ("P2", "import threading\nthreading.Timer(1.0, print).start()\n", 2, "P2 branch"),
    "call_later": ("P2", "def f(loop):\n    loop.call_later(2, print)\n", 2, "P2 branch"),
    "call_at": ("P2", "def f(loop):\n    loop.call_at(2, print)\n", 2, "P2 branch"),
    # P2s
    "a shell sleep": ("P2s", "argv = ['sh', '-c', '$SLEEP 3; echo done']\n", 1, "P2s branch"),
    "a shell sleep of .5": ("P2s", "argv = ['sh', '-c', '$SLEEP .5']\n", 1, "P2s branch"),
    "a timeout tool": ("P2s", "argv = ['sh', '-c', '$TIMEOUT -k 1 5 cat']\n", 1, "P2s branch"),
    "a timeout with --signal": ("P2s", "s = '$TIMEOUT --signal=KILL 5 cat'\n", 1, "P2s branch"),
    "a timeout with -sKILL": ("P2s", "s = '$TIMEOUT -sKILL 5 cat'\n", 1, "P2s branch"),
    "a Python sleep in a string": ("P2s", "code = 'import time; time.$SLEEP(2)'\n", 1,
                                   "P2s branch"),
    "an interpolated sleep": ("P2s", "n = 3\nscript = f'$SLEEP {n}; exit 0'\n", 2, "P2s branch"),
    "a child's loop on a clock": ("P1", "s = 'while [ $($DATE +%s) $LT 9 ]; do :; done'\n", 1,
                                  "P2s branch"),
    "a child's loop that sleeps on a count": (
        "P1", "s = 'while [ $n $LT 9 ]; do $SLEEP 1; n=$((n+1)); done'\n", 1, "P2s branch"),
    "a timeout argv": ("P2s", "argv = ['timeout', '-s', 'KILL', '30', 'sh']\n", 1, "P2s argv"),
    "a timeout argv holding a name": ("P2s", "def f(n):\n    return ['timeout', n, 'cat']\n", 2,
                                      "P2s argv"),
    "a timeout argv of an f-string": ("P2s", "def f(n):\n    return ['timeout', f'{n}', 'cat']\n",
                                      2, "P2s argv"),
    "a timeout argv of str()": ("P2s", "def f():\n    return ['timeout', str(5), 'cat']\n", 2,
                                "P2s argv"),
    "the timeout tool through its argv": (
        "P2s", "def f(layout, rt):\n    t = [*layout.tool('timeout'), '-s', 'TERM']\n"
        "    rt.exec([*t, '5', 'sh'])\n", 3, "P2s argv"),
    "the timeout tool's -k": (
        "P2s", "def f(layout):\n    return [*layout.tool('timeout'), '-k', '1']\n", 2, "P2s argv"),
    # P3 keywords
    **{
        f"a {kw} keyword": ("P3", f"def f(g):\n    g({kw}=2.5)\n", 2, "P3 keywords")
        for kw in ("timeout", "within", "deadline", "seconds", "wait", "quiet", "interval",
                   "delay", "every", "grace", "hold_timeout", "hold_seconds", "hold_within",
                   "poll_interval", "start_delay", "max_wait", "stop_grace", "hang_after",
                   "guard")
    },
    # P3 positionals
    "a join": ("P3", "def f(t):\n    t.join(5)\n", 2, "P3 positional join"),
    "a wait": ("P3", "def f(e):\n    e.wait(0.5)\n", 2, "P3 positional wait"),
    "a wait_for": ("P3", "def f(c, p):\n    c.wait_for(p, 3)\n", 2, "P3 positional wait_for"),
    "a communicate": ("P3", "def f(p):\n    p.communicate(None, 30)\n", 2,
                      "P3 positional communicate"),
    "a queue get": ("P3", "def f(queue):\n    queue.get(True, 2)\n", 2, "P3 positional get"),
    "an inbox get": ("P3", "def f(inbox):\n    inbox.get(True, 5)\n", 2, "P3 positional get"),
    "a settimeout": ("P3", "def f(s):\n    s.settimeout(5)\n", 2, "P3 positional settimeout"),
    "an acquire": ("P3", "def f(lock):\n    lock.acquire(True, 5)\n", 2,
                   "P3 positional acquire"),
    "a result": ("P3", "def f(future):\n    future.result(5)\n", 2, "P3 positional result"),
    "a poll": ("P3", "def f(poller):\n    poller.poll(100)\n", 2, "P3 positional poll"),
    "a selector's select": ("P3", "def f(selector):\n    selector.select(1)\n", 2,
                            "P3 positional select"),
    "select.select": ("P3", "import select\nselect.select([], [], [], 2)\n", 2,
                      "P3 positional select.select"),
    "setdefaulttimeout": ("P3", "import socket\nsocket.setdefaulttimeout(5)\n", 2,
                          "P3 positional socket.setdefaulttimeout"),
    "create_connection": ("P3", "import socket\nsocket.create_connection(('h', 1), 5)\n", 2,
                          "P3 positional socket.create_connection"),
    "alarm": ("P3", "import signal\nsignal.alarm(5)\n", 2, "P3 positional signal.alarm"),
    "fail_after": ("P3", "import anyio\nanyio.fail_after(5)\n", 2,
                   "P3 positional anyio.fail_after"),
    "move_on_after": ("P3", "import trio\ntrio.move_on_after(5)\n", 2,
                      "P3 positional trio.move_on_after"),
    "urlopen": ("P3", "import urllib.request\nurllib.request.urlopen('u', None, 5)\n", 2,
                "P3 positional urllib.request.urlopen"),
    "asyncio.wait_for": ("P3", "import asyncio\nasync def f(c):\n"
                         "    await asyncio.wait_for(c, 5)\n",
                         3, "P3 positional asyncio.wait_for"),
    "a local name shadowing an import": (
        "P3", "from tests.other import restoring\n" + fn("restoring = make()\nrestoring.join(3)"),
        4, "P3 positional join"),
    "a positional constant": ("P3", "SHORT = 0.5\ndef test_x(t):\n    t.join(SHORT)\n", 3,
                              "P3 positional join"),
    # P3 values that are not checked where they are given
    "a fixture's value": ("P3", "import pytest\n@pytest.fixture\ndef short():\n    return 0.5\n"
                          "def test_x(short, t):\n    t.join(timeout=short)\n", 6, "P3 keywords"),
    "a lambda's parameter": ("P3", "f = lambda t, n: t.join(timeout=n)\n", 1, "P3 keywords"),
    "a local constant": ("P3", fn("TIMEOUT = 0.5\nt.join(timeout=TIMEOUT)"), 3, "P3 keywords"),
    "a tuple constant": ("P3", "A_TIMEOUT, B = 0.5, 1\ndef test_x(t):\n"
                         "    t.join(timeout=A_TIMEOUT)\n", 1, "P3 constants"),
    "a constant built from a call": ("P3", "WAIT_SECONDS = float('0.5')\n", 1, "P3 constants"),
    "a rebound constant": ("P3", "WAIT = 1\nWAIT = 2\ndef test_x(t):\n    t.join(timeout=WAIT)\n",
                           4, "P3 keywords"),
    "a product's value in a test": ("P3", "from rcman import channel\n"
                                    + fn("t.join(channel.REQUEST_TIMEOUT)"), 3,
                                    "P3 positional join"),
    "a helper's parameter given a number": (
        "P3", "def helper(t, n=None):\n    t.join(timeout=n)\ndef test_x(t):\n    helper(t, 0.5)\n",
        4, "P3 table"),
    "a helper's default that is not checked by name": (
        "P3", "def helper(t, n=0.5):\n    t.join(timeout=n)\n", 2, "P3 keywords"),
    # P3 table and defaults
    "a table position": ("P3", fn("gone(p, 60)", "p"), 2, "P3 table"),
    "a table position of a method": ("P3", fn("s.recording_held(5)", "s"), 2, "P3 table"),
    "a table position given a constant": ("P3", "X = 5\n" + fn("held(1, X)"), 3, "P3 table"),
    "a disagreeing name at the union": ("P3", fn("go(1, 60)\ngo(60, 1)"), 2, "P3 table"),
    "a disagreeing name, its other position": ("P3", fn("go(1, 60)\ngo(60, 1)"), 3, "P3 table"),
    "a disagreeing POSITIONAL name": ("P3", fn("world.run(['a'], 10)", "world"), 2, "P3 table"),
    "a parameter default": ("P3", "def f(x, timeout=30):\n    pass\n", 1, "P3 defaults"),
    "a product's default outside a stand-in": (
        "P3", "from rcman import runtime as rt\ndef f(g, timeout=rt.PODMAN_TIMEOUT):\n    pass\n",
        2, "P3 defaults"),
    "tier_guard in a default": ("P3", "from tests.hang_guard import tier_guard\n"
                                "def f(within=tier_guard()):\n    pass\n", 2, "P3 defaults"),
    "a constant": ("P3", "ANSWERS_WITHIN = 15.0\n", 1, "P3 constants"),
    "a constant of a plural timing word": ("P3", "STOP_INTERVALS = 0.5\n", 1, "P3 constants"),
    "a class constant": ("P3", "class C:\n    POLL_SECONDS = 0.1\n", 2, "P3 constants"),
    "tier_guard at module level": ("P3", "from tests.hang_guard import tier_guard\n"
                                   "WAIT_SECONDS = tier_guard()\n", 2, "P3 constants"),
    "pytest.mark.timeout": ("P3", "import pytest\n@pytest.mark.timeout(5)\ndef test_x():\n"
                            "    pass\n", 2, "P3 mark"),
    "a guard under 1": ("P3", "from tests.hang_guard import HANG_GUARD\n"
                        "def f(g):\n    g(timeout=HANG_GUARD / 2)\n", 3, "P3 keywords"),
    # P4
    "a monkeypatched deadline": ("P4", fn("monkeypatch.setattr(mod, 'STOP_TIMEOUT', 0.1)",
                                          "monkeypatch, mod"), 2, "P4 branch"),
    "patched.setattr": ("P4", "def test_x(monkeypatch, mod):\n"
                        "    with monkeypatch.context() as patched:\n"
                        "        patched.setattr(mod, 'STOP_TIMEOUT', 0.1)\n", 3, "P4 branch"),
    "the builtin setattr": ("P4", fn("setattr(mod, 'STOP_TIMEOUT', 0.1)", "mod"), 2, "P4 branch"),
    "a string target": ("P4", fn("monkeypatch.setattr('rcman.x.GRACE_SECONDS', 0.5)",
                                 "monkeypatch"), 2, "P4 branch"),
    "mock.patch": ("P4", "from unittest import mock\n"
                   "def test_x():\n    with mock.patch('rcman.x.POLL_SECONDS', 0.01):\n"
                   "        pass\n", 3, "P4 branch"),
    "mock.patch.object": ("P4", "from unittest import mock\ndef test_x(m):\n"
                          "    with mock.patch.object(m, 'IDLE_SECONDS', 1):\n        pass\n", 3,
                          "P4 branch"),
    "a plain assignment": ("P4", fn("mod.STOP_TIMEOUT = 0.2", "mod"), 2, "P4 branch"),
    "a product's value shortening another": (
        "P4", "from rcman import forward\n"
        + fn("monkeypatch.setattr(forward, 'IDLE_SECONDS', forward.PACE_SECONDS)", "monkeypatch"),
        3, "P4 branch"),
    "a loop-variable name": ("P4", "def test_x(monkeypatch, m):\n"
                             "    for name in ('IDLE_SECONDS', 'HEAD_SECONDS'):\n"
                             "        monkeypatch.setattr(m, name, 0.3)\n", 3, "P4 branch"),
    "an unresolved name": ("P4", fn("monkeypatch.setattr(m, name, 0.3)", "monkeypatch, m, name"),
                           2, "P4 branch"),
    "a name a loop unpacks": ("P4", "def test_x(monkeypatch, m, n):\n"
                              "    for module, name in ((m, 'HEAD_SECONDS'), (n, 'IDLE')):\n"
                              "        monkeypatch.setattr(module, name, 0.3)\n", 3, "P4 branch"),
    "a parametrized name": ("P4", "import pytest\n"
                            "@pytest.mark.parametrize('name', ['HEAD_SECONDS', 'IDLE'])\n"
                            "def test_x(monkeypatch, m, name):\n"
                            "    monkeypatch.setattr(m, name, 0.3)\n", 4, "P4 branch"),
    "a patch of time itself": ("P4", "import time\ndef test_x(monkeypatch):\n"
                               "    monkeypatch.setattr(time, '$SLEEP', print)\n", 3,
                               "P4 branch"),
    "a patch of time through a product": (
        "P4", "from rcman import workspaces\n"
        + fn("monkeypatch.setattr(workspaces.time, 'monotonic', abs)", "monkeypatch"), 3,
        "P4 branch"),
    "a patch of the guard": ("P4", "from tests import hang_guard\n"
                             + fn("monkeypatch.setattr(hang_guard, 'HANG_GUARD', 0.01)",
                                  "monkeypatch"), 3, "P4 branch"),
    "a restoring setattr": ("P4", "def test_x(monkeypatch, m):\n"
                            "    monkeypatch.setattr(m, 'WAIT_SECONDS', 0)\n"
                            "    monkeypatch.setattr(m, 'WAIT_SECONDS', 0)\n", 3, "P4 branch"),
    "runs_out at a number": ("P4", "from tests.hang_guard import runs_out\n"
                             + fn("runs_out(monkeypatch, m, 'X_SECONDS', at=0.5)",
                                  "monkeypatch, m"), 3, "P4 runs_out"),
    # the review's second round
    "a module's helper called through its module": (
        "P3", "def settle(t, n=None):\n    t.join(timeout=n)\nfrom tests import helpers_x\n"
        "def test_x(t):\n    helpers_x.settle(t, 0.5)\n", 5, "P3 flow through a module"),
    "a method given a name bound to a number": (
        "P3", "class W:\n    def ready(self, t, s=None):\n        t.join(timeout=s)\n"
        "def test_x(t, w):\n    s = 0.5\n    w.ready(t, s)\n", 6, "P3 every value"),
    "a method given a fixture": (
        "P3", "import pytest\nclass W:\n    def ready(self, t, s=None):\n"
        "        t.join(timeout=s)\n@pytest.fixture\ndef short():\n    return 0.5\n"
        "def test_x(t, short):\n    W().ready(t, short)\n", 9, "P3 every value"),
    "a disagreeing name given a constant": ("P3", "SHORT = 0.5\n" + fn("go('a', SHORT)"), 3,
                                            "P3 every value"),
    "a constant rebound through global": (
        "P3", "WAIT = 0\ndef bump():\n    global WAIT\n    WAIT = 5\n"
        "def test_x(t):\n    t.join(timeout=WAIT)\n", 6, "P3 global"),
    "functools.partial": (
        "P3", "import functools\ndef helper(t, n=None):\n    t.join(timeout=n)\n"
        "def test_x(t):\n    functools.partial(helper, n=0.5)\n", 5, "P3 partial"),
    "a keyword and a flow in one call": (
        "P3", "def helper(t, n=None, timeout=None):\n    t.join(timeout=n)\n"
        "def test_x(t):\n    helper(t, 0.5, timeout=3)\n", 4, "P3 one site"),
    "an alias of the guard": ("P3", "from tests.hang_guard import HANG_GUARD\n"
                              "ANSWERS = HANG_GUARD\n", 2, "P3 alias"),
    "a product's constant as a stand-in's default": (
        "P3", "from rcman import runtime as rt\n"
        "class Stand:\n    def exec(self, argv, timeout=rt.PODMAN_TIMEOUT):\n        pass\n",
        3, "P3 defaults"),
    "runs_out at a product's constant": (
        "P4", "from rcman import repository\nfrom tests.hang_guard import runs_out\n"
        + fn("runs_out(monkeypatch, m, 'X_SECONDS', at=repository.X_SECONDS)", "monkeypatch, m"),
        4, "P4 runs_out"),
    # the review's third round: a name at a positional slot is seconds unless shown otherwise
    "a helper's positional parameter given a number": (
        "P3", "def helper(t, n):\n    t.join(n)\ndef test_x(t):\n    helper(t, 0.5)\n", 4,
        "P3 positional name"),
    "a method's positional parameter given a number": (
        "P3", "class W:\n    def ready(self, t, n):\n        t.join(n)\n"
        "def test_x(w, t):\n    w.ready(t, 0.5)\n", 5, "P3 positional name"),
    "a fixture at a positional slot": ("P3", fn("e.wait(short)", "e, short"), 2,
                                       "P3 positional name"),
    "a name bound from a call at a positional slot": ("P3", fn("s = f()\ne.wait(s)", "e, f"), 3,
                                                      "P3 positional name"),
    "a call at a positional slot": ("P3", fn("e.wait(f())", "e, f"), 2, "P3 positional name"),
    "a disagreeing name given an imported number": (
        "P3", "from tests.planted_constants import SHORT\n" + fn("go('a', SHORT)"), 3,
        "P3 seconds through an import"),
    # the review's fourth round
    "a helper's parameter at a disagreeing slot given a number": (
        "P3", "def helper(p, n):\n    go(p, n)\ndef test_x(p):\n    helper(p, 0.5)\n", 4,
        "P3 disagreeing parameter"),
    "a helper's parameter at a table position given a number": (
        "P3", "def helper(p, n):\n    held(p, n)\ndef test_x(p):\n    helper(p, 0.5)\n", 4,
        "P3 table position flow"),
    "a parameter of a helper that builds a double": (
        "P3", "from tests.time_doubles import FakeTime\ndef looping(t, timeout=None):\n"
        "    t.join(timeout=timeout)\n    return FakeTime()\n"
        "def test_x(t):\n    looping(t, timeout=0.5)\n", 6, "P3 flow past a double's helper"),
    "a guard handed short": ("P3", "from tests.hang_guard import handed\n"
                             + fn("with handed(0.5):\n    pass"), 3, "P3 table"),
    "an engine started with a short guard": (
        "P3", "from tests import podman_service as ps\n" + fn("with ps.started(0.5):\n    pass"),
        3, "P3 table"),
    "a typed double given a real event": (
        "P3", "import threading\nfrom tests.time_doubles import ControlledEvent\n"
        "def waited(e: ControlledEvent):\n    assert e.wait(1)\n"
        "def test_x():\n    waited(threading.Event())\n", 4, "P3 typed double given"),
    "a typed double no call is seen to give": (
        "P3", "import threading\nfrom tests.time_doubles import ControlledEvent\n"
        "def waited(e: ControlledEvent):\n    assert e.wait(1)\n"
        "def test_x():\n    threading.Thread(target=waited, args=(threading.Event(),))\n", 4,
        "P3 typed double given"),
    "a sleep argv": ("P2s", "import subprocess\nsubprocess.Popen(['$SLEEP', '5'])\n", 2,
                     "P2s sleep argv"),
    "a sleep argv by its path": ("P2s", "argv = ['/bin/$SLEEP', '60']\n", 1, "P2s sleep argv"),
    "a sleep in a call's arguments": (
        "P2s", "def f(engine):\n    engine.cli('run', 'bb', 'busybox', '$SLEEP', '600')\n", 2,
        "P2s sleep argv"),
    "a sleep argv of an f-string": ("P2s", "def f(n):\n    return ['$SLEEP', f'{n}']\n", 2,
                                    "P2s sleep argv"),
    "a sleep argv of a name bound to a number": (
        "P2s", "LENGTH = '600'\ndef f():\n    return ['$SLEEP', LENGTH]\n", 3, "P2s sleep argv"),
    "a sleep argv in a child's Python": (
        "P2s", "code = \"import os; os.execv('/bin/$SLEEP', ['$SLEEP', '600'])\"\n", 1,
        "P2s sleep argv"),
    "a sleep argv split over lines in a child's Python": (
        "P2s", "code = (\"run('busybox', '$SLEEP',\\n\"\n        \"    '600')\\n\")\n", 1,
        "P2s sleep argv"),
    "a seconds constant written as a string": ("P3", "POLL_SECONDS = '0.01'\n", 1,
                                               "P3 numeric words"),
    "a timing name run together": ("P3", "IDLETIMEOUT = 0.5\n", 1, "P3 constants"),
    "a timing name run together after a word": ("P3", "MAXWAIT = 2\n", 1, "P3 constants"),
    "a loop's name assigned again": (
        "P4", "def test_x(monkeypatch, m):\n    for name in ('A',):\n"
        "        name = 'POLL_SECONDS'\n        monkeypatch.setattr(m, name, 0.01)\n", 4,
        "P4 reassigned"),
    "a parametrized name assigned again": (
        "P4", "import pytest\n@pytest.mark.parametrize('name', ['A'])\n"
        "def test_x(monkeypatch, m, name):\n    name = name + '_SECONDS'\n"
        "    monkeypatch.setattr(m, name, 0.01)\n", 5, "P4 reassigned"),
    "a closure's name given a timing literal": (
        "P4", "def into(monkeypatch, name, value):\n    def init(self):\n"
        "        setattr(self, name, value)\n    monkeypatch.setattr(C, 'init', init)\n"
        "def test_x(monkeypatch):\n    into(monkeypatch, 'POLL_SECONDS', 0.5)\n", 3,
        "P4 branch"),
    "a sleep argv of a number through str()": (
        "P2s", "def f():\n    return ['$SLEEP', str(60)]\n", 2, "P2s sleep argv"),
    "a sleep argv of a name bound to a number through str()": (
        "P2s", "LENGTH = 60\ndef f():\n    return ['$SLEEP', str(LENGTH)]\n", 3, "P2s sleep argv"),
    "a sleep argv whose helper's caller gives a number": (
        "P2s", "def held(n):\n    return ['$SLEEP', n]\ndef test_x():\n    held('60')\n", 4,
        "P2s sleep argv parameter"),
    "a sleep argv of the guard once": (
        "P2s", "from tests.hang_guard import HANG_GUARD\n"
        "def f():\n    return ['$SLEEP', f'{HANG_GUARD:g}']\n", 3, "P2s twice the guard"),
    "a shell sleep of the guard once": (
        "P2s", "from tests.hang_guard import HANG_GUARD\n"
        "argv = ['sh', '-c', f'$SLEEP {HANG_GUARD:g}']\n", 2, "P2s twice the guard"),
    "a shell sleep of the guard and a bit": (
        "P2s", "from tests.hang_guard import HANG_GUARD\n"
        "argv = ['sh', '-c', f'$SLEEP {HANG_GUARD + 7:g}']\n", 2, "P2s twice the guard"),
    # the guard's own names
    "a local HANG_GUARD": ("GUARD", "HANG_GUARD = 0.5\n", 1, "guard names"),
}  # fmt: skip


CLEAN = {
    "the guard": ("guard", "from tests.hang_guard import HANG_GUARD\n"
                  "def f(g):\n    g(timeout=HANG_GUARD)\n"),
    "a multiple of the guard": ("guard", "from tests.hang_guard import HANG_GUARD\n"
                                "def f(g):\n    g(timeout=2 * HANG_GUARD)\n"),
    "the guard through its module": ("guard", "from tests import hang_guard\n"
                                     "def f(g):\n    g(timeout=hang_guard.HANG_GUARD)\n"),
    "tier_guard in a test": ("tier_guard", "from tests.hang_guard import tier_guard\n"
                             "def f(g):\n    g(timeout=tier_guard())\n"),
    "None": ("none", "def f(g):\n    g(timeout=None)\n"),
    "a name that holds a timing word within one of its own": (
        "structure", "MOST_RELAYS_PER_WORKSPACE = 1\ndef test_x(monkeypatch, mod):\n"
        "    monkeypatch.setattr(mod, 'MOST_RELAYS_PER_WORKSPACE', 100)\n"),
    "minus one": ("minus one", "def f(lock):\n    lock.acquire(True, -1)\n"),
    "zero": ("zero", "def f(e, g):\n    e.wait(0)\n    g(within=0)\n"),
    "a sleep of 0": ("turn", "import asyncio\nasync def f():\n"
                          "    await asyncio.$SLEEP(0)\n"),
    "a helper's parameter passed on": (
        "parameter", "from tests.hang_guard import HANG_GUARD\n"
        "def helper(g, timeout=None):\n    g(timeout=timeout)\n"
        "def test_x(g):\n    helper(g, HANG_GUARD)\n"),
    "a constant of zero": ("constant", "NO_WAIT = 0\ndef test_x(t):\n"
                           "    t.join(timeout=NO_WAIT)\n"),
    "a double in a closure": (
        "double in a closure", "from tests.time_doubles import FakeTime\n"
        "def test_x():\n    fake = FakeTime()\n    def loop():\n        fake.advance(5)\n"
        "    loop()\n"),
    "a join on an id": ("positional not seconds", "def f(threads, first):\n"
                        "    threads.join(first.id)\n"),
    "a method given a string": ("not seconds", "class W:\n    def ready(self, t, s=None):\n"
                                "        t.join(timeout=s)\ndef test_x(t, w):\n"
                                "    w.ready(t, 'x')\n"),
    "str.join": ("structure", "def f(parts, sep):\n    return sep.join(p for p in parts)\n"),
    "a join on chr(10)": ("structure", "def f(screen):\n    last = screen()\n"
                          "    return chr(10).join(last)\n"),
    "dict.get": ("structure", "def f(d):\n    return d.get('k', 5)\n"),
    "os.path.join": ("structure", "import os\ndef f(a):\n    return os.path.join(a, 'b')\n"),
    "a fake's sleep and advance": (
        "double", "from tests.time_doubles import FakeTime\n"
        "def test_x():\n    fake = FakeTime()\n    fake.advance(600)\n    fake.$SLEEP(5)\n"),
    "a fixture's double": (
        "double", "import pytest\nfrom tests.time_doubles import FakeTime\n"
        "@pytest.fixture\ndef times():\n    return FakeTime()\n"
        "def test_x(times):\n    times.advance(600)\n"),
    "a constant of a module whose time is a double": (
        "double", "from tests.time_doubles import FakeTime\n"
        "def test_x(monkeypatch, mod):\n    fake = FakeTime()\n"
        "    monkeypatch.setattr(mod, 'time', fake)\n"
        "    monkeypatch.setattr(mod, 'STOP_TIMEOUT', 5)\n"),
    "a non-timing setattr": ("structure", "def test_x(monkeypatch, m):\n"
                             "    monkeypatch.setattr(m, 'NAME', 'x')\n"),
    "a resolved non-timing name": ("structure", "def test_x(monkeypatch, m):\n"
                                   "    for name in ('_changed', '_closing'):\n"
                                   "        monkeypatch.setattr(m, name, None)\n"),
    "a non-timing name a loop unpacks": (
        "structure", "def test_x(monkeypatch, m, n):\n"
        "    for module, name in ((m, 'unlink'), (n, 'rmdir')):\n"
        "        monkeypatch.setattr(module, name, None)\n"),
    "a non-timing parametrized name": (
        "structure", "import pytest\n@pytest.mark.parametrize('table', ['NODE_SHA256', 'UV'])\n"
        "def test_x(monkeypatch, m, table):\n    monkeypatch.setattr(m, table, {})\n"),
    "a child's wait loop": ("child loop",
                                  "s = 'while [ ! -e f ]; do $SLEEP 0.05; done'\n"),
    "a child's endless loop": ("child loop", "s = 'while :; do $SLEEP 1; done'\n"),
    "a child's counted loop that does not sleep": (
        "structure", "s = 'while [ $i $LT 3 ]; do i=$((i+1)); done'\n"),
    "a child's Python loop": ("child loop",
                                    "code = 'import time\\nwhile True:\\n"
                                    "    time.$SLEEP(0.01)\\n'\n"),
    "a child's sleep at the guard (OUT)": ("guard", "from tests.hang_guard import HANG_GUARD\n"
                                           "s = f'$SLEEP {3 * HANG_GUARD}'\n"),
    "a child's timeout at RUNS_OUT": ("runs out", "from tests.hang_guard import RUNS_OUT\n"
                                      "s = f'$TIMEOUT {RUNS_OUT} cat'\n"),
    "a timeout argv at RUNS_OUT": ("runs out", "from tests.hang_guard import RUNS_OUT\n"
                                   "argv = ['timeout', '-k', f'{RUNS_OUT}', str(RUNS_OUT), "
                                   "'sh']\n"),
    "a benchmark": ("benchmark", "import time\nimport pytest\ndef measure():\n"
                          "    s = time.perf_counter()\n    return time.perf_counter() - s\n"
                          "@pytest.mark.benchmark\ndef test_b():\n    measure()\n"),
    "a clock read not compared": ("structure", "import time\ndef f():\n    return time.time()\n"),
    "a clock read inside a call": ("structure", "import time\ndef f(stamp):\n"
                                   "    label = stamp(time.time())\n    return label > 'a'\n"),
    "a clock's None compared": ("structure", "import time\ndef f(a):\n"
                                "    left = None if a else time.monotonic()\n"
                                "    return left is None\n"),
    "a tuple paired by element": ("structure", "import time\ndef f(n):\n"
                                  "    t, count = time.time(), n\n    return count > 3\n"),
    "runs_out at 0 and at a product's floor": (
        "structure", "from rcman import repository\nfrom tests.hang_guard import runs_out\n"
        "def test_x(monkeypatch, m):\n    runs_out(monkeypatch, m, 'X_SECONDS')\n"
        "    runs_out(monkeypatch, m, 'Y_SECONDS', at=repository.FLOOR_SECONDS)\n"),
    "a helper's parameter typed as a double": (
        "typed double", "from tests.time_doubles import FakeTime\n"
        "def stepping(fake: FakeTime):\n    def run():\n        fake.advance(5)\n"
        "    return run\n"
        "def test_x():\n    stepping(FakeTime())()\n    made = FakeTime()\n    stepping(made)\n"),
    "a docstring that names a sleep": ("structure", "def f():\n"
                                       "    '''Waits with $SLEEP 5 in the shell.'''\n"),
    # the review's third round
    "a constant whose name holds a timing word but whose value is words": (
        "not seconds", "WORKSPACE = 'workspace'\nINTERVALS = ['idle_minutes', 'absolute_hours']\n"),
    "a join of a name bound to a list": ("not seconds", "def f(sep):\n    parts = ['a']\n"
                                         "    return sep.join(parts)\n"),
    "what is not seconds at a disagreeing slot": (
        "seconds only at a disagreeing slot",
        "from tests.planted_constants import NAME\nfrom tests.people import BOB\n"
        + fn("go(BOB, tmp_path)\ngo(NAME, folder)\ngo(*args)\ngo(r.json(), url)",
             "tmp_path, folder, args, r, url")),
    "a helper's parameters at a disagreeing slot given what is not seconds": (
        "only seconds through a loose flow",
        "def helper(c, p):\n    go(c, p)\n" + fn("helper(client, tmp_path)", "client, tmp_path")),
    "a wait on a loop variable over names": (
        "loop variable not seconds",
        fn("for name in ('one', 'two'):\n    run.wait(name)\n"
           "found = [run.wait(n) for n in ('one', 'two')]", "run")),
    "a Clock's advance": ("double", "from tests.time_doubles import Clock\n"
                               "def test_x():\n    clock = Clock()\n    clock.advance(600)\n"),
    "a double's method as a value": (
        "a double's method", "from tests.time_doubles import Time\n"
        "def helper(m):\n    times = Time()\n    return m(clock=times.clock, wait=times.wait)\n"),
    "a sleep argv that outlasts the test": (
        "guard", "from tests.hang_guard import HANG_GUARD, tier_guard\n"
        "OUTLASTING = f'{3 * HANG_GUARD:g}'\nBUILD = f'{3 * HANG_GUARD + 7:g}'\n"
        "argv = ['$SLEEP', OUTLASTING]\nbuilt = f'RUN [\"busybox\", \"$SLEEP\", \"{BUILD}\"]'\n"
        "def test_x(engine):\n    engine.cli('run', 'bb', '$SLEEP', f'{3 * tier_guard():g}')\n"
        "    code = f\"os.execv('/bin/$SLEEP', ['$SLEEP', '{3 * HANG_GUARD:g}'])\"\n"),
    "a sleep argv whose helper's callers give what outlasts the test": (
        "guard", "from tests.hang_guard import tier_guard\n"
        "def held(n):\n    return ['$SLEEP', n]\n"
        "def test_x():\n    held(f'{2 * tier_guard():g}')\n    held('infinity')\n"),
    "a sleep argv of infinity, compared, or no argv": (
        "structure", "def f(d, s, run):\n    argv = ['$SLEEP', 'infinity']\n"
        "    assert run[-2:] == ['$SLEEP', '600']\n    d._record('$SLEEP', s)\n"),
    "a timeout argv at int() of the guard's multiple": (
        "guard", "import sys\nfrom tests.hang_guard import tier_guard\ndef f():\n"
        "    return ['timeout', '-s', 'KILL', str(int(2 * tier_guard())), sys.executable]\n"),
    "a closure's name given literals": (
        "structure", "def into(monkeypatch, name, double):\n    def init(self):\n"
        "        setattr(self, name, double)\n    monkeypatch.setattr(C, 'init', init)\n"
        "def test_x(monkeypatch):\n    into(monkeypatch, '_changed', 1)\n"),
    "a helper that builds a double given its intervals": (
        "a double's helper", "from tests.time_doubles import Time\n"
        "def looping(s, **changes):\n    times = Time()\n    return s, times\n"
        "def test_x(s):\n    looping(s, disk_interval_seconds=600)\n"),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_form_is_flagged_where_it_is(name):
    form, source, line, _branch = PLANTED[name]
    found = flagged(source)
    assert found.count((form, line)) == 1, (name, found)


@pytest.mark.parametrize("name", sorted(CLEAN))
def test_an_allowed_form_is_not_flagged(name):
    assert flagged(CLEAN[name][1]) == []


def test_the_parameter_table_records_agreeing_and_disagreeing_names():
    table = planted_table()
    assert table.positions == {
        "gone": frozenset({1}),
        "recording_held": frozenset({0}),
        "held": frozenset({1}),
        "advance": frozenset({0}),
        "handed": frozenset({0}),
        "started": frozenset({0}),
    }
    assert table.disagreeing == {"run": frozenset({1, 2}), "go": frozenset({0, 1})}


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("a fake's sleep and advance", "double"),
        ("a fixture's double", "double"),
        ("a sleep of 0", "turn"),
        ("a benchmark", "benchmark"),
        ("a child's wait loop", "child loop"),
    ],
)
def test_the_derived_allowances_are_recorded(name, key):
    """Recorded where they allow a site, for the real tree's non-vacuity checks."""
    scan = scan_source("t.py", placed(CLEAN[name][1]), planted_table())
    assert key in {k for k, _ in scan.derived}


@pytest.mark.parametrize("key", sorted({k for k, _ in CLEAN.values()} - {"structure"}))
def test_each_allowance_has_a_clean_twin(key):
    """Every allowance the scan knows is held by a CLEAN twin; the mutation run turns each off
    and sees its twin flagged."""
    assert key in ALLOWANCES


def test_every_allowance_of_the_scan_has_a_clean_twin():
    assert set(ALLOWANCES) <= {k for k, _ in CLEAN.values()}


def test_an_allowance_turned_off_flags_its_clean_twin(monkeypatch):
    """Planted twin of the allowances: the doubles' off, the fake's calls are sites."""
    monkeypatch.setitem(ALLOWANCES, "double", False)
    assert flagged(CLEAN["a fake's sleep and advance"][1]) != []


# --- the tier rule ------------------------------------------------------------------------------

USES = "def helper(g):\n    g(timeout={given})\n"
BOTH = ("container", "host")
TIERED = {
    # (the file, the tiers that reach helper, the guard it gives): whether it is a TIER site
    ("tests/container/test_c.py", None, "hang_guard.HANG_GUARD"): True,
    ("tests/container/test_c.py", None, "hang_guard.CONTAINER_HANG_GUARD"): False,
    ("tests/test_h.py", None, "hang_guard.CONTAINER_HANG_GUARD"): True,
    ("tests/test_h.py", None, "hang_guard.HANG_GUARD"): False,
    ("tests/shared.py", BOTH, "hang_guard.HANG_GUARD"): True,
    ("tests/shared.py", BOTH, "hang_guard.CONTAINER_HANG_GUARD"): True,
    ("tests/shared.py", BOTH, "hang_guard.tier_guard()"): False,
    ("tests/shared.py", ("container",), "hang_guard.HANG_GUARD"): True,
    ("tests/shared.py", ("container",), "3 * hang_guard.CONTAINER_HANG_GUARD"): False,
}


@pytest.mark.parametrize(("path", "tiers", "guard"), sorted(TIERED, key=str))
def test_a_guard_of_another_tier_is_flagged(path, tiers, guard):
    """Planted twins of the tier rule, and its CLEAN twins: each tier's own guard, and
    tier_guard() where both tiers run the code."""
    source = "from tests import hang_guard\n" + USES.format(given=guard)
    reached = {"helper": set(tiers)} if tiers else None
    found = [(s.form, s.line) for s in scan_source(path, source, Table(), None, reached).sites]
    assert found == ([("TIER", 3)] if TIERED[path, tiers, guard] else []), found


TREES = {
    "tests/shared.py": "def helper():\n    pass\ndef unused():\n    pass\n",
    "tests/container/test_c.py": "from tests.shared import helper\ndef test_c():\n    helper()\n",
    "tests/test_h.py": "from tests import shared\ndef test_h():\n    shared.helper()\n"
                       "def test_e(engine):\n    pass\n",
    "tests/conftest.py": "import pytest\n@pytest.fixture\ndef engine(podman_service):\n"
                         "    return GIVES\nGIVES = 1\n",
}  # fmt: skip


def test_the_tiers_are_reached_through_imports_and_fixtures():
    """Each tier's roots: all of tests/container/, and a test elsewhere by whether it asks for
    the engine, through a fixture of conftest's here; what each reaches through an import, a
    module's attribute and a fixture; and a name nothing reaches is in no tier."""
    reached = tier_reach({p: ast.parse(s) for p, s in TREES.items()})
    assert reached["tests/shared.py"] == {"helper": {"host", "container"}}
    assert reached["tests/test_h.py"] == {"test_h": {"host"}, "test_e": {"container"}}
    assert reached["tests/conftest.py"] == {"engine": {"container"}, "GIVES": {"container"}}


# --- named patterns: clean twins and stale checks ------------------------------------------------


@pytest.mark.parametrize("named", NAMED, ids=lambda n: f"{n.key}-{n.where[0] or 'module'}")
def test_a_named_pattern_covers_only_its_subject(named):
    """CLEAN twin of each named pattern: a site of its form in its subject is covered; the same
    site in another file, or of another form, is not."""
    site = Site(named.path, 1, sorted(named.forms)[0], "planted", named.where[0])
    assert named.covers(site)
    assert not named.covers(Site("tests/elsewhere.py", 1, site.form, "planted", site.where))
    assert not named.covers(Site(named.path, 1, "GUARD", "planted", site.where))


def test_a_named_pattern_whose_subject_is_gone_is_reported():
    gone = Named("until", "tests/until.py", ("no_such_function",), frozenset({"P2"}), "planted")
    assert named_problems([], [gone]) == [
        "until: tests/until.py ('no_such_function',) holds no site of ['P2']"
    ]
    missing = Named("until", "tests/no_such.py", ("",), frozenset({"P2"}), "planted")
    assert named_problems([], [missing]) == ["until: tests/no_such.py is gone"]


def test_the_stand_ins_are_programs_that_tests_run():
    assert stand_in_problems() == []


def test_a_stand_in_named_only_by_the_guard_is_reported():
    """Planted twin: with only this module scanned, no stand-in is named by a test."""
    problems = stand_in_problems([GUARD_FILE])
    assert problems == [f"{p} is named in the string of no scanned file" for p in STAND_INS]


# --- the real tree -------------------------------------------------------------------------------

# The allowances the real tree's scan must have made, each at least once, and two files whose
# doubles it must have found: that each holds what it is for, and the scan sees the tree.
DERIVED = {"double", "child loop", "benchmark"}
UNDER_DOUBLES = ("tests/test_workspace_limits.py", "tests/test_monitor.py")
# The allowances the scan of the frontend's TypeScript must have made, each at least once.
TS_DERIVED = {"turn", "guard"}


def real_tree_problems(tree: Tree) -> list[str]:
    """What is wrong with the scope as ``tree`` scanned it: every site no named pattern covers;
    the stale checks of the named patterns and the stand-ins; and the non-vacuity checks, that
    sites were seen at all and the derived allowances were made where they are known to be."""
    problems = [f"{s.path}:{s.line}: {s.form}: {s.text} [{s.where}]"
                for s in unexempted(tree.sites)]  # fmt: skip
    problems += named_problems(tree.sites)
    problems += stand_in_problems()
    if not tree.sites:
        problems.append("the scan saw no site at all, not even a named pattern's")
    kinds = {k for k, _ in tree.derived}
    problems += [f"no site was allowed as {k}" for k in sorted(DERIVED - kinds)]
    doubled = {s.path for k, s in tree.derived if k == "double"}
    problems += [f"no double was found in {p}" for p in UNDER_DOUBLES if p not in doubled]
    typescript = {k for k, s in tree.derived if s.path.startswith("frontend/")}
    problems += [f"no TypeScript site was allowed as {k}" for k in sorted(TS_DERIVED - typescript)]
    return problems


def test_no_test_waits_on_real_time():
    """The guard over the real tree, scanned once, the Python and the frontend's TypeScript: no site
    but the named patterns', which are not stale, and a scan that saw what it is known to have
    to see."""
    python, typescript = scan_tree(), ts_scan_tree()
    tree = Tree(python.sites + typescript.sites, python.derived + typescript.derived, python.table)
    assert real_tree_problems(tree) == []


def test_a_planted_site_in_the_tree_is_a_problem():
    """Planted twin of the real tree's check: the tree's scan with one site more, and with its
    known allowances missing, is reported."""
    planted = Site("tests/test_planted.py", 2, "P2", "time.$SLEEP(1)", "test_x")
    tree = Tree([planted], [], Table())
    problems = real_tree_problems(tree)
    assert "tests/test_planted.py:2: P2: time.$SLEEP(1) [test_x]" in problems
    assert [p for p in problems if p.startswith("no site was allowed as")] == [
        f"no site was allowed as {k}" for k in sorted(DERIVED)
    ]
    assert [p for p in problems if p.startswith("no double")] == [
        f"no double was found in {p}" for p in UNDER_DOUBLES
    ]
    assert [p for p in problems if p.startswith("no TypeScript site")] == [
        f"no TypeScript site was allowed as {k}" for k in sorted(TS_DERIVED)
    ]
    nothing = "the scan saw no site at all, not even a named pattern's"
    assert nothing not in problems
    assert nothing in real_tree_problems(Tree([], [], Table()))


# --- nothing autouse is added --------------------------------------------------------------------

AUTOUSE = {
    ("tests/conftest.py", "collections_shared_by_the_workers"),
    ("tests/conftest.py", "databases_that_do_not_wait_for_the_disk"),
    ("tests/conftest.py", "github_stand_in"),
    ("tests/time_doubles.py", "time_doubles"),
}


def autouse_fixtures(sources: dict[str, str]) -> set[tuple[str, str]]:
    found = set()
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, FUNCTIONS):
                for d in node.decorator_list:
                    if isinstance(d, ast.Call) and any(
                        k.arg == "autouse" and getattr(k.value, "value", False) for k in d.keywords
                    ):
                        found.add((path, node.name))
    return found


def test_the_guard_adds_no_autouse_fixture():
    sources = {p: (REPO / p).read_text(encoding="utf-8") for p, _ in AUTOUSE}
    assert autouse_fixtures(sources) == AUTOUSE


def test_an_autouse_fixture_added_is_seen():
    """Planted twin."""
    sources = {p: (REPO / p).read_text(encoding="utf-8") for p, _ in AUTOUSE}
    sources["tests/conftest.py"] += "\n@pytest.fixture(autouse=True)\ndef planted():\n    pass\n"
    assert autouse_fixtures(sources) - AUTOUSE == {("tests/conftest.py", "planted")}


# --- the scope -----------------------------------------------------------------------------------


def test_each_root_of_the_scope_is_read_and_holds_a_named_file():
    files = scoped_files()
    for root, known in (("tests", "tests/test_until.py"), ("devtools", "devtools/tmuxctl.py"),
                        ("tests/container", "tests/container/stack.py")):  # fmt: skip
        assert any(f.startswith(root + "/") for f in files), root
        assert known in files
    assert not set(STAND_INS) & set(files)


def test_an_empty_scope_reads_nothing():
    """With no root nothing is read, where git ls-files without a path would list everything."""
    assert scoped_files(()) == []
    assert scoped_files(("no-such-root",)) == []


def test_a_planted_file_in_each_root_is_read(tmp_path):
    """A file of the scanned shape, planted under each root of a scratch repository, is listed
    and its site found, tests/container/ among them; a stand-in is not listed."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=HANG_GUARD)
    planted = ["devtools/planted.py", "tests/container/planted.py", "tests/planted.py"]
    for rel in [*planted, "devtools/fake_claude.py", "tests/container/stand_ins.py"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(placed("import time\ntime.$SLEEP(1)\n"), encoding="utf-8")
    assert scoped_files(SCOPE, tmp_path) == planted
    for rel in planted:
        source = (tmp_path / rel).read_text(encoding="utf-8")
        assert ("P2", 2) in [(s.form, s.line) for s in scan_source(rel, source).sites]


# --- the frontend's TypeScript: its twins and its scope ------------------------------------------

UNIT_IMPORT = 'import { UNIT_HANG_GUARD_MS } from "./test/hangGuard";\n'
# Vitest's config, setting the keys ``{keys}`` names.
VITE = (
    'import {{ defineConfig }} from "vitest/config";\n'
    'import {{ UNIT_TEST_GUARD_MS as T }} from "./src/test/hangGuard";\n'
    "export default defineConfig({{\n  test: {{ {keys} }},\n}});\n"
)

# name: (form, {rel under frontend/: source}, the rel flagged, its line)
PLANTED_TS = {
    "Date.now compared": (
        "P1",
        {"src/x.test.ts": "const start = Date.now();\nwhile (Date.now() < start + 5) f();\n"},
        "src/x.test.ts",
        2,
    ),
    "performance.now differenced": (
        "P1",
        {
            "src/x.test.ts": "const a = performance.now();\n"
            "const b = performance.now();\nconst took = b - a;\n"
        },
        "src/x.test.ts",
        3,
    ),
    "a clock in a matcher": (
        "P1",
        {
            "src/x.test.ts": "const start = Date.now();\n"
            "expect(Date.now()).toBeLessThan(start + 9);\n"
        },
        "src/x.test.ts",
        2,
    ),
    "shouldAdvanceTime": (
        "P1",
        {"src/x.test.ts": "vi.useFakeTimers({ shouldAdvanceTime: true });\n"},
        "src/x.test.ts",
        1,
    ),
    "waitForTimeout": (
        "P2",
        {"src/test/x.ts": "await page.waitForTimeout(100);\n"},
        "src/test/x.ts",
        1,
    ),
    "setTimeout with a delay": (
        "P2",
        {"src/x.test.ts": "await new Promise((r) => setTimeout(r, 50));\n"},
        "src/x.test.ts",
        1,
    ),
    "window.setTimeout": (
        "P2",
        {"src/test/x.ts": "window.setTimeout(f, 10);\n"},
        "src/test/x.ts",
        1,
    ),
    "setInterval": ("P2", {"src/x.test.tsx": "setInterval(f, 1000);\n"}, "src/x.test.tsx", 1),
    "a timeout option": (
        "P3",
        {"src/x.test.tsx": "await expect(x).toBeVisible({ timeout: 2000 });\n"},
        "src/x.test.tsx",
        1,
    ),
    "a Timeout key": (
        "P3",
        {"src/test/setup.ts": "configure({ asyncUtilTimeout: 3000 });\n"},
        "src/test/setup.ts",
        1,
    ),
    "test.setTimeout": (
        "P3",
        {"src/x.test.ts": "test.setTimeout(90_000);\n"},
        "src/x.test.ts",
        1,
    ),
    "test.slow": ("P3", {"src/x.test.ts": "test.slow();\n"}, "src/x.test.ts", 1),
    "testInfo.slow": (
        "P3",
        {"src/x.test.ts": 'test("x", async ({ page }, testInfo) => {\n  testInfo.slow();\n});\n'},
        "src/x.test.ts",
        2,
    ),
    "test.info().slow": ("P3", {"src/x.test.ts": "test.info().slow();\n"}, "src/x.test.ts", 1),
    "userEvent with a delay": (
        "P2",
        {"src/x.test.tsx": "const user = userEvent.setup({\n  delay: 50,\n});\n"},
        "src/x.test.tsx",
        2,
    ),
    "vi.waitFor without a timeout": (
        "P3",
        {"src/x.test.ts": "await vi.waitFor(() => f());\n"},
        "src/x.test.ts",
        1,
    ),
    "vi.waitUntil without a timeout": (
        "P3",
        {"src/x.test.ts": "await vi.waitUntil(() => f(), { interval: 5 });\n"},
        "src/x.test.ts",
        1,
    ),
    "a timeout through a parameter": (
        "P3",
        {
            "src/test/x.ts": "export async function made(p, wait) {\n"
            "  await expect(p).toBeVisible({ timeout: wait });\n}\n"
        },
        "src/test/x.ts",
        2,
    ),
    "a local guard": (
        "GUARD",
        {"src/x.test.ts": "const UNIT_HANG_GUARD_MS = 5;\n"},
        "src/x.test.ts",
        1,
    ),
    "Vitest without a hook's": (
        "P3c",
        {"vite.config.ts": VITE.format(keys="testTimeout: T, teardownTimeout: T")},
        "vite.config.ts",
        3,
    ),
}

# name: (the allowance, {rel under frontend/: source})
CLEAN_TS = {
    "zero": ("zero", {"src/test/setup.ts": "configure({ asyncUtilTimeout: 0 });\n"}),
    "the guard": (
        "guard",
        {"src/x.test.ts": UNIT_IMPORT + "configure({ asyncUtilTimeout: UNIT_HANG_GUARD_MS });\n"},
    ),
    "vi.waitFor given the guard": (
        "guard",
        {
            "src/x.test.ts": UNIT_IMPORT + "await vi.waitFor("
            "() => f(), { timeout: UNIT_HANG_GUARD_MS });\n"
        },
    ),
    "a turn": (
        "turn",
        {"src/test/x.ts": "await new Promise((r) => setTimeout(r));\nsetTimeout(f, 0);\n"},
    ),
    "every key set": (
        "structure",
        {"vite.config.ts": VITE.format(keys="testTimeout: T, hookTimeout: T, teardownTimeout: T")},
    ),
    "a clock read and not compared": (
        "structure",
        {"src/x.test.ts": "const at = Date.now();\nreport(at);\n"},
    ),
    "a destructured or typed timeout": (
        "structure",
        {
            "src/x.test.ts": "type T = { timeout: number };\n"
            "const f = ({ timeout }: T) => timeout;\n"
        },
    ),
    "a spy on a timer": ("structure", {"src/x.test.ts": 'vi.spyOn(globalThis, "setInterval");\n'}),
    "userEvent with no delay": (
        "structure",
        {
            "src/x.test.tsx": "userEvent.setup({ delay: null });\nuserEvent.setup({ delay: 0 });\n"
            "userEvent.setup({ advanceTimers: vi.advanceTimersByTime });\nuserEvent.setup();\n"
        },
    ),
    "shouldAdvanceTime off": (
        "structure",
        {"src/x.test.ts": "vi.useFakeTimers({ shouldAdvanceTime: false });\n"},
    ),
}

# Each form, as each language spells it: the name of its planted twin in PLANTED (Python) and
# PLANTED_TS (TypeScript), or None where the language does not spell it, with the reason in
# NOT_SPELLED.
FORMS = {
    "P1": ("a deadline loop", "Date.now compared"),
    "P2": ("time.sleep", "waitForTimeout"),
    "P2s": ("a shell sleep", None),
    "P3": ("a join", "a timeout option"),
    "P3c": (None, "Vitest without a hook's"),
    "P4": ("a monkeypatched deadline", None),
    "GUARD": ("a local HANG_GUARD", "a local guard"),
    "TIER": (None, None),
}
NOT_SPELLED = {
    ("P2s", "ts"): "the frontend's tests start no child program",
    ("P3c", "python"): "pytest's limits are the hang guard's, in pyproject.toml, which "
    "tests/test_hang_guard.py and tests/test_make.py hold",
    ("P4", "ts"): "an ES module's exports are read-only bindings, which no test can set: no spec "
    "patches a module's timing constant",
    ("TIER", "python"): "spelled by TIERED's twins, which give the tiers that reach a helper",
    ("TIER", "ts"): "one tier: the component tests' (frontend/src/test/hangGuard.ts), so the "
    "scan has no tier rule there",
}


@pytest.fixture(scope="module")
def ts_twins() -> dict[str, TsTree]:
    """Every TypeScript twin scanned, parsed in one call of node."""
    tables = {**{f"planted {n}": t[1] for n, t in PLANTED_TS.items()},
              **{f"clean {n}": t[1] for n, t in CLEAN_TS.items()}}  # fmt: skip
    given = {
        f"{i}/{rel}": src for i, files in enumerate(tables.values()) for rel, src in files.items()
    }
    trees = parse_ts(given)
    out = {}
    for i, (name, files) in enumerate(tables.items()):
        out[name] = ts_scan_sources(files, {rel: trees[f"{i}/{rel}"] for rel in files})
    return out


def ts_found(tree: TsTree, rel: str | None = None) -> list[tuple[str, int]]:
    return [(s.form, s.line) for s in tree.sites if rel is None or s.path == f"frontend/{rel}"]


@pytest.mark.parametrize("name", sorted(PLANTED_TS))
def test_a_planted_typescript_form_is_flagged_where_it_is(ts_twins, name):
    form, files, rel, line = PLANTED_TS[name]
    assert ts_found(ts_twins[f"planted {name}"], rel).count((form, line)) == 1, (name, ts_twins)


@pytest.mark.parametrize("name", sorted(CLEAN_TS))
def test_an_allowed_typescript_form_is_not_flagged(ts_twins, name):
    key, _files = CLEAN_TS[name]
    scanned = ts_twins[f"clean {name}"]
    assert ts_found(scanned) == []
    if key != "structure":  # the allowance made is the one the twin is for
        assert key in {k for k, _ in scanned.derived}


def test_an_allowance_turned_off_flags_its_typescript_twin(monkeypatch):
    """Planted twin of the allowances: the guard not allowed, its CLEAN twin is a site."""
    monkeypatch.setitem(ALLOWANCES, "guard", False)
    _key, files = CLEAN_TS["the guard"]
    assert ts_found(ts_scan_sources(files)) == [("P3", 2)]


@pytest.mark.parametrize("form", sorted(FORMS))
def test_each_form_is_spelled_in_each_language_or_said_why_not(form):
    python, typescript = FORMS[form]
    assert (python in PLANTED) if python else ((form, "python") in NOT_SPELLED)
    assert (typescript in PLANTED_TS) if typescript else ((form, "ts") in NOT_SPELLED)
    if python:
        assert PLANTED[python][0] == form
    if typescript:
        assert PLANTED_TS[typescript][0] == form


def test_every_form_planted_is_in_the_table():
    planted = {t[0] for t in PLANTED.values()} | {t[0] for t in PLANTED_TS.values()}
    assert planted | {"TIER"} == set(FORMS)


def test_the_typescript_scope_is_the_frontends_tests_and_their_configs():
    rels = ts_scope()
    for known in ("src/api.test.ts", "src/test/setup.ts", "src/test/hangGuard.ts", *TS_CONFIGS):
        assert known in rels, known
    assert "src/api.ts" not in rels  # the product's own
    vite = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    assert re.findall(r"include:\s*\[([^\]]*)\]", vite) == [f'"{TS_INCLUDE}"']
    assert TS_TESTS.match("src/api.test.ts") and TS_TESTS.match("src/a/b.test.tsx")
    assert not TS_TESTS.match("src/api.ts")


def test_a_planted_typescript_file_in_each_root_is_read(tmp_path):
    """A file of the scanned shape planted under each root of the TypeScript scope, in a
    scratch repository, is listed and its site found, the config among them, whose unset
    limits are found too; the product's own, and another config, are not listed."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=HANG_GUARD)
    frontend = tmp_path / "frontend"
    planted = sorted(["src/x.test.tsx", "src/test/x.ts", *TS_CONFIGS])
    for rel in [*planted, "src/x.tsx", "other.config.ts"]:
        (frontend / rel).parent.mkdir(parents=True, exist_ok=True)
        (frontend / rel).write_text("setInterval(f, 1);\nexport default {};\n", encoding="utf-8")
    assert ts_scope(frontend) == planted
    tree = ts_scan_tree(frontend)
    for rel in planted:
        assert ("P2", 1) in ts_found(tree, rel), rel
    assert [s.path for s in tree.sites if s.form == "P2"] == [f"frontend/{r}" for r in planted]
    for rel in TS_CONFIGS:
        assert ("P3c", 2) in ts_found(tree, rel), rel
