"""The Makefile's test targets: make test runs every test that runs on the host, make
test-container the tests that need a container, make benchmark the tests that measure and
report, and make check, the full check, the host tests with the lint and the frontend's tests,
leaving out make test-container, the on-demand run. No target but make benchmark runs a
benchmark test. A container test that cannot run, or a test of the real claude, fails rather
than skips where make requires it, none of the runs runs a test that needs a real Claude
account, and every test of every run is under the hang guard (tests/hang_guard.py). make lint
and make format cover both the Python code and the frontend.

Each target is run by the real make with uv, npm and npx replaced by recorders, so
what a target would run is read from what it ran; which tests a pytest command line
selects is read from pytest's own collection (tests/collected.py). The benchmark's place is
read from the Makefile and pyproject.toml themselves, without make: each pytest call's marker
expression, worked out for every set of the markers, and the targets each target makes.
"""

import ast
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.collected import collect
from tests.container_marker import MARKER
from tests.hang_guard import CONTAINER_GUARD, HANG_GUARD, HOST_GUARD

REPO = Path(__file__).resolve().parent.parent
MAKEFILE = REPO / "Makefile"
RECORDER = """#!{python}
import json, os, sys
with open(os.environ["MAKE_RECORD"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{
        "argv": [os.path.basename(sys.argv[0]), *sys.argv[1:]],
        "cwd": os.getcwd(),
        "env": {{k: v for k, v in os.environ.items() if k.startswith("RCMAN_")}},
    }}) + "\\n")
"""


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: str
    env: tuple[tuple[str, str], ...]

    def pytest_args(self) -> tuple[str, ...] | None:
        """The arguments given to pytest, when this call is `uv run pytest`."""
        if self.argv[:3] == ("uv", "run", "pytest"):
            return self.argv[3:]
        return None


