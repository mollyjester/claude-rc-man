"""tests/container_marker.py: the container marker is derived from the docker fixture.

A test carries it exactly when it uses the fixture, directly or through another, and
nowhere is it placed by hand.
"""

import ast
from pathlib import Path

import pytest

from tests import container_marker
from tests.collected import collect

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
NAME = container_marker.MARKER
MARK = "mark." + NAME  # built, so that this module does not hold it
FIXTURE = container_marker.FIXTURE

PROJECT_CONFTEST = """
import pytest
from tests import container_marker

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    container_marker.mark(items)

@pytest.fixture(scope="session")
def docker():
    return ["docker"]

@pytest.fixture
def image(docker):
    return "an-image"
"""
PROJECT_TESTS = """
def test_uses_it(docker):
    pass

def test_uses_it_through_another(image):
    pass

def test_does_not(tmp_path):
    pass
"""


def project(pytester, hook=True):
    conftest = PROJECT_CONFTEST
    if not hook:
        conftest = conftest.replace("    container_marker.mark(items)", "    pass")
        assert conftest != PROJECT_CONFTEST  # the plant took effect
    pytester.makeconftest(conftest)
    pytester.makepyfile(test_project=PROJECT_TESTS)
    pytester.makeini(
        f"[pytest]\npythonpath = {REPO}\naddopts = --strict-markers -p no:cacheprovider\n"
        f"markers =\n    {container_marker.MARKER}: derived\n"
    )


def outcomes(pytester, expression):
    result = pytester.runpytest_subprocess("-m", expression, "-v")
    return {
        line.split("::")[1].split()[0]
        for line in result.outlines
        if "::" in line and " PASSED" in line
    }


def test_a_user_of_the_fixture_is_marked_directly_or_through_another(pytester):
    project(pytester)
    assert outcomes(pytester, container_marker.MARKER) == {
        "test_uses_it",
        "test_uses_it_through_another",
    }
    assert outcomes(pytester, f"not {container_marker.MARKER}") == {"test_does_not"}


def test_without_the_hook_nothing_is_marked(pytester):
    """Planted twin: the same project with the hook doing nothing marks no test."""
    project(pytester, hook=False)
    assert outcomes(pytester, container_marker.MARKER) == set()
    assert len(outcomes(pytester, f"not {container_marker.MARKER}")) == 3  # all of them ran


def test_in_the_suite_the_marker_is_exactly_the_fixtures_users():
    found = collect()
    marked = {n for n, t in found.items() if container_marker.MARKER in t["markers"]}
    users = {n for n, t in found.items() if container_marker.FIXTURE in t["fixtures"]}
    suite = {n for n in found if n.startswith("tests/test_container_suite.py::")}
    # Non-vacuity: the fixture is used, and the container suite is among its users.
    assert "tests/test_container_suite.py::test_docker_is_usable" in suite
    assert "tests/test_container_suite.py::test_the_whole_stack_in_a_browser" in suite
    assert suite and suite <= users
    assert marked == users
    assert "tests/test_collected.py::test_uses_tmp_path" not in marked


def _named(node, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _call(node) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return getattr(node.func, "attr", None) or getattr(node.func, "id", None)


def hand_placed(source: str) -> list[int]:
    """The lines where the source places the marker itself: pytest.mark.container or
    mark.container, called or not, getattr(<...>.mark, "container"), or the marker's
    name given to add_marker or applymarker, wherever these appear. A name held in a
    variable is not followed."""
    found = []
    for node in ast.walk(ast.parse(source)):
        attribute = (
            isinstance(node, ast.Attribute)
            and node.attr == NAME
            and (
                (isinstance(node.value, ast.Attribute) and node.value.attr == "mark")
                or (isinstance(node.value, ast.Name) and node.value.id == "mark")
            )
        )
        by_getattr = _call(node) == "getattr" and len(node.args) >= 2 and _named(node.args[1], NAME)
        by_string = _call(node) in ("add_marker", "applymarker") and any(
            _named(a, NAME) for a in node.args
        )
        if attribute or by_getattr or by_string:
            found.append(node.lineno)
    return found


def by_name(source: str) -> list[int]:
    """The lines where the source asks for the fixture by name, with getfixturevalue."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if _call(node) == "getfixturevalue" and any(_named(a, FIXTURE) for a in node.args)
    ]


def test_the_marker_is_never_placed_by_hand():
    sources = sorted(TESTS.rglob("*.py"))
    assert TESTS / "test_container_suite.py" in sources  # non-vacuity
    found = [
        f"{p.relative_to(REPO)}:{line}"
        for p in sources
        for line in hand_placed(p.read_text(encoding="utf-8"))
    ]
    assert found == []


@pytest.mark.parametrize(
    "planted",
    [
        f"@pytest.{MARK}\ndef test_x():\n    pass\n",
        f"pytestmark = pytest.{MARK}\n",
        f"pytestmark = [pytest.{MARK}, pytest.mark.skip]\n",
        f"@pytest.{MARK}(reason='x')\ndef test_x():\n    pass\n",
        f"from pytest import mark\n@{MARK}\ndef test_x():\n    pass\n",
        f'def pytest_collection_modifyitems(items):\n    items[0].add_marker("{NAME}")\n',
        f'def test_x(request):\n    request.applymarker("{NAME}")\n',
        f'def test_x(request):\n    request.node.add_marker("{NAME}")\n',
        f'pytestmark = getattr(pytest.mark, "{NAME}")\n',
        f'def test_x(request):\n    request.applymarker(getattr(pytest.mark, "{NAME}"))\n',
    ],
)
def test_the_hand_placed_marker_guard_finds_a_planted_marker(planted):
    assert hand_placed(planted) != []


def test_the_hand_placed_marker_guard_leaves_a_user_of_the_fixture_alone():
    assert hand_placed("def test_x(docker):\n    pass\n") == []
    assert hand_placed("@pytest.mark.skip\ndef test_x():\n    pass\n") == []
    assert hand_placed('def test_x(request):\n    request.applymarker("skip")\n') == []
    assert hand_placed("item.add_marker(getattr(pytest.mark, MARKER))\n") == []
    assert hand_placed('x = getattr(pytest.mark, "skip")\n') == []


def test_the_fixture_is_never_requested_by_name():
    """A test asking for the fixture with request.getfixturevalue does not hold it in its
    fixture names, so it would not carry the marker."""
    sources = sorted(TESTS.rglob("*.py"))
    assert TESTS / "test_container_suite.py" in sources  # non-vacuity
    found = [
        f"{p.relative_to(REPO)}:{line}"
        for p in sources
        for line in by_name(p.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_the_by_name_guard_finds_a_planted_request():
    planted = f'def test_x(request):\n    request.getfixturevalue("{FIXTURE}")\n'
    assert by_name(planted) == [2]
    other = planted.replace(f'"{FIXTURE}"', '"tmp_path"')
    assert other != planted and by_name(other) == []  # the plant was the fixture's name
