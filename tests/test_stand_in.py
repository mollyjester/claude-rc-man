"""The stand-in for github.com (tests/git_fixtures.py, installed for every test by the
``github_stand_in`` fixture of tests/conftest.py): a git clone, fetch, pull, push or ls-remote
run through the host runtime of an address under https://github.com/ reaches a folder of the
test's own, and one that could reach the real one fails the guard."""

from __future__ import annotations

from pathlib import Path

import pytest
from rcman import runtime as rt
from rcman.repository import Repository

from tests import git_fixtures as gf
from tests.git_fixtures import make_repo

NAME = "rcman-ws-1"
URL = "https://github.com/octo/widget"
BASE = Path("/stand/in")
CLONE = ["env", "-i", "HOME=/h", "git", "-C", "/w", "clone", "--", URL, "."]
OTHER = ["env", "-i", "HOME=/h", "git", "-C", "/w", "status"]
# Each subcommand that may reach a remote, as rcman's argv has it.
NETWORK = {
    "clone": CLONE,
    "fetch": ["env", "-i", "HOME=/h", "git", "-C", "/w", "fetch", "--prune"],
    "pull": ["env", "-i", "HOME=/h", "git", "-C", "/w", "pull"],
    "push": ["env", "-i", "HOME=/h", "git", "-C", "/w", "push", "-u", "origin", "HEAD"],
    "ls-remote": ["env", "-i", "HOME=/h", "git", "-C", "/w", "ls-remote", "origin"],
}
# What reaches no remote, and is left as it is: among it the push URL a push to GitHub is
# known by, which a rewrite would turn into the stand-in's folder.
LOCAL = [
    OTHER,
    ["env", "-i", "HOME=/h", "git", "-C", "/w", "remote", "get-url", "--push", "--", "origin"],
    ["env", "-i", "HOME=/h", "git", "-C", "/w", "rev-parse", "-q", "--verify", "@{upstream}"],
]
RULE = f"url.{BASE}/.insteadOf=https://github.com/"


def repository(runtime: rt.HostRuntime) -> Repository:
    runtime.create(rt.Spec(NAME, slot=1, workspace=1, person=1))

    def run(argv, timeout):
        return runtime.exec(NAME, argv, timeout=timeout)

    def run_stdin(argv, stdin, timeout):
        return runtime.exec(NAME, argv, stdin, timeout=timeout)

    return Repository(runtime.layout(NAME), run, run_stdin)


@pytest.mark.parametrize("sub", sorted(NETWORK))
def test_a_network_argv_is_given_the_stand_in_before_its_subcommand(sub):
    argv = NETWORK[sub]
    out = gf.through_stand_in(argv, BASE)
    assert out != argv
    assert out.index("-c") < out.index(sub) and RULE in out
    assert out.index(RULE) < out.index(sub)
    assert [t for t in out if t not in ("-c", RULE, *gf.STAND_IN_CONFIG)] == argv


@pytest.mark.parametrize("argv", LOCAL, ids=lambda argv: argv[6])
def test_an_argv_that_reaches_no_remote_is_left_as_it_is(argv):
    assert gf.through_stand_in(argv, BASE) == argv
    gf.assert_through_stand_in(argv, BASE)  # nor is it the guard's


def test_an_argv_with_no_git_in_it_is_left_as_it_is():
    assert gf.through_stand_in(["cat", "clone"], BASE) == ["cat", "clone"]


@pytest.mark.parametrize("sub", sorted(NETWORK))
def test_the_guard_passes_a_network_argv_through_the_stand_in_and_refuses_one_not_through_it(sub):
    argv = NETWORK[sub]
    gf.assert_through_stand_in(gf.through_stand_in(argv, BASE), BASE)
    with pytest.raises(AssertionError, match="github.com"):
        gf.assert_through_stand_in(argv, BASE)  # planted: an argv that is not rewritten
    elsewhere = gf.through_stand_in(argv, Path("/somewhere/else"))
    with pytest.raises(AssertionError):  # the stand-in of another folder is not this one's
        gf.assert_through_stand_in(elsewhere, BASE)