def recorded(tmp_path: Path, target: str, makefile: Path = MAKEFILE) -> list[Call]:
    """What `make <target>` runs, with nothing on PATH but recorders for uv, npm and
    npx: any other program it ran would not be found, and make would fail."""
    fakebin = tmp_path / f"bin-{target}"
    fakebin.mkdir()
    for name in ("uv", "npm", "npx"):
        (fakebin / name).write_text(RECORDER.format(python=sys.executable), encoding="utf-8")
        (fakebin / name).chmod(0o755)
    log = tmp_path / f"{target}.jsonl"
    log.touch()
    proc = subprocess.run(
        [shutil.which("make"), "-f", str(makefile), "-C", str(REPO), target],
        env={"PATH": str(fakebin), "MAKE_RECORD": str(log)},
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = []
    for line in log.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        calls.append(Call(tuple(raw["argv"]), raw["cwd"], tuple(sorted(raw["env"].items()))))
    return calls


def selected(calls: list[Call]) -> dict[Call, set[str]]:
    """The tests each pytest call selects, by call."""
    return {
        call: set(collect(call.pytest_args(), dict(call.env)))
        for call in calls
        if call.pytest_args() is not None
    }


RUNS = ("test", "test-container")


@dataclass(frozen=True)
class Collected:
    """What a bare pytest collects: every test, and those carrying the container marker."""

    full: set[str]
    container: set[str]

    @property
    def host(self) -> set[str]:
        return self.full - self.container


def everything() -> Collected:
    found = collect()
    return Collected(
        set(found),
        {n for n, t in found.items() if MARKER in t["markers"]},
    )


def ran_by(calls: list[Call]) -> set[str]:
    return set().union(*selected(calls).values()) if calls else set()


def split_problems(test: list[Call], container: list[Call]) -> list[str]:
    """What is wrong with the split between the two targets' pytest calls: every test
    belongs to exactly one run, which is make test for a test that runs on the host and make
    test-container for a container test; and none of the runs runs a test a bare pytest
    leaves out."""
    tests = everything()
    host, ran = ran_by(test), ran_by(container)
    problems = [f"in none of the runs: {n}" for n in sorted(tests.full - host - ran)]
    problems += [f"a container test in make test: {n}" for n in sorted(tests.container & host)]
    problems += [f"not in make test-container: {n}" for n in sorted(tests.container - ran)]
    problems += [
        f"not a container test, in make test-container: {n}"
        for n in sorted((ran & tests.full) - tests.container)
    ]
    for tests_run, target in (
        (host, "make test"),
        (ran, "make test-container"),
    ):
        problems += left_out(tests_run, tests.full, target)
    return problems


def left_out(ran: set[str], full: set[str], target: str) -> list[str]:
    """Collected a target runs that a bare pytest leaves out. A target's own -m replaces the
    -m of pyproject's addopts, so without its own 'not real_claude' a target runs the tests
    that need a real Claude account, and without its own 'not benchmark' the benchmark's."""
    return [f"left out of a bare run, in {target}: {n}" for n in sorted(ran - full)]


def without_real_claude(runs: dict[Call, set[str]], only: set[str] | None = None) -> list[str]:
    """The calls (those selecting a test of ``only``, if given) without
    RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests of the real claude would skip."""
    return [
        f"without RCMAN_REQUIRE_REAL_CLAUDE=1: {' '.join(call.argv)}"
        for call, found in runs.items()
        if (only is None or found & only) and ("RCMAN_REQUIRE_REAL_CLAUDE", "1") not in call.env
    ]


def check_problems(check: list[Call]) -> list[str]:
    """What make check would get wrong: a host test none of its pytest calls selects, an
    on-demand test (a container test) it runs, a test a bare pytest leaves out, or a call
    without RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests of the real claude would skip."""
    tests = everything()
    runs = selected(check)
    ran = set().union(*runs.values()) if runs else set()
    problems = [f"not run by make check: {n}" for n in sorted(tests.host - ran)]
    problems += [f"an on-demand test in make check: {n}" for n in sorted(tests.container & ran)]
    return problems + left_out(ran, tests.full, "make check") + without_real_claude(runs)


def could_skip(calls: list[Call]) -> list[str]:
    """The calls selecting container tests without RCMAN_REQUIRE_CONTAINER=1, where those
    tests would skip rather than fail."""
    tests = everything()
    return [
        f"container tests without RCMAN_REQUIRE_CONTAINER=1: {' '.join(call.argv)}"
        for call, found in selected(calls).items()
        if found & tests.container and ("RCMAN_REQUIRE_CONTAINER", "1") not in call.env
    ]


def split_of(tmp_path: Path, **makefiles: Path) -> list[str]:
    """split_problems for the two targets, each from its entry of ``makefiles`` (by
    the target's name with _ for -) or else the real Makefile."""
    calls = {
        target: recorded(tmp_path, target, makefiles.get(target.replace("-", "_"), MAKEFILE))
        for target in RUNS
    }
    return split_problems(*calls.values())


def known_positives(tests: Collected) -> None:
    """Non-vacuity: every set holds tests known to belong there."""
    assert "tests/test_container_suite.py::test_podman_is_usable" in tests.container
    assert "tests/test_make.py::test_every_test_is_in_exactly_one_of_the_two_runs" in tests.host
    assert any(n.startswith("tests/test_rules.py::test_the_backend_and_") for n in tests.host)


def test_every_test_is_in_exactly_one_of_the_two_runs(tmp_path):
    runs = {target: recorded(tmp_path, target) for target in RUNS}
    for calls in runs.values():
        assert [c.pytest_args() is not None for c in calls] == [True]
        assert all(c.cwd == str(REPO) for c in calls)
    known_positives(everything())
    assert split_problems(*runs.values()) == []


def test_make_check_runs_the_host_tests_and_no_on_demand_run(tmp_path):
    check = recorded(tmp_path, "check")
    assert len([c for c in check if c.pytest_args() is not None]) >= 1  # non-vacuity
    assert check_problems(check) == []


def test_no_container_test_of_make_test_container_can_skip(tmp_path):
    calls = recorded(tmp_path, "test-container")
    assert len([c for c in calls if c.pytest_args() is not None]) == 1  # non-vacuity
    assert could_skip(calls) == []


def planted(tmp_path: Path, old: str, new: str, count: int = 1) -> Path:
    """A copy of the Makefile with ``old`` replaced ``count`` times (-1: everywhere); the
    change must have taken effect."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert old in text, old
    path = tmp_path / "Makefile.planted"
    path.write_text(text.replace(old, new, count), encoding="utf-8")
    assert path.read_text(encoding="utf-8") != text
    return path


TEST_M = '-m "not container and not real_claude and not benchmark"'
CONTAINER_M = '-m "container and not real_claude and not benchmark"'
BENCH_M = '-m "benchmark and not real_claude"'


def runs_in_parallel(makefile: Path) -> bool:
    """Whether make -j may run targets beside each other: the Makefile has no .NOTPARALLEL."""
    return re.search(r"^\.NOTPARALLEL:", makefile.read_text(encoding="utf-8"), re.M) is None


def test_no_target_runs_beside_another_under_make_j(tmp_path):
    """make -j must run the targets it is given one after the other, as it does without -j.
    Planted twin: without the line, make -j may run targets together."""
    assert not runs_in_parallel(MAKEFILE)
    assert runs_in_parallel(planted(tmp_path, ".NOTPARALLEL:\n", ""))


def test_a_test_dropped_from_make_test_is_reported(tmp_path):
    """Planted twin: make test deselecting one test leaves it in none of the runs."""
    dropped = "tests/test_make.py::test_a_test_dropped_from_make_test_is_reported"
    makefile = planted(
        tmp_path, f"--dist worksteal {TEST_M}", f"--dist worksteal {TEST_M} --deselect {dropped}"
    )
    (tmp_path / "probe").mkdir()
    assert any(dropped in c.argv for c in recorded(tmp_path / "probe", "test", makefile))  # it took
    assert split_of(tmp_path, test=makefile) == [f"in none of the runs: {dropped}"]


def test_make_check_with_the_container_run_is_reported(tmp_path):
    """Planted twin: make check gaining test-container holds an on-demand run."""
    makefile = planted(tmp_path, "check: lint test ", "check: lint test test-container ")
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [
        f"an on-demand test in make check: {n}" for n in sorted(everything().container)
    ]


def test_container_tests_that_could_skip_are_reported(tmp_path):
    """Planted twin: make test-container without RCMAN_REQUIRE_CONTAINER=1, where the tests
    would skip rather than fail."""
    makefile = planted(tmp_path, "test-container: export RCMAN_REQUIRE_CONTAINER = 1\n", "")
    problems = could_skip(recorded(tmp_path, "test-container", makefile))
    assert len(problems) == 1, problems
    assert problems[0].startswith("container tests without RCMAN_REQUIRE_CONTAINER=1")
    assert "pytest" in problems[0]


def real_claude_tests() -> list[str]:
    """The tests a bare pytest leaves out, by pyproject's addopts; non-vacuity: some."""
    found = sorted(collect(["-m", "real_claude"]))
    assert "tests/test_claude_real_account.py" in {n.split("::")[0] for n in found}
    return found


def test_real_claude_tests_run_by_make_check_are_reported(tmp_path):
    """Planted twin: make check running make test without 'and not real_claude'."""
    makefile = planted(tmp_path, TEST_M, '-m "not container and not benchmark"')
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [f"left out of a bare run, in make check: {n}" for n in real_claude_tests()]


# --- make benchmark: on demand, and in no other target, read from the Makefile itself ---

PYPROJECT = REPO / "pyproject.toml"
BENCHMARK = "benchmark"
MARKERS = ("container", "real_claude", BENCHMARK)
# Every set of the markers a test may carry, for which a marker expression is worked out.
MARKER_SETS = [
    frozenset(c) for n in range(len(MARKERS) + 1) for c in itertools.combinations(MARKERS, n)
]
BENCHMARK_TESTS = {
    "tests/test_channel.py::test_the_channels_latency_on_the_host",
    "tests/test_claude_checks.py::test_how_long_redacting_a_screen_takes_by_its_size",
}


@dataclass
class Rule:
    """A target of the Makefile: the targets it makes first and its recipe."""

    prerequisites: list[str] = field(default_factory=list)
    recipe: list[str] = field(default_factory=list)


def makefile_rules(text: str) -> dict[str, Rule]:
    """The Makefile's rules, as make reads this one: ``target: prerequisites  ## help``,
    ``target: export NAME = value``, and the recipe's tab-led lines under a rule."""
    rules: dict[str, Rule] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("\t"):
            if current is not None:
                current.recipe.append(line.strip())
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = re.match(r"^([A-Za-z][\w.-]*):(?![=:])(.*)$", line)
        if not found or found.group(1).startswith("."):
            current = None
            continue
        rule = rules.setdefault(found.group(1), Rule())
        rest = found.group(2).split("#", 1)[0].strip()
        if re.match(r"^export\s+\w+\s*=", rest):
            current = None
        else:
            rule.prerequisites += rest.split()
            current = rule
    return rules


def submade(rule: Rule) -> list[str]:
    """The targets a recipe makes with ``$(MAKE)``: the words after it that are no option, no
    variable's value and no comment."""
    found = []
    for line in rule.recipe:
        called = re.match(r"^[@+-]*\$[({]MAKE[)}]\s+(.*)$", line)
        if called:
            for word in shlex.split(called.group(1), comments=True):
                if not word.startswith("-") and "=" not in word:
                    found.append(word)
    return found


def made_with(rules: dict[str, Rule], target: str) -> set[str]:
    """``target`` and every target it makes, first or with ``$(MAKE)`` in its recipe, at any
    depth."""
    seen, todo = set(), [target]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            rule = rules.get(name, Rule())
            todo += [*rule.prerequisites, *submade(rule)]
    return seen


def pytest_calls(rule: Rule) -> list[list[str]]:
    """The arguments of each `uv run pytest` of a recipe."""
    words = [shlex.split(line) for line in rule.recipe]
    return [w[3:] for w in words if w[:3] == ["uv", "run", "pytest"]]


def marker_expression(args: list[str]) -> str | None:
    """A pytest command line's -m, the last one given, as pytest takes it; None without one."""
    found = None
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args):
            found = args[i + 1]
        elif a.startswith("-m") and len(a) > 2:
            found = a[2:]
    return found


