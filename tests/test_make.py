"""The Makefile's test targets: make test runs every test that runs on the host, make
test-container every test that needs a container, and make check both, with a
container test that cannot run, or a test of the real claude, failing rather than
skipping, and none of them running a test that needs a real Claude account. make lint
and make format cover both the Python code and the frontend.

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


def everything() -> tuple[set[str], set[str]]:
    """Every test a bare pytest collects, and those of them carrying the marker."""
    full = collect()
    return set(full), {n for n, t in full.items() if MARKER in t["markers"]}


def split_problems(test: list[Call], container: list[Call]) -> list[str]:
    """What is wrong with the split between the two targets' pytest calls: a test in
    neither, a container test run by make test, make test-container running other than
    exactly the container tests, or either running a test a bare pytest leaves out."""
    full, marked = everything()
    host = set().union(*selected(test).values()) if test else set()
    ran = set().union(*selected(container).values()) if container else set()
    problems = [f"in neither: {n}" for n in sorted(full - host - marked)]
    problems += [f"a container test in make test: {n}" for n in sorted(marked & host)]
    problems += [f"not in make test-container: {n}" for n in sorted(marked - ran)]
    problems += [
        f"not a container test, in make test-container: {n}" for n in sorted((ran & full) - marked)
    ]
    problems += left_out(host, full, "make test") + left_out(ran, full, "make test-container")
    return problems


def left_out(ran: set[str], full: set[str], target: str) -> list[str]:
    """Tests a target runs that a bare pytest leaves out. A target's own -m replaces the
    -m 'not real_claude' of pyproject's addopts, so without its own 'not real_claude' a
    target runs the tests that need a real Claude account."""
    return [f"left out of a bare run, in {target}: {n}" for n in sorted(ran - full)]


def check_problems(check: list[Call]) -> list[str]:
    """What make check would get wrong: a test none of its pytest calls selects, a test a
    bare pytest leaves out, a call selecting container tests without
    RCMAN_REQUIRE_CONTAINER=1, where they would skip, or a call without
    RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests of the real claude would."""
    full, marked = everything()
    runs = selected(check)
    ran = set().union(*runs.values()) if runs else set()
    problems = [f"not run by make check: {n}" for n in sorted(full - ran)]
    problems += left_out(ran, full, "make check")
    problems += [
        f"container tests without RCMAN_REQUIRE_CONTAINER=1: {' '.join(call.argv)}"
        for call, tests in runs.items()
        if tests & marked and ("RCMAN_REQUIRE_CONTAINER", "1") not in call.env
    ]
    problems += [
        f"without RCMAN_REQUIRE_REAL_CLAUDE=1: {' '.join(call.argv)}"
        for call in runs
        if ("RCMAN_REQUIRE_REAL_CLAUDE", "1") not in call.env
    ]
    return problems


def known_positives(host: set[str], marked: set[str]) -> None:
    """Non-vacuity: both sides hold tests known to belong there."""
    assert "tests/test_container_suite.py::test_docker_is_usable" in marked
    assert "tests/test_container_suite.py::test_the_whole_stack_in_a_browser" in marked
    assert "tests/test_make.py::test_every_test_is_in_exactly_one_of_the_two_runs" in host
    assert any(n.startswith("tests/test_rules.py::test_the_backend_and_") for n in host)


def test_every_test_is_in_exactly_one_of_the_two_runs(tmp_path):
    test, container = recorded(tmp_path, "test"), recorded(tmp_path, "test-container")
    assert [c.pytest_args() is not None for c in test] == [True]
    assert all(c.cwd == str(REPO) for c in test + container)
    full, marked = everything()
    known_positives(set().union(*selected(test).values()), marked)
    assert split_problems(test, container) == []


def test_make_check_runs_every_test_and_container_tests_cannot_skip(tmp_path):
    check = recorded(tmp_path, "check")
    assert len([c for c in check if c.pytest_args() is not None]) >= 2  # non-vacuity
    assert check_problems(check) == []


def planted(tmp_path: Path, old: str, new: str, count: int = 1) -> Path:
    """A copy of the Makefile with ``old`` replaced ``count`` times (-1: everywhere); the
    change must have taken effect."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert old in text, old
    path = tmp_path / "Makefile.planted"
    path.write_text(text.replace(old, new, count), encoding="utf-8")
    assert path.read_text(encoding="utf-8") != text
    return path


def test_a_test_dropped_from_make_test_is_reported(tmp_path):
    """Planted twin: make test deselecting one test leaves it in neither run."""
    dropped = "tests/test_make.py::test_a_test_dropped_from_make_test_is_reported"
    makefile = planted(
        tmp_path,
        'uv run pytest -n auto -m "not container and not real_claude"',
        f'uv run pytest -n auto -m "not container and not real_claude" --deselect {dropped}',
    )
    test = recorded(tmp_path, "test", makefile)
    assert any(dropped in c.argv for c in test)  # the plant reached pytest
    container = recorded(tmp_path, "test-container")
    assert split_problems(test, container) == [f"in neither: {dropped}"]


