"""Nothing outside the clock reads the time or waits on real time, and every timing value says
what kind of seconds it is.

The corpus is what rcman runs, read from the corpus of ``devtools/docs_guard.py`` (tracked files
and untracked ones git does not ignore): the Python files under ``ROOTS``, ``helpers/clock.py``
among them, and the fake ``claude`` (``FAKE``). The tests and the other devtools are outside it,
and so are the workspace's shell scripts and the text of a child program held in a string.

The rules, each a ``Site.rule``:

- ``a``: no time read and no timed wait but through a clock. What ``RealClock`` hands each call to
  is the forbidden set, read from its body, with the rest of ``time``'s and ``datetime``'s reads
  and the standard library's other timed waits (``FORBIDDEN``, ``WALL``, ``TIMED``, ``METHODS``,
  ``TIMERS``), a socket's timeouts among them. An alias is followed wherever the file imports
  it or assigns a module or class of ``TIME_MODULES`` to a name, a reference is a site without a
  call, and so is ``getattr`` of ``time``. A wait with no seconds is allowed, and so is
  ``asyncio.sleep(0)``, a yield. A clock call is not a site: a call of a timed method only the
  clock has (``Clockwork.only``, read from the protocol), whatever it is called on but a
  module, or of one it shares with the standard library on a receiver whose last name holds
  "clock" and given its seconds where the protocol has them.
- ``b``: every seconds value at a timed site is classified: the seconds of a clock call (the
  position of the ``Clock`` protocol's ``seconds``), any ``timeout=`` (written, or in a ``**``
  of a dict written out), a timeout the standard library takes by position
  (``POSITIONAL_TIMEOUTS``), the corpus's own parameters named in ``TIMED_PARAMETERS`` (their
  defaults, a class's fields of those names, and what a call gives them, matched by the
  function's name, where only a value that reads as seconds is a site), and what a program is
  handed in an argument list after ``timeout`` or ``sleep``, or in ``--time=`` and
  ``--timeout=``, written or formatted.
  ``allowed`` says which values are classified; seconds written as text (``SECONDS_TEXT``) are
  not.
- ``c``: no name ``PollOwn``: a wait on rcman's own state is an event or a condition.
- ``d``: no subscript or ``.get`` of ``globals()``, ``vars()`` or ``locals()``, which would hide
  a constant from ``allowed``.
- ``e``: every numeric constant named as seconds (``SECONDS_NAME``), a module's or a class's, is
  a marker call.

What it does not follow: a module reached through ``importlib`` or ``__import__``; keyword
arguments spread from anything but a dict written out; a method looked up by ``getattr`` on an
object; a timeout set on an object other than through a call this module names; a class's
constant read through ``self`` or ``cls``, an object's attribute (rule ``e`` holds the constant
itself); and ``join`` and ``get``, which it tells from ``str.join`` and ``dict.get`` by what they
are given, not by what they are called on, so a ``str.join`` on a separator held in a variable
(``sep.join(parts)``) is taken for a thread's join with seconds and flagged: such a join is
written on its separator itself or given a collection. Review covers the rest.

``NOT_YET_ON_THE_CLOCK`` names what is not on the clock yet, each key with the number of sites
it covers and the ``Owner``, the change that removes it. The one permanent exemption is the class
``RealClock`` itself. project-knowledge/tests.md describes the guard.
"""

from __future__ import annotations

import ast
import builtins
import enum
import inspect
import re
import subprocess
import textwrap
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pytest

from devtools import docs_guard

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
ROOTS = ("backend/rcman/", "helpers/", "installer/")
FAKE = "devtools/fake_claude.py"
CLOCK = "helpers/clock.py"
# The class whose body is the standard library's time and waits: the one permanent exemption.
REAL_CLOCK = "RealClock"


# --- the corpus ---------------------------------------------------------------------------------


def corpus(root: Path) -> list[str]:
    """The Python files rcman runs, repository-relative."""
    return [
        rel
        for rel in docs_guard.corpus_files(root)
        if rel.endswith(".py") and (rel.startswith(ROOTS) or rel == FAKE)
    ]


def module_name(rel: str) -> str:
    """The name a file is imported by: backend/ is on the path, the repository's root too."""
    parts = rel.removesuffix(".py").split("/")
    if parts[0] == "backend":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass(eq=False)
class Module:
    rel: str
    name: str
    source: str
    tree: ast.Module
    # Module-level names and the values they are given; a name given a value more than once
    # keeps each.
    constants: dict[str, list[ast.expr]] = field(default_factory=dict)
    # Each name the file imports, anywhere in it, and the dotted name it stands for.
    imports: dict[str, str] = field(default_factory=dict)
    # The names the file's own module-level functions and classes have.
    defined: set[str] = field(default_factory=set)


def _module_level(body):
    """The statements at a module's top level, those inside its if, try and with blocks too."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for block in ("body", "orelse", "finalbody"):
                yield from _module_level(getattr(node, block, []))
            for handler in getattr(node, "handlers", []):
                yield from _module_level(handler.body)


def _given(targets, value):
    """(name, value) for each name an assignment gives a value, a tuple's element by element."""
    for target in targets:
        if isinstance(target, ast.Name):
            yield target, value
        elif isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
                yield from (
                    pair
                    for t, v in zip(target.elts, value.elts, strict=True)
                    for pair in _given([t], v)
                )
            else:
                yield from ((t, None) for t in target.elts if isinstance(t, ast.Name))


def _assignments(node):
    if isinstance(node, ast.Assign):
        return list(_given(node.targets, node.value))
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return list(_given([node.target], node.value))
    return []


def _imports(tree, package: str) -> dict[str, str]:
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    found[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, *([base] if base else [])])
            for alias in node.names:
                found[alias.asname or alias.name] = f"{base}.{alias.name}"
    return found


def _assigned_aliases(tree, imports: dict[str, str]) -> None:
    """A name given a module of TIME_MODULES, or a class of one, as ``DT = datetime.datetime``,
    anywhere in the file: an alias as an import's is."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            dotted = _dotted(node.value, imports)
            if (
                isinstance(target, ast.Name)
                and target.id not in imports
                and dotted
                and dotted.split(".")[0] in TIME_MODULES
            ):
                imports[target.id] = dotted


def read_module(rel: str, source: str) -> Module:
    tree = ast.parse(source)
    name = module_name(rel)
    package = name if rel.endswith("__init__.py") else name.rpartition(".")[0]
    module = Module(rel, name, source, tree, imports=_imports(tree, package))
    _assigned_aliases(tree, module.imports)
    for node in _module_level(tree.body):
        for target, value in _assignments(node):
            module.constants.setdefault(target.id, []).append(value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module.defined.add(node.name)
    return module


# --- the clock: its markers, its calls and what the real one hands on ----------------------------


def markers(clock_source: str) -> set[str]:
    """The NewTypes helpers/clock.py defines: the markers."""
    found = set()
    for node in ast.parse(clock_source).body:
        for target, value in _assignments(node):
            if (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", None) == "NewType"
                and value.args
                and getattr(value.args[0], "value", None) == target.id
            ):
                found.add(target.id)
    return found


def _class(tree, name: str) -> ast.ClassDef:
    """The module's class of that name, or an empty one where it has none."""
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    return found[0] if found else ast.ClassDef(name=name, body=[])


def clock_seconds(clock_source: str) -> dict[str, int]:
    """Each method of the Clock protocol that takes seconds, and the position of its seconds."""
    found = {}
    for node in _class(ast.parse(clock_source), "Clock").body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [a.arg for a in node.args.args[1:]]
            if "seconds" in names:
                found[node.name] = names.index("seconds")
    return found


def _dotted(node, imports: dict[str, str]) -> str | None:
    """The dotted name an expression stands for through the file's imports: ``t.monotonic``
    after ``import time as t`` is ``time.monotonic``."""
    if isinstance(node, ast.Name):
        return imports.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, imports)
        return f"{base}.{node.attr}" if base else None
    return None


def delegated(clock_source: str) -> set[str]:
    """What RealClock's body hands its calls to, of the standard library's modules."""
    tree = ast.parse(clock_source)
    imports = _imports(tree, "helpers")
    found = set()
    for node in ast.walk(_class(tree, REAL_CLOCK)):
        if isinstance(node, ast.Call):
            name = _dotted(node.func, imports)
            if name and name.split(".")[0] in {"time", "asyncio", "threading"}:
                found.add(name)
    return found


class Clockwork(NamedTuple):
    markers: frozenset[str]
    seconds: dict[str, int]
    forbidden: frozenset[str]
    # The clock's timed methods no standard library object has (METHODS): a call of one is a
    # clock call whatever it is called on.
    only: frozenset[str] = frozenset()


def clockwork(clock_source: str) -> Clockwork:
    """What the guard reads from helpers/clock.py: the markers, the clock's timed calls, and the
    forbidden set (FORBIDDEN): what RealClock delegates to, with each time read's _ns form."""
    handed = delegated(clock_source)
    reads = {n for n in handed if n.startswith("time.") and n != "time.sleep"}
    forbidden = handed | {f"{n}_ns" for n in reads} | FORBIDDEN
    seconds = clock_seconds(clock_source)
    only = frozenset(seconds) - set(METHODS)
    return Clockwork(frozenset(markers(clock_source)), seconds, forbidden, only)