def picks(expression: str, markers: frozenset[str]) -> bool:
    """Whether -m ``expression`` selects a test carrying ``markers``: and, or, not and the
    markers' names, which is all the Makefile and pyproject use."""

    def value(node: ast.AST) -> bool:
        if isinstance(node, ast.BoolOp):
            parts = [value(v) for v in node.values]
            return all(parts) if isinstance(node.op, ast.And) else any(parts)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not value(node.operand)
        if isinstance(node, ast.Name) and node.id in MARKERS:
            return node.id in markers
        raise AssertionError(f"not a marker expression this reads: {expression!r}")

    return value(ast.parse(expression, mode="eval").body)


def benchmark_problems(makefile: str, pyproject: str) -> list[str]:
    """What is wrong with the benchmark's place, read from the Makefile and pyproject.toml:
    a bare pytest, or a pytest call of a target but make benchmark, that selects a benchmark
    test, for any set of markers it may carry with it; make benchmark's call selecting any
    other than the benchmark's tests but those of a real Claude account; a target that makes
    make benchmark; and make benchmark not one pytest call, in one process (no -n or --dist),
    with -s."""
    rules = makefile_rules(makefile)
    addopts = tomllib.loads(pyproject)["tool"]["pytest"]["ini_options"]["addopts"]
    bare = marker_expression(shlex.split(addopts))
    problems = [
        f"a bare pytest runs a benchmark test carrying {sorted(m)}"
        for m in MARKER_SETS
        if BENCHMARK in m and (bare is None or picks(bare, m))
    ]
    for target, rule in sorted(rules.items()):
        for args in pytest_calls(rule):
            expression = marker_expression(args) or bare
            for m in MARKER_SETS:
                chosen = expression is None or picks(expression, m)
                if target != BENCHMARK and BENCHMARK in m and chosen:
                    problems.append(f"make {target} runs a benchmark test carrying {sorted(m)}")
                wanted = BENCHMARK in m and "real_claude" not in m
                if target == BENCHMARK and chosen != wanted:
                    what = "runs" if chosen else "leaves out"
                    problems.append(f"make benchmark {what} a test carrying {sorted(m)}")
        if target != BENCHMARK and BENCHMARK in made_with(rules, target):
            problems.append(f"make {target} makes make benchmark")
    bench = rules.get(BENCHMARK, Rule())
    calls = pytest_calls(bench)
    if len(calls) != 1 or len(bench.recipe) != 1:
        problems.append(f"make benchmark is not one pytest call: {bench.recipe}")
    for args in calls:
        if "-s" not in args:
            problems.append("make benchmark does not show what its tests report (-s)")
        if any(a.startswith(("-n", "--numprocesses", "--dist")) for a in args):
            problems.append(f"make benchmark runs in more than one process: {args}")
    return problems


