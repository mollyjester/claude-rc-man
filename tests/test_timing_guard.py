"""The timing guard, G2: no test waits on, or races, real time.

A static scan of the test code (project-knowledge/tests.md, "Time in tests"). It imports no
scanned module and runs nothing: each file is parsed once and walked once. It flags

- P1, elapsed real time compared: a comparison with a clock read, or with a name assigned from
  one in the same function, and a difference of two such;
- P2, a sleep: time.sleep, asyncio.sleep, anyio.sleep and trio.sleep, resolved through imports,
  with any argument but 0, and threading.Timer, loop.call_later and loop.call_at;
- P2s, a sleep or a timeout written in a child program's source, held in a string: the shell's
  ``sleep N`` and ``timeout [-s S] [-k K] N``, Python's ``time.sleep(N)``, and the numbers of an
  argv of the ``timeout`` tool; an interpolated one too, unless it is a multiple of the guard or
  RUNS_OUT;
- P3, a timeout that is not the guard: a keyword of a timing name, a known positional, a
  number at a position the parameter table gives, a parameter's default, a module's or a
  class's constant of a timing name, and pytest.mark.timeout;
- P4, a product's deadline, grace or pace shortened: monkeypatch's, mock's and the builtin
  setattr of a timing constant, a plain ``module.NAME = value``, a patch of the time module
  itself, a name that cannot be resolved, and a restoring setattr;
- a module defining HANG_GUARD, CONTAINER_HANG_GUARD or tier_guard other than
  tests/hang_guard.py.

Allowed values are the tier's guard and its multiples of at least 1, tier_guard() where a test
reads it, None, -1 (the standard library's "no limit") and 0. The only exemptions are the named
patterns of NAMED, each with its reason and a stale check, and the stand-ins of STAND_INS.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent
HANG_GUARD_MODULE = "tests.hang_guard"
GUARD_NAMES = {"HANG_GUARD", "CONTAINER_HANG_GUARD"}
RUNS_OUT_NAME = "RUNS_OUT"

# --- what is a timing name -----------------------------------------------------------------------

TIMING_KEYWORD = re.compile(
    r"^(timeout|within|deadline|seconds|wait|quiet|interval|delay|every|grace)$"
    r"|_(timeout|seconds|within|interval|delay|wait|grace)$"
)
TIMING_CONSTANT = re.compile(
    r"TIMEOUT|SECONDS|WITHIN|DEADLINE|GRACE|WAIT|PACE|TICK|POLL|INTERVAL|DELAY|QUIET|_FOR$|_LIMIT$"
)


def timing_constant(name: str) -> bool:
    return name.isupper() and bool(TIMING_CONSTANT.search(name))


# The clocks: a call of one is a clock read.
CLOCKS = {
    f"time.{name}{ns}"
    for name in ("monotonic", "perf_counter", "time")
    for ns in ("", "_ns")
} | {"datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
     "datetime.date.today"}  # fmt: skip
SLEEPS = {"time.sleep", "asyncio.sleep", "anyio.sleep", "trio.sleep"}
TIMERS = {"threading.Timer"}
LOOP_TIMERS = {"call_later", "call_at"}
# A positional timeout: a method's name, the position, and what the receiver must look like
# (None: anything but a string constant).
POSITIONAL = {
    "wait": 0, "join": 0, "settimeout": 0, "setdefaulttimeout": 0, "alarm": 0,
    "fail_after": 0, "move_on_after": 0, "result": 0, "poll": 0,
    "wait_for": 1, "communicate": 1, "create_connection": 1, "acquire": 1,
    "urlopen": 2,
}  # fmt: skip
QUEUE_GET = re.compile(r"queue|_q$|^q$", re.I)


@dataclass(frozen=True, order=True)
class Site:
    path: str
    line: int
    form: str
    text: str
    where: str = ""  # the qualified name of the function it is in, "" at module level


# --- the parameter table (P3) --------------------------------------------------------------------


@dataclass
class Table:
    """For each function name, the positions of its parameters with a timing name, where every
    definition of the name agrees; ``disagreeing`` holds the names left out."""

    positions: dict[str, frozenset[int]] = field(default_factory=dict)
    disagreeing: set[str] = field(default_factory=set)


def timing_positions(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if args and args[0] in ("self", "cls"):
        args = args[1:]
    return frozenset(i for i, name in enumerate(args) if TIMING_KEYWORD.search(name))


def build_table(trees: Iterable[ast.AST]) -> Table:
    seen: dict[str, set[frozenset[int]]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                seen.setdefault(node.name, set()).add(timing_positions(node))
    table = Table()
    for name, kinds in seen.items():
        if len(kinds) > 1:
            if any(kinds):
                table.disagreeing.add(name)
        elif only := next(iter(kinds)):
            table.positions[name] = only
    return table


# --- the scan of one Python file -----------------------------------------------------------------


def number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = number(node.operand)
        return None if inner is None else -inner
    return None


SHELL = re.compile(
    r"(?P<what>\bsleep\s+|\btime\.sleep\(\s*|\btimeout\s+(?:-[sk]\s+\S+\s+)*)"
    r"(?P<n>\d+(?:\.\d+)?|\{[^}]*\})"
)
SHELL_LOOP = re.compile(r"\bwhile\b(?P<cond>[^;]*?);\s*do\b(?P<body>.*?)\bdone\b", re.S)
CHILD_CLOCK = re.compile(r"\bdate\b|\$SECONDS|\btime\.(monotonic|time|perf_counter)\b")
CHILD_COUNTER = re.compile(r"-(lt|le|gt|ge|eq)\s+\S*\d")


class FileScan:
    """The sites of one file. ``tier`` is "host", "container" or "shared"."""

    def __init__(self, path: str, source: str, table: Table, tier: str = "host"):
        self.path = path
        self.source = source
        self.tree = ast.parse(source)
        self.table = table
        self.tier = tier
        self.sites: list[Site] = []
        self.derived: list[tuple[str, Site]] = []  # (named pattern, site) allowed by H7/H8/H9/H13
        self.modules: dict[str, str] = {}  # local name -> module
        self.names: dict[str, str] = {}  # local name -> module.name
        self._imports()
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.functions = {
            n.name: n
            for n in self.tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.benchmark_only = self._benchmark_only()

    # -- names ----------------------------------------------------------------------------------

    def _imports(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        self.modules[top] = top
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def qual(self, node: ast.AST) -> str | None:
        """The dotted name ``node`` stands for, through the file's imports; None for one that is
        not rooted in an import."""
        if isinstance(node, ast.Name):
            return self.names.get(node.id) or self.modules.get(node.id)
        if isinstance(node, ast.Attribute):
            inner = self.qual(node.value)
            return f"{inner}.{node.attr}" if inner else None
        return None

    def is_guard(self, node: ast.AST) -> bool:
        q = self.qual(node)
        return q is not None and q in {f"{HANG_GUARD_MODULE}.{n}" for n in GUARD_NAMES}

    def is_tier_guard_call(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and not node.args
            and self.qual(node.func) == f"{HANG_GUARD_MODULE}.tier_guard"
        )

    def is_runs_out(self, node: ast.AST) -> bool:
        return self.qual(node) == f"{HANG_GUARD_MODULE}.{RUNS_OUT_NAME}"

    def is_product(self, node: ast.AST) -> bool:
        """A product's own value (rule (b), H9): a name of rcman's or of helpers."""
        q = self.qual(node)
        return q is not None and q.split(".")[0] in ("rcman", "helpers")

    def guard_multiple(self, node: ast.AST, minimum: float = 1) -> bool:
        if self.is_guard(node) or self.is_tier_guard_call(node):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            for k, g in ((node.left, node.right), (node.right, node.left)):
                if (n := number(k)) is not None and n >= minimum and self.guard_multiple(g):
                    return True
        return False

    def allowed(self, node: ast.AST, where: ast.AST | None, read_at_test_time: bool = True) -> bool:
        """An allowed value: the guard or a multiple of it of at least 1, tier_guard() where a
        test reads it, None, -1 and 0, a product's own value, or a name that is checked where it
        is given its value: a parameter of the function it is in, or a timing constant."""
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if number(node) in (0, -1):
            return True
        if self.is_tier_guard_call(node):
            return read_at_test_time
        if self.guard_multiple(node):
            return not any(self.is_tier_guard_call(n) for n in ast.walk(node)) or read_at_test_time
        if self.is_product(node):
            return True
        if isinstance(node, ast.Name):
            if node.id in self.parameters(where):
                return True
            if timing_constant(node.id) and node.id not in self.names:
                return True  # a constant of this file: checked where it is set
        return False

    @staticmethod
    def parameters(fn: ast.AST | None) -> set[str]:
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return set()
        a = fn.args
        return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | {
            x.arg for x in (a.vararg, a.kwarg) if x
        }

    def timeish(self, node: ast.AST) -> bool:
        """A value that reads as seconds: a number, or arithmetic or a name of a timing kind."""
        for n in ast.walk(node):
            if (v := number(n)) is not None and v not in (0, -1):
                return True
            if isinstance(n, ast.Name) and (timing_constant(n.id) or TIMING_KEYWORD.search(n.id)):
                return True
            if isinstance(n, ast.Attribute) and timing_constant(n.attr):
                return True
        return False

    def enclosing(self, node: ast.AST) -> list[ast.AST]:
        found = []
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
                found.append(node)
        return found

    def function_of(self, node: ast.AST) -> ast.AST | None:
        for n in self.enclosing(node):
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                return n
        return None

    def where(self, node: ast.AST) -> str:
        return ".".join(
            n.name for n in reversed(self.enclosing(node)) if not isinstance(n, ast.Lambda)
        )

    def text(self, node: ast.AST) -> str:
        return " ".join((ast.get_source_segment(self.source, node) or "").split())[:120]

    def site(self, node: ast.AST, form: str, text: str | None = None) -> None:
        self.sites.append(
            Site(self.path, node.lineno, form, text or self.text(node), self.where(node))
        )

    # -- H7: doubles ----------------------------------------------------------------------------

    DOUBLES = {"FakeTime", "WatchedCondition", "ControlledEvent", "RecordingLock", "Time"}

    def makes_double(self, call: ast.AST, depth: int = 1) -> bool:
        """A call that builds a double of tests/time_doubles.py: its constructor, or a helper of
        this module (one level) that returns one or a tuple holding one."""
        if not isinstance(call, ast.Call):
            return False
        q = self.qual(call.func) or ""
        if q.startswith("tests.time_doubles.") and q.rsplit(".", 1)[1] in self.DOUBLES:
            return True
        if depth and isinstance(call.func, ast.Name) and call.func.id in self.functions:
            helper = self.functions[call.func.id]
            built = self.double_names(helper, depth - 1)
            for ret in ast.walk(helper):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    parts = ret.value.elts if isinstance(ret.value, ast.Tuple) else [ret.value]
                    for part in parts:
                        if self.makes_double(part, depth - 1) or (
                            isinstance(part, ast.Name) and part.id in built
                        ):
                            return True
        return False

    def double_names(self, fn: ast.AST, depth: int = 1) -> set[str]:
        """Names a function binds to a double, its own or a helper's (H7)."""
        found: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and self.makes_double(node.value, depth):
                for target in node.targets:
                    for n in ast.walk(target):
                        if isinstance(n, ast.Name):
                            found.add(n.id)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            found |= {a.arg for a in fn.args.args if a.arg in ("fake", "times", "clock")}
        return found

    def on_a_double(self, call: ast.Call) -> bool:
        fn = self.function_of(call)
        if fn is None or not isinstance(call.func, ast.Attribute):
            return False
        receiver = call.func.value
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        return isinstance(receiver, ast.Name) and receiver.id in self.double_names(fn)

    def doubled_modules(self, fn: ast.AST) -> set[str]:
        """The objects whose ``time`` (or seams for time) the function replaces with a double."""
        found = set()
        doubles = self.double_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and self.setattr_args(node):
                obj, name, value = self.setattr_args(node)
                if name in ("time", "_changed", "_closing") or (name or "").endswith("lock"):
                    if self.makes_double(value) or (
                        isinstance(value, ast.Name) and value.id in doubles
                    ):
                        found.add(ast.dump(obj))
        return found

    # -- H10: the benchmark --------------------------------------------------------------------

    def is_benchmark(self, fn: ast.AST) -> bool:
        for d in getattr(fn, "decorator_list", []):
            if isinstance(d, ast.Attribute) and d.attr == "benchmark":
                return True
        return False

    def _benchmark_only(self) -> set[str]:
        """Functions of this module that only benchmark tests call (H10)."""
        callers: dict[str, set[str]] = {name: set() for name in self.functions}
        for fn in self.functions.values():
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id in callers and node.id != fn.name:
                    callers[node.id].add(fn.name)
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for n in ast.walk(node):
                    if isinstance(n, ast.Name) and n.id in callers:
                        callers[n.id].add("<module>")
        only = {n for n, f in self.functions.items() if self.is_benchmark(f)}
        changed = True
        while changed:
            changed = False
            for name, by in callers.items():
                if name not in only and by and by <= only:
                    only.add(name)
                    changed = True
        return only

    def in_benchmark(self, node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in self.benchmark_only
            for n in self.enclosing(node)
        )

    # -- the walk -------------------------------------------------------------------------------

    def scan(self) -> list[Site]:
        self._clocks()
        self._defining_the_guard()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self._call(node)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self._defaults(node)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                self._assign(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if not isinstance(self.parents.get(node), ast.JoinedStr):
                    self._child_source(node, node.value, [])
            elif isinstance(node, ast.JoinedStr):
                self._joined(node)
            elif isinstance(node, ast.List | ast.Tuple):
                self._timeout_argv(node)
        self._setattrs()
        self.sites = sorted(
            {s for s in self.sites if not self._benchmark_site(s)},
        )
        return self.sites

    def _benchmark_site(self, site: Site) -> bool:
        head = site.where.split(".")[0] if site.where else ""
        if head in self.benchmark_only:
            self.derived.append(("H10", site))
            return True
        return False

    # P1 ---------------------------------------------------------------------------------------

    def clock_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or node.args:
            return False
        if self.qual(node.func) in CLOCKS:
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

    def _clocks(self) -> None:
        scopes = [self.tree] + [
            n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope in scopes:
            own = [n for n in ast.walk(scope) if self.function_of(n) is (
                None if scope is self.tree else scope)]  # fmt: skip
            derived: set[str] = set()
            changed = True
            while changed:
                changed = False
                for n in own:
                    if isinstance(n, ast.Assign | ast.AugAssign | ast.AnnAssign) and n.value:
                        if self._clocky(n.value, derived):
                            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                            for t in targets:
                                for x in ast.walk(t):
                                    if isinstance(x, ast.Name) and x.id not in derived:
                                        derived.add(x.id)
                                        changed = True
            reported: set[int] = set()
            for n in own:
                if isinstance(n, ast.Compare) and any(
                    self._clocky(x, derived) for x in [n.left, *n.comparators]
                ):
                    self.site(n, "P1")
                    reported.add(n.lineno)
            for n in own:
                if (
                    isinstance(n, ast.BinOp)
                    and isinstance(n.op, ast.Sub)
                    and self._clocky(n.left, derived)
                    and self._clocky(n.right, derived)
                    and n.lineno not in reported
                ):
                    self.site(n, "P1")

    def _clocky(self, node: ast.AST, derived: set[str]) -> bool:
        return any(
            self.clock_read(n) or (isinstance(n, ast.Name) and n.id in derived)
            for n in ast.walk(node)
        )

    # P2, P3 (calls), P4 (setattr calls, collected below) ---------------------------------------

    def _call(self, node: ast.Call) -> None:
        q = self.qual(node.func)
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
            node.func, "id", None)  # fmt: skip
        fn = self.function_of(node)
        if q in SLEEPS:
            if node.args and number(node.args[0]) == 0:
                self.derived.append(("H8", Site(self.path, node.lineno, "P2", self.text(node))))
            else:
                self.site(node, "P2")
        if q in TIMERS or name in LOOP_TIMERS:
            self.site(node, "P2")
        if self.on_a_double(node):
            if node.args or node.keywords:
                self.derived.append(("H7", Site(self.path, node.lineno, "P3", self.text(node))))
            return
        for kw in node.keywords:
            if kw.arg and TIMING_KEYWORD.search(kw.arg) and not self.allowed(kw.value, fn):
                self.site(node, "P3", f"{kw.arg}={self.text(kw.value)} in {self.text(node.func)}")
        position = POSITIONAL.get(name)
        if name == "get" and isinstance(node.func, ast.Attribute):
            receiver = self.text(node.func.value)
            position = 1 if QUEUE_GET.search(receiver.split(".")[-1]) else None
        if name == "select" and q == "select.select":
            position = 3
        elif name == "select" and isinstance(node.func, ast.Attribute):
            position = 0
        if (
            position is not None
            and len(node.args) > position
            and not (
                isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Constant)
            )
        ):
            value = node.args[position]
            if self.timeish(value) and not self.allowed(value, fn):
                self.site(node, "P3", f"{name}(…{self.text(value)}…)")
        elif name in self.table.positions and q not in SLEEPS and q not in (
            f"{HANG_GUARD_MODULE}.runs_out", f"{HANG_GUARD_MODULE}.pace"):  # fmt: skip
            for i in sorted(self.table.positions[name]):
                if i < len(node.args) and (v := number(node.args[i])) is not None and v not in (
                    0, -1):  # fmt: skip
                    self.site(node, "P3", f"{name} at {i}: {self.text(node.args[i])}")
        if q in ("pytest.mark.timeout",) and node.args and not self.allowed(node.args[0], None):
            self.site(node, "P3")

    def _defaults(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        a = fn.args
        positional = a.posonlyargs + a.args
        pairs = list(zip(positional[len(positional) - len(a.defaults) :], a.defaults, strict=True))
        pairs += [(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
        for param, default in pairs:
            if TIMING_KEYWORD.search(param.arg) and not self.allowed(
                default, None, read_at_test_time=False
            ):
                self.site(default, "P3", f"{fn.name}({param.arg}={self.text(default)})")

    def _assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        if node.value is None:
            return
        owner = self.enclosing(node)
        at_module_or_class = not owner or isinstance(owner[0], ast.ClassDef)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and at_module_or_class and timing_constant(t.id):
                if not self.allowed(node.value, None, read_at_test_time=False) and (
                    self.timeish(node.value)
                    or any(self.is_tier_guard_call(n) for n in ast.walk(node.value))
                ):
                    self.site(node, "P3")
            if isinstance(t, ast.Attribute) and timing_constant(t.attr):
                self.assignments.append((node, t, node.value))

    def _defining_the_guard(self) -> None:
        if self.path == "tests/hang_guard.py":
            return
        for node in ast.walk(self.tree):
            defined = []
            if isinstance(node, ast.Assign):
                defined = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined = [node.target.id]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                defined = [node.name]
            if set(defined) & (GUARD_NAMES | {"tier_guard"}):
                self.site(node, "GUARD", f"defines {', '.join(defined)}")

    # P2s --------------------------------------------------------------------------------------

    def _child_source(self, node: ast.AST, text: str, values: list[ast.AST]) -> None:
        """Sleeps and timeouts in a string a child program runs. ``values`` are the
        interpolated expressions of an f-string, written as {0}, {1}, … in ``text``."""
        if "sleep" not in text and "timeout" not in text and "while" not in text:
            return
        loops = [(m.start(), m.end(), m.group("cond")) for m in SHELL_LOOP.finditer(text)]
        for loop in loops:
            if CHILD_CLOCK.search(loop[2]) or CHILD_COUNTER.search(loop[2]):
                self.site(node, "P1", f"a child's loop on a clock or a count: {loop[2].strip()}")
        python_loop_lines = self._python_loop_lines(text)
        for m in SHELL.finditer(text):
            n = m.group("n")
            what = m.group("what").strip()
            if n.startswith("{"):
                index = n[1:-1]
                if index.isdigit() and int(index) < len(values):
                    value = values[int(index)]
                    if self.is_runs_out(value) or self.guard_multiple(value):
                        continue
                self.site(node, "P2s", f"{what} {n}: interpolated, not the guard or RUNS_OUT")
                continue
            if float(n) == 0:
                continue
            in_loop = any(start <= m.start() < end and not (
                CHILD_CLOCK.search(cond) or CHILD_COUNTER.search(cond))
                for start, end, cond in loops)  # fmt: skip
            line = text.count("\n", 0, m.start())
            if "sleep" in what and (in_loop or line in python_loop_lines):
                self.derived.append(("H13", Site(self.path, node.lineno, "P2s", m.group(0))))
                continue
            self.site(node, "P2s", f"{m.group(0)!r} in a child's source")

    def _python_loop_lines(self, text: str) -> set[int]:
        """The lines (0-based) of a ``while`` body in ``text``, if it parses as Python, whose
        test reads no clock and counts nothing."""
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
                    lines |= set(range(b.lineno - 1, (b.end_lineno or b.lineno)))
        return lines

    def _joined(self, node: ast.JoinedStr) -> None:
        parts, values = [], []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value).replace("{", "{{").replace("}", "}}"))
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + str(len(values)) + "}")
                values.append(v.value)
        self._child_source(node, "".join(parts), values)

    def _timeout_argv(self, node: ast.List | ast.Tuple) -> None:
        elts = node.elts
        for i, e in enumerate(elts):
            if isinstance(e, ast.Constant) and e.value == "timeout":
                for later in elts[i + 1 :]:
                    if isinstance(later, ast.Constant) and isinstance(later.value, str):
                        if re.fullmatch(r"\d+(\.\d+)?", later.value) and float(later.value):
                            self.site(node, "P2s", f"timeout argv: {self.text(node)}")
                            return
                        if not later.value.startswith("-") and not re.fullmatch(
                            r"[A-Z]+", later.value
                        ):
                            return  # the command begins
                    elif isinstance(later, ast.JoinedStr | ast.FormattedValue | ast.Name):
                        value = later.value if isinstance(later, ast.FormattedValue) else later
                        if not (self.is_runs_out(value) or self.guard_multiple(value)):
                            if not isinstance(later, ast.JoinedStr):
                                self.site(node, "P2s", f"timeout argv: {self.text(node)}")
                                return

    # P4 ---------------------------------------------------------------------------------------

    assignments: list = []

    def setattr_args(self, node: ast.Call):
        """(object, name or None, value) of a setattr-like call, or None. The name is None when
        it is not a string constant; a string target "module.NAME" gives a Name object."""
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

    def _setattrs(self) -> None:
        by_function: dict[tuple, list[str]] = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not (got := self.setattr_args(node)):
                continue
            obj, name, value = got
            fn = self.function_of(node)
            if self.qual(obj) == "time" or (isinstance(obj, ast.Name) and obj.id == "time"):
                self.site(node, "P4", f"the time module patched: {self.text(node)}")
                continue
            names = [name] if name is not None else self.resolve_name(node.args[1], node)
            if names is None:
                self.site(node, "P4", f"a name that cannot be resolved: {self.text(node)}")
                continue
            timing = [n for n in names if timing_constant(n)]
            if not timing:
                continue
            given_back = []
            for resolved in timing:
                key = (id(fn), ast.dump(obj), resolved)
                by_function.setdefault(key, []).append(resolved)
                if len(by_function[key]) == 2:
                    given_back.append(resolved)
            if given_back:
                self.site(node, "P4", f"{', '.join(given_back)} given back by a second setattr")
            if self.allowed(value, fn) and not (
                isinstance(value, ast.Name) and value.id in self.parameters(fn)
                and not self._helper_values_allowed(fn, value.id)):  # fmt: skip
                continue
            if fn is not None and ast.dump(obj) in self.doubled_modules(fn):
                self.derived.append(("H7", Site(self.path, node.lineno, "P4", self.text(node))))
                continue
            self.site(node, "P4", f"{', '.join(timing)} = {self.text(value)}")
        for node, target, value in self.assignments:
            fn = self.function_of(node)
            if not self.allowed(value, fn):
                self.site(node, "P4", f"{self.text(target)} = {self.text(value)}")

    def resolve_name(self, node: ast.AST, at: ast.AST) -> list[str] | None:
        """The names a non-literal setattr name takes: a loop variable over a literal tuple or
        list, or a module-local helper's parameter given literals at every call site."""
        if not isinstance(node, ast.Name):
            return None
        for loop in self.enclosing_loops(at):
            if isinstance(loop.target, ast.Name) and loop.target.id == node.id:
                if isinstance(loop.iter, ast.Tuple | ast.List) and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in loop.iter.elts
                ):
                    return [e.value for e in loop.iter.elts]
                return None
        fn = self.function_of(at)
        if isinstance(fn, ast.FunctionDef) and node.id in self.parameters(fn):
            return self.literal_arguments(fn, node.id)
        return None

    def enclosing_loops(self, node: ast.AST) -> Iterator[ast.For]:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.For):
                yield node

    def literal_arguments(self, fn: ast.FunctionDef, param: str) -> list[str] | None:
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        index = params.index(param) if param in params else None
        found: list[str] = []
        calls = 0
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (node.func.id == fn.name)
            ):
                calls += 1
                given = None
                if index is not None and index < len(node.args):
                    given = node.args[index]
                for kw in node.keywords:
                    if kw.arg == param:
                        given = kw.value
                if not (isinstance(given, ast.Constant) and isinstance(given.value, str)):
                    return None
                found.append(given.value)
        return found if calls else None

    def _helper_values_allowed(self, fn: ast.AST, param: str) -> bool:
        """A helper's value parameter is allowed when every call site gives an allowed value."""
        if not isinstance(fn, ast.FunctionDef):
            return False
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        index = params.index(param) if param in params else None
        calls = 0
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (node.func.id == fn.name)
            ):
                calls += 1
                given = node.args[index] if index is not None and index < len(node.args) else None
                for kw in node.keywords:
                    if kw.arg == param:
                        given = kw.value
                if given is None or not self.allowed(given, self.function_of(node)):
                    return False
        return calls > 0