# The time reads and timed waits forbidden beside what RealClock delegates to; a timed one of
# them with no seconds (TIMED) is allowed.
FORBIDDEN = frozenset(
    {
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.process_time_ns",
        "time.thread_time",
        "time.thread_time_ns",
        "time.clock_gettime",
        "time.clock_gettime_ns",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
        "datetime.date.today",
        "asyncio.timeout",
        "asyncio.timeout_at",
        "threading.Timer",
        "signal.alarm",
        "signal.setitimer",
    }
)
# Wall-clock reads that read the time only when given none: the number of arguments that
# gives one.
WALL = {"time.localtime": 1, "time.gmtime": 1, "time.ctime": 1, "time.strftime": 2}
# Calls that wait only when given seconds: the position of the seconds, and None for a
# keyword only. select.select waits without end when given none.
TIMED = {
    "asyncio.wait": None,
    "asyncio.wait_for": 1,
    "asyncio.timeout": 0,
    "asyncio.timeout_at": 0,
    "select.select": 3,
    "socket.setdefaulttimeout": 0,
    "concurrent.futures.wait": 1,
}
# The one wait with seconds that is no wait: a sleep of a literal 0 yields to the event loop.
YIELD = "asyncio.sleep"
# Methods that wait when given seconds: the position of the seconds, or None for a keyword
# only. join's positional is seconds unless it is text or a collection, as str.join's is; get's
# only after a block flag, as a queue's get(True, 5).
METHODS = {
    "wait": 0,
    "wait_for": 1,
    "acquire": 1,
    "join": 0,
    "result": 0,
    "select": 0,
    "poll": 0,
    "settimeout": 0,
    "communicate": None,
    "get": None,
}
# What str.join is given, which no thread's join is.
JOINED = (ast.List, ast.Tuple, ast.Set, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.JoinedStr)
# Standard library calls that take a timeout as a positional argument: its position.
POSITIONAL_TIMEOUTS = {
    "urllib.request.urlopen": 2,
    "socket.create_connection": 1,
    "http.client.HTTPConnection": 2,
}
# The modules whose objects an assignment makes an alias of.
TIME_MODULES = {"time", "datetime", "asyncio", "threading", "signal", "select", "socket"}
# Methods that set a timer.
TIMERS = {"call_later", "call_at"}
# The calls given seconds of their own that keep the kind of seconds they are given.
KEEPING = {"min", "max", "int", "float", "str", "round", "abs"}
# The names of the corpus's own parameters that take seconds.
TIMED_PARAMETERS = {"timeout", "seconds", "wait", "within", "grace", "interval"}
# Seconds written as text, as sleep(1) and timeout(1) read them.
SECONDS_TEXT = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+)[smhd]?\s*")
# A module constant named as seconds.
SECONDS_NAME = re.compile(r"(^|_)(SECONDS|TIMEOUT|GRACE|WITHIN|WAIT)$")
# What a program given seconds in its arguments is called.
PROGRAMS = {"timeout", "sleep"}
# The flags of timeout(1): those that take a value, and the one whose value is seconds.
TIMEOUT_FLAGS = {"-s", "--signal"}
KILL_AFTER = {"-k", "--kill-after"}
# The formatted flags that hand a program seconds.
SECONDS_FLAGS = ("--time=", "--timeout=")
# The names that would hide a constant from the guard.
NAMESPACES = {"globals", "vars", "locals"}
POLL_OWN = "PollOwn"


class Site(NamedTuple):
    rel: str
    qualname: str
    rule: str
    line: int
    text: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.rel, self.qualname, self.rule, self.text)

    def __str__(self) -> str:
        return (
            f"{self.rel}:{self.line} in {self.qualname or 'the module'}: ({self.rule}) {self.text}"
        )


def _string(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _number(node) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _seconds_text(node) -> bool:
    """Text that is seconds other than none: a number, with a unit of sleep(1)'s or none."""
    text = _string(node)
    match = SECONDS_TEXT.fullmatch(text) if text is not None else None
    return bool(match) and float(match[1]) != 0


def _literal_only(node) -> bool:
    """A number, or arithmetic of numbers alone."""
    if isinstance(node, ast.BinOp):
        return _literal_only(node.left) and _literal_only(node.right)
    if isinstance(node, ast.UnaryOp):
        return _literal_only(node.operand)
    return _number(node)


def _none(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _keyword(call, name):
    return next((k for k in call.keywords if k.arg == name), None)


def _receiver(func) -> str | None:
    """The last name of what a method is called on."""
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _parameters(function) -> list[str]:
    a = function.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x]


# --- the corpus read together: what a name stands for ------------------------------------------


class Corpus:
    """Every file of the corpus, by the name it is imported by, and what the guard reads from
    helpers/clock.py. ``exempt_constants`` are the module constants exempted under rule (e)."""

    def __init__(self, modules: dict[str, Module], work: Clockwork, exempt_constants=frozenset()):
        self.modules = {m.name: m for m in modules.values()}
        self.by_rel = modules
        self.work = work
        self.exempt_constants = exempt_constants
        # The corpus's functions with a timed parameter: its name -> {parameter: position}.
        self.timed: dict[str, dict[str, int]] = {}
        for module in modules.values():
            for node in ast.walk(module.tree):
                # The clock's own methods are clock calls, read by their receiver.
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (
                    module.rel == CLOCK and isinstance(getattr(node, "_parent", None), ast.ClassDef)
                ):
                    positional = [x.arg for x in (*node.args.posonlyargs, *node.args.args)]
                    if positional[:1] in (["self"], ["cls"]):
                        positional = positional[1:]
                    for name in TIMED_PARAMETERS & set(_parameters(node)):
                        given = self.timed.setdefault(node.name, {})
                        given[name] = positional.index(name) if name in positional else -1

    def lookup(self, dotted: str):
        """A corpus module, or (module, constant), that a dotted name stands for."""
        if dotted in self.modules:
            return self.modules[dotted]
        base, _, name = dotted.rpartition(".")
        if base in self.modules:
            module = self.modules[base]
            if name in module.constants:
                return module, name
            if f"{base}.{name}" in self.modules:
                return self.modules[f"{base}.{name}"]
        return None

    def marker_call(self, node, module: Module) -> bool:
        if not isinstance(node, ast.Call):
            return False
        dotted = _dotted(node.func, module.imports)
        if module.rel == CLOCK and isinstance(node.func, ast.Name):
            dotted = f"helpers.clock.{node.func.id}"
        return bool(dotted) and dotted.rpartition(".")[::2] in {
            ("helpers.clock", m) for m in self.work.markers
        }


class Scope(NamedTuple):
    """Where a value is read: its module and the functions it is in, outermost first."""

    module: Module
    functions: tuple


def _bindings(function):
    """Each name a function binds, and how: the value given it, ("aug", op, value) for an
    augmented assignment, or None where it is bound otherwise (a loop, a with, an import)."""
    found: dict[str, list] = {}

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if not isinstance(child, ast.Lambda):
                    found.setdefault(child.name, []).append(None)
                continue
            for target, value in _assignments(child):
                found.setdefault(target.id, []).append(value)
            if isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
                found.setdefault(child.target.id, []).append(("aug", child.op, child.value))
            if isinstance(child, ast.NamedExpr):
                found.setdefault(child.target.id, []).append(child.value)
            targets = []
            if isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
                targets = [child.target]
            if isinstance(child, ast.withitem) and child.optional_vars is not None:
                targets = [child.optional_vars]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        found.setdefault(name.id, []).append(None)
            if isinstance(child, ast.ExceptHandler) and child.name:
                found.setdefault(child.name, []).append(None)
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    found.setdefault(alias.asname or alias.name.split(".")[0], []).append(None)
            walk(child)

    walk(function)
    return found


class Values:
    """Whether a value at a timed site says what kind of seconds it is (``allowed``), and
    whether one reads as seconds (``reads_as_seconds``)."""

    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self._bound: dict[int, dict] = {}

    def bound(self, function) -> dict:
        if id(function) not in self._bound:
            self._bound[id(function)] = _bindings(function)
        return self._bound[id(function)]

    # A value's kind.

    def allowed(self, node, scope: Scope, seen=frozenset()) -> bool:
        """A marker call; a name that resolves to a labelled constant, or one exempted under
        (e); a parameter, or a local every value of which is allowed; an attribute of an object
        that is not a module; a call's result; None or 0; KEEPING's calls or math.ceil of
        allowed values, a conditional or ``or`` of them; and arithmetic of them, where a number
        is allowed only as a factor or a divisor."""
        if isinstance(node, ast.Constant):
            if _number(node):
                return node.value == 0
            return not _seconds_text(node)
        if isinstance(node, ast.Name):
            return self._name(node.id, scope, seen)
        if isinstance(node, ast.Attribute):
            module = self._module_of(node.value, scope)
            if module is None:
                return True  # an attribute of an object
            if not isinstance(module, Module):
                return False  # a module outside the corpus: nothing classifies it
            return self._constant(module, node.attr, seen)
        if isinstance(node, ast.Call):
            if self.corpus.marker_call(node, scope.module):
                return True
            if self._keeping(node, scope):
                return all(self.allowed(a, scope, seen) for a in node.args)
            return True  # a call's result
        if isinstance(node, ast.IfExp):
            return self.allowed(node.body, scope, seen) and self.allowed(node.orelse, scope, seen)
        if isinstance(node, ast.BoolOp):
            return all(self.allowed(v, scope, seen) for v in node.values)
        if isinstance(node, ast.BinOp):
            return self._arithmetic(node.op, node.left, node.right, scope, seen)
        if isinstance(node, ast.UnaryOp):
            return self.allowed(node.operand, scope, seen)
        if isinstance(node, ast.NamedExpr):
            return self.allowed(node.value, scope, seen)
        return True  # not a number: a lambda, a string formatted, an await

    def _arithmetic(self, op, left, right, scope, seen) -> bool:
        """A number is allowed as a factor or a divisor of something that is not a number."""
        factor = isinstance(op, (ast.Mult, ast.Div, ast.FloorDiv))
        return all(
            (factor and _number(side) and not _literal_only(other))
            or self.allowed(side, scope, seen)
            for side, other in ((left, right), (right, left))
        )

    def _keeping(self, call, scope) -> bool:
        func = call.func
        if isinstance(func, ast.Name) and func.id in KEEPING:
            return not self._local(func.id, scope) and func.id not in scope.module.imports
        return _dotted(func, scope.module.imports) == "math.ceil"

    def _local(self, name, scope):
        for function in reversed(scope.functions):
            if name in _parameters(function) or name in self.bound(function):
                return function
        return None

    def _name(self, name, scope: Scope, seen) -> bool:
        functions = list(scope.functions)
        while functions:
            function = functions.pop()
            if name in _parameters(function):
                return True
            bindings = self.bound(function).get(name)
            if bindings is not None:
                if (id(function), name) in seen:
                    return True
                seen = seen | {(id(function), name)}
                inner = Scope(scope.module, (*functions, function))
                return all(self._binding(b, name, inner, seen) for b in bindings)
        return self._module_name(scope.module, name, seen)

    def _binding(self, binding, name, scope, seen) -> bool:
        if binding is None:
            return True
        if isinstance(binding, tuple):
            _, op, value = binding
            return self._arithmetic(op, ast.Name(id=name), value, scope, seen)
        return self.allowed(binding, scope, seen)

    def _module_name(self, module: Module, name: str, seen) -> bool:
        if name in module.constants:
            return self._constant(module, name, seen)
        if name in module.imports:
            found = self.corpus.lookup(module.imports[name])
            if isinstance(found, tuple):
                return self._constant(*found, seen)
            return found is not None  # a module, or a name from outside the corpus
        return name in module.defined or hasattr(builtins, name)

    def _constant(self, module: Module, name: str, seen) -> bool:
        if (module.rel, name) in self.corpus.exempt_constants:
            return True
        if name not in module.constants:
            return f"{module.name}.{name}" in self.corpus.modules or name in module.defined
        if (module.rel, name) in seen:
            return True
        seen = seen | {(module.rel, name)}
        at_top = Scope(module, ())
        return all(v is None or self.allowed(v, at_top, seen) for v in module.constants[name])

    def _module_of(self, node, scope: Scope):
        """The module an expression names, a Module of the corpus or the dotted name of one
        outside it, or None for an object."""
        if isinstance(node, ast.Name) and self._local(node.id, scope):
            return None
        dotted = _dotted(node, scope.module.imports)
        if dotted is None:
            return None
        found = self.corpus.lookup(dotted)
        if isinstance(found, Module):
            return found
        if found is None and isinstance(node, ast.Name):
            imported = [
                n for n in ast.walk(scope.module.tree) if isinstance(n, ast.Import)
            ]  # ``import x`` names a module; ``from x import y`` may name anything
            if any(
                (a.asname or a.name.split(".")[0]) == node.id for n in imported for a in n.names
            ):
                return dotted
        return None

    # What reads as seconds where a name is shared with other parameters.

    def reads_as_seconds(self, node, scope: Scope) -> bool:
        """A number, seconds as text, arithmetic of numbers alone or with a number added, or
        an unlabelled numeric constant, alone or in arithmetic."""
        if _number(node):
            return node.value != 0
        if _string(node) is not None:
            return _seconds_text(node)
        if isinstance(node, ast.BinOp):
            sides = (node.left, node.right)
            if _literal_only(node):
                return True
            if isinstance(node.op, (ast.Add, ast.Sub)) and any(
                _number(side) and side.value != 0 for side in sides
            ):
                return True
            return any(not _number(side) and self.reads_as_seconds(side, scope) for side in sides)
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name) and self._local(node.id, scope):
                return False
            return self.numeric(node, scope) and not self.allowed(node, scope)
        return False

    def numeric(self, node, scope: Scope, seen=frozenset()) -> bool:
        """A number, or arithmetic, a marker call or a conversion with one in it, or a name
        that resolves to a constant that is one."""
        if _number(node):
            return True
        if isinstance(node, ast.BinOp):
            return self.numeric(node.left, scope, seen) or self.numeric(node.right, scope, seen)
        if isinstance(node, ast.UnaryOp):
            return self.numeric(node.operand, scope, seen)
        if isinstance(node, ast.Call):
            if self.corpus.marker_call(node, scope.module) or self._keeping(node, scope):
                return any(self.numeric(a, scope, seen) for a in node.args)
            return False
        found = None
        if isinstance(node, ast.Name) and not self._local(node.id, scope):
            module = scope.module
            if node.id in module.constants:
                found = (module, node.id)
            elif node.id in module.imports:
                found = self.corpus.lookup(module.imports[node.id])
        elif isinstance(node, ast.Attribute):
            module = self._module_of(node.value, scope)
            if isinstance(module, Module) and node.attr in module.constants:
                found = (module, node.attr)
        if isinstance(found, tuple) and found not in seen:
            module, name = found
            return any(
                v is not None and self.numeric(v, Scope(module, ()), seen | {found})
                for v in module.constants[name]
            )
        return False