def benchmark_marked() -> set[str]:
    """The tests a @pytest.mark.benchmark decorates, read from their source."""
    found = set()
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                ast.unparse(d) == "pytest.mark.benchmark" for d in node.decorator_list
            ):
                found.add(f"{path.relative_to(REPO)}::{node.name}")
    return found


def test_only_make_benchmark_runs_the_benchmark_and_in_one_process():
    """The three runs' split, as far as the benchmark goes, and make benchmark's own arguments,
    read without make. make benchmark takes the benchmark tests, those of a real Claude account
    aside, and no other target's pytest call, nor a bare pytest, takes one; no target makes it.
    The two other runs' split of what is left is held with make itself
    (test_every_test_is_in_exactly_one_of_the_two_runs)."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    rules = makefile_rules(makefile)
    # Non-vacuity: the targets' pytest calls are read, and the benchmark's tests are marked.
    assert {t for t, r in rules.items() if pytest_calls(r)} == {*RUNS, BENCHMARK}
    assert pytest_calls(rules[BENCHMARK]) == [shlex.split(BENCH_M) + ["-s"]]
    assert "lint" in made_with(rules, "check")
    assert benchmark_marked() == BENCHMARK_TESTS
    assert benchmark_problems(makefile, PYPROJECT.read_text(encoding="utf-8")) == []


def test_a_benchmark_test_run_by_another_target_is_reported():
    """Planted twin of the above: make test without its own 'and not benchmark'."""
    makefile, project = MAKEFILE.read_text(encoding="utf-8"), PYPROJECT.read_text(encoding="utf-8")
    planted_text = makefile.replace(TEST_M, TEST_M.replace(" and not benchmark", ""), 1)
    assert planted_text != makefile  # the plant took effect
    problems = benchmark_problems(planted_text, project)
    assert problems and problems[0] == "make test runs a benchmark test carrying ['benchmark']"


# --- the hang guard: every test of every run under it ---


def phony() -> list[str]:
    """The Makefile's targets, as its .PHONY names them."""
    found = re.findall(r"^\.PHONY:(.*)$", MAKEFILE.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1
    return found[0].split()


def guard_of(test: dict) -> dict:
    """The hang guard a test should run under: its tier's, the container tests' for a test
    carrying the container marker (tests/hang_guard.py)."""
    timeout = HOST_GUARD
    if MARKER in test["markers"]:
        timeout = CONTAINER_GUARD
    return {"timeout": timeout, "method": "thread", "func_only": False}


def unguarded(calls: list[Call]) -> list[str]:
    """The tests a target's pytest calls run under another hang guard than their tier's
    (tests/hang_guard.py), or under none, as pytest-timeout itself works it out for each."""
    problems = []
    for call in calls:
        if call.pytest_args() is None:
            continue
        for name, test in sorted(collect(call.pytest_args(), dict(call.env)).items()):
            if test["hang_guard"] != guard_of(test):
                problems.append(f"not under the hang guard ({test['hang_guard']}): {name}")
    return problems


@pytest.mark.parametrize("target", phony())
def test_every_test_of_a_target_is_under_its_tiers_hang_guard(tmp_path, target):
    assert unguarded(recorded(tmp_path, target)) == []


def test_the_targets_run_tests_of_both_tiers(tmp_path):
    """Non-vacuity for the above: some of the targets .PHONY names run pytest, and between them
    they run tests of both tiers."""
    ran = set().union(*(ran_by(recorded(tmp_path, target)) for target in phony()))
    tests = everything()
    assert ran & tests.host and ran & tests.container


def test_a_run_without_the_hang_guard_is_reported(tmp_path):
    """Planted twin: make test-container leaving pytest-timeout out runs its tests
    unguarded."""
    makefile = planted(tmp_path, CONTAINER_M, f"{CONTAINER_M} -p no:timeout")
    calls = recorded(tmp_path, "test-container", makefile)
    assert any("no:timeout" in c.argv for c in calls)  # the plant took effect
    assert unguarded(calls) == [
        f"not under the hang guard (None): {n}" for n in sorted(everything().container)
    ]


# --- make lint and make format: ruff for the Python code, Biome for the frontend ---

FRONTEND = REPO / "frontend"
RUFF = {
    "lint": [("uv", "run", "ruff", "format", "--check", "."), ("uv", "run", "ruff", "check", ".")],
    "format": [("uv", "run", "ruff", "format", "."), ("uv", "run", "ruff", "check", "--fix", ".")],
}


@pytest.mark.parametrize("target", ["lint", "format"])
def test_lint_and_format_run_ruff_and_the_frontend_script_of_the_same_name(tmp_path, target):
    calls = recorded(tmp_path, target)
    assert [(c.argv, c.cwd) for c in calls] == [
        *((argv, str(REPO)) for argv in RUFF[target]),
        (("npm", "run", target), str(FRONTEND)),
    ]


def lint_tree(tmp_path: Path, planted: str | None):
    """The real `make lint` in a scratch tree holding the Makefile, the frontend's
    package.json and Biome's configuration, and ``planted`` as frontend/src/planted.ts.
    uv is a recorder, so only the frontend's half does real work; npm and Biome are the
    real ones, Biome from the frontend's own packages."""
    biome = FRONTEND / "node_modules" / "@biomejs" / "biome"
    assert biome.is_dir(), "the frontend's packages are not installed: run make frontend-setup"
    tree = tmp_path / "tree"
    (tree / "frontend" / "src").mkdir(parents=True)
    shutil.copy(MAKEFILE, tree / "Makefile")
    for name in ("package.json", "biome.jsonc"):
        shutil.copy(FRONTEND / name, tree / "frontend" / name)
    (tree / "frontend" / "node_modules").symlink_to(FRONTEND / "node_modules")
    if planted is not None:
        (tree / "frontend" / "src" / "planted.ts").write_text(planted, encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "uv").write_text(RECORDER.format(python=sys.executable), encoding="utf-8")
    (fakebin / "uv").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("RCMAN_")}
    env |= {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}", "MAKE_RECORD": "/dev/null"}
    return subprocess.run(
        [shutil.which("make"), "-C", str(tree), "lint"],
        env=env,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )


