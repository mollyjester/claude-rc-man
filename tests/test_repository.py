"""backend/rcman/repository.py: the git verbs of a workspace, against real git repositories in
host-runtime workspaces, through a real channel and the runtime's exec."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from rcman import runtime as rt
from rcman.repository import Repository

from tests.git_fixtures import (
    commit_on_origin,
    fixture_git,
    make_repo,
    push_branch_to_origin,
    write,
)
from tests.hang_guard import HANG_GUARD

NAME = "rcman-ws-1"
AUTHOR = "[user]\n\tname = Ann Author\n\temail = ann@example.com\n"
TOKEN = "a-token-for-the-tests"


class World:
    """A host-runtime workspace, its repository verbs, and every argv they ran."""

    def __init__(self, runtime: rt.HostRuntime, tmp_path: Path):
        runtime.create(rt.Spec(NAME, slot=1, workspace=1, person=1))
        self.runtime, self.tmp_path = runtime, tmp_path
        self.layout = runtime.layout(NAME)
        self.work = Path(self.layout.work)
        self.home = Path(self.layout.home)
        self.argvs: list[list[str]] = []
        self.channel = runtime.channel(NAME)
        self.config(AUTHOR)

    def config(self, text: str) -> None:
        folder = self.home / ".config" / "git"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config").write_text(text)

    def credential(self, token: str) -> None:
        folder = self.home / ".config" / "git"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "credential").write_text(f"username=x-access-token\npassword={token}\n")

    def _decoyed(self, argv: list[str]) -> list[str]:
        """``argv`` run as the process this test runs in could: with a variable of the
        runner's environment that git must not be given."""
        self.argvs.append(argv)
        return [*self.layout.tool("env"), "RCMAN_DECOY=present", *argv]

    def run(self, argv, timeout):
        return self.channel.run(self._decoyed(argv), timeout)

    def run_stdin(self, argv, stdin, timeout):
        return self.runtime.exec(NAME, self._decoyed(argv), stdin, timeout=timeout)

    def repository(self, layout: rt.Layout | None = None) -> Repository:
        return Repository(layout or self.layout, self.run, self.run_stdin)


@pytest.fixture
def world(runtime, tmp_path):
    made = World(runtime, tmp_path)
    try:
        yield made
    finally:
        made.channel.close()


@pytest.fixture
def repo(world, tmp_path):
    """The workspace's repository, on main with a commit, and an origin holding main and
    feature outside the workspace."""
    return make_repo(world.work, origin=tmp_path / "origins" / "repo.git")


@pytest.fixture
def git(world, repo) -> Repository:
    return world.repository()


def origin_of(repo: Path) -> Path:
    return Path(fixture_git(repo, "remote", "get-url", "origin").strip())


def listed(out: dict, which: str) -> list[tuple[str, str]]:
    return [(f["path"], f["change"]) for f in out[which]["files"]]


def branch_of(path: Path) -> str:
    return fixture_git(path, "branch", "--show-current").strip()


def commit_in(repo: Path, name: str) -> str:
    write(repo / name)
    fixture_git(repo, "add", name)
    fixture_git(repo, "commit", "-q", "-m", f"Add {name}")
    return fixture_git(repo, "rev-parse", "HEAD").strip()


def on_remote(bare: Path, branch: str) -> str:
    return fixture_git(bare, "rev-parse", branch).strip()


# --- status --------------------------------------------------------------------------