def test_the_git_of_an_argv_is_found_past_a_variable_whose_value_ends_in_git():
    """rcman's argv gives git its environment first: a value such as a path ending in /git
    is not git, and the git that runs is the one after the timeout tool."""
    argv = [
        "env", "-i", "HOME=/h", "X=/a/git", "timeout", "-s", "TERM", "-k", "5", "60",
        "git", "-C", "/w", "fetch", "--prune",
    ]  # fmt: skip
    out = gf.through_stand_in(argv, BASE)
    assert out[out.index("fetch") - 1] == gf.STAND_IN_CONFIG[-1]  # given to the real git
    assert out[: out.index("-C")] == argv[: argv.index("-C")]  # nothing put before it
    with pytest.raises(AssertionError, match="github.com"):
        gf.assert_through_stand_in(argv, BASE)  # planted twin: unrewritten, refused


def test_a_github_address_is_cloned_from_the_stand_in(runtime, github_stand_in, tmp_path):
    make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")
    git = repository(runtime)
    git.clone(URL)
    assert (Path(runtime.layout(NAME).work) / "README.md").is_file()


def test_an_address_the_stand_in_lacks_fails_without_reaching_the_network(runtime, github_stand_in):
    git = repository(runtime)
    from rcman.repository import CloneFailed

    with pytest.raises(CloneFailed) as failed:
        git.clone("https://github.com/octo/not-there")
    said = failed.value.err.decode()
    assert str(github_stand_in) in said  # the positive: git looked in the stand-in
    assert "resolve host" not in said and "github.com" not in said


def test_a_fetch_pull_and_push_of_a_github_address_reach_the_stand_in(
    runtime, github_stand_in, tmp_path
):
    """The positive of the rewrite in force: a workspace whose origin is on GitHub fetches,
    pulls and pushes the stand-in's repository, and git still names its push URL as
    GitHub's, as a push to GitHub is known by."""
    origin = github_stand_in / "octo" / "widget.git"
    make_repo(tmp_path / "src", origin=origin)
    git = repository(runtime)
    git.clone(URL)
    layout = runtime.layout(NAME)

    def run(*args: str):
        argv = layout.with_env(git.env, ["git", "-C", layout.work, *args])
        result = runtime.exec(NAME, argv)
        assert result.code == 0, (args, result.err)
        return result.out.decode()

    theirs = gf.commit_on_origin(origin, "main", "theirs.txt")
    run("fetch", "--prune")
    run("pull", "--ff-only")
    work = Path(layout.work)
    assert gf.fixture_git(work, "rev-parse", "HEAD").strip() == theirs
    ours = commit_in(work, "ours.txt")
    run("push")
    assert gf.fixture_git(origin, "rev-parse", "main").strip() == ours
    assert run("remote", "get-url", "--push", "origin").strip() == URL


def commit_in(repo: Path, name: str) -> str:
    gf.write(repo / name)
    gf.fixture_git(repo, "add", name)
    gf.fixture_git(repo, "commit", "-q", "-m", f"Add {name}")
    return gf.fixture_git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("sub", ["clone", "fetch", "pull", "push"])
def test_the_runtime_refuses_a_network_argv_the_rewrite_missed(runtime, monkeypatch, sub):
    """Planted twin of the guard in force for every host runtime: with the rewrite taken
    away, a clone, fetch, pull or push fails the test run before git is started."""
    repository(runtime)
    monkeypatch.setattr(gf, "through_stand_in", lambda argv, base: argv)
    with pytest.raises(AssertionError, match="github.com"):
        runtime.exec(NAME, NETWORK[sub])
    runtime.exec(NAME, OTHER)  # positive control: what reaches no remote is run