WELL_FORMATTED = "export const planted = { a: 1 };\n"


def output(proc: subprocess.CompletedProcess) -> str:
    """What a run printed, without the colours Biome writes even to a pipe."""
    return re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr)


def checked(proc: subprocess.CompletedProcess) -> int:
    """How many files Biome says it checked."""
    found = re.findall(r"^Checked (\d+) files? in ", output(proc), re.MULTILINE)
    assert len(found) == 1, output(proc)
    return int(found[0])


def test_make_lint_fails_on_a_mis_formatted_frontend_file_and_names_it(tmp_path):
    """Positive control: Biome checks one file more with a well-formatted plant than without
    it, so a lint that found no files could not pass here. Planted twin: the same file, not
    formatted as Biome would."""
    bare = lint_tree(tmp_path / "bare", None)
    assert bare.returncode == 0, bare.stdout + bare.stderr
    proc = lint_tree(tmp_path / "formatted", WELL_FORMATTED)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert checked(proc) == checked(bare) + 1
    planted = WELL_FORMATTED.replace("{ a: 1 };", "{a:1}")
    assert planted != WELL_FORMATTED  # the plant took effect
    proc = lint_tree(tmp_path / "planted", planted)
    assert proc.returncode != 0
    assert "planted.ts" in output(proc)
    assert "File content differs from formatting output" in output(proc)


