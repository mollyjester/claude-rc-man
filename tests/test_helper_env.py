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
HOME_HOOK = "RC_HELPER_" + "HOME"  # built, so that this module does not name it itself
# Sites that name the home hook outside helper_env, each with why it may:
# (file relative to tests/, the function it is in) -> reason.
EXEMPT = {
    ("container/test_admin_in_container.py", "test_test_hooks_do_not_pass_through_sudo"): (
        "hands the hook to sudo to show that sudo drops it; the helper never sees it"
    ),
}


def hook_sites(source: str) -> list[tuple[int, str]]:
    """Where the source names the home hook other than to read it, with the function each
    is in: the name as a string anywhere - a dict's key, a subscript assigned to, the key
    given to setenv, setdefault or putenv, or held in a variable for any of them - or as
    "RC_HELPER_HOME=..." in an argv, or as a keyword, as dict() and update() take it.
    Reading it (env["RC_HELPER_HOME"]) and asking whether a mapping holds it are not."""
    tree = ast.parse(source)
    found = []
    reads = set()  # the string nodes that only read it

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            reads.add(id(node.slice))
        if isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            reads.add(id(node.left))

    def names_it(node) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(f"{HOME_HOOK}="):
                return True
            return node.value == HOME_HOOK and id(node) not in reads
        return isinstance(node, ast.keyword) and node.arg == HOME_HOOK

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if names_it(node):
            found.append((node.lineno if hasattr(node, "lineno") else 0, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


def configs_without_github(source: str) -> list[int]:
    """The lines where the source makes rc-helper's Config without saying where GitHub is,
    which would leave an in-process helper pointed at the real one."""
    tree = ast.parse(source)
    modules, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "helpers":
            modules |= {a.asname or a.name for a in node.names if a.name == "rc_helper"}
        elif isinstance(node, ast.ImportFrom) and node.module == "helpers.rc_helper":
            names |= {a.asname or a.name for a in node.names if a.name == "Config"}
        elif isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names if a.name == "helpers.rc_helper" and a.asname}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        made = (
            isinstance(func, ast.Attribute)
            and func.attr == "Config"
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ) or (isinstance(func, ast.Name) and func.id in names)
        if made and not any(k.arg == "github" for k in node.keywords):
            found.append(node.lineno)
    return found


def test_every_in_process_config_says_where_github_is():
    found, made = [], 0
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        made += source.count("Config(")
        found += [f"{path.relative_to(TESTS)}:{line}" for line in configs_without_github(source)]
    assert made > 0  # non-vacuity
    assert found == []


@pytest.mark.parametrize(
    "planted",
    [
        "from helpers import rc_helper as h\ncfg = h.Config(home=x, claude=y, tmux_socket=None)",
        "from helpers import rc_helper\ncfg = rc_helper.Config(home=x, claude=y, tmux_socket=None)",
        "from helpers.rc_helper import Config\ncfg = Config(home=x, claude=y, tmux_socket=None)",
        "import helpers.rc_helper as rh\ncfg = rh.Config(home=x, claude=y, tmux_socket=None)",
    ],
)
def test_the_config_guard_finds_a_config_with_no_github(planted):
    """Planted twins: each way of reaching rc-helper's Config is found without github=,
    and not with it; another module's Config is not rc-helper's."""
    assert configs_without_github(planted) == [2]
    assert (
        configs_without_github(planted.replace("tmux_socket=None", "tmux_socket=None, github=g"))
        == []
    )
    other = "from helpers import rc_admin as ra\ncfg = ra.Config(home=x)"
    assert configs_without_github(other) == []


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
        'env = dict(PATH="/usr/bin", RC_HELPER_HOME="/x")',
        'env.update(RC_HELPER_HOME="/x")',
        'env.setdefault("RC_HELPER_HOME", "/x")',
        'os.putenv("RC_HELPER_HOME", "/x")',
        'run(["env", "RC_HELPER_HOME=/x", "rc-helper", "list"])',
        'KEY = "RC_HELPER_HOME"',
    ],
)
def test_the_guard_finds_a_planted_environment(planted):
    """Planted twins: each way of naming the hook is found, inside a function too."""
    assert hook_sites(planted) != []
    assert hook_sites(f"def f():\n    {planted}\n") == [(2, "f")]


def test_the_guard_leaves_reading_the_hook_alone():
    for source in (
        "env = helper_env(home, claude, sock)",
        'home = env["RC_HELPER_HOME"]',
        'assert "RC_HELPER_HOME" not in seen',
    ):
        assert hook_sites(source) == [], source


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


# --- git given GitHub's address -------------------------------------------------------

# The tests' modules that give git GitHub's address on purpose, each keeping it away
# from the real one itself: the hook check, the stand-in and the fixtures. A literal
# address is all this guard sees; fixture git, which tests give addresses held in
# variables too, refuses every network transport (git_fixtures.fixture_env).
GITHUB_SANCTIONED = {"helper_env.py", "github_server.py", "git_fixtures.py"}
# Git calls given GitHub's address elsewhere, each with why it reaches no GitHub:
# (file relative to tests/, the function it is in) -> reason.
GITHUB_EXEMPT = {
    (
        "container/test_github_in_container.py",
        "test_the_token_is_stored_for_github_com_in_the_persons_home",
    ): "reads the configuration key naming GitHub's credential helper; git reaches nothing",
}
GITHUB_HOST = "github." + "com"  # built, so that this module does not name it itself