def scan_source(path: str, source: str, table: Table | None = None, tier: str = "host") -> FileScan:
    scan = FileScan(path, source, table or Table(), tier)
    scan.assignments = []
    scan.scan()
    return scan


# --- the scope -----------------------------------------------------------------------------------

# Whole files that run only as another program, imitating a program rcman drives, and the
# programs whose deadlines are their behaviour and decide no test (H11; Q1 of #149): what races
# them is scanned in the tests that run them. Each with its reason; its stale check is
# ``stand_in_problems``.
STAND_INS = {
    "devtools/fake_claude.py": "runs as `claude` in a workspace, imitating Claude Code's timing",
    "devtools/pre_test_commit.py": "the pre-test hook: its DEADLINE and GRACE are its behaviour",
    "devtools/held_folders.py": "the sweep of held folders: UNLOCKED_FOR and TMUX_GONE_WITHIN "
    "are its behaviour",
}

# The roots of the scope now: host Python and devtools. tests/container/ and the TypeScript
# scope join it in the later steps of #149.
SCOPE = ("tests", "devtools")


def scoped_files(roots: Iterable[str] = SCOPE, repo: Path = REPO) -> list[str]:
    """The Python files of ``roots`` that git does not ignore, but the stand-ins, relative to
    ``repo``; tests/container/ is not yet in the scope. No root: no file."""
    roots = list(roots)
    if not roots:
        return []
    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "--", *roots],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=HANG_GUARD,
    ).stdout.split()
    return sorted(
        f
        for f in listed
        if f.endswith(".py")
        and not f.startswith("tests/container/")
        and "/fakes/" not in f
        and (repo / f).exists()
        and f not in STAND_INS
    )