BIOME = FRONTEND / "node_modules" / ".bin" / "biome"
BIOME_CONFIG = FRONTEND / "biome.jsonc"


def biome(*args: str) -> str:
    proc = subprocess.run(
        [str(BIOME), *args], cwd=FRONTEND, capture_output=True, text=True, timeout=HANG_GUARD
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)


def enabled_rules() -> set[str]:
    """The rules Biome runs over the frontend, as group/name: biome rage lists them."""
    text = biome("rage", "--linter")
    section = text[text.index("Enabled rules:") :]
    return set(re.findall(r"^ +([a-zA-Z0-9]+/[a-zA-Z0-9]+)$", section, re.MULTILINE))


def default_severity(rule: str) -> str:
    """A rule's default severity, as biome explain states it."""
    found = re.findall(r"^- Default severity: (\w+)$", biome("explain", rule.split("/")[1]), re.M)
    assert len(found) == 1, rule
    return found[0]


def jsonc(text: str):
    """JSON with comments, as biome.jsonc is written: comments outside strings removed."""
    token = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
    return json.loads(token.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else "", text))


def configured_levels(config) -> dict[str, str]:
    """The level biome.jsonc sets for each rule it names, at the top level, as group/name."""
    levels = {}
    for group, rules in config["linter"]["rules"].items():
        for name, setting in rules.items() if isinstance(rules, dict) else ():
            level = setting["level"] if isinstance(setting, dict) else setting
            levels[f"{group}/{name}"] = level
    return levels


def unraised(config, enabled: set[str], info: set[str]) -> list[str]:
    """Enabled rules whose default severity is info that the configuration leaves there,
    where make lint, which fails on warnings, would pass their findings."""
    levels = configured_levels(config)
    return sorted(r for r in enabled & info if levels.get(r) not in ("warn", "error"))


def test_every_rule_biome_runs_at_info_is_raised_to_a_warning():
    from concurrent.futures import ThreadPoolExecutor

    assert BIOME.exists(), "the frontend's packages are not installed: run make frontend-setup"
    enabled = enabled_rules()
    assert "style/useTemplate" in enabled  # non-vacuity: a rule known to run at info
    with ThreadPoolExecutor(8) as pool:
        rules = sorted(enabled)
        severities = dict(zip(rules, pool.map(default_severity, rules), strict=True))
    info = {r for r, level in severities.items() if level == "info"}
    assert "style/useTemplate" in info
    config = jsonc(BIOME_CONFIG.read_text(encoding="utf-8"))
    assert unraised(config, enabled, info) == []
    # Planted twin: the same configuration without one of those rules raised.
    del config["linter"]["rules"]["style"]["useTemplate"]
    assert unraised(config, enabled, info) == ["style/useTemplate"]