def test_container_tests_run_by_make_test_are_reported(tmp_path):
    """Planted twin: make test without the container filter runs the container tests."""
    makefile = planted(tmp_path, '-m "not container and not real_claude"', "")
    test = recorded(tmp_path, "test", makefile)
    _, marked = everything()
    problems = split_problems(test, recorded(tmp_path, "test-container"))
    assert problems == [f"a container test in make test: {n}" for n in sorted(marked)]


def test_make_check_without_the_container_run_is_reported(tmp_path):
    """Planted twin: make check without test-container misses every container test."""
    makefile = planted(tmp_path, "check: lint test test-container", "check: lint test")
    _, marked = everything()
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [f"not run by make check: {n}" for n in sorted(marked)]


def test_container_tests_that_could_skip_are_reported(tmp_path):
    """Planted twin: make test-container without RCMAN_REQUIRE_CONTAINER=1."""
    makefile = planted(tmp_path, "test-container: export RCMAN_REQUIRE_CONTAINER = 1\n", "")
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert len(problems) == 1
    assert problems[0].startswith("container tests without RCMAN_REQUIRE_CONTAINER=1")


def real_claude_tests() -> list[str]:
    """The tests a bare pytest leaves out, by pyproject's addopts; non-vacuity: some."""
    found = sorted(collect(["-m", "real_claude"]))
    assert "tests/test_claude_real_account.py" in {n.split("::")[0] for n in found}
    return found


def test_real_claude_tests_run_by_make_test_are_reported(tmp_path):
    """Planted twin: a target's own -m replaces addopts' -m 'not real_claude', so make
    test without its own 'and not real_claude' runs the tests that need a real account."""
    makefile = planted(tmp_path, '-m "not container and not real_claude"', '-m "not container"')
    test = recorded(tmp_path, "test", makefile)
    problems = split_problems(test, recorded(tmp_path, "test-container"))
    assert problems == [f"left out of a bare run, in make test: {n}" for n in real_claude_tests()]


def test_real_claude_tests_run_by_make_test_container_are_reported(tmp_path):
    """Planted twin: make test-container selecting the real-account tests too."""
    makefile = planted(
        tmp_path, '-m "container and not real_claude"', '-m "container or real_claude"'
    )
    container = recorded(tmp_path, "test-container", makefile)
    problems = split_problems(recorded(tmp_path, "test"), container)
    assert problems == [
        f"left out of a bare run, in make test-container: {n}" for n in real_claude_tests()
    ]


def test_real_claude_tests_run_by_make_check_are_reported(tmp_path):
    """Planted twin: make check running make test without 'and not real_claude'."""
    makefile = planted(tmp_path, '-m "not container and not real_claude"', '-m "not container"')
    problems = check_problems(recorded(tmp_path, "check", makefile))
    assert problems == [f"left out of a bare run, in make check: {n}" for n in real_claude_tests()]


def test_make_check_without_the_real_claude_requirement_is_reported(tmp_path):
    """Planted twin: make check not setting RCMAN_REQUIRE_REAL_CLAUDE=1, where the tests
    of the real claude would skip rather than fail without it."""
    makefile = planted(tmp_path, "check: export RCMAN_REQUIRE_REAL_CLAUDE = 1\n", "")
    calls = [c for c in recorded(tmp_path, "check", makefile) if c.pytest_args() is not None]
    assert len(calls) >= 2  # non-vacuity: the host run and the container run
    assert check_problems(calls) == [
        f"without RCMAN_REQUIRE_REAL_CLAUDE=1: {' '.join(c.argv)}" for c in calls
    ]


@pytest.mark.parametrize("target", ["test", "test-container"])
def test_a_program_the_recorder_does_not_stand_in_for_fails_the_target(tmp_path, target):
    """Positive control for the recorder: a recipe running anything else fails make."""
    makefile = planted(tmp_path, "\tuv run pytest", "\tpytest", -1)
    with pytest.raises(AssertionError):
        recorded(tmp_path, target, makefile)


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


def lint_tree(tmp_path: Path, planted: str | None, node_modules: bool = True):
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
    if node_modules:
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


def test_make_lint_fails_on_a_mis_formatted_frontend_file_and_names_it(tmp_path):
    """Planted twin: the same file, not formatted as Biome would."""
    planted = WELL_FORMATTED.replace("{ a: 1 };", "{a:1}")
    assert planted != WELL_FORMATTED  # the plant took effect
    proc = lint_tree(tmp_path, planted)
    assert proc.returncode != 0
    assert "planted.ts" in output(proc)


def test_make_lint_fails_without_the_frontend_packages(tmp_path):
    """Without frontend/node_modules there is no Biome, and make lint fails, not passes."""
    proc = lint_tree(tmp_path, WELL_FORMATTED, node_modules=False)
    assert proc.returncode != 0
    assert "biome" in output(proc)