def table_trees() -> list[ast.AST]:
    """The ASTs the parameter table is built from: backend, helpers, tests and devtools."""
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "--",
         "backend", "helpers", "tests", "devtools"],
        cwd=REPO, capture_output=True, text=True, check=True, timeout=HANG_GUARD,
    ).stdout.split()  # fmt: skip
    trees = []
    for f in files:
        if f.endswith(".py") and (REPO / f).exists():
            trees.append(ast.parse((REPO / f).read_text(encoding="utf-8")))
    return trees


# --- the named patterns --------------------------------------------------------------------------


@dataclass(frozen=True)
class Named:
    """A named exemption: the sites of ``forms`` in ``path``, in the functions ``where`` names
    (a prefix of the qualified name; "" for the whole file)."""

    key: str
    path: str
    where: tuple[str, ...]
    forms: frozenset[str]
    reason: str

    def covers(self, site: Site) -> bool:
        return (
            site.path == self.path
            and site.form in self.forms
            and any(
                site.where == w or site.where.startswith(w + ".") or w == "" for w in self.where
            )
        )


NAMED = (
    Named("H1", "tests/until.py", ("until", "until_async"), frozenset({"P1", "P2"}),
          "until's pace between looks and its bound at the tier's guard, which only ends a hang"),
    Named("H1", "tests/until.py", ("",), frozenset({"P3"}),
          "until's PACE and SLOW, the paces between looks"),
    Named("H2", "tests/time_doubles.py",
          ("_Double._hang_left", "FakeTime._poll", "FakeTime.sleep", "WatchedCondition.wait_for"),
          frozenset({"P1", "P2", "P3"}),
          "the doubles' hang bound, which only turns a frozen wait into DoubleHung"),
    Named("H2", "tests/time_doubles.py", ("",), frozenset({"P3"}),
          "the doubles' POLL_CAP and POLL_GAP, and their waits at their hang bound"),
    Named("H3", "tests/hang_guard.py", ("",), frozenset({"P1", "P2", "P3", "P4"}),
          "the hang guard itself, and runs_out and pace, which set a name a test gives them"),
    Named("H3", "tests/conftest.py", ("pytest_timeout_set_timer",), frozenset({"P2", "P3"}),
          "the hang guard's timer"),
    Named("H4", "tests/test_time_doubles.py", ("",), frozenset({"P3"}),
          "the doubles' own tests: the seconds they hand a double, and the short hang bounds "
          "they give the machinery whose ending of a hang they assert"),
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


def stand_in_problems() -> list[str]:
    """Stale checks of STAND_INS: each exists, and is run by a test, by its name."""
    problems = []
    scanned = [REPO / f for f in scoped_files()]
    for path in STAND_INS:
        if not (REPO / path).exists():
            problems.append(f"{path} is gone")
            continue
        stem = Path(path).stem
        if not any(stem in f.read_text(encoding="utf-8") for f in scanned):
            problems.append(f"{path} is named by no scanned file")
    return problems


def scan_tree(
    files: Iterable[str] | None = None,
) -> tuple[list[Site], list[tuple[str, Site]], Table]:
    """Every site of the scope, before the named patterns, the derived allowances, and the
    parameter table."""
    table = build_table(table_trees())
    sites: list[Site] = []
    derived: list[tuple[str, Site]] = []
    for f in scoped_files() if files is None else files:
        scan = scan_source(f, (REPO / f).read_text(encoding="utf-8"), table)
        sites += scan.sites
        derived += scan.derived
    return sites, derived, table


def unexempted(sites: list[Site]) -> list[Site]:
    return [s for s in sites if not any(n.covers(s) for n in NAMED)]


# --- the planted twins ---------------------------------------------------------------------------

# Each planted form, written so that this module's own source holds none (``placed``).


def placed(source: str) -> str:
    """A planted source with its placeholders put in place: this module writes $SLEEP, $TIMEOUT,
    $DATE and $LT for the words, so that its own strings hold no planted form."""
    for placeholder, word in (("$SLEEP", "sleep"), ("$TIMEOUT", "timeout"), ("$DATE", "date"),
                              ("$LT", "-lt")):  # fmt: skip
        source = source.replace(placeholder, word)
    return source


def flagged(source: str, table: Table | None = None, path: str = "tests/test_planted.py"):
    return [(s.form, s.line) for s in scan_source(path, placed(source), table).sites]


PLANTED = {
    # P1
    "a deadline loop": ("P1", "import time\ndeadline = time.monotonic() + 5\n"
                        "while time.monotonic() < deadline:\n    pass\n", 3),
    "a difference stored": ("P1", "import time\nstarted = time.perf_counter()\n"
                            "took = time.perf_counter() - started\n", 3),
    "datetime.now compared": ("P1", "from datetime import datetime as dt\n"
                              "def f(t):\n    return dt.now() > t\n", 3),
    "an aliased clock": ("P1", "import time as t\ndef f(x):\n    return t.monotonic() > x\n", 3),
    "loop.time": ("P1", "def f(loop, end):\n    return loop.time() < end\n", 2),
    # P2
    "time.sleep": ("P2", "import time\ntime.$SLEEP(1)\n", 2),
    "an aliased sleep": ("P2", "from time import $SLEEP as nap\nnap(0.5)\n", 2),
    "asyncio.sleep": ("P2", "import asyncio\nasync def f():\n    await asyncio.$SLEEP(0.1)\n", 3),
    "a Timer": ("P2", "import threading\nthreading.Timer(1.0, print).start()\n", 2),
    "call_later": ("P2", "def f(loop):\n    loop.call_later(2, print)\n", 2),
    # P2s
    "a shell sleep": ("P2s", "argv = ['sh', '-c', '$SLEEP 3; echo done']\n", 1),
    "a timeout tool": ("P2s", "argv = ['sh', '-c', '$TIMEOUT -k 1 5 cat']\n", 1),
    "a Python sleep in a string": ("P2s", "code = 'import time; time.$SLEEP(2)'\n", 1),
    "a timeout argv": ("P2s", "argv = ['timeout', '-s', 'KILL', '30', 'sh']\n", 1),
    "an interpolated sleep": ("P2s", "n = 3\nscript = f'$SLEEP {n}; exit 0'\n", 2),
    "a child's loop on a clock": ("P1", "s = 'while [ $($DATE +%s) $LT 9 ]; do :; done'\n", 1),
    # P3
    "a timeout keyword": ("P3", "import subprocess\nsubprocess.run(['true'], timeout=10)\n", 2),
    "a within keyword": ("P3", "def f(g):\n    g(within=2.5)\n", 2),
    "a _seconds keyword": ("P3", "def f(g):\n    g(hold_seconds=4)\n", 2),
    "a join": ("P3", "def f(t):\n    t.join(5)\n", 2),
    "a wait": ("P3", "def f(e):\n    e.wait(0.5)\n", 2),
    "a wait_for": ("P3", "def f(c, p):\n    c.wait_for(p, 3)\n", 2),
    "a communicate": ("P3", "def f(p):\n    p.communicate(None, 30)\n", 2),
    "a queue get": ("P3", "def f(queue):\n    queue.get(True, 2)\n", 2),
    "a parameter default": ("P3", "def f(x, timeout=30):\n    pass\n", 1),
    "a constant": ("P3", "ANSWERS_WITHIN = 15.0\n", 1),
    "a class constant": ("P3", "class C:\n    POLL_SECONDS = 0.1\n", 2),
    "pytest.mark.timeout": ("P3", "import pytest\n@pytest.mark.timeout(5)\ndef test_x():\n"
                            "    pass\n", 2),
    "tier_guard in a default": ("P3", "from tests.hang_guard import tier_guard\n"
                                "def f(within=tier_guard()):\n    pass\n", 2),
    "tier_guard at module level": ("P3", "from tests.hang_guard import tier_guard\n"
                                   "WAIT_SECONDS = tier_guard()\n", 2),
    "a guard under 1": ("P3", "from tests.hang_guard import HANG_GUARD\n"
                        "def f(g):\n    g(timeout=HANG_GUARD / 2)\n", 3),
    # P4
    "a monkeypatched deadline": ("P4", "def test_x(monkeypatch, mod):\n"
                                 "    monkeypatch.setattr(mod, 'STOP_TIMEOUT', 0.1)\n", 2),
    "a string target": ("P4", "def test_x(monkeypatch):\n"
                        "    monkeypatch.setattr('rcman.x.GRACE_SECONDS', 0.5)\n", 2),
    "mock.patch": ("P4", "from unittest import mock\n"
                   "def test_x():\n    with mock.patch('rcman.x.POLL_SECONDS', 0.01):\n"
                   "        pass\n", 3),
    "mock.patch.object": ("P4", "from unittest import mock\ndef test_x(m):\n"
                          "    with mock.patch.object(m, 'IDLE_SECONDS', 1):\n        pass\n", 3),
    "a plain assignment": ("P4", "def test_x(mod):\n    mod.STOP_TIMEOUT = 0.2\n", 2),
    "a loop-variable name": ("P4", "def test_x(monkeypatch, m):\n"
                             "    for name in ('IDLE_SECONDS', 'HEAD_SECONDS'):\n"
                             "        monkeypatch.setattr(m, name, 0.3)\n", 3),
    "an unresolved name": ("P4", "def test_x(monkeypatch, m, name):\n"
                           "    monkeypatch.setattr(m, name, 0.3)\n", 2),
    "a patch of time itself": ("P4", "import time\ndef test_x(monkeypatch):\n"
                               "    monkeypatch.setattr(time, '$SLEEP', print)\n", 3),
    "a restoring setattr": ("P4", "def test_x(monkeypatch, m):\n"
                            "    monkeypatch.setattr(m, 'WAIT_SECONDS', 0)\n"
                            "    monkeypatch.setattr(m, 'WAIT_SECONDS', 0)\n", 3),
    # the guard's own names
    "a local HANG_GUARD": ("GUARD", "HANG_GUARD = 0.5\n", 1),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_form_is_flagged_where_it_is(name):
    form, source, line = PLANTED[name]
    assert (form, line) in flagged(source), (name, flagged(source))


def test_the_parameter_table_flags_a_number_at_a_timing_position():
    """B5: a position the parameter table gives, for a function, a method and several
    definitions that agree."""
    defs = ast.parse(
        "def gone(path, within):\n    pass\n"
        "class S:\n    def recording_held(self, seconds):\n        pass\n"
        "def run(argv, timeout):\n    pass\n"
        "def run(cmd, wait_timeout=None):\n    pass\n"
    )
    table = build_table([defs])
    assert table.positions == {
        "gone": frozenset({1}),
        "recording_held": frozenset({0}),
        "run": frozenset({1}),
    }
    source = "def test_x(s, p):\n    gone(p, 60)\n    s.recording_held(5)\n    run(['a'], 10)\n"
    assert flagged(source, table) == [("P3", 2), ("P3", 3), ("P3", 4)]


def test_a_name_whose_definitions_disagree_is_left_out_of_the_table_and_listed():
    defs = ast.parse("def held(path, seconds):\n    pass\ndef held(seconds, path):\n    pass\n")
    table = build_table([defs])
    assert "held" not in table.positions and table.disagreeing == {"held"}
    assert flagged("def t(p):\n    held(p, 1.5)\n", table) == []


CLEAN = {
    "the guard": "from tests.hang_guard import HANG_GUARD\ndef f(g):\n    g(timeout=HANG_GUARD)\n",
    "a multiple of the guard": "from tests.hang_guard import HANG_GUARD\n"
    "def f(g):\n    g(timeout=2 * HANG_GUARD)\n",
    "the guard through its module": "from tests import hang_guard\n"
    "def f(g):\n    g(timeout=hang_guard.CONTAINER_HANG_GUARD)\n",
    "tier_guard in a test": "from tests.hang_guard import tier_guard\n"
    "def f(g):\n    g(timeout=tier_guard())\n",
    "None": "def f(g):\n    g(timeout=None)\n",
    "minus one": "def f(lock):\n    lock.acquire(True, -1)\n",
    "zero": "def f(e, g):\n    e.wait(0)\n    g(within=0)\n",
    "a sleep of 0 (H8)": "import asyncio\nasync def f():\n    await asyncio.$SLEEP(0)\n",
    "a parameter passed on": "def f(g, timeout):\n    g(timeout=timeout)\n",
    "a product constant (H9)": "from rcman import runtime as rt\n"
    "def f(g, timeout=rt.PODMAN_TIMEOUT):\n    g(timeout=rt.PODMAN_TIMEOUT)\n",
    "str.join": "def f(parts):\n    return ', '.join(parts)\n",
    "dict.get": "def f(d):\n    return d.get('k', 5)\n",
    "a fake's sleep and advance (H7)": "from tests.time_doubles import FakeTime\n"
    "def test_x():\n    fake = FakeTime()\n    fake.advance(600)\n    fake.$SLEEP(5)\n",
    "a constant of a module whose time is a double (H7)": "from tests.time_doubles import "
    "FakeTime\n"
    "def test_x(monkeypatch, mod):\n    fake = FakeTime()\n"
    "    monkeypatch.setattr(mod, 'time', fake)\n"
    "    monkeypatch.setattr(mod, 'STOP_TIMEOUT', 5)\n",
    "a non-timing setattr": "def test_x(monkeypatch, m):\n"
    "    monkeypatch.setattr(m, 'NAME', 'x')\n",
    "a resolved non-timing name": "def test_x(monkeypatch, m):\n"
    "    for name in ('_changed', '_closing'):\n        monkeypatch.setattr(m, name, None)\n",
    "a child's wait loop (H13)": "s = 'while [ ! -e f ]; do $SLEEP 0.05; done'\n",
    "a child's endless loop (H13)": "s = 'while :; do $SLEEP 1; done'\n",
    "a child's Python loop (H13)": "code = 'import time\\nwhile True:\\n"
    "    time.$SLEEP(0.01)\\n'\n",
    "a child's sleep at the guard (OUT)": "from tests.hang_guard import HANG_GUARD\n"
    "s = f'$SLEEP {3 * HANG_GUARD}'\n",
    "a child's timeout at RUNS_OUT": "from tests.hang_guard import RUNS_OUT\n"
    "s = f'$TIMEOUT {RUNS_OUT} cat'\n",
    "a benchmark (H10)": "import time\nimport pytest\ndef measure():\n"
    "    s = time.perf_counter()\n    return time.perf_counter() - s\n"
    "@pytest.mark.benchmark\ndef test_b():\n    measure()\n",
    "a clock read not compared": "import time\ndef f():\n    return time.time()\n",
}


@pytest.mark.parametrize("name", sorted(CLEAN))
def test_an_allowed_form_is_not_flagged(name):
    assert flagged(CLEAN[name]) == []


def test_each_planted_twin_has_its_form_flagged_once_and_nothing_else():
    """The planted forms are flagged as exactly one site of their form, so a twin cannot pass
    on another form's site."""
    for name, (form, source, line) in PLANTED.items():
        found = flagged(source)
        assert found.count((form, line)) == 1, (name, found)


def test_the_derived_allowances_are_recorded():
    """H7, H8, H10 and H13 are recorded where they allow a site, for the real tree's
    non-vacuity checks."""
    for name, key in (
        ("a fake's sleep and advance (H7)", "H7"),
        ("a sleep of 0 (H8)", "H8"),
        ("a benchmark (H10)", "H10"),
        ("a child's wait loop (H13)", "H13"),
    ):
        assert key in {k for k, _ in scan_source("t.py", placed(CLEAN[name])).derived}, name


# --- the scope -----------------------------------------------------------------------------------


def test_each_root_of_the_scope_is_read_and_holds_a_named_file():
    files = scoped_files()
    for root, known in (("tests", "tests/test_until.py"), ("devtools", "devtools/tmuxctl.py")):
        assert any(f.startswith(root + "/") for f in files), root
        assert known in files
    assert not any(f.startswith("tests/container/") for f in files)  # a later step's
    assert not set(STAND_INS) & set(files)


def test_an_empty_scope_reads_nothing():
    """With no root nothing is read, where git ls-files without a path would list everything."""
    assert scoped_files(()) == []
    assert scoped_files(("no-such-root",)) == []


def test_a_planted_file_in_each_root_is_read(tmp_path):
    """A file of the scanned shape, planted under each root of a scratch repository, is listed
    and its site found; one under tests/container/ and a stand-in are not listed."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=HANG_GUARD)
    planted = ["tests/planted.py", "devtools/planted.py"]
    for rel in [*planted, "tests/container/planted.py", "devtools/fake_claude.py"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(placed("import time\ntime.$SLEEP(1)\n"), encoding="utf-8")
    assert scoped_files(SCOPE, tmp_path) == sorted(planted)
    for rel in planted:
        source = (tmp_path / rel).read_text(encoding="utf-8")
        assert ("P2", 2) in [(s.form, s.line) for s in scan_source(rel, source).sites]


def test_the_stand_ins_are_still_there_and_still_run():
    assert stand_in_problems() == []


def test_a_named_pattern_whose_subject_is_gone_is_reported():
    gone = Named("H1", "tests/until.py", ("no_such_function",), frozenset({"P2"}), "planted")
    assert named_problems([], [gone]) == [
        "H1: tests/until.py ('no_such_function',) holds no site of ['P2']"
    ]
    missing = Named("H1", "tests/no_such.py", ("",), frozenset({"P2"}), "planted")
    assert named_problems([], [missing]) == ["H1: tests/no_such.py is gone"]
