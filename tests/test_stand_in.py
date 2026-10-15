"""The stand-in for github.com (tests/git_fixtures.py, installed for every test by the
``github_stand_in`` fixture of tests/conftest.py): a git clone run through the host
runtime of an address under https://github.com/ reaches a folder of the test's own, and a
clone that could reach the real one fails the guard."""

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
OTHER = ["env", "-i", "HOME=/h", "git", "-C", "/w", "fetch", "--prune"]


def repository(runtime: rt.HostRuntime) -> Repository:
    runtime.create(rt.Spec(NAME, slot=1, workspace=1, person=1))

    def run(argv, timeout):
        return runtime.exec(NAME, argv, timeout=timeout)

    def run_stdin(argv, stdin, timeout):
        return runtime.exec(NAME, argv, stdin, timeout=timeout)

    return Repository(runtime.layout(NAME), run, run_stdin)


def test_a_clone_argv_is_given_the_stand_in_before_its_subcommand():
    out = gf.through_stand_in(CLONE, BASE)
    assert out != CLONE
    rule = f"url.{BASE}/.insteadOf=https://github.com/"
    assert out.index("-c") < out.index("clone") and rule in out
    assert out.index(rule) < out.index("clone")
    assert [t for t in out if t not in ("-c", rule, *gf.STAND_IN_CONFIG)] == CLONE


def test_an_argv_that_is_no_clone_is_left_as_it_is():
    assert gf.through_stand_in(OTHER, BASE) == OTHER
    assert gf.through_stand_in(["cat", "clone"], BASE) == ["cat", "clone"]  # no git in it


def test_the_guard_passes_a_clone_through_the_stand_in_and_refuses_one_not_through_it():
    gf.assert_through_stand_in(gf.through_stand_in(CLONE, BASE), BASE)
    with pytest.raises(AssertionError, match="github.com"):
        gf.assert_through_stand_in(CLONE, BASE)  # planted: a clone that is not rewritten
    elsewhere = gf.through_stand_in(CLONE, Path("/somewhere/else"))
    with pytest.raises(AssertionError):  # the stand-in of another folder is not this one's
        gf.assert_through_stand_in(elsewhere, BASE)
    gf.assert_through_stand_in(OTHER, BASE)  # what is no clone is not the guard's


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


def test_the_runtime_refuses_a_clone_the_rewrite_missed(runtime, monkeypatch):
    """Planted twin of the guard in force for every host runtime: with the rewrite taken
    away, a clone fails the test run before git is started."""
    repository(runtime)
    monkeypatch.setattr(gf, "through_stand_in", lambda argv, base: argv)
    with pytest.raises(AssertionError, match="github.com"):
        runtime.exec(NAME, CLONE)
    runtime.exec(NAME, OTHER)  # positive control: what is no clone is run
