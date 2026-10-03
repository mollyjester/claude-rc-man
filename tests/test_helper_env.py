"""Every rc-helper the tests run has GitHub pointed away from the real one.

tests/helper_env.py builds the one environment the tests run rc-helper with;
the guard here finds, in the tests' own source, every other place that names
rc-helper's home hook, and the hook check shows git really is sent to the
address the hook gives, before anything is fetched.
"""

import ast
from pathlib import Path

import pytest

from tests.github_server import UNREACHABLE
from tests.helper_env import assert_github_hooked, helper_env

TESTS = Path(__file__).resolve().parent
HOME_HOOK = "RC_HELPER_HOME"
# Sites that name the home hook outside helper_env, each with why it may:
# (file relative to tests/, the function it is in) -> reason.
EXEMPT = {
    ("container/test_admin_in_container.py", "test_test_hooks_do_not_pass_through_sudo"): (
        "hands the hook to sudo to show that sudo drops it; the helper never sees it"
    ),
}


def hook_sites(source: str) -> list[tuple[int, str]]:
    """Where the source names the home hook as a key - in a dict, a call's keywords
    or a subscript assignment - with the function each is in."""
    tree = ast.parse(source)
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        keys = []
        if isinstance(node, ast.Dict):
            keys = node.keys
        elif isinstance(node, ast.Assign):
            keys = [t.slice for t in node.targets if isinstance(t, ast.Subscript)]
        elif isinstance(node, ast.Call):
            keys = [a for a in node.args[:1] if getattr(node.func, "attr", "") == "setenv"]
        for key in keys:
            if isinstance(key, ast.Constant) and key.value == HOME_HOOK:
                found.append((node.lineno, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


def unhooked_sites() -> tuple[list[str], int]:
    """Every site naming the home hook outside tests/helper_env.py and EXEMPT, and how
    many sites there are in all."""
    flagged, total = [], 0
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        for line, function in hook_sites(path.read_text(encoding="utf-8")):
            total += 1
            if rel != "helper_env.py" and (rel, function) not in EXEMPT:
                flagged.append(f"{rel}:{line} in {function or 'the module'}")
    return flagged, total


def test_every_rc_helper_environment_is_built_by_helper_env():
    flagged, total = unhooked_sites()
    assert total >= 2  # non-vacuity: helper_env's own and the exempt one are found
    assert flagged == []


def test_the_exemptions_still_have_their_sites():
    for rel, function in EXEMPT:
        assert (rel, function) in {
            (rel, f) for _, f in hook_sites((TESTS / rel).read_text(encoding="utf-8"))
        }, (rel, function)


@pytest.mark.parametrize(
    "planted",
    [
        'env = {"PATH": "/usr/bin", "RC_HELPER_HOME": "/x"}',
        'env["RC_HELPER_HOME"] = "/x"',
        'monkeypatch.setenv("RC_HELPER_HOME", "/x")',
        'run(dict(PATH="/usr/bin"), {"RC_HELPER_HOME": home})',
    ],
)
def test_the_guard_finds_a_planted_environment(planted):
    """Planted twins: each way of naming the hook is found, inside a function too."""
    assert hook_sites(planted) != []
    assert hook_sites(f"def f():\n    {planted}\n") == [(2, "f")]
    assert hook_sites("env = helper_env(home, claude, sock)") == []


def test_helper_env_always_sets_github():
    env = helper_env(Path("/h"), Path("/c"), Path("/s"))
    assert env["RC_HELPER_GITHUB"] == UNREACHABLE
    assert helper_env(Path("/h"), Path("/c"), Path("/s"), "http://127.0.0.1:9/")[
        "RC_HELPER_GITHUB"
    ] == ("http://127.0.0.1:9/")
    assert env["RC_HELPER_HOME"] == "/h"


def test_the_hook_check_passes_for_the_tests_github_and_the_unreachable_address(github, tmp_path):
    assert_github_hooked(tmp_path, github.url, github)
    assert_github_hooked(tmp_path, UNREACHABLE)


def test_the_hook_check_fails_without_the_hook_before_anything_is_fetched(
    github, tmp_path, monkeypatch
):
    """Planted twin: a git_env that drops the hook is caught by the address git would
    use, before git is asked to reach it."""
    from helpers import git_run
    from helpers import rc_helper as h

    plain = h.git_env
    monkeypatch.setattr(
        h, "git_env", lambda cfg: {k: v for k, v in plain(cfg).items() if "CONFIG" not in k}
    )
    reached = []
    monkeypatch.setattr(
        git_run.Git, "run", lambda self, cwd, args, **kw: reached.append(args) or "x"
    )
    with pytest.raises(AssertionError):
        assert_github_hooked(tmp_path, github.url, github)
    assert reached == []  # nothing went through the network path
