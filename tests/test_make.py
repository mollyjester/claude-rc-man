"""The Makefile's test targets: make test runs every test that runs on the host, make
test-container the tests that need a container but not the whole stack, make check-ui
the whole-stack browser test, make benchmark the tests that measure and report, make check
the host tests with the lint and the frontend's tests, and make check-all all of those but
the benchmark. The two on-demand runs, test-container and check-ui, are not part of make
check, and no target but make benchmark runs a benchmark test. A container test that cannot
run, or a test of the real claude, fails rather than skips where make requires it, none of
the runs runs a test that needs a real Claude account, and every test of every run is under
the hang guard (tests/hang_guard.py). make lint and make format cover both the Python code
and the frontend.

Each target is run by the real make with uv, npm and npx replaced by recorders, so
what a target would run is read from what it ran; which tests a pytest command line
selects is read from pytest's own collection (tests/collected.py).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.collected import collect
from tests.container_marker import MARKER
from tests.hang_guard import CONTAINER_GUARD, HOST_GUARD

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
        timeout=60,
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


STACK = "stack"
BENCHMARK = "benchmark"
RUNS = ("test", "test-container", "check-ui", "benchmark")


@dataclass(frozen=True)
class Collected:
    """What a bare pytest collects, with the benchmark tests it leaves out: every test,
    those carrying the container marker, those carrying the stack marker and those carrying
    the benchmark marker."""

    full: set[str]
    container: set[str]
    stack: set[str]
    benchmark: set[str]

    @property
    def host(self) -> set[str]:
        return self.full - self.container


def everything() -> Collected:
    """Every test but those of a real Claude account: a bare pytest's, which pyproject's
    addopts leaves the benchmark out of, and the benchmark's own."""
    found = collect() | collect(["-m", f"{BENCHMARK} and not real_claude"])
    return Collected(
        set(found),
        {n for n, t in found.items() if MARKER in t["markers"]},
        {n for n, t in found.items() if STACK in t["markers"]},
        {n for n, t in found.items() if BENCHMARK in t["markers"]},
    )


def ran_by(calls: list[Call]) -> set[str]:
    return set().union(*selected(calls).values()) if calls else set()


def split_problems(
    test: list[Call], container: list[Call], ui: list[Call], bench: list[Call]
) -> list[str]:
    """What is wrong with the split between the four targets' pytest calls: every test
    belongs to exactly one run, which is make benchmark for a benchmark test, and for any
    other make test for a test that runs on the host, make test-container for a container
    test but the whole-stack one, and make check-ui for the whole-stack one; and none of the
    runs runs a test of a real Claude account."""
    tests = everything()
    host, ran, shown, measured = ran_by(test), ran_by(container), ran_by(ui), ran_by(bench)
    problems = [
        f"in none of the runs: {n}" for n in sorted(tests.full - host - ran - shown - measured)
    ]
    for tests_run, target in (
        (host, "make test"),
        (ran, "make test-container"),
        (shown, "make check-ui"),
    ):
        problems += [
            f"a benchmark test in {target}: {n}" for n in sorted(tests.benchmark & tests_run)
        ]
    problems += [f"not in make benchmark: {n}" for n in sorted(tests.benchmark - measured)]
    problems += [
        f"not a benchmark test, in make benchmark: {n}"
        for n in sorted((measured & tests.full) - tests.benchmark)
    ]
    problems += [f"a container test in make test: {n}" for n in sorted(tests.container & host)]
    problems += [
        f"not in make test-container: {n}"
        for n in sorted(tests.container - tests.stack - tests.benchmark - ran)
    ]
    problems += [f"a stack test in make test-container: {n}" for n in sorted(tests.stack & ran)]
    problems += [
        f"not a container test, in make test-container: {n}"
        for n in sorted((ran & tests.full) - tests.container)
    ]
    problems += [
        f"not in make check-ui: {n}" for n in sorted(tests.stack - tests.benchmark - shown)
    ]
    problems += [
        f"not a stack test, in make check-ui: {n}"
        for n in sorted((shown & tests.full) - tests.stack)
    ]
    for tests_run, target in (
        (host, "make test"),
        (ran, "make test-container"),
        (shown, "make check-ui"),
        (measured, "make benchmark"),
    ):
        problems += left_out(tests_run, tests.full, target)
    return problems