# --- the scan -----------------------------------------------------------------------------------


def _segment(module: Module, node) -> str:
    return ast.get_source_segment(module.source, node) or ""


class Scan:
    """The sites of one file of the corpus."""

    def __init__(self, corpus: Corpus, module: Module):
        self.corpus, self.module, self.work = corpus, module, corpus.work
        self.values = Values(corpus)
        self.sites: list[Site] = []
        self.timed_nodes: set[int] = set()
        self.waits: set[int] = set()
        self.parts: set[int] = set()  # the text of an f-string, read with the f-string

    def site(self, rule, node, qualname, text=None):
        line = getattr(node, "lineno", 0)
        self.sites.append(
            Site(self.module.rel, qualname, rule, line, text or _segment(self.module, node))
        )

    def run(self) -> list[Site]:
        self._visit(self.module.tree, "", ())
        self._rule_e(self.module.tree.body, "")
        return sorted(self.sites, key=lambda s: (s.line, s.rule, s.text))

    def _visit(self, node, qualname, functions):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            self._defaults(node, qualname, Scope(self.module, functions))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{qualname}.{node.name}" if qualname else node.name
        if isinstance(node, ast.ClassDef):
            self._rule_e(node.body, qualname, in_class=True)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            functions = (*functions, node)
        scope = Scope(self.module, functions)
        self._rule_a(node, qualname)
        self._rule_b(node, qualname, scope)
        self._rule_c(node, qualname)
        self._rule_d(node, qualname)
        for child in ast.iter_child_nodes(node):
            self._visit(child, qualname, functions)

    def _clock_call(self, call) -> bool:
        """A call of a method only the clock has, on anything but a module; or of a timed
        method it shares with the standard library, on a receiver whose last name holds
        "clock", given its seconds where the protocol has them."""
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in self.work.seconds:
            return False
        if self._is_module(func.value):
            return False
        if func.attr in self.work.only:
            return True
        if "clock" not in (_receiver(func) or "").lower():
            return False
        position = self.work.seconds[func.attr]
        return _keyword(call, "seconds") is not None or len(call.args) > position

    def _is_module(self, node) -> bool:
        """A module the file imports, or one in it, as ``os.path``."""
        if isinstance(node, ast.Attribute):
            return self._is_module(node.value)
        return isinstance(node, ast.Name) and any(
            (a.asname or a.name.split(".")[0]) == node.id
            for n in ast.walk(self.module.tree)
            if isinstance(n, ast.Import)
            for a in n.names
        )

    # (e) Every numeric constant named as seconds, a module's or a class's, has its marker.

    def _rule_e(self, body, qualname, in_class=False):
        for node in _module_level(body):
            for target, value in _assignments(node):
                if in_class and value is not None and target.id in TIMED_PARAMETERS:
                    self._strict(value, qualname, Scope(self.module, ()))  # a field's default
                if value is not None and SECONDS_NAME.search(target.id):
                    scope = Scope(self.module, ())
                    if self.values.numeric(value, scope) and not self.corpus.marker_call(
                        value, self.module
                    ):
                        self.site("e", target, qualname, target.id)

    # (a) No time read and no timed wait but through a clock.

    def _rule_a(self, node, qualname):
        imports = self.module.imports
        if isinstance(node, ast.Call):
            if self._forbidden_call(node, imports):
                self.waits.add(id(node))  # its seconds are this site's, not one of their own
                self.site("a", node, qualname)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            return
        if isinstance(node, (ast.Attribute, ast.Name)) and not self._called(node):
            name = _dotted(node, imports)
            if name and (name in self.work.forbidden or name in WALL or name in TIMED):
                if not self._part_of_dotted(node):
                    self.site("a", node, qualname)

    def _called(self, node) -> bool:
        parent = getattr(node, "_parent", None)
        return isinstance(parent, ast.Call) and parent.func is node

    def _part_of_dotted(self, node) -> bool:
        parent = getattr(node, "_parent", None)
        return (
            isinstance(parent, ast.Attribute) and _dotted(parent, self.module.imports) is not None
        )

    def _forbidden_call(self, call, imports) -> bool:
        name = _dotted(call.func, imports)
        if name == "builtins.getattr" or (
            isinstance(call.func, ast.Name) and call.func.id == "getattr"
        ):
            if len(call.args) >= 2 and _string(call.args[1]) is not None:
                base = _dotted(call.args[0], imports)
                got = f"{base}.{_string(call.args[1])}" if base else None
                return base == "time" or (got is not None and got in self.work.forbidden)
            return False
        if name is not None:
            if name == YIELD and len(call.args) == 1 and not call.keywords:
                if _number(call.args[0]) and call.args[0].value == 0:
                    return False
            if name in TIMED:
                return self._seconds_given(call, TIMED[name])
            if name in WALL:
                return len(call.args) < WALL[name] or (
                    len(call.args) == WALL[name] and _none(call.args[-1])
                )
            if name in self.work.forbidden:
                return True
        func = call.func
        if not isinstance(func, ast.Attribute) or self._clock_call(call):
            return False
        if self._is_module(func.value):
            return False  # a module's function, not a method
        if func.attr in TIMERS:
            return True
        if func.attr == "time" and not call.args and self._loop(func.value):
            return True
        if func.attr in METHODS:
            position = METHODS[func.attr]
            if func.attr == "join":
                given = _keyword(call, "timeout")
                if given is not None:
                    return not _none(given.value)
                return (
                    len(call.args) == 1
                    and not isinstance(func.value, ast.Constant)
                    and not isinstance(call.args[0], JOINED)
                    and _string(call.args[0]) is None
                )
            if func.attr == "get" and _keyword(call, "timeout") is None:
                flag = call.args[0] if call.args else None
                return (
                    len(call.args) >= 2
                    and isinstance(flag, ast.Constant)
                    and isinstance(flag.value, bool)
                    and not _none(call.args[1])
                )
            if func.attr == "acquire":
                blocking = _keyword(call, "blocking")
                if (
                    call.args
                    and _string(call.args[0]) is None
                    and getattr(call.args[0], "value", True) is False
                ):
                    return False
                if blocking is not None and getattr(blocking.value, "value", True) is False:
                    return False
            return self._seconds_given(call, position)
        return False

    def _loop(self, node) -> bool:
        if isinstance(node, ast.Call):
            return getattr(node.func, "attr", getattr(node.func, "id", "")) in {
                "get_running_loop",
                "get_event_loop",
            }
        name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        return "loop" in name.lower()

    @staticmethod
    def _seconds_given(call, position) -> bool:
        given = _keyword(call, "timeout")
        if given is None and position is not None and len(call.args) > position:
            given = call.args[position]
        else:
            given = given.value if given is not None else None
        return given is not None and not _none(given)

    # (b) Every seconds value at a timed site is classified.

    def _rule_b(self, node, qualname, scope):
        if isinstance(node, ast.Call):
            self._call_sites(node, qualname, scope)
            self._argv(node.args, qualname, scope)
        if isinstance(node, (ast.List, ast.Tuple)):
            self._argv(node.elts, qualname, scope)
        text = _string(node)
        if text is not None and text.startswith(SECONDS_FLAGS) and id(node) not in self.parts:
            handed = ast.Constant(text.partition("=")[2])
            if not self.values.allowed(handed, scope):
                self.site("b", node, qualname)
        if isinstance(node, ast.JoinedStr):
            self.parts |= {id(part) for part in node.values}
            for part, value in zip(node.values, node.values[1:], strict=False):
                text = _string(part)
                if text is not None and text.endswith(SECONDS_FLAGS):
                    if isinstance(value, ast.FormattedValue):
                        self._strict(value.value, qualname, scope)

    def _defaults(self, function, qualname, scope):
        """The default of a timed parameter, read where the function is defined."""
        a = function.args
        positional = [*a.posonlyargs, *a.args]
        given = zip(positional[len(positional) - len(a.defaults) :], a.defaults, strict=True)
        for arg, value in [*given, *zip(a.kwonlyargs, a.kw_defaults, strict=True)]:
            if value is not None and arg.arg in TIMED_PARAMETERS:
                self._strict(value, qualname, scope)

    def _strict(self, value, qualname, scope, site=None):
        if id(value) in self.timed_nodes:
            return
        self.timed_nodes.add(id(value))
        if not self.values.allowed(value, scope):
            self.site("b", site or value, qualname)

    def _call_sites(self, call, qualname, scope):
        if id(call) in self.waits:
            return
        timeout = _keyword(call, "timeout")
        if timeout is not None:
            self._strict(timeout.value, qualname, scope, timeout)
        for spread in (k.value for k in call.keywords if k.arg is None):
            if isinstance(spread, ast.Dict):
                for key, value in zip(spread.keys, spread.values, strict=True):
                    if _string(key) == "timeout":
                        self._strict(value, qualname, scope)
        position = POSITIONAL_TIMEOUTS.get(_dotted(call.func, self.module.imports))
        if position is not None and len(call.args) > position:
            self._strict(call.args[position], qualname, scope)
        func = call.func
        if self._clock_call(call):
            given = _keyword(call, "seconds")
            position = self.work.seconds[func.attr]
            if given is not None:
                self._strict(given.value, qualname, scope, given)
            elif len(call.args) > position:
                self._strict(call.args[position], qualname, scope)
            return
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        parameters = self.corpus.timed.get(name, {})
        for parameter, position in parameters.items():
            given = _keyword(call, parameter)
            value = given.value if given is not None else None
            if value is None and 0 <= position < len(call.args):
                value = call.args[position]
                if any(isinstance(a, ast.Starred) for a in call.args[: position + 1]):
                    continue
            if value is None or id(value) in self.timed_nodes:
                continue
            if self.values.reads_as_seconds(value, scope):
                self.timed_nodes.add(id(value))
                self.site("b", given or value, qualname)

    def _argv(self, elements, qualname, scope):
        for i, element in enumerate(elements):
            program = _string(element)
            if program in PROGRAMS:
                pass
            elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Call):
                args = element.value.args
                program = _string(args[0]) if args else None
                if program not in PROGRAMS:
                    continue
            else:
                continue
            for value in self._handed(program, elements[i + 1 :]):
                if isinstance(value, ast.JoinedStr):
                    for part in value.values:
                        if isinstance(part, ast.FormattedValue):
                            self._strict(part.value, qualname, scope)
                else:
                    self._strict(value, qualname, scope)

    @staticmethod
    def _handed(program, after):
        """The seconds a program is handed in the arguments after it."""
        j = 0
        while j < len(after):
            element = after[j]
            if isinstance(element, ast.Starred):
                return
            text = _string(element)
            if program == "timeout" and text in TIMEOUT_FLAGS:
                j += 2
                continue
            if program == "timeout" and text in KILL_AFTER:
                if j + 1 < len(after):
                    yield after[j + 1]
                j += 2
                continue
            if text is not None and text.startswith("-"):
                j += 1
                continue
            yield element
            return

    # (c) No PollOwn, and (d) nothing that hides a constant.

    def _rule_c(self, node, qualname):
        names = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.alias):
            names = [node.name.split(".")[-1], node.asname]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.keyword):
            names = [node.arg]
        if POLL_OWN in names:
            self.site("c", node, qualname, POLL_OWN)

    def _rule_d(self, node, qualname):
        def namespace(value):
            return (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in NAMESPACES
            )

        if isinstance(node, ast.Subscript) and namespace(node.value):
            self.site("d", node, qualname)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and namespace(node.func.value)
        ):
            self.site("d", node, qualname)


