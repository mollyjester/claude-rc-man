"""The stand-in for github.com (tests/git_fixtures.py, installed for every test by the
``github_stand_in`` fixture of tests/conftest.py): a git command that may reach a remote is
given a folder of the test's own in place of https://github.com/, a clone through the host
runtime reaches it, and one that could reach the real one fails the guard."""

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


def test_a_network_argv_is_given_the_stand_in_and_any_other_is_left_as_it_is():
    for sub, argv in NETWORK.items():
        out = gf.through_stand_in(argv, BASE)
        assert out.index(RULE) < out.index(sub), sub
        assert [t for t in out if t not in ("-c", RULE, *gf.STAND_IN_CONFIG)] == argv
        gf.assert_through_stand_in(out, BASE)
    for argv in LOCAL:
        assert gf.through_stand_in(argv, BASE) == argv
        gf.assert_through_stand_in(argv, BASE)  # nor is it the guard's


def test_a_github_address_is_cloned_from_the_stand_in(runtime, github_stand_in, tmp_path):
    make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")
    git = repository(runtime)
    git.clone(URL)
    assert (Path(runtime.layout(NAME).work) / "README.md").is_file()


def test_the_runtime_refuses_a_network_argv_the_rewrite_missed(runtime, monkeypatch):
    """Planted twin of the guard in force for every host runtime: with the rewrite taken
    away, a clone fails the test run before git is started."""
    repository(runtime)
    monkeypatch.setattr(gf, "through_stand_in", lambda argv, base: argv)
    with pytest.raises(AssertionError, match="github.com"):
        runtime.exec(NAME, CLONE)
    runtime.exec(NAME, OTHER)  # positive control: what reaches no remote is run