def left_out(ran: set[str], full: set[str], target: str) -> list[str]:
    """Collected a target runs that are not among ``full``, the tests but those of a real
    Claude account. A target's own -m replaces the -m of pyproject's addopts, so without its
    own 'not real_claude' a target runs the tests that need a real Claude account."""
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
    on-demand test (a container test) or a benchmark test it runs, a test of a real Claude
    account, or a call without RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests of the real
    claude would skip."""
    tests = everything()
    runs = selected(check)
    ran = set().union(*runs.values()) if runs else set()
    problems = [f"not run by make check: {n}" for n in sorted(tests.host - tests.benchmark - ran)]
    problems += [f"an on-demand test in make check: {n}" for n in sorted(tests.container & ran)]
    problems += [f"a benchmark test in make check: {n}" for n in sorted(tests.benchmark & ran)]
    return problems + left_out(ran, tests.full, "make check") + without_real_claude(runs)


def check_all_problems(check_all: list[Call]) -> list[str]:
    """What make check-all would get wrong: a test but the benchmark's that none of its
    pytest calls selects, a benchmark test it runs, a test of a real Claude account, a call
    selecting container tests without RCMAN_REQUIRE_CONTAINER=1, where they would skip, or a
    call selecting host tests without RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests of the
    real claude would."""
    tests = everything()
    runs = selected(check_all)
    ran = set().union(*runs.values()) if runs else set()
    problems = [
        f"not run by make check-all: {n}" for n in sorted(tests.full - tests.benchmark - ran)
    ]
    problems += [f"a benchmark test in make check-all: {n}" for n in sorted(tests.benchmark & ran)]
    problems += left_out(ran, tests.full, "make check-all")
    problems += [
        f"container tests without RCMAN_REQUIRE_CONTAINER=1: {' '.join(call.argv)}"
        for call, found in runs.items()
        if found & tests.container and ("RCMAN_REQUIRE_CONTAINER", "1") not in call.env
    ]
    return problems + without_real_claude(runs, tests.host)


def split_of(tmp_path: Path, **makefiles: Path) -> list[str]:
    """split_problems for the four targets, each from its entry of ``makefiles`` (by
    the target's name with _ for -) or else the real Makefile."""
    calls = {
        target: recorded(tmp_path, target, makefiles.get(target.replace("-", "_"), MAKEFILE))
        for target in RUNS
    }
    return split_problems(*calls.values())


def known_positives(tests: Collected) -> None:
    """Non-vacuity: every set holds tests known to belong there."""
    assert "tests/test_container_suite.py::test_podman_is_usable" in tests.container
    assert "tests/test_container_suite.py::test_podman_is_usable" not in tests.stack
    browser = "tests/container/test_stack_in_a_browser.py::test_the_whole_stack_in_a_browser"
    assert browser in tests.stack
    assert tests.stack <= tests.container  # a stack test needs the container too
    assert "tests/test_make.py::test_every_test_is_in_exactly_one_of_the_four_runs" in tests.host
    assert any(n.startswith("tests/test_rules.py::test_the_backend_and_") for n in tests.host)
    assert HOST_BENCHMARK in tests.benchmark and HOST_BENCHMARK in tests.host
    assert PODMAN_BENCHMARK in tests.benchmark and PODMAN_BENCHMARK in tests.container
    assert REDACTION_BENCHMARK in tests.benchmark
    assert (
        "tests/test_channel.py::test_only_a_channel_over_the_bound_in_every_batch_is_reported"
        in (tests.host - tests.benchmark)
    )


HOST_BENCHMARK = "tests/test_channel.py::test_the_channels_latency_on_the_host"
PODMAN_BENCHMARK = (
    "tests/container/test_runtime_in_podman.py::test_the_channels_latency_through_podman"
)
REDACTION_BENCHMARK = (
    "tests/test_claude_checks.py::test_how_long_redacting_a_screen_takes_by_its_size"
)


def test_every_test_is_in_exactly_one_of_the_four_runs(tmp_path):
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


def test_make_check_all_runs_every_test_but_the_benchmark_and_container_tests_cannot_skip(
    tmp_path,
):
    check_all = recorded(tmp_path, "check-all")
    assert len([c for c in check_all if c.pytest_args() is not None]) >= 3  # non-vacuity
    assert check_all_problems(check_all) == []


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
CONTAINER_M = '-m "container and not stack and not real_claude and not benchmark"'
UI_M = '-m "stack and not real_claude and not benchmark"'
BENCH_M = '-m "benchmark and not real_claude"'
CHECK_REAL = "check: export RCMAN_REQUIRE_REAL_CLAUDE = 1\n"


def runs_in_parallel(makefile: Path) -> bool:
    """Whether make -j may run targets beside each other: the Makefile has no .NOTPARALLEL."""
    return re.search(r"^\.NOTPARALLEL:", makefile.read_text(encoding="utf-8"), re.M) is None


def test_no_target_runs_beside_another_under_make_j():
    """make -j check-all must run its parts one after the other, as it does without -j."""
    assert not runs_in_parallel(MAKEFILE)


def test_a_makefile_without_notparallel_is_reported(tmp_path):
    """Planted twin: without the line, make -j may run the on-demand runs together."""
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


def test_container_tests_run_by_make_test_are_reported(tmp_path):
    """Planted twin: make test without the container filter runs the container tests."""
    makefile = planted(tmp_path, TEST_M, "")
    problems = split_of(tmp_path, test=makefile)
    tests = everything()
    assert problems == [
        f"a container test in make test: {n}" for n in sorted(tests.container - tests.benchmark)
    ]


def test_the_stack_test_run_by_make_test_container_is_reported(tmp_path):
    """Planted twin: make test-container without 'and not stack' runs the whole-stack test
    too, which make check-ui runs."""
    makefile = planted(
        tmp_path, CONTAINER_M, '-m "container and not real_claude and not benchmark"'
    )
    problems = split_of(tmp_path, test_container=makefile)
    assert problems == [
        f"a stack test in make test-container: {n}" for n in sorted(everything().stack)
    ]


def test_make_check_with_the_container_run_is_reported(tmp_path):
    """Planted twin: make check gaining test-container holds an on-demand run."""
    makefile = planted(tmp_path, "check: lint test ", "check: lint test test-container ")
    tests = everything()
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [
        f"an on-demand test in make check: {n}"
        for n in sorted(tests.container - tests.stack - tests.benchmark)
    ]


def test_make_check_with_the_whole_stack_run_is_reported(tmp_path):
    """Planted twin: make check gaining check-ui holds an on-demand run."""
    makefile = planted(tmp_path, "check: lint test ", "check: lint test check-ui ")
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [f"an on-demand test in make check: {n}" for n in sorted(everything().stack)]


def test_make_check_without_the_host_run_is_reported(tmp_path):
    """Planted twin: make check without test misses every host test."""
    makefile = planted(tmp_path, "check: lint test ", "check: lint ")
    problems = check_problems(recorded(tmp_path, "check", makefile))
    tests = everything()
    assert problems == [f"not run by make check: {n}" for n in sorted(tests.host - tests.benchmark)]


def test_make_check_all_without_the_container_run_is_reported(tmp_path):
    """Planted twin: make check-all without test-container misses those container tests."""
    makefile = planted(tmp_path, "check-all: check test-container ", "check-all: check ")
    tests = everything()
    problems = check_all_problems(recorded(tmp_path, "check-all", makefile))
    assert problems == [
        f"not run by make check-all: {n}"
        for n in sorted(tests.container - tests.stack - tests.benchmark)
    ]


def test_make_check_all_without_the_whole_stack_run_is_reported(tmp_path):
    """Planted twin: make check-all without check-ui misses the whole-stack test."""
    makefile = planted(
        tmp_path, "check-all: check test-container check-ui", "check-all: check test-container"
    )
    problems = check_all_problems(recorded(tmp_path, "check-all", makefile))
    assert problems == [f"not run by make check-all: {n}" for n in sorted(everything().stack)]


def test_container_tests_that_could_skip_are_reported(tmp_path):
    """Planted twin: make test-container, then make check-ui, without
    RCMAN_REQUIRE_CONTAINER=1, where the tests would skip rather than fail."""
    for target in ("test-container", "check-ui"):
        (tmp_path / target).mkdir()
        makefile = planted(tmp_path / target, f"{target}: export RCMAN_REQUIRE_CONTAINER = 1\n", "")
        problems = check_all_problems(recorded(tmp_path / target, "check-all", makefile))
        assert len(problems) == 1, problems
        assert problems[0].startswith("container tests without RCMAN_REQUIRE_CONTAINER=1")
        assert "pytest" in problems[0]


# --- make benchmark: on demand, in no other target ---


def phony() -> list[str]:
    """The Makefile's targets, as its .PHONY names them."""
    found = re.findall(r"^\.PHONY:(.*)$", MAKEFILE.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1
    return found[0].split()


@pytest.mark.parametrize(("target", "old"), [("test", TEST_M), ("test-container", CONTAINER_M)])
def test_a_benchmark_test_run_by_another_target_is_reported(tmp_path, target, old):
    """Planted twin: make test, or make test-container, without its 'and not benchmark' runs
    the benchmark tests of its tier too, which only make benchmark runs."""
    makefile = planted(tmp_path, old, old.replace(" and not benchmark", ""))
    tests = everything()
    tier = tests.host if target == "test" else tests.container - tests.stack
    expected = sorted(tests.benchmark & tier)
    assert expected  # non-vacuity: the benchmark has a test of each of these tiers
    assert split_of(tmp_path, **{target.replace("-", "_"): makefile}) == [
        f"a benchmark test in make {target}: {n}" for n in expected
    ]


def test_a_benchmark_left_out_of_make_benchmark_is_in_none_of_the_runs(tmp_path):
    """Planted twin: make benchmark deselecting a benchmark test leaves it in none of the runs."""
    makefile = planted(tmp_path, BENCH_M, f"{BENCH_M} --deselect {HOST_BENCHMARK}")
    assert split_of(tmp_path, benchmark=makefile) == [
        f"in none of the runs: {HOST_BENCHMARK}",
        f"not in make benchmark: {HOST_BENCHMARK}",
    ]


def test_make_check_all_with_the_benchmark_is_reported(tmp_path):
    """Planted twin: make check-all gaining make benchmark runs the benchmark, which no target
    but make benchmark runs."""
    makefile = planted(
        tmp_path,
        "check-all: check test-container check-ui",
        "check-all: check test-container check-ui benchmark",
    )
    calls = recorded(tmp_path, "check-all", makefile)
    (bench,) = [c for c in calls if c.pytest_args() and "benchmark and not real_claude" in c.argv]
    assert check_all_problems(calls) == [
        *(f"a benchmark test in make check-all: {n}" for n in sorted(everything().benchmark)),
        # make benchmark sets no RCMAN_REQUIRE_REAL_CLAUDE, which only make check needs
        f"without RCMAN_REQUIRE_REAL_CLAUDE=1: {' '.join(bench.argv)}",
    ]


@pytest.mark.parametrize("target", [t for t in phony() if t != "benchmark"])
def test_no_target_but_make_benchmark_runs_a_benchmark_test(tmp_path, target):
    benchmark = everything().benchmark
    assert benchmark  # non-vacuity
    assert ran_by(recorded(tmp_path, target)) & benchmark == set()


def test_make_benchmark_runs_in_one_process_and_its_container_test_cannot_skip(tmp_path):
    """One pytest call, without workers, printing what each test reports (-s), with
    RCMAN_REQUIRE_CONTAINER=1, as its Podman test would otherwise skip."""
    calls = [c for c in recorded(tmp_path, "benchmark") if c.pytest_args() is not None]
    assert len(calls) == 1
    (call,) = calls
    assert "-s" in call.pytest_args()
    assert not any(a.startswith(("-n", "--numprocesses", "--dist")) for a in call.pytest_args())
    assert ("RCMAN_REQUIRE_CONTAINER", "1") in call.env
    assert ran_by(calls) == everything().benchmark


def real_claude_tests() -> list[str]:
    """The tests a bare pytest leaves out, by pyproject's addopts; non-vacuity: some."""
    found = sorted(collect(["-m", "real_claude"]))
    assert "tests/test_claude_real_account.py" in {n.split("::")[0] for n in found}
    return found


@pytest.mark.parametrize(
    ("target", "old", "new"),
    [
        ("test", TEST_M, '-m "not container and not benchmark"'),
        (
            "test-container",
            CONTAINER_M,
            '-m "container and not stack and not benchmark or real_claude"',
        ),
        ("check-ui", UI_M, '-m "stack and not benchmark or real_claude"'),
        ("benchmark", BENCH_M, '-m "benchmark or real_claude"'),
    ],
)
def test_real_claude_tests_run_by_a_target_are_reported(tmp_path, target, old, new):
    """Planted twin: a target's own -m replaces addopts' -m 'not real_claude', so the
    target without its own 'and not real_claude' runs the tests that need a real account."""
    makefile = planted(tmp_path, old, new)
    problems = split_of(tmp_path, **{target.replace("-", "_"): makefile})
    assert problems == [
        f"left out of a bare run, in make {target}: {n}" for n in real_claude_tests()
    ]


def test_real_claude_tests_run_by_make_check_are_reported(tmp_path):
    """Planted twin: make check running make test without 'and not real_claude'."""
    makefile = planted(tmp_path, TEST_M, '-m "not container and not benchmark"')
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [f"left out of a bare run, in make check: {n}" for n in real_claude_tests()]


def test_make_check_without_the_real_claude_requirement_is_reported(tmp_path):
    """Planted twin: make check not setting RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests
    of the real claude would skip rather than fail without it."""
    makefile = planted(tmp_path, CHECK_REAL, "")
    calls = [c for c in recorded(tmp_path, "check", makefile) if c.pytest_args() is not None]
    assert len(calls) >= 1  # non-vacuity
    assert check_problems(calls) == [
        f"without RCMAN_REQUIRE_REAL_CLAUDE=1: {' '.join(c.argv)}" for c in calls
    ]


def test_make_check_all_without_the_real_claude_requirement_is_reported(tmp_path):
    """Planted twin: make check, which make check-all runs, not setting it."""
    makefile = planted(tmp_path, CHECK_REAL, "")
    problems = check_all_problems(recorded(tmp_path, "check-all", makefile))
    assert len(problems) == 1, problems
    assert problems[0].startswith("without RCMAN_REQUIRE_REAL_CLAUDE=1: uv run pytest")


# --- the hang guard: every test of every run under it ---


def guard_of(test: dict) -> dict:
    """The hang guard a test should run under: its tier's, the container tests' for a test
    carrying the container marker (tests/hang_guard.py)."""
    timeout = CONTAINER_GUARD if MARKER in test["markers"] else HOST_GUARD
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
    """Planted twin: make check-ui leaving pytest-timeout out runs its test unguarded."""
    makefile = planted(tmp_path, UI_M, f"{UI_M} -p no:timeout")
    calls = recorded(tmp_path, "check-ui", makefile)
    assert any("no:timeout" in c.argv for c in calls)  # the plant took effect
    assert unguarded(calls) == [
        f"not under the hang guard (None): {n}" for n in sorted(everything().stack)
    ]


def test_a_run_with_another_method_is_reported(tmp_path):
    """Planted twin: make check-ui asking for the signal method, which leaves a test whose
    worker thread never ends hanging."""
    makefile = planted(tmp_path, UI_M, f"{UI_M} --timeout-method signal")
    found = {"timeout": CONTAINER_GUARD, "method": "signal", "func_only": False}
    assert unguarded(recorded(tmp_path, "check-ui", makefile)) == [
        f"not under the hang guard ({found}): {n}" for n in sorted(everything().stack)
    ]


def test_a_container_test_under_the_host_guard_is_reported(tmp_path):
    """Planted twin: make check-ui giving the container tests the host tests' guard."""
    makefile = planted(tmp_path, UI_M, f"{UI_M} -o container_timeout={HOST_GUARD}")
    found = {"timeout": HOST_GUARD, "method": "thread", "func_only": False}
    assert unguarded(recorded(tmp_path, "check-ui", makefile)) == [
        f"not under the hang guard ({found}): {n}" for n in sorted(everything().stack)
    ]


@pytest.mark.parametrize("target", RUNS)
def test_a_program_the_recorder_does_not_stand_in_for_fails_the_target(tmp_path, target):
    """Positive control for the recorder: a recipe running anything else fails make."""
    makefile = planted(tmp_path, "\tuv run pytest", "\tpytest", -1)
    with pytest.raises(AssertionError):
        recorded(tmp_path, target, makefile)


# --- make screenshots: no test, and in no test run ---

SHOTS = ("npx", "playwright", "test", "--config", "playwright.screenshots.config.ts")


def test_make_screenshots_builds_the_frontend_then_takes_the_screenshots_with_their_own_config(
    tmp_path,
):
    calls = recorded(tmp_path, "screenshots")
    assert [(c.argv, c.cwd) for c in calls] == [
        (("npm", "run", "build"), str(FRONTEND)),
        (SHOTS, str(FRONTEND)),
    ]


@pytest.mark.parametrize("target", ["e2e", "check", "check-all"])
def test_no_test_run_takes_the_screenshots(tmp_path, target):
    calls = recorded(tmp_path, target)
    assert any(c.argv[:3] == ("npx", "playwright", "test") for c in calls)  # positive control
    assert not any("playwright.screenshots.config.ts" in c.argv for c in calls)


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


def lint_tree(
    tmp_path: Path, planted: str | None, node_modules: bool = True, where: str = "src/planted.ts"
):
    """The real `make lint` in a scratch tree holding the Makefile, the frontend's
    package.json and Biome's configuration, and ``planted`` as frontend/``where``.
    uv is a recorder, so only the frontend's half does real work; npm and Biome are the
    real ones, Biome from the frontend's own packages."""
    biome = FRONTEND / "node_modules" / "@biomejs" / "biome"
    assert biome.is_dir(), "the frontend's packages are not installed: run make frontend-setup"
    tree = tmp_path / "tree"
    (tree / "frontend" / "src").mkdir(parents=True)
    shutil.copy(MAKEFILE, tree / "Makefile")
    for name in ("package.json", "biome.jsonc"):
        shutil.copy(FRONTEND / name, tree / "frontend" / name)
    if node_modules:
        (tree / "frontend" / "node_modules").symlink_to(FRONTEND / "node_modules")
    if planted is not None:
        (tree / "frontend" / where).write_text(planted, encoding="utf-8")
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
        timeout=120,
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


def test_make_lint_passes_a_well_formatted_frontend_file_it_checked(tmp_path):
    """Positive control: Biome checks one file more with the plant than without it, so
    a lint that found no files could not pass here."""
    bare = lint_tree(tmp_path / "bare", None)
    assert bare.returncode == 0, bare.stdout + bare.stderr
    proc = lint_tree(tmp_path / "planted", WELL_FORMATTED)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert checked(proc) == checked(bare) + 1


FORMAT_FINDING = "File content differs from formatting output"


def test_make_lint_fails_on_a_mis_formatted_frontend_file_and_names_it(tmp_path):
    """Planted twin: the same file, not formatted as Biome would."""
    planted = WELL_FORMATTED.replace("{ a: 1 };", "{a:1}")
    assert planted != WELL_FORMATTED  # the plant took effect
    proc = lint_tree(tmp_path, planted)
    assert proc.returncode != 0
    assert "planted.ts" in output(proc) and FORMAT_FINDING in output(proc)


INFO_LEVEL = 'export const planted = (a: string) => "x" + a;\n'  # useTemplate: an info


def test_make_lint_fails_on_a_frontend_finding_biome_reports_as_an_info(tmp_path):
    """Planted twin: a lint finding whose default severity is info, in a file formatted as
    Biome would, fails make lint as any other finding does."""
    proc = lint_tree(tmp_path, INFO_LEVEL)
    assert "lint/style/useTemplate" in output(proc)  # the plant was found
    assert FORMAT_FINDING not in output(proc)  # and only as a lint finding
    assert proc.returncode != 0
    assert "planted.ts" in output(proc)


def test_make_lint_checks_the_frontends_index_html(tmp_path):
    """The page's index.html is among the files Biome checks: the real one passes, one
    file more than without it, and a planted one without a language fails."""
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    bare = lint_tree(tmp_path / "bare", None)
    proc = lint_tree(tmp_path / "real", index, where="index.html")
    assert proc.returncode == 0, output(proc)
    assert checked(proc) == checked(bare) + 1
    planted = index.replace('<html lang="en">', "<html>")
    assert planted != index  # the plant took effect
    proc = lint_tree(tmp_path / "planted", planted, where="index.html")
    assert proc.returncode != 0
    assert "index.html" in output(proc) and "lint/a11y/useHtmlLang" in output(proc)


BIOME = FRONTEND / "node_modules" / ".bin" / "biome"
BIOME_CONFIG = FRONTEND / "biome.jsonc"


def biome(*args: str) -> str:
    proc = subprocess.run(
        [str(BIOME), *args], cwd=FRONTEND, capture_output=True, text=True, timeout=60
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


def test_make_lint_fails_without_the_frontend_packages(tmp_path):
    """Without frontend/node_modules there is no Biome, and make lint fails, not passes."""
    proc = lint_tree(tmp_path, WELL_FORMATTED, node_modules=False)
    assert proc.returncode != 0
    assert "biome" in output(proc)