def _parented(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node
    return tree


def read_sources(root: Path) -> dict[str, str]:
    """The corpus at ``root``: each file's source, by its path in the repository."""
    return {rel: (root / rel).read_text(encoding="utf-8") for rel in corpus(root)}


def read_corpus(sources: dict[str, str], exempt=None) -> Corpus:
    """The corpus of ``sources``, the constants exempted under (e) read from ``exempt``."""
    exempt = NOT_YET_ON_THE_CLOCK if exempt is None else exempt
    modules = {}
    for rel, source in sources.items():
        module = read_module(rel, source)
        _parented(module.tree)
        modules[rel] = module
    clock = modules.get(CLOCK)
    work = clockwork(clock.source) if clock else Clockwork(frozenset(), {}, FORBIDDEN)
    constants = frozenset((rel, text) for rel, _, rule, text in exempt if rule == "e")
    return Corpus(modules, work, constants)


def sites(sources: dict[str, str], exempt=None) -> list[Site]:
    """Every site of the corpus of ``sources``, RealClock's among them."""
    read = read_corpus(sources, exempt)
    found = []
    for module in read.by_rel.values():
        found += Scan(read, module).run()
    return found


def permanent(site: Site) -> bool:
    """A site of RealClock's own body: the standard library it hands each call to."""
    return site.rel == CLOCK and (
        site.qualname == REAL_CLOCK or site.qualname.startswith(f"{REAL_CLOCK}.")
    )


def problems_in(sources: dict[str, str], exempt=None) -> list[str]:
    """What the guard finds in ``sources``: each site no exemption covers, each exemption that
    covers fewer sites than its count, each root with nothing read, and RealClock not found."""
    exempt = NOT_YET_ON_THE_CLOCK if exempt is None else exempt
    found = sites(sources, exempt)
    out = []
    counted: dict[tuple, int] = {}
    for site in found:
        if permanent(site):
            continue
        counted[site.key] = counted.get(site.key, 0) + 1
        if counted[site.key] > (exempt[site.key].count if site.key in exempt else 0):
            out.append(str(site))
    for key, not_yet in exempt.items():
        if counted.get(key, 0) < not_yet.count:
            out.append(f"stale: {key} covers {counted.get(key, 0)} of {not_yet.count} sites")
    for prefix in ROOTS:
        if not any(rel.startswith(prefix) for rel in sources):
            out.append(f"no Python file under {prefix} was read")
    if not any(permanent(s) for s in found):
        out.append(f"{REAL_CLOCK} was not found in {CLOCK}")
    return out


def problems(root: Path, exempt=None) -> list[str]:
    return problems_in(read_sources(root), exempt)


def idle_owners(exempt) -> list[Owner]:
    """The owners with no exemption left: the change has landed, and its name goes."""
    return [owner for owner in Owner if not any(n.owner is owner for n in exempt.values())]


# --- what is not on the clock yet ---------------------------------------------------------------


class Owner(enum.Enum):
    """The change that takes each exemption away, and with it its own name here."""

    OWN_STATE_EVENTS = "events on rcman's own state"
    SERVICE_ON_THE_CLOCK = "the service's waits on the clock"
    RUNTIME_ON_THE_CLOCK = "the runtime, the helpers and the installer on the clock"
    FAKE_CLAUDE_CLOCK = "a fake claude with no clock of its own"
    LATENCY_OUT_OF_PASS_FAIL = "the latency checks out of pass/fail"


class NotYet(NamedTuple):
    count: int
    owner: Owner
    reason: str


# Why each is not on the clock yet.
OWN_STATE_POLL = "polls rcman's own state on real time: it becomes an event or a condition"
SERVICE_WAIT = "the service reads the time or waits on real time, not on its part's clock"
WALL_STAMP = "a wall-clock stamp read from datetime or time, not from its part's clock's wall()"
DEADLINE_BY_NAME = (
    "a deadline read by its name from globals(), which hides it from the guard, until the wait"
    " is on the clock"
)
RUNTIME_WAIT = (
    "the runtime reads the time or waits on real time; it and the parts it makes take no clock"
)
E_WALL = "the runtime reads the wall clock from time, not from a clock's wall()"
HELPER_WAIT = "the canary's checks read the time and wait on real time; they take no clock"
INSTALLER_WAIT = "the installer reads the time and waits on real time; it takes no clock"
UPGRADE_WALL = "the upgrade wrapper reads the wall clock from datetime; it takes no clock"
UPGRADE_ISOLATED = (
    "upgrade.py runs as root's copy under python3 -I, the standard library only, and cannot"
    " import helpers.clock: it is given a clock of its own"
)
BUILD_CANCEL_POLL = "polls the build's own cancel, rcman's own state: it becomes an event"
FAKE_REAL_TIME = "the fake claude has no clock of its own and waits on real time"
LATENCY_BOUND = "a latency bound only the tests read, as a pass or a fail"
# (file, qualname or "" at module level, rule, the site's text) -> what it covers.
NOT_YET_ON_THE_CLOCK: dict[tuple[str, str, str, str], NotYet] = {
    # Owner.OWN_STATE_EVENTS
    ("backend/rcman/monitor.py", "", "e", "RESTORED_POLL_SECONDS"): NotYet(
        1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL
    ),
    ("backend/rcman/workspaces.py", "", "e", "BUILD_QUEUE_POLL_SECONDS"): NotYet(
        1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL
    ),
    ("backend/rcman/workspaces.py", "", "e", "RESTORE_POLL_SECONDS"): NotYet(
        1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL
    ),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._restore_all",
        "a",
        "free.acquire(timeout=RESTORE_POLL_SECONDS)",
    ): NotYet(1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._restore_all",
        "a",
        "time.sleep(RESTORE_POLL_SECONDS)",
    ): NotYet(1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._wait_for_build",
        "a",
        "self._builds.acquire(timeout=BUILD_QUEUE_POLL_SECONDS)",
    ): NotYet(1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._wait_while_made",
        "a",
        "self._closing.wait(WORKING_POLL_SECONDS)",
    ): NotYet(1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL),
    ("backend/rcman/workspaces.py", "Workspaces._wait_while_made", "a", "time.monotonic()"): NotYet(
        2, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL
    ),
    (
        "backend/rcman/workspaces.py",
        "Workspaces.close",
        "a",
        "self._restoring.join(timeout=2 * RUNTIME_WAIT_SECONDS)",
    ): NotYet(1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL),
    ("backend/rcman/workspaces.py", "Workspaces.close", "a", "thread.join(timeout=30)"): NotYet(
        1, Owner.OWN_STATE_EVENTS, OWN_STATE_POLL
    ),
    # Owner.SERVICE_ON_THE_CLOCK
    ("backend/rcman/accounts.py", "Accounts._wait", "a", "time.monotonic()"): NotYet(
        4, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/accounts.py", "Accounts._wait", "a", "time.sleep(POLL_SECONDS)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/accounts.py", "Accounts.begin", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/accounts.py", "Accounts.finish", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/accounts.py", "Accounts.settle", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/audit.py", "write", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    ("backend/rcman/claude_server.py", "Server._still_runs", "a", "time.monotonic()"): NotYet(
        2, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    (
        "backend/rcman/claude_server.py",
        "Server._still_runs",
        "a",
        "time.sleep(POLL_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/claude_server.py", "Server._wait", "a", "time.monotonic()"): NotYet(
        4, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/claude_server.py", "Server._wait", "a", "time.sleep(POLL_SECONDS)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    (
        "backend/rcman/claude_server.py",
        "Server.confirm",
        "a",
        "time.sleep(ANSWER_PACE_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/claude_versions.py",
        "ClaudeVersions._approve",
        "a",
        "dt.datetime.now(dt.UTC)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP),
    ("backend/rcman/claude_versions.py", "ClaudeVersions._new", "a", "time.time()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    (
        "backend/rcman/claude_versions.py",
        "ClaudeVersions.mark_failed",
        "a",
        "dt.datetime.now(dt.UTC)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP),
    (
        "backend/rcman/forward.py",
        "Exchange.answer",
        "a",
        "asyncio.wait_for(self.body(MOST_DRAINED), DRAIN_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._connection",
        "a",
        "asyncio.get_running_loop().time()",
    ): NotYet(2, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._connection",
        "a",
        "asyncio.wait_for(reader.read(READ_CHUNK), max(within, 0))",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._end_from_thread",
        "a",
        "asyncio.run_coroutine_threadsafe(ending(), loop).result(FROM_THREAD_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._end_where",
        "a",
        "asyncio.wait_for(splice.finished.wait(), FROM_THREAD_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._enter",
        "a",
        "asyncio.wait_for(x.body(MOST_ENTER_BODY), BODY_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._raw",
        "a",
        "asyncio.wait({down, ended}, timeout=SPLICE_DRAIN_SECONDS,\n"
        + " " * 35
        + "return_when=asyncio.FIRST_COMPLETED)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._reap_loop",
        "a",
        "asyncio.sleep(REAP_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Forwarding._watch",
        "a",
        "asyncio.wait_for(relay.process.stdout.read(1), POOL_IDLE_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/forward.py",
        "Relay.close",
        "a",
        "asyncio.wait_for(self.process.wait(), RELAY_ENDS_WITHIN)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/main.py", "_retention", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    (
        "backend/rcman/main.py",
        "create_app.lifespan.daily",
        "a",
        "asyncio.sleep(RETENTION_INTERVAL_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/rcman_upgrade.py", "Upgrades._state", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/rcman_upgrade.py", "Upgrades.request", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/rcman_upgrade.py", "Upgrades.under_way", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/rcman_upgrade.py", "write_request", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    ("backend/rcman/repository.py", "Repository._seconds", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/repository.py", "_verb.wrap.verb", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/repository.py", "_verb.wrap.verb", "d", "globals()[deadline]"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, DEADLINE_BY_NAME
    ),
    ("backend/rcman/routers/claude.py", "connection", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    ("backend/rcman/routers/client_errors.py", "report", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    (
        "backend/rcman/routers/terminal.py",
        "_watch",
        "a",
        "asyncio.sleep(REVOCATION_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/routers/terminal.py",
        "terminal",
        "a",
        "asyncio.wait_for(websocket.receive(), FIRST_RESIZE_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/terminals.py",
        "Terminal._end_program",
        "a",
        "asyncio.wait_for(process.wait(), END_GRACE_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/terminals.py",
        "Terminal._exit",
        "a",
        "asyncio.wait_for(asyncio.shield(output), EXIT_DRAIN_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/terminals.py",
        "Terminal.wait_for_pidfile",
        "a",
        "asyncio.sleep(POLL_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/terminals.py", "Terminal.wait_for_pidfile", "a", "time.monotonic()"): NotYet(
        2, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    (
        "backend/rcman/terminals.py",
        "Terminals._from_thread",
        "a",
        "future.result(FROM_THREAD_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/terminals.py", "end_leader", "a", "time.monotonic()"): NotYet(
        2, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/terminals.py", "end_leader", "a", "time.sleep(POLL_SECONDS)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    ("backend/rcman/unrecognised.py", "Watch._write", "a", "dt.datetime.now(dt.UTC)"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, WALL_STAMP
    ),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._close_idle_channels",
        "a",
        "self._closing.wait(IDLE_CHECK_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/workspaces.py",
        "Workspaces._watch",
        "a",
        "done.wait(WORKING_POLL_SECONDS)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    ("backend/rcman/workspaces.py", "Workspaces.limits_report", "a", "time.monotonic()"): NotYet(
        1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT
    ),
    (
        "backend/rcman/workspaces.py",
        "Workspaces.limits_report.used",
        "a",
        "time.monotonic()",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    (
        "backend/rcman/workspaces.py",
        "Workspaces.recording_held",
        "a",
        "self._images_lock.acquire(timeout=wait)",
    ): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, SERVICE_WAIT),
    # Owner.RUNTIME_ON_THE_CLOCK
    ("backend/rcman/builder.py", "", "e", "WATCH_SECONDS"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, BUILD_CANCEL_POLL
    ),
    ("backend/rcman/builder.py", "pump", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/builder.py", "pump.watch", "a", "finished.wait(WATCH_SECONDS)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/builder.py", "pump.watch", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel.__init__", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/channel.py",
        "Channel._broken",
        "a",
        "self._error_reader.join(timeout=5)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/channel.py", "Channel._end", "a", "process.wait(timeout=grace)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel._fill", "a", "selector.select(left)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel._fill", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel._start", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel.mark_closed_if_idle", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/channel.py", "Channel.run", "a", "time.monotonic()"): NotYet(
        3, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/runtime.py", "HostRuntime.wait_ready", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "HostRuntime.wait_ready",
        "a",
        "time.sleep(READY_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime._build", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime._build",
        "a",
        "waiting.select(builder.BUILD_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime._clean_staging", "a", "time.time()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, E_WALL
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime._cp",
        "a",
        "proc.wait(timeout=max(deadline - time.monotonic(), PULL_POLL_SECONDS))",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime._cp", "a", "time.monotonic()"): NotYet(
        3, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime._cp",
        "a",
        "waiting.select(PULL_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime._load",
        "a",
        "proc.communicate(timeout=builder.BUILD_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime._load", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/runtime.py", "PodmanRuntime._pull", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime._pull",
        "a",
        "waiting.select(PULL_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime._stage", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/runtime.py", "PodmanRuntime._stage.counted", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/runtime.py", "PodmanRuntime.collect_images", "a", "time.time()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, E_WALL
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime.disk_usage",
        "a",
        "proc.communicate(timeout=DISK_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "PodmanRuntime.disk_usage", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    ("backend/rcman/runtime.py", "PodmanRuntime.wait_ready", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "PodmanRuntime.wait_ready",
        "a",
        "time.sleep(READY_POLL_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    ("backend/rcman/runtime.py", "_run_keeping_tail", "a", "proc.wait(timeout)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT
    ),
    (
        "backend/rcman/runtime.py",
        "_run_keeping_tail",
        "a",
        "thread.join(timeout=TAIL_JOIN_SECONDS)",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, RUNTIME_WAIT),
    (
        "helpers/claude_checks.py",
        "_read",
        "a",
        "selector.select(wait if left is None else min(wait, left))",
    ): NotYet(1, Owner.RUNTIME_ON_THE_CLOCK, HELPER_WAIT),
    ("helpers/claude_checks.py", "_read", "a", "time.monotonic()"): NotYet(
        3, Owner.RUNTIME_ON_THE_CLOCK, HELPER_WAIT
    ),
    ("helpers/claude_checks.py", "_read", "a", "time.sleep(POLL_SECONDS)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, HELPER_WAIT
    ),
    ("helpers/claude_checks.py", "capture", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, HELPER_WAIT
    ),
    ("helpers/claude_checks.py", "run_in_pty", "a", "time.monotonic()"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, HELPER_WAIT
    ),
    ("installer/install.py", "_status_over_tcp", "b", "timeout=5"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, INSTALLER_WAIT
    ),
    ("installer/install.py", "ensure_podman", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, INSTALLER_WAIT
    ),
    ("installer/install.py", "ensure_podman", "a", "time.sleep(0.5)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, INSTALLER_WAIT
    ),
    ("installer/install.py", "health", "a", "time.monotonic()"): NotYet(
        2, Owner.RUNTIME_ON_THE_CLOCK, INSTALLER_WAIT
    ),
    ("installer/install.py", "health", "a", "time.sleep(1)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, INSTALLER_WAIT
    ),
    ("installer/upgrade.py", "", "e", "GIT_TIMEOUT"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, UPGRADE_ISOLATED
    ),
    ("installer/upgrade.py", "now", "a", "datetime.datetime.now(datetime.UTC)"): NotYet(
        1, Owner.RUNTIME_ON_THE_CLOCK, UPGRADE_WALL
    ),
    # Owner.FAKE_CLAUDE_CLOCK
    ("devtools/fake_claude.py", "", "e", "MIN_ANSWER_SECONDS"): NotYet(
        1, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME
    ),
    ("devtools/fake_claude.py", "", "e", "READY_SECONDS"): NotYet(
        1, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME
    ),
    ("devtools/fake_claude.py", "Fake.paced_answer", "a", "time.monotonic()"): NotYet(
        2, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME
    ),
    (
        "devtools/fake_claude.py",
        "Fake.serve",
        "a",
        'time.sleep(float(self.env.get("FAKE_CLAUDE_READY_SECONDS", READY_SECONDS)))',
    ): NotYet(1, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME),
    ("devtools/fake_claude.py", "Fake.show_lines", "a", "datetime.datetime.now()"): NotYet(
        1, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME
    ),
    ("devtools/fake_claude.py", "credentials", "a", "time.time()"): NotYet(
        1, Owner.FAKE_CLAUDE_CLOCK, FAKE_REAL_TIME
    ),
    # Owner.LATENCY_OUT_OF_PASS_FAIL
    ("backend/rcman/channel.py", "", "e", "LATENCY_P95_SECONDS"): NotYet(
        1, Owner.LATENCY_OUT_OF_PASS_FAIL, LATENCY_BOUND
    ),
}


def test_nothing_outside_the_clock_reads_or_waits_on_real_time():
    assert problems(ROOT) == []


def test_each_exemption_covers_exactly_its_count():
    counted: dict[tuple, int] = {}
    for site in sites(read_sources(ROOT)):
        if not permanent(site):
            counted[site.key] = counted.get(site.key, 0) + 1
    assert NOT_YET_ON_THE_CLOCK  # non-vacuity: there are exemptions to count
    assert {key: n.count for key, n in NOT_YET_ON_THE_CLOCK.items()} == {
        key: counted.get(key, 0) for key in NOT_YET_ON_THE_CLOCK
    }


def test_every_owner_has_an_exemption_and_every_exemption_a_reason():
    assert idle_owners(NOT_YET_ON_THE_CLOCK) == []
    for key, not_yet in NOT_YET_ON_THE_CLOCK.items():
        assert not_yet.count >= 1 and isinstance(not_yet.owner, Owner), key
        assert not_yet.reason.strip(), key


def test_an_owner_with_no_exemption_left_is_found():
    """Planted twin: an owner's exemptions all taken away, as its change does."""
    owner = Owner.LATENCY_OUT_OF_PASS_FAIL
    left = {k: n for k, n in NOT_YET_ON_THE_CLOCK.items() if n.owner is not owner}
    assert len(left) < len(NOT_YET_ON_THE_CLOCK)  # the plant took effect
    assert idle_owners(left) == [owner]


# --- the corpus ---------------------------------------------------------------------------------


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A scratch repository whose files are all untracked."""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


CLOCK_SOURCE = (ROOT / CLOCK).read_text(encoding="utf-8")
# A corpus the guard passes: a Python file under each root, the clock among them.
CLEAN_REPO = {CLOCK: CLOCK_SOURCE, "backend/rcman/ok.py": "x = 1\n", "installer/__init__.py": ""}
PLANTED = "backend/rcman/planted.py"
A_READ = "import time\n\n\ndef f():\n    return time.monotonic()\n"


def test_the_corpus_is_every_python_file_under_its_roots_and_the_fake():
    rels = set(corpus(ROOT))
    on_disk = sorted(
        p.relative_to(ROOT).as_posix() for prefix in ROOTS for p in (ROOT / prefix).rglob("*.py")
    )
    ignored = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--stdin"],
        input="\n".join(on_disk),
        capture_output=True,
        text=True,
    ).stdout.split()
    assert rels == {*on_disk, FAKE} - set(ignored)
    assert {
        CLOCK,
        "backend/rcman/main.py",
        "backend/rcman/routers/terminal.py",
        "helpers/claude_checks.py",
        "installer/__init__.py",
        "installer/install.py",
        "installer/upgrade.py",
        FAKE,
    } <= rels
    assert not any(rel.startswith("tests/") for rel in rels)
    assert not any(rel.startswith("devtools/") and rel != FAKE for rel in rels)


def test_the_clean_repository_passes(tmp_path):
    assert problems(make_repo(tmp_path, CLEAN_REPO), {}) == []  # the control for the plants below


def test_a_new_untracked_module_is_read_and_an_ignored_one_is_not(tmp_path):
    files = {**CLEAN_REPO, PLANTED: A_READ, "backend/rcman/ignored.py": A_READ}
    root = make_repo(tmp_path, {**files, ".gitignore": "ignored.py\n"})
    assert PLANTED in corpus(root) and "backend/rcman/ignored.py" not in corpus(root)
    found = problems(root, {})
    assert len(found) == 1 and found[0].startswith(f"{PLANTED}:5 in f: (a)"), found


def test_what_is_outside_the_corpus_is_not_read(tmp_path):
    outside = {"tests/test_x.py": A_READ, "devtools/other.py": A_READ, "frontend/x.py": A_READ}
    root = make_repo(tmp_path, {**CLEAN_REPO, **outside})
    assert set(outside) <= set(docs_guard.corpus_files(root))  # the plant took effect
    assert problems(root, {}) == []
    assert len(problems(make_repo(tmp_path / "fake", {**CLEAN_REPO, FAKE: A_READ}), {})) == 1


@pytest.mark.parametrize("prefix", ROOTS)
def test_a_root_with_nothing_read_fails_the_guard(tmp_path, prefix):
    files = {rel: text for rel, text in CLEAN_REPO.items() if not rel.startswith(prefix)}
    found = problems(make_repo(tmp_path, files), {})
    assert f"no Python file under {prefix} was read" in found


def test_a_corpus_without_the_real_clock_fails_the_guard(tmp_path):
    files = {**CLEAN_REPO, CLOCK: CLOCK_SOURCE.replace(f"class {REAL_CLOCK}:", "class Other:")}
    assert f"{REAL_CLOCK} was not found in {CLOCK}" in problems(make_repo(tmp_path, files), {})


# --- the clock: its markers, its calls and RealClock ---------------------------------------------


def test_the_markers_are_the_clocks_newtypes_and_none_is_for_polling_own_state():
    from helpers import clock as clock_module

    newtypes = {n for n, v in vars(clock_module).items() if isinstance(v, typing.NewType)}
    assert newtypes and markers(CLOCK_SOURCE) == newtypes
    assert POLL_OWN not in newtypes


def test_a_newtype_planted_in_the_clock_is_a_marker_and_poll_own_is_flagged():
    planted = CLOCK_SOURCE + '\nPlanted = NewType("Planted", float)\n'
    assert markers(planted) == markers(CLOCK_SOURCE) | {"Planted"}
    own = CLOCK_SOURCE + f'\n{POLL_OWN} = NewType("{POLL_OWN}", float)\n'
    found = problems_in({**CLEAN_REPO, CLOCK: own}, {})
    assert len(found) == 1 and f"(c) {POLL_OWN}" in found[0], found


def test_the_clock_calls_seconds_are_where_the_protocol_has_them():
    from helpers.clock import Clock

    expected = {}
    for name, member in vars(Clock).items():
        if callable(member) and not name.startswith("_"):
            parameters = list(inspect.signature(member).parameters)[1:]
            if "seconds" in parameters:
                expected[name] = parameters.index("seconds")
    assert expected and clock_seconds(CLOCK_SOURCE) == expected


def test_real_clock_is_the_one_permanent_exemption_and_each_of_its_calls_is_found():
    found = [s for s in sites({**CLEAN_REPO}, {}) if permanent(s)]
    methods = {
        n.name
        for n in _class(ast.parse(CLOCK_SOURCE), REAL_CLOCK).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {s.qualname for s in found} == {f"{REAL_CLOCK}.{m}" for m in methods}
    assert {"time.monotonic", "time.time", "time.sleep", "asyncio.wait_for"} <= delegated(
        CLOCK_SOURCE
    )
    assert problems_in(CLEAN_REPO, {}) == []  # positive control: its sites are no problem


def test_a_forbidden_form_elsewhere_in_the_clock_is_flagged():
    planted = CLOCK_SOURCE + "\n\ndef planted():\n    return time.monotonic()\n"
    found = problems_in({**CLEAN_REPO, CLOCK: planted}, {})
    assert len(found) == 1 and "in planted: (a) time.monotonic()" in found[0], found


def test_a_constant_is_followed_to_its_marker_across_the_corpus():
    read = read_corpus(read_sources(ROOT))
    values = Values(read)
    grace = "UNRECOGNISED_GRACE_SECONDS"
    accounts, runtime = (
        read.by_rel["backend/rcman/accounts.py"],
        read.by_rel["backend/rcman/runtime.py"],
    )
    module, name = read.lookup(accounts.imports[grace])
    assert (module.rel, name) == ("backend/rcman/claude_server.py", grace)
    (value,) = module.constants[name]
    assert value.func.id == "Grace" and read.marker_call(value, module)
    uses = [n for n in ast.walk(accounts.tree) if isinstance(n, ast.Name) and n.id == grace]
    assert uses and all(values.allowed(n, Scope(accounts, ())) for n in uses)
    build = [
        n
        for n in ast.walk(runtime.tree)
        if isinstance(n, ast.Attribute) and _segment(runtime, n) == "builder.BUILD_SECONDS"
    ]
    assert build and all(values.allowed(n, Scope(runtime, ())) for n in build)
    (value,) = read.modules["rcman.builder"].constants["BUILD_SECONDS"]
    assert value.func.id == "Deadline"


def test_a_constant_whose_marker_is_dropped_is_flagged_where_it_is_used():
    """Planted twin: a pair of modules, the source of the constant with its marker and without."""
    using = (
        "from rcman import source\nfrom rcman.source import STOP_GRACE\n\n\n"
        "def f(clock):\n    clock.sleep(STOP_GRACE)\n    clock.sleep(source.STOP_GRACE)\n"
    )
    labelled = "from helpers.clock import Grace\n\nSTOP_GRACE = Grace(3.0)\n"
    pair = {**CLEAN_REPO, "backend/rcman/using.py": using}
    assert problems_in({**pair, "backend/rcman/source.py": labelled}, {}) == []
    dropped = labelled.replace("Grace(3.0)", "3.0")
    assert dropped != labelled  # the plant took effect
    found = problems_in({**pair, "backend/rcman/source.py": dropped}, {})
    assert sorted(f.split(": ", 1)[1] for f in found) == [
        "(b) STOP_GRACE",
        "(b) source.STOP_GRACE",
        "(e) STOP_GRACE",
    ], found


# --- exemptions ----------------------------------------------------------------------------------

EXEMPT_READ = {(PLANTED, "f", "a", "time.monotonic()"): NotYet(1, Owner.SERVICE_ON_THE_CLOCK, "x")}


def test_an_exemption_covers_its_count_and_no_more():
    assert problems_in({**CLEAN_REPO, PLANTED: A_READ}, EXEMPT_READ) == []  # positive control
    twice = A_READ.replace(
        "return time.monotonic()", "time.monotonic()\n    return time.monotonic()"
    )
    assert twice != A_READ  # the plant took effect
    found = problems_in({**CLEAN_REPO, PLANTED: twice}, EXEMPT_READ)
    assert len(found) == 1 and f"{PLANTED}:6 in f: (a) time.monotonic()" == found[0], found


def test_an_exemption_with_fewer_sites_than_its_count_is_stale():
    found = problems_in({**CLEAN_REPO, PLANTED: "x = 1\n"}, EXEMPT_READ)
    assert len(found) == 1 and found[0].startswith("stale:"), found


def test_a_new_site_in_an_exempt_function_is_flagged():
    beside = A_READ.replace("    return", "    time.sleep(1)\n    return")
    assert beside != A_READ  # the plant took effect
    found = problems_in({**CLEAN_REPO, PLANTED: beside}, EXEMPT_READ)
    assert found == [f"{PLANTED}:5 in f: (a) time.sleep(1)"], found


# --- the forms: each planted, at module level and in a function ----------------------------------


class Form(NamedTuple):
    rule: str
    spellings: tuple[str, ...]
    # Whether the form is one in a function too: a module constant is not.
    in_function: bool = True


FORMS = {
    "a time read RealClock hands on": Form(
        "a", ("import time\nnow = time.monotonic()", "import time\nnow = time.time()")
    ),
    "a wait RealClock hands on": Form(
        "a",
        (
            "import time\ntime.sleep(x)",
            "import asyncio\nasyncio.sleep(x)",
            "import asyncio\nasyncio.wait_for(x, 5)",
            "import asyncio\nasyncio.wait(x, timeout=5)",
        ),
    ),
    "perf_counter and the _ns forms": Form(
        "a",
        (
            "import time\nnow = time.perf_counter()",
            "import time\nnow = time.perf_counter_ns()",
            "import time\nnow = time.monotonic_ns()",
            "import time\nnow = time.time_ns()",
        ),
    ),
    "a wall-clock read": Form(
        "a",
        (
            "import datetime\nnow = datetime.datetime.now()",
            "import datetime\nnow = datetime.datetime.today()",
            "import datetime\nnow = datetime.datetime.utcnow()",
            "import datetime\nnow = datetime.date.today()",
            "import time\nnow = time.localtime()",
            "import time\nnow = time.gmtime()",
            "import time\nnow = time.ctime()",
            'import time\nnow = time.strftime("%H")',
        ),
    ),
    "the event loop's time and its timers": Form(
        "a",
        (
            "now = loop.time()",
            "import asyncio\nnow = asyncio.get_running_loop().time()",
            "loop.call_later(1, f)",
            "loop.call_at(t, f)",
        ),
    ),
    "the standard library's other timed waits": Form(
        "a",
        (
            "import asyncio\nasyncio.timeout(5)",
            "import asyncio\nasyncio.timeout_at(t)",
            "import threading\nthreading.Timer(1, f)",
            "import signal\nsignal.alarm(1)",
            "import signal\nsignal.setitimer(signal.ITIMER_REAL, 1)",
            "import select\nselect.select(r, w, x, 1)",
            "import socket\nsocket.setdefaulttimeout(5)",
            "import time\nnow = time.clock_gettime(time.CLOCK_MONOTONIC)",
            "import time\nnow = time.process_time()",
        ),
    ),
    "a method given seconds": Form(
        "a",
        (
            "event.wait(1)",
            "proc.wait(timeout=1)",
            "condition.wait_for(p, 1)",
            "lock.acquire(timeout=1)",
            "lock.acquire(True, 1)",
            "thread.join(timeout=1)",
            "thread.join(1)",
            "future.result(1)",
            "selector.select(1)",
            "proc.communicate(timeout=1)",
            "queue.get(timeout=1)",
            "queue.get(True, 1)",
            "thread.join(limit)",
            "sock.settimeout(5)",
            "import select\nselect.epoll().poll(5)",
            "self.clock_event.wait(5)",
        ),
    ),
    "an alias, wherever it is imported": Form(
        "a",
        (
            "import time as t\nnow = t.monotonic()",
            "from time import monotonic as m\nnow = m()",
            "from time import sleep\nsleep(1)",
            "import asyncio as aio\naio.sleep(1)",
            "import datetime as dt\nnow = dt.datetime.now()",
            "from datetime import datetime\nnow = datetime.now()",
            "import datetime\nDT = datetime.datetime\nnow = DT.now()",
        ),
    ),
    "a reference without a call": Form(
        "a",
        (
            "import time\nnow = time.monotonic",
            "import time\nf(default=time.time)",
            "from datetime import datetime\nf(datetime.now)",
            'import time\nnow = getattr(time, "monotonic")()',
            'import time\nnow = getattr(time, "anything")',
        ),
    ),
    "a clock call's seconds given a number": Form(
        "b",
        (
            "clock.sleep(5)",
            "self.clock.wait(event, 5)",
            "self._clock.within(task, 2.5)",
            "clock.poll(seconds=1)",
        ),
    ),
    "a timeout= given a number": Form(
        "b",
        (
            "subprocess.run(argv, timeout=30)",
            "def g(timeout):\n    pass\ng(timeout=30)",
        ),
    ),
    "a number added": Form(
        "b",
        (
            "from helpers.clock import Grace\nGIVEN = Grace(1.0)\nclock.sleep(GIVEN + 5)",
            "def g(wait):\n    pass\ng(x + 5)",
        ),
    ),
    "a name given no marker": Form(
        "b",
        (
            "LIMIT = 5\nclock.sleep(LIMIT)",
            "LIMIT = 5\nsubprocess.run(argv, timeout=LIMIT)",
            "LIMIT = 5\nclock.sleep(round(LIMIT))",
            "LIMIT = 5\nclock.sleep(abs(LIMIT))",
        ),
    ),
    "a timed parameter of the corpus's own given a number": Form(
        "b",
        (
            "def g(x, grace):\n    pass\ng(1, 5)",
            "def g(x, interval=None):\n    pass\ng(1, interval=5)",
            'def g(within):\n    pass\ng("10")',
        ),
    ),
    "a timed parameter of the corpus's own given a constant with no marker": Form(
        "b", ("LIMIT = 5\n\n\ndef g(seconds):\n    pass\n\n\ng(LIMIT)",), in_function=False
    ),
    "seconds handed to a program in its arguments": Form(
        "b",
        (
            'argv = ["timeout", "-k", "5", "git"]',
            'argv = ["timeout", "30", "git"]',
            'argv = [busybox, "sleep", "5"]',
            'argv = [*layout.tool("timeout"), "-s", "TERM", "30", "git"]',
            'run(*_busybox("sleep"), "5")',
            'argv = ("sleep", "5")',
            'row = (ident, "host", "sleep", "2")',
            'argv = ["sleep", "0.5"]',
            'argv = ["timeout", "5s", "git"]',
            'LIMIT = 5\nargv = ["sleep", str(LIMIT)]',
            'LIMIT = 5\nargv = ["sleep", f"{LIMIT}"]',
        ),
    ),
    "a number factored by a number": Form(
        "b",
        (
            "clock.sleep(2 * 3)",
            "subprocess.run(argv, timeout=60 * 60)",
            "def g(x, wait):\n    pass\ng(p, 5 * 60)",
        ),
    ),
    "a timed parameter's default given a number": Form(
        "b", ("def g(grace=2):\n    pass", "def g(x, timeout=30.0):\n    pass")
    ),
    "a call of a method only the clock has, whatever its receiver": Form(
        "b",
        ("REAL.sleep(5)", "clocks.forward.sleep(5)", "c.sleep(5)", "c.within(task, 5)"),
    ),
    "a timeout handed to the standard library otherwise": Form(
        "b",
        (
            "from urllib.request import urlopen\nurlopen(u, None, 5)",
            'subprocess.run(argv, **{"timeout": 30})',
        ),
    ),
    "seconds handed to a program in a flag": Form(
        "b",
        (
            'argv = ["--time=10"]',
            'argv = ["--time=0.5"]',
            'argv = [f"--time={10}"]',
            'LIMIT = 5\nargv = [f"--timeout={LIMIT}"]',
        ),
    ),
    "a name for polling rcman's own state": Form(
        "c",
        (
            f"from helpers.clock import {POLL_OWN}",
            f"{POLL_OWN} = float",
            f"x = clock_module.{POLL_OWN}(1.0)",
        ),
    ),
    "a constant hidden in a namespace": Form(
        "d",
        (
            'x = globals()["LIMIT"]',
            'x = vars()["LIMIT"]',
            'x = locals().get("LIMIT")',
            'x = globals().get("LIMIT")',
        ),
    ),
    "a module constant named as seconds with no marker": Form(
        "e",
        (
            "A_SECONDS = 1",
            "TIMEOUT = 5.0",
            "A_GRACE = 2",
            "A_WITHIN = 3",
            "A_WAIT = 4",
            "A_TIMEOUT: float = 5.0",
            'A_SECONDS, NAME = 1, "x"',
            "A_SECONDS = 2 * 60",
            "from helpers.clock import Grace\nB_GRACE = Grace(1.0)\nA_GRACE = B_GRACE",
        ),
        in_function=False,
    ),
}

CLEAN = {
    "a wait with no seconds": (
        "import asyncio\nasyncio.sleep(0)",
        "import concurrent.futures\nconcurrent.futures.wait(fs)",
        "from concurrent.futures import wait\nwait(fs)",
        "sock.settimeout(None)",
        "proc.poll()",
        "self.clock.wait(event, None)",
        "import shlex\ntext = shlex.join(argv)",
        "event.wait()",
        "event.wait(None)",
        "lock.acquire()",
        "lock.acquire(blocking=False)",
        "lock.acquire(False)",
        "thread.join()",
        "proc.wait()",
        "future.result()",
        "proc.communicate()",
        "queue.get()",
        "import asyncio\nasyncio.wait(tasks)",
        "import asyncio\nasyncio.wait_for(x, None)",
        "import asyncio\nasyncio.timeout(None)",
    ),
    "a method that shares a timed one's name": (
        'text = " ".join(parts)',
        'import os\npath = os.path.join("a", "b")',
        'x = d.get("k", 5)',
        "x = task.result()",
    ),
    "another parameter where a timed one's name is shared": (
        'def run(argv, timeout=None):\n    pass\nrun("-t", "name")',
        "def _wait(until, timeout):\n    pass\nself._wait(sign_in, lambda s: True)",
        "def _end(grace=None):\n    pass\n_end(grace=0)",
    ),
    "seconds that say what kind they are": (
        "from helpers.clock import Grace\nSTOP_GRACE = Grace(1.0)\nclock.sleep(STOP_GRACE)",
        "from helpers.clock import Grace\nSTOP_GRACE = Grace(1.0)\nclock.sleep(2 * STOP_GRACE)",
        "def g(d, clock):\n    clock.sleep(max(d - clock.now(), 0))",
        "def g(seconds, clock):\n    clock.sleep(seconds)",
        "def g(clock):\n    left = None\n    clock.wait(event, left)",
    ),
    "what a program is handed, with its marker": (
        "from helpers.clock import Handed\nKILL_SECONDS = Handed(5)\n"
        'argv = [*tool("timeout"), "-k", KILL_SECONDS, KILL_SECONDS, "git"]',
        "from helpers.clock import RealBound\nRUN_TIMEOUT = RealBound(30.0)\n"
        "subprocess.run(argv, timeout=RUN_TIMEOUT)",
        "from helpers.clock import Handed\nSTOP_SECONDS = Handed(10)\n"
        'argv = [f"--time={STOP_SECONDS}"]',
    ),
    "what is not seconds": (
        "NANOSECONDS = 1_000_000_000",
        'argv = ["sleep", "infinity"]',
        'argv = ["--time=0"]',
    ),
}


def _in_function(snippet: str) -> str:
    return "def f():\n" + textwrap.indent(snippet, "    ") + "\n"


def _planted(source: str) -> list[Site]:
    return [s for s in sites({**CLEAN_REPO, PLANTED: source}, {}) if s.rel == PLANTED]


def _forms(table, in_function: bool) -> list:
    return [
        pytest.param(form, spelling, id=f"{name}: {spelling}")
        for name, form in table.items()
        for spelling in form.spellings
        if form.in_function or not in_function
    ]


@pytest.mark.parametrize(("form", "spelling"), _forms(FORMS, in_function=False))
def test_the_guard_finds_each_form_at_module_level(form, spelling):
    assert [(s.rule, s.qualname) for s in _planted(spelling + "\n")] == [(form.rule, "")]


@pytest.mark.parametrize(("form", "spelling"), _forms(FORMS, in_function=True))
def test_the_guard_finds_each_form_in_a_function(form, spelling):
    source = _in_function(spelling)
    assert textwrap.indent(spelling, "    ") in source  # the plant took effect
    assert [(s.rule, s.qualname) for s in _planted(source)] == [(form.rule, "f")]


def test_a_module_constant_named_as_seconds_is_no_site_in_a_function():
    """Rule (e) is of module constants: a function's own name is followed where it is used."""
    (spelling, *_) = FORMS["a module constant named as seconds with no marker"].spellings
    assert _planted(spelling + "\n") and _planted(_in_function(spelling)) == []


@pytest.mark.parametrize(
    "source",
    [pytest.param(s, id=f"{name}: {s}") for name, spellings in CLEAN.items() for s in spellings],
)
def test_the_guard_leaves_alone_what_is_on_the_clock_or_not_seconds(source):
    assert _planted(source + "\n") == []
    assert _planted(_in_function(source)) == []


def test_a_class_constant_named_as_seconds_with_no_marker_is_flagged():
    """Rule (e) reads a class's body as it reads a module's."""
    source = "class K:\n    STOP_SECONDS = 5\n    NAME = 'x'\n"
    assert [(s.rule, s.qualname, s.text) for s in _planted(source)] == [("e", "K", "STOP_SECONDS")]
    labelled = "from helpers.clock import Grace\n\n\n" + source.replace("5", "Grace(5)")
    assert _planted(labelled) == []  # positive control


def test_a_class_field_named_for_a_timed_parameter_is_classified():
    """A dataclass's field, as ``timeout: float = 30.0``, is given seconds as a default is."""
    source = "class K:\n    timeout: float = 30.0\n    name: str = 'x'\n"
    assert [(s.rule, s.qualname, s.text) for s in _planted(source)] == [("b", "K", "30.0")]
    labelled = "from helpers.clock import Deadline\n\n\n" + source.replace("30.0", "Deadline(30.0)")
    assert _planted(labelled) == []  # positive control