def test_status_reports_the_branch_its_upstream_and_how_far_apart_they_are(git, repo):
    branch = git.status()["branch"]
    assert branch["name"] == "main" and branch["upstream"] == "origin/main"
    assert (branch["ahead"], branch["behind"]) == (0, 0)
    assert branch["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip()
    commit_in(repo, "local.txt")
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    fixture_git(repo, "fetch", "-q")
    branch = git.status()["branch"]
    assert (branch["ahead"], branch["behind"]) == (1, 1)


def test_status_lists_each_kind_of_change_in_its_list(git, repo):
    commit_in(repo, "both.txt")
    write(repo / "README.md", "changed\n")
    write(repo / "new.txt")
    fixture_git(repo, "add", "new.txt")
    write(repo / "folder" / "inside.txt")
    write(repo / "both.txt", "staged\n")
    fixture_git(repo, "add", "both.txt")
    write(repo / "both.txt", "and not\n")
    out = git.status()
    assert listed(out, "staged") == [("both.txt", "modified"), ("new.txt", "added")]
    assert sorted(listed(out, "not_staged")) == [
        ("README.md", "modified"),
        ("both.txt", "modified"),
        ("folder/", "untracked"),
    ]
    assert out["staged"]["more"] == 0
    assert "path" not in out


# --- staging -------------------------------------------------------------------------


def test_stage_stages_only_the_files_given(git, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    write(repo / "README.md", "changed\n")
    git.stage(["a.txt", "README.md"])
    out = git.status()
    assert listed(out, "staged") == [("README.md", "modified"), ("a.txt", "added")]
    assert listed(out, "not_staged") == [("b.txt", "untracked")]


def test_unstage_unstages_only_the_files_given(git, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    fixture_git(repo, "add", ".")
    git.unstage(["a.txt"])
    out = git.status()
    assert listed(out, "staged") == [("b.txt", "added")]
    assert listed(out, "not_staged") == [("a.txt", "untracked")]


# --- committing ----------------------------------------------------------------------


def test_commit_records_the_message_from_stdin_and_the_author_of_the_configuration(git, repo):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    message = "Add a.txt, ünïcode\n\nWith a body: --author-name=x; $(rm -rf ~)\n"
    out = git.commit(message)
    assert out == {"commit": fixture_git(repo, "rev-parse", "HEAD").strip(), "branch": "main"}
    shown = fixture_git(repo, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B")
    assert shown.startswith("Ann Author <ann@example.com>\nAnn Author <ann@example.com>\n")
    assert shown.split("\n", 2)[2].rstrip("\n") == message.rstrip("\n")
    assert listed(git.status(), "staged") == []
    assert "Add a.txt" not in repr(out)


# --- branches ------------------------------------------------------------------------


def test_branches_lists_local_and_remote_tracking_branches(git, repo):
    fixture_git(repo, "remote", "set-head", "origin", "main")
    fixture_git(repo, "branch", "local-only")
    out = git.branches()
    assert out["local"] == {
        "branches": [
            {"name": "local-only", "current": False, "upstream": None},
            {"name": "main", "current": True, "upstream": "origin/main"},
        ],
        "more": 0,
    }
    assert out["remote"] == {
        "branches": [
            {"name": "origin/feature", "local": None, "taken_by": None},
            {"name": "origin/main", "local": "main", "taken_by": "main"},
        ],
        "more": 0,
    }


def test_switch_to_a_remote_only_branch_makes_a_local_branch_tracking_it(git, repo):
    assert git.switch("origin/feature", remote=True) == {"branch": "feature"}
    assert branch_of(repo) == "feature"
    assert fixture_git(repo, "config", "branch.feature.remote").strip() == "origin"
    assert fixture_git(repo, "config", "branch.feature.merge").strip() == "refs/heads/feature"


def test_new_branch_makes_a_branch_and_switches_to_it(git, repo):
    assert git.new_branch("topic/new") == {"branch": "topic/new"}
    assert branch_of(repo) == "topic/new"
    assert fixture_git(repo, "rev-parse", "topic/new") == fixture_git(repo, "rev-parse", "main")


# --- fetching and pushing ------------------------------------------------------------


def test_fetch_adds_new_remote_branches_and_prunes_gone_ones(git, repo):
    push_branch_to_origin(repo, origin_of(repo), "later")
    fixture_git(origin_of(repo), "branch", "-D", "feature")

    def names():
        return [b["name"] for b in git.branches()["remote"]["branches"]]

    before = names()
    assert "origin/feature" in before and "origin/later" not in before  # positive control
    assert git.fetch() == {}
    assert names() == ["origin/later", "origin/main"]
    assert branch_of(repo) == "main"
    assert listed(git.status(), "not_staged") == []


def test_a_branch_with_no_upstream_is_pushed_and_set_to_track_it(git, repo):
    fixture_git(repo, "switch", "-q", "-c", "topic")
    head = commit_in(repo, "topic.txt")
    assert git.push() == {"branch": "topic", "upstream": "origin/topic", "upstream_set": True}
    assert on_remote(origin_of(repo), "topic") == head
    tracked = fixture_git(repo, "for-each-ref", "--format=%(upstream)", "refs/heads/topic")
    assert tracked.strip() == "refs/remotes/origin/topic"


# --- commit and push -----------------------------------------------------------------


def test_commit_and_push_commits_then_pushes(git, repo):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    out = git.commit_push("A message")
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    assert out["commit"] == head and out["push_problem"] is None
    assert out["push"]["upstream"] == "origin/main"
    assert on_remote(origin_of(repo), "main") == head


def test_a_failed_push_after_the_commit_is_reported_with_the_commit(git, repo):
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    write(repo / "a.txt")
    git.stage(["a.txt"])
    out = git.commit_push("A message")
    assert out["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip()
    assert out["push"] is None
    problem = out["push_problem"]
    assert (problem["code"], problem["status"]) == ("git_failed", 409)
    assert any("rejected" in line for line in problem["git"])


# --- sync ----------------------------------------------------------------------------


def subcommands(world: World) -> list[str]:
    """The git subcommand of each argv run, in order: what follows git's -C and the work
    folder in rcman's argv."""
    found = []
    for argv in world.argvs:
        if "git" in argv:
            found.append(argv[argv.index(world.layout.work, argv.index("git")) + 1])
    return found


def head_of(repo: Path) -> str:
    return fixture_git(repo, "rev-parse", "HEAD").strip()


def synced(**changed) -> dict:
    """Sync's answer for main and origin/main, with nothing done but what ``changed`` says."""
    return {
        "branch": "main",
        "upstream": "origin/main",
        "pulled": False,
        "pushed": False,
        "upstream_set": False,
        "push_problem": None,
        **changed,
    }


def pushes_elsewhere(repo: Path, mirror: Path) -> Path:
    """main pushed to ``mirror``, a bare repository holding main as it is, made its push
    remote, while it still tracks origin/main: once main is fast-forwarded from origin,
    where it pushes lacks what was pulled, so one sync both pulls and pushes. push.default
    is current, as git names no push destination (@{push}) for a simple push to a remote
    other than the upstream's."""
    fixture_git(mirror.parent, "clone", "-q", "--bare", str(origin_of(repo)), str(mirror))
    fixture_git(repo, "remote", "add", "mirror", str(mirror))
    fixture_git(repo, "fetch", "-q", "mirror")
    fixture_git(repo, "config", "branch.main.pushRemote", "mirror")
    fixture_git(repo, "config", "push.default", "current")
    assert fixture_git(repo, "rev-parse", "--symbolic-full-name", "@{push}").strip() == (
        "refs/remotes/mirror/main"
    )  # the plant took effect
    return mirror


def test_sync_fast_forwards_then_pushes_in_that_order(git, repo, world, tmp_path):
    mirror = pushes_elsewhere(repo, tmp_path / "mirror.git")
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    world.argvs.clear()
    assert git.sync() == synced(pulled=True, pushed=True)
    assert head_of(repo) == theirs and on_remote(mirror, "main") == theirs
    ran = subcommands(world)
    assert ran.index("fetch") < ran.index("merge") < ran.index("push")


# --- a push to GitHub ----------------------------------------------------------------


def test_a_push_to_github_goes_with_the_token_to_the_stand_in(git, repo, world, github_stand_in):
    on_github = github_stand_in / "owner" / "name.git"
    fixture_git(world.tmp_path, "clone", "-q", "--bare", str(origin_of(repo)), str(on_github))
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/owner/name.git")
    world.credential(TOKEN)
    shown = git.status()
    assert (shown["push_to_github"], shown["github_token"]) == (True, True)
    ours = commit_in(repo, "ours.txt")
    assert git.push()["upstream"] == "origin/main"
    assert on_remote(on_github, "main") == ours
    assert TOKEN not in " ".join(sum(world.argvs, []))


# --- how git is run ------------------------------------------------------------------


def test_git_runs_with_the_allowlisted_environment(repo, world):
    """An allow-list: the runner's variables are dropped, and of a plan's, those of git (which
    could make it print what it sends, the token with it); the plan's others are kept."""
    plan = {"GIT_TRACE_CURL": "1", "JAVA_HOME": "/jdk"}
    git = world.repository(dataclasses.replace(world.layout, env=plan))
    dumped = world.tmp_path / "env"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nenv > {dumped}\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    git.stage(["a.txt"])
    git.commit("m")
    seen = dict(line.split("=", 1) for line in dumped.read_text().splitlines() if "=" in line)
    probe = world.run([*world.layout.tool("env")], HANG_GUARD)
    assert b"RCMAN_DECOY=present" in probe.out  # positive control: the runner had it
    assert "RCMAN_DECOY" not in seen and "GIT_TRACE_CURL" not in seen
    assert seen["JAVA_HOME"] == "/jdk"
    assert seen["HOME"] == world.layout.home and seen["LC_ALL"] == "C"
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and seen["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["GIT_CEILING_DIRECTORIES"] == str(world.work.parent)
    assert seen["PATH"].endswith(world.layout.path)  # git puts its own programs first
    allowed = {"HOME", "PATH", "LC_ALL", "JAVA_HOME", "PWD", "OLDPWD", "SHLVL", "_"}
    assert {k for k in seen if not k.startswith("GIT_")} <= allowed


# --- cloning ---------------------------------------------------------------------------

CLONE_URL = "https://github.com/octo/widget"


@pytest.fixture
def widget(github_stand_in, tmp_path) -> Path:
    """A repository on the tests' stand-in for github.com, at the address CLONE_URL."""
    return make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")


def test_a_clone_brings_every_branch_into_the_work_folder(world, widget):
    git = world.repository()
    assert git.version() >= (2, 31)  # the git in the workspace is one rcman can drive
    assert git.clone(CLONE_URL) == {}
    assert (world.work / "README.md").is_file() and (world.work / ".git").is_dir()
    assert fixture_git(world.work, "branch", "-r").split() == [
        "origin/HEAD",
        "->",
        "origin/main",
        "origin/feature",
        "origin/main",
    ]  # every branch of the repository, not the default one alone


def undriven(verbs: set[str], text: str) -> set[str]:
    """The verbs of ``verbs`` that ``text`` never calls."""
    return {v for v in verbs if f".{v}(" not in text}


def test_every_verb_is_driven_here():
    """The verbs the routes call are the ones tested: a new public verb needs a test."""
    verbs = {
        n for n in vars(Repository) if not n.startswith("_") and callable(getattr(Repository, n))
    }
    assert len(verbs) >= 10  # non-vacuity
    assert undriven(verbs, Path(__file__).read_text()) == set()


def test_the_verb_check_finds_a_verb_no_test_calls():
    """Planted twin: a verb named but never called is found."""
    assert undriven({"status", "planted"}, "git.status()\n# planted\n") == {"planted"}
