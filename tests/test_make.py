"""The Makefile's test targets: make test runs every test that runs on the host, make
test-container every test that needs a container, and make check both, with a
container test that cannot run failing rather than skipping.

Each target is run by the real make with uv, npm and npx replaced by recorders, so
what a target would run is read from what it ran; which tests a pytest command line
selects is read from pytest's own collection (tests/collected.py).
"""

import json
import shutil
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
    import subprocess

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
    neither, a container test run by make test, or make test-container running other
    than exactly the container tests."""
    full, marked = everything()
    host = set().union(*selected(test).values()) if test else set()
    ran = set().union(*selected(container).values()) if container else set()
    problems = [f"in neither: {n}" for n in sorted(full - host - marked)]
    problems += [f"a container test in make test: {n}" for n in sorted(marked & host)]
    problems += [f"not in make test-container: {n}" for n in sorted(marked - ran)]
    problems += [f"not a container test, in make test-container: {n}" for n in sorted(ran - marked)]
    return problems


def check_problems(check: list[Call]) -> list[str]:
    """What make check would miss: a test none of its pytest calls selects, or a call
    selecting container tests without RCMAN_REQUIRE_CONTAINER=1, where they would skip."""
    full, marked = everything()
    runs = selected(check)
    ran = set().union(*runs.values()) if runs else set()
    problems = [f"not run by make check: {n}" for n in sorted(full - ran)]
    problems += [
        f"container tests without RCMAN_REQUIRE_CONTAINER=1: {' '.join(call.argv)}"
        for call, tests in runs.items()
        if tests & marked and ("RCMAN_REQUIRE_CONTAINER", "1") not in call.env
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


@pytest.mark.parametrize("target", ["test", "test-container"])
def test_a_program_the_recorder_does_not_stand_in_for_fails_the_target(tmp_path, target):
    """Positive control for the recorder: a recipe running anything else fails make."""
    makefile = planted(tmp_path, "\tuv run pytest", "\tpytest", -1)
    with pytest.raises(AssertionError):
        recorded(tmp_path, target, makefile)