def github_git_calls(source: str) -> list[tuple[int, str]]:
    """Where the source runs git with GitHub's address among its arguments, with the
    function each is in: fixture_git, and any call whose first argument is "git" or a
    list or tuple starting with it, with a string anywhere in its arguments, an
    f-string's parts among them, that names GitHub's host."""
    found = []

    def runs_git(call: ast.Call) -> bool:
        if getattr(call.func, "id", None) == "fixture_git":
            return True
        first = call.args[0] if call.args else None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            first = first.elts[0]
        return isinstance(first, ast.Constant) and first.value == "git"

    def names_github(call: ast.Call) -> bool:
        return any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and GITHUB_HOST in node.value
            for arg in [*call.args, *(k.value for k in call.keywords)]
            for node in ast.walk(arg)
        )

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call) and runs_git(node) and names_github(node):
            found.append((node.lineno, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), "")
    return found


def github_git_sites() -> tuple[list[str], int]:
    """Every git call given GitHub's address outside GITHUB_SANCTIONED and GITHUB_EXEMPT,
    and how many such calls there are in all."""
    flagged, total = [], 0
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        for line, function in github_git_calls(path.read_text(encoding="utf-8")):
            total += 1
            if rel not in GITHUB_SANCTIONED and (rel, function) not in GITHUB_EXEMPT:
                flagged.append(f"{rel}:{line} in {function or 'the module'}")
    return flagged, total


def test_git_is_given_githubs_address_only_where_it_reaches_no_github():
    flagged, total = github_git_sites()
    assert total >= 1  # non-vacuity: the exempt site is found
    assert flagged == []


def test_the_github_exemptions_still_have_their_sites():
    for rel, function in GITHUB_EXEMPT:
        sites = github_git_calls((TESTS / rel).read_text(encoding="utf-8"))
        assert function in {f for _, f in sites}, (rel, function)


@pytest.mark.parametrize(
    "planted",
    [
        f'fixture_git(repo, "clone", "https://{GITHUB_HOST}/o/r")',
        f'fixture_git(repo, "fetch", f"https://{GITHUB_HOST}/{{owner}}/r")',
        f'subprocess.run(["git", "clone", "https://{GITHUB_HOST}/o/r"])',
        f'as_bob("git", "-C", repo, "ls-remote", "https://{GITHUB_HOST}/o/r")',
        f'run(("git", "push", url), env={{"X": "https://{GITHUB_HOST}/"}})',
    ],
)
def test_the_github_guard_finds_a_planted_git_call(planted):
    """Planted twins: each way of giving git the address is found, inside a function too."""
    assert github_git_calls(planted) != []
    assert github_git_calls(f"def f():\n    {planted}\n") == [(2, "f")]


def test_the_github_guard_leaves_other_calls_alone():
    for source in (
        f'fixture_git(repo, "clone", "{UNREACHABLE}o/r")',
        f'subprocess.run(["curl", "https://{GITHUB_HOST}/"])',
        f'helper(env, "git-clone", "--url=https://{GITHUB_HOST}/o/r")',
    ):
        assert github_git_calls(source) == [], source


def test_fixture_git_refuses_https_before_reaching_anything(tmp_path):
    """Whatever address a test gives fixture git, it never speaks https, so it cannot
    reach the real GitHub, whatever a rewrite would have sent there."""
    import subprocess

    from tests.git_fixtures import fixture_env

    address = "https://127.0.0.1:1/owner/repository"
    env = fixture_env(tmp_path / "home")
    refused = subprocess.run(["git", "ls-remote", address], env=env, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "transport 'https' not allowed" in refused.stderr, refused.stderr
    plain = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG_")}
    tried = subprocess.run(["git", "ls-remote", address], env=plain, capture_output=True, text=True)
    assert "not allowed" not in tried.stderr, tried.stderr  # positive control: git tried


@pytest.mark.parametrize(
    ("address", "transport"),
    [
        ("http://127.0.0.1:1/owner/repository", "http"),
        ("ssh://127.0.0.1:1/owner/repository", "ssh"),
        ("git@github.com:owner/repository.git", "ssh"),
        ("git://127.0.0.1:1/owner/repository", "git"),
    ],
)
def test_fixture_git_refuses_every_network_transport_before_connecting(
    tmp_path, address, transport
):
    """Fixture git reaches no network at all: each transport is refused before anything
    is connected to, an scp-like address too."""
    import subprocess

    from tests.git_fixtures import fixture_env

    env = fixture_env(tmp_path / "home")
    refused = subprocess.run(["git", "ls-remote", address], env=env, capture_output=True, text=True)
    assert refused.returncode != 0
    assert f"transport '{transport}' not allowed" in refused.stderr, refused.stderr


def test_fixture_git_still_reads_a_repository_by_its_path(tmp_path):
    """Positive control: the local repositories the fixtures make, and clone from by
    their path, are still read."""
    from tests.git_fixtures import fixture_git, make_repo

    source = make_repo(tmp_path / "source")
    fixture_git(tmp_path, "clone", "-q", "--", str(source), "copy")
    fixture_git(tmp_path, "clone", "-q", "--", f"file://{source}", "by-url")
    assert (tmp_path / "copy" / "README.md").is_file()
    assert (tmp_path / "by-url" / "README.md").is_file()
