"""backend/rcman/repository.py: the git verbs of a workspace, against real git repositories in
host-runtime workspaces, through a real channel and the runtime's exec."""

from __future__ import annotations

import dataclasses
import http.server
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import repository as repo_module
from rcman import runtime as rt
from rcman.problems import Problem
from rcman.repository import Repository

from tests.git_fixtures import (
    FIXTURE_HOME,
    add_submodule,
    commit_on_origin,
    fixture_env,
    fixture_git,
    make_repo,
    push_branch_to_origin,
    write,
)

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
        self.results: list[rt.Result] = []  # what each of them answered
        self.channel = runtime.channel(NAME)
        self.decoy = True
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
        result = self.channel.run(self._decoyed(argv), timeout)
        self.results.append(result)
        return result

    def run_stdin(self, argv, stdin, timeout):
        result = self.runtime.exec(NAME, self._decoyed(argv), stdin, timeout=timeout)
        self.results.append(result)
        return result

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


def refused(call, code: str) -> Problem:
    with pytest.raises(Problem) as raised:
        call()
    assert raised.value.code == code, (raised.value.code, raised.value.extra)
    return raised.value


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


def merge_conflict(repo: Path) -> None:
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "theirs\n")
    fixture_git(repo, "commit", "-q", "-am", "theirs")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "ours\n")
    fixture_git(repo, "commit", "-q", "-am", "ours")
    merge = subprocess.run(
        ["git", "merge", "-q", "other"],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    assert merge.returncode != 0  # positive control: there is a conflict


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


def test_status_of_a_detached_head(git, repo):
    assert git.status()["branch"]["name"] == "main"  # positive control
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert git.status()["branch"]["name"] is None


def test_status_of_no_commits_yet(git, repo):
    fixture_git(repo, "switch", "-q", "--orphan", "trunk")
    assert git.status()["branch"] == {
        "name": "trunk",
        "commit": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }


def test_status_of_a_branch_whose_upstream_is_gone_names_it_with_no_ahead_or_behind(git, repo):
    fixture_git(repo, "switch", "-q", "-c", "feature", "--track", "origin/feature")
    branch = git.status()["branch"]
    assert branch["upstream"] == "origin/feature"
    assert (branch["ahead"], branch["behind"]) == (0, 0)  # positive control: counted while there
    fixture_git(origin_of(repo), "branch", "-D", "feature")
    git.fetch()  # the verb's own fetch prunes it
    assert "origin/feature" not in [b["name"] for b in git.branches()["remote"]["branches"]]
    branch = git.status()["branch"]
    assert branch["name"] == "feature" and branch["upstream"] == "origin/feature"
    assert (branch["ahead"], branch["behind"]) == (None, None)


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


def test_a_rename_is_listed_with_where_it_came_from(git, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    [moved] = git.status()["staged"]["files"]
    assert moved == {"path": "MOVED.txt", "change": "renamed", "from": "README.md"}


def test_a_conflict_is_listed_as_unmerged(git, repo):
    merge_conflict(repo)
    assert listed(git.status(), "not_staged") == [("README.md", "unmerged")]


def test_a_submodule_is_marked(git, repo, tmp_path):
    add_submodule(repo, make_repo(tmp_path / "library"), "library")
    files = git.status()["staged"]["files"]
    assert {"path": "library", "change": "added", "submodule": True} in files
    assert {"path": ".gitmodules", "change": "added"} in files


def test_a_long_list_is_capped_and_says_how_many_more(git, repo):
    for i in range(repo_module.MAX_LISTED_FILES + 3):
        write(repo / f"f{i:05}.txt")
    out = git.status()["not_staged"]
    assert len(out["files"]) == repo_module.MAX_LISTED_FILES
    assert out["more"] == 3


def test_a_name_that_is_not_utf8_is_listed_but_cannot_be_staged(git, repo):
    os.close(os.open(bytes(repo) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY))
    write(repo / "plain.txt")
    files = git.status()["not_staged"]["files"]
    assert {"path": "plain.txt", "change": "untracked"} in files  # positive control
    odd = next(f for f in files if f["path"] != "plain.txt")
    assert odd["selectable"] is False and odd["path"].startswith("caf")
    assert "selectable" not in next(f for f in files if f["path"] == "plain.txt")
    refused(lambda: git.stage(["caf\udce9.txt"]), "file_invalid")
    assert listed(git.status(), "staged") == []


def test_status_says_when_a_merge_is_in_progress(git, repo):
    """Until the merge is committed, also once its conflicts are resolved and staged."""
    assert git.status()["merging"] is False  # positive control's twin: no merge
    merge_conflict(repo)
    assert git.status()["merging"] is True
    write(repo / "README.md", "resolved\n")
    git.stage(["README.md"])
    resolved = git.status()
    assert listed(resolved, "not_staged") == []  # no unmerged entry is left to say so
    assert resolved["merging"] is True
    git.commit("Merge other")
    assert git.status()["merging"] is False


def test_status_writes_nothing_into_the_repository(git, repo):
    index = repo / ".git" / "index"
    time.sleep(1.1)
    os.utime(repo / "README.md")  # the index's record of it is now stale
    before = index.stat().st_mtime_ns
    git.status()
    assert index.stat().st_mtime_ns == before
    fixture_git(repo, "status")  # positive control: git itself refreshes the index
    assert index.stat().st_mtime_ns != before


# --- a workspace that is no repository -------------------------------------------------


def test_a_workspace_with_no_git_of_its_own_is_not_a_repository(world):
    git = world.repository()
    for verb in (git.status, git.branches, git.fetch, git.push):
        refused(verb, "not_a_repository")
    refused(lambda: git.stage(["a"]), "not_a_repository")
    refused(lambda: git.commit("m"), "not_a_repository")


def test_a_repository_above_the_workspace_is_not_the_workspaces(world):
    """The workspace's volume is a repository: git would find it from the workspace's
    folder, but GIT_CEILING_DIRECTORIES keeps it from looking above the folder."""
    volume = world.work.parent
    fixture_git(volume, "init", "-q", "-b", "main")
    assert fixture_git(world.work, "rev-parse", "--show-toplevel").strip() == str(volume)
    refused(world.repository().status, "not_a_repository")
    make_repo(world.work)
    assert world.repository().status()["branch"]["name"] == "main"  # positive control


def test_an_empty_git_folder_is_not_a_repository(world):
    (world.work / ".git").mkdir()
    refused(world.repository().status, "not_a_repository")


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


def test_unstaging_a_rename_unstages_both_its_sides(git, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    git.unstage(["MOVED.txt"])
    out = git.status()
    assert listed(out, "staged") == []
    assert sorted(listed(out, "not_staged")) == [
        ("MOVED.txt", "untracked"),
        ("README.md", "deleted"),
    ]


def test_unstage_works_before_the_first_commit(git, repo):
    fixture_git(repo, "update-ref", "-d", "refs/heads/main")  # main has no commit again
    assert git.status()["branch"]["commit"] is None  # the plant took effect
    write(repo / "a.txt")
    git.stage(["a.txt"])
    assert ("a.txt", "added") in listed(git.status(), "staged")
    git.unstage(["a.txt"])
    assert ("a.txt", "untracked") in listed(git.status(), "not_staged")


def test_a_deleted_file_and_an_untracked_folder_are_staged(git, repo):
    (repo / "README.md").unlink()
    write(repo / "folder" / "x.txt")
    git.stage(["README.md", "folder/"])
    assert listed(git.status(), "staged") == [("README.md", "deleted"), ("folder/x.txt", "added")]


NAMES = ["with space.txt", "with\nnewline.txt", "ünïcode.txt", "tab\there.txt"]


def test_names_with_spaces_and_newlines_round_trip_through_staging(git, repo):
    for name in NAMES:
        write(repo / name)
    assert sorted(path for path, _ in listed(git.status(), "not_staged")) == sorted(NAMES)
    git.stage(NAMES)
    assert sorted(listed(git.status(), "staged")) == sorted((n, "added") for n in NAMES)


def test_names_are_taken_literally(git, repo):
    for name in ("*", "a", "ab", ":(glob)a*"):
        write(repo / name)
    git.stage(["*", ":(glob)a*"])
    assert sorted(listed(git.status(), "staged")) == [("*", "added"), (":(glob)a*", "added")]


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../x", "file_outside_repository"),
        ("/etc/passwd", "file_outside_repository"),
        ("a/../../x", "file_outside_repository"),
        (".git/config", "file_outside_repository"),
        (".GIT/config", "file_outside_repository"),
        ("a/.Git/config", "file_outside_repository"),
        ("a//b", "file_invalid"),
        ("./a", "file_invalid"),
        ("", "file_invalid"),
        ("a\0b", "file_invalid"),
        ("x" * (repo_module.MAX_PATH_LENGTH + 1), "file_invalid"),
    ],
)
def test_a_file_outside_the_repository_is_refused(git, repo, name, code):
    write(repo / "a")
    write(repo / "README.md", "changed\n")
    for verb in (git.stage, git.unstage):
        refused(lambda verb=verb: verb(["README.md", name]), code)
    assert listed(git.status(), "staged") == []  # nothing staged, not even README.md


def test_the_lexical_checks_let_an_ordinary_name_through(git, repo):
    """Positive control for the refusals above: a name with dots and a .gitignore."""
    write(repo / "a.b" / ".gitignore", "x\n")
    git.stage(["a.b/.gitignore"])
    assert listed(git.status(), "staged") == [("a.b/.gitignore", "added")]


def test_a_file_through_a_link_out_of_the_repository_is_refused_by_git(git, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "secret.txt")
    (repo / "out").symlink_to(elsewhere)
    problem = refused(lambda: git.stage(["out/secret.txt"]), "git_failed")
    assert any("beyond a symbolic link" in line for line in problem.extra["git"]), problem.extra
    assert listed(git.status(), "staged") == []
    assert (elsewhere / "secret.txt").read_text() == "content\n"
    assert not (elsewhere / ".git").exists()


def test_a_link_in_the_repository_is_staged_as_the_link(git, repo):
    (repo / "link").symlink_to("README.md")
    git.stage(["link"])
    assert fixture_git(repo, "ls-files", "-s", "link").startswith("120000 ")


def test_a_list_of_files_goes_on_stdin_never_in_an_argv(git, repo, world):
    write(repo / "SECRET-FILE-NAME.txt")
    git.stage(["SECRET-FILE-NAME.txt"])
    staged = [a for a in world.argvs if "--literal-pathspecs" in a]
    assert staged  # positive control: the staging ran
    assert "SECRET-FILE-NAME.txt" not in " ".join(sum(world.argvs, []))
    assert ("SECRET-FILE-NAME.txt", "added") in listed(git.status(), "staged")


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


def test_the_commit_message_reaches_no_argv(git, repo, world):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    git.commit("SECRET-MESSAGE-42")
    commits = [a for a in world.argvs if "commit" in a]
    assert commits  # positive control
    assert "SECRET-MESSAGE-42" not in " ".join(sum(world.argvs, []))
    assert "SECRET-MESSAGE-42" in fixture_git(repo, "log", "-1", "--format=%B")


def test_commit_is_refused_when_nothing_is_staged(git, repo):
    write(repo / "README.md", "changed but not staged\n")
    refused(lambda: git.commit("A message"), "nothing_staged")
    fixture_git(repo, "add", "README.md")
    git.commit("A message")  # positive control


def test_a_message_as_long_as_allowed_is_committed(git, repo):
    from helpers import rules

    write(repo / "a.txt")
    git.stage(["a.txt"])
    git.commit("x" * rules.MAX_COMMIT_MESSAGE_LENGTH)


def test_hooks_run_and_a_refusal_comes_back_in_their_words(git, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'No commits on Fridays.' >&2\nexit 1\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    git.stage(["a.txt"])
    problem = refused(lambda: git.commit("A message"), "git_failed")
    assert problem.extra == {"git": ["No commits on Fridays."], "git_more": 0}


def test_a_lock_held_on_the_index_passes_on_gits_words(git, repo):
    write(repo / "a.txt")
    (repo / ".git" / "index.lock").write_text("")
    problem = refused(lambda: git.stage(["a.txt"]), "git_failed")
    assert any("index.lock" in line for line in problem.extra["git"])


# --- branches ------------------------------------------------------------------------


def remote_branch(git: Repository, name: str) -> dict:
    return next(b for b in git.branches()["remote"]["branches"] if b["name"] == name)


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


def test_a_long_list_of_branches_is_capped(git, repo):
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    count = repo_module.MAX_LISTED_BRANCHES + 1
    lines = "".join(f"create refs/heads/b{i:05} {head}\n" for i in range(count))
    fixture_git(repo, "update-ref", "--stdin", input=lines.encode())
    local = git.branches()["local"]
    assert len(local["branches"]) == repo_module.MAX_LISTED_BRANCHES
    assert local["more"] == 2  # the extra one, and main


def test_switch_to_a_local_branch(git, repo):
    fixture_git(repo, "branch", "other")
    assert git.switch("other") == {"branch": "other"}
    assert branch_of(repo) == "other"


def test_switch_to_a_remote_only_branch_makes_a_local_branch_tracking_it(git, repo):
    assert git.switch("origin/feature", remote=True) == {"branch": "feature"}
    assert branch_of(repo) == "feature"
    assert fixture_git(repo, "config", "branch.feature.remote").strip() == "origin"
    assert fixture_git(repo, "config", "branch.feature.merge").strip() == "refs/heads/feature"


@pytest.mark.parametrize(
    ("branch", "remote", "code"),
    [
        ("nope", False, "branch_not_found"),
        ("feature", False, "branch_not_found"),  # only on origin
        ("main", True, "branch_not_found"),
        ("origin/feature", False, "branch_not_found"),
        ("-f", False, "branch_invalid"),
        ("", False, "branch_invalid"),
        ("a\0b", False, "branch_invalid"),
    ],
)
def test_switch_refuses_a_branch_that_is_not_there(git, repo, branch, remote, code):
    refused(lambda: git.switch(branch, remote=remote), code)
    assert branch_of(repo) == "main"


def test_a_remote_branch_whose_name_a_local_branch_has_is_not_switched_to(git, repo):
    assert remote_branch(git, "origin/feature")["taken_by"] is None  # positive control
    fixture_git(repo, "branch", "--no-track", "feature", "origin/main")
    assert remote_branch(git, "origin/feature")["taken_by"] == "feature"
    refused(lambda: git.switch("origin/feature", remote=True), "branch_name_taken")
    assert branch_of(repo) == "main"
    assert git.switch("feature") == {"branch": "feature"}


def test_a_remote_with_a_slash_in_its_name_gives_the_name_git_gives(git, repo):
    fixture_git(repo, "remote", "add", "a/b", str(origin_of(repo)))
    fixture_git(repo, "fetch", "-q", "a/b")
    assert git.switch("a/b/feature", remote=True) == {"branch": "b/feature"}
    fixture_git(repo, "switch", "-q", "main")
    fixture_git(repo, "branch", "-q", "-D", "b/feature")
    fixture_git(repo, "branch", "--no-track", "b/feature", "origin/main")
    assert remote_branch(git, "a/b/feature")["taken_by"] == "b/feature"
    refused(lambda: git.switch("a/b/feature", remote=True), "branch_name_taken")


@pytest.mark.parametrize(
    ("local", "remote", "taken_by"),
    [
        ("feature/x", "origin/feature", "feature/x"),
        ("topic", "origin/topic/x", "topic"),
        ("feature-x", "origin/feature", None),  # positive controls: names beside each other
        ("topic/y", "origin/topic/x", None),
    ],
)
def test_a_local_branch_whose_name_git_cannot_keep_beside_the_new_one_takes_it(
    git, repo, local, remote, taken_by
):
    push_branch_to_origin(repo, origin_of(repo), "topic/x")
    fixture_git(repo, "fetch", "-q", "origin")
    fixture_git(repo, "branch", "--no-track", local, "origin/main")
    assert remote_branch(git, remote)["taken_by"] == taken_by
    if taken_by is None:
        git.switch(remote, remote=True)
    else:
        refused(lambda: git.switch(remote, remote=True), "branch_name_taken")
        assert branch_of(repo) == "main"


def test_a_switch_blocked_by_local_changes_passes_on_gits_words(git, repo):
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "on other\n")
    fixture_git(repo, "commit", "-q", "-am", "other")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "my unsaved work\n")
    problem = refused(lambda: git.switch("other"), "git_failed")
    assert any("would be overwritten" in line for line in problem.extra["git"])
    assert branch_of(repo) == "main"
    assert (repo / "README.md").read_text() == "my unsaved work\n"


def test_new_branch_makes_a_branch_and_switches_to_it(git, repo):
    assert git.new_branch("topic/new") == {"branch": "topic/new"}
    assert branch_of(repo) == "topic/new"
    assert fixture_git(repo, "rev-parse", "topic/new") == fixture_git(repo, "rev-parse", "main")


@pytest.mark.parametrize("name", ["a..b", "@{-1}", "a b", "x.lock", "a~1", "HEAD", "-x", ""])
def test_new_branch_refuses_a_name_git_would_not_take_as_given(git, repo, name):
    fixture_git(repo, "switch", "-q", "-c", "previous")
    fixture_git(repo, "switch", "-q", "main")  # so that @{-1} names a branch
    refused(lambda: git.new_branch(name), "branch_invalid")
    assert branch_of(repo) == "main"


def test_new_branch_with_a_name_taken_passes_on_gits_words(git, repo):
    problem = refused(lambda: git.new_branch("main"), "git_failed")
    assert any("already exists" in line for line in problem.extra["git"])


def test_new_branch_before_the_first_commit(git, repo):
    fixture_git(repo, "update-ref", "-d", "refs/heads/main")
    assert git.new_branch("start") == {"branch": "start"}
    assert git.status()["branch"]["name"] == "start"


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


@pytest.fixture
def asks_for_a_password():
    """An HTTP server that answers every request with a demand for a password."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="rcman test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
    server.shutdown()


def test_fetch_from_a_remote_that_needs_a_password_fails_without_asking(
    git, repo, asks_for_a_password
):
    fixture_git(repo, "remote", "set-url", "origin", asks_for_a_password)
    began = time.monotonic()
    problem = refused(git.fetch, "git_failed")
    assert any("terminal prompts disabled" in line for line in problem.extra["git"])
    assert time.monotonic() - began < repo_module.GIT_NETWORK_TIMEOUT / 4


def test_a_branch_with_no_upstream_is_pushed_and_set_to_track_it(git, repo):
    fixture_git(repo, "switch", "-q", "-c", "topic")
    head = commit_in(repo, "topic.txt")
    assert git.push() == {"branch": "topic", "upstream": "origin/topic", "upstream_set": True}
    assert on_remote(origin_of(repo), "topic") == head
    tracked = fixture_git(repo, "for-each-ref", "--format=%(upstream)", "refs/heads/topic")
    assert tracked.strip() == "refs/remotes/origin/topic"


def test_a_branch_with_an_upstream_is_pushed_there(git, repo):
    head = commit_in(repo, "more.txt")
    assert git.push() == {"branch": "main", "upstream": "origin/main", "upstream_set": False}
    assert on_remote(origin_of(repo), "main") == head


def test_a_rejected_push_comes_back_in_gits_words(git, repo):
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    commit_in(repo, "local.txt")
    problem = refused(git.push, "git_failed")
    assert any("rejected" in line for line in problem.extra["git"])


def test_a_detached_head_is_not_pushed(git, repo):
    commit_in(repo, "more.txt")
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    before = on_remote(origin_of(repo), "main")
    refused(git.push, "detached_head")
    assert on_remote(origin_of(repo), "main") == before


def test_a_branch_with_no_commit_yet_is_not_pushed_but_passes_the_check(git, repo):
    fixture_git(repo, "switch", "-q", "--orphan", "trunk")
    fixture_git(repo, "reset", "-q")
    assert git.status()["branch"]["commit"] is None  # the plant took effect
    shown = git.status()
    assert (shown["push_to_github"], shown["push_has_remote"]) == (False, True)
    refused(git.push, "no_commits_yet")
    assert git.push(check=True) == {"branch": "trunk"}
    head = commit_in(repo, "first.txt")
    git.push()  # positive control: once committed, it goes
    assert on_remote(origin_of(repo), "trunk") == head


def test_a_repository_with_no_remote_has_nowhere_to_push(world):
    lonely = make_repo(world.work)
    commit_in(lonely, "x.txt")
    git = world.repository()
    shown = git.status()
    assert (shown["push_has_remote"], shown["push_to_github"]) == (False, False)
    refused(git.push, "no_push_remote")
    refused(lambda: git.push(check=True), "no_push_remote")


def test_a_branch_tracking_a_remote_of_another_name_has_a_remote(world, tmp_path):
    lonely = make_repo(world.work)
    elsewhere = tmp_path / "elsewhere.git"
    fixture_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(elsewhere))
    fixture_git(lonely, "remote", "add", "elsewhere", str(elsewhere))
    fixture_git(lonely, "push", "-q", "-u", "elsewhere", "main")
    git = world.repository()
    assert git.status()["push_has_remote"] is True
    head = commit_in(lonely, "y.txt")
    git.push()
    assert on_remote(elsewhere, "main") == head
    fixture_git(lonely, "switch", "-q", "-c", "topic")
    assert git.status()["push_has_remote"] is False  # no upstream, and no origin
    fixture_git(lonely, "remote", "rename", "elsewhere", "origin")
    assert git.status()["push_has_remote"] is True
    assert git.push()["upstream"] == "origin/topic"


def test_status_says_how_far_ahead_of_where_it_pushes_the_branch_is(git, repo, tmp_path):
    assert git.status()["push_ahead"] == 0
    commit_in(repo, "more.txt")
    assert git.status()["push_ahead"] == 1
    fixture_git(repo, "switch", "-q", "-c", "no-upstream")
    assert git.status()["push_ahead"] is None


# --- a push to GitHub ----------------------------------------------------------------


@pytest.fixture
def on_github(world, repo):
    """The repository's origin is on GitHub, as far as the address goes; nothing is
    connected to, as every case here is refused before git is asked to push."""
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/owner/name.git")
    return repo


def test_status_says_whether_a_push_goes_to_github_and_whether_a_token_is_there(
    git, world, on_github
):
    shown = git.status()
    assert (shown["push_to_github"], shown["github_token"]) == (True, False)
    world.credential(TOKEN)
    assert git.status()["github_token"] is True
    fixture_git(on_github, "remote", "set-url", "--push", "origin", str(world.tmp_path / "x.git"))
    shown = git.status()
    assert (shown["push_to_github"], shown["github_token"]) == (False, None)


def test_the_environment_git_is_run_with_does_not_move_its_configuration(world):
    moving = {
        "XDG_CONFIG_HOME": "/elsewhere",
        "GIT_CONFIG_GLOBAL": "/elsewhere/config",
        "GIT_CONFIG_SYSTEM": "/elsewhere/system",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "store",
    }
    layout = dataclasses.replace(world.layout, env={**moving, "JAVA_HOME": "/jdk"})
    env = world.repository(layout).env
    assert env["JAVA_HOME"] == "/jdk"  # the control: the plan's other variables reach git
    assert not set(moving) & set(env)


def test_rcmans_git_gets_no_variable_of_git_that_a_plan_sets_beyond_the_ones_rcman_sets(world):
    """An allow-list: a plan's GIT_ variables are all dropped, among them those that make git
    print what it sends (the token with it), name a program to ask for a password, or run."""
    sets = {
        "GIT_TRACE_CURL": "1",
        "GIT_TRACE": "trace.log",
        "GIT_TRACE_REDACT": "0",
        "GIT_CURL_VERBOSE": "1",
        "GIT_ASKPASS": "/x/ask",
        "GIT_EXEC_PATH": "/x",
        "GIT_SSH_COMMAND": "evil",
        "GIT_TERMINAL_PROMPT": "1",  # rcman's own wins
        "GIT_OPTIONAL_LOCKS": "1",
        "GIT_CEILING_DIRECTORIES": "/",
        "LC_ALL": "xx",
        "JAVA_HOME": "/jdk",
    }
    env = world.repository(dataclasses.replace(world.layout, env=sets)).env
    assert {k for k in env if k.startswith("GIT_")} == {
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
        "GIT_CEILING_DIRECTORIES",
    }
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CEILING_DIRECTORIES"] != "/" and env["LC_ALL"] == "C"
    assert env["JAVA_HOME"] == "/jdk"


def test_asking_whether_a_token_is_stored_moves_no_byte_of_the_credential_file(
    git, world, on_github
):
    world.credential(TOKEN)
    world.results.clear()
    assert git.status()["github_token"] is True
    touching = [
        (argv, result)
        for argv, result in zip(world.argvs[-len(world.results) :], world.results, strict=True)
        if any(a.endswith("/credential") for a in argv)
    ]
    assert touching  # the control: the file was asked about
    for _, result in touching:
        assert TOKEN.encode() not in result.out + result.err and result.out == b""


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("username=x-access-token\npassword=t\n", True),
        ("password=t", True),
        ("password=\n", False),
        ("password=  \n", False),
        ("", False),
        ("username=x\n", False),
        ("note=password=t\n", False),
    ],
)
def test_a_credential_file_holds_a_token_only_with_a_password_that_is_not_blank(
    git, world, on_github, text, stored
):
    folder = world.home / ".config" / "git"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "credential").write_text(text)
    assert git.status()["github_token"] is stored


def test_an_emptied_credential_file_is_no_token(git, world, on_github):
    world.credential(TOKEN)
    assert git.status()["github_token"] is True
    (world.home / ".config" / "git" / "credential").write_text("")
    assert git.status()["github_token"] is False


def test_a_push_to_github_is_refused_without_a_token(git, world, on_github):
    commit_in(on_github, "more.txt")
    refused(git.push, "github_token_not_set")
    refused(lambda: git.push(check=True), "github_token_not_set")
    world.credential(TOKEN)
    assert git.push(check=True) == {"branch": "main"}  # positive control: the check passes


def test_a_push_remote_of_its_own_decides_where_a_branch_is_pushed(git, world, repo):
    fixture_git(repo, "remote", "add", "github", "https://github.com/owner/name.git")
    assert git.status()["push_to_github"] is False
    fixture_git(repo, "config", "branch.main.pushRemote", "github")
    assert git.status()["push_to_github"] is True
    refused(git.push, "github_token_not_set")


def test_the_token_is_read_without_its_value_reaching_an_argv_or_an_answer(git, world, on_github):
    world.credential(TOKEN)
    out = git.status()
    assert TOKEN not in repr(out)
    git.push(check=True)
    assert TOKEN not in " ".join(sum(world.argvs, []))


# --- commit and push -----------------------------------------------------------------


def test_commit_and_push_commits_then_pushes(git, repo):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    out = git.commit_push("A message")
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    assert out["commit"] == head and out["push_problem"] is None
    assert out["push"]["upstream"] == "origin/main"
    assert on_remote(origin_of(repo), "main") == head


def test_commit_and_push_commits_nothing_when_the_push_would_be_refused_first(git, repo):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    before = fixture_git(repo, "rev-parse", "HEAD")
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    refused(lambda: git.commit_push("A message"), "detached_head")
    assert fixture_git(repo, "rev-parse", "HEAD") == before


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


def test_a_failure_of_the_runtime_in_the_push_half_is_the_push_problem_not_a_lost_commit(
    git, repo, world
):
    from rcman.channel import ChannelBroken, ChannelTimeout

    cases = [
        (rt.RuntimeFailed("boom"), "workspace_failed"),
        (ChannelBroken("gone"), "workspace_failed"),
        (ChannelTimeout(), "timeout"),
        (rt.RuntimeUnavailable("down"), "runtime_unavailable"),
    ]
    real = world.run_stdin
    for n, (failure, code) in enumerate(cases):
        write(repo / f"a{n}.txt")
        git.stage([f"a{n}.txt"])

        def failing(argv, stdin, timeout, failure=failure):
            if "push" in argv:
                raise failure
            return real(argv, stdin, timeout)

        world.run_stdin = failing
        out = world.repository().commit_push("m")
        assert out["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip(), code
        assert out["push"] is None and out["push_problem"]["code"] == code
    world.run_stdin = real
    assert git.push()["branch"] == "main"  # positive control: without the fault it goes


def test_a_programming_error_in_the_push_half_is_not_hidden(git, repo, world):
    write(repo / "a.txt")
    git.stage(["a.txt"])
    real = world.run_stdin

    def failing(argv, stdin, timeout):
        if "push" in argv:
            raise KeyError("a bug")
        return real(argv, stdin, timeout)

    world.run_stdin = failing
    with pytest.raises(KeyError):
        world.repository().commit_push("m")


def test_the_host_of_a_github_address_is_matched_whatever_its_case(git, repo):
    for url in ("https://GitHub.com/owner/name.git", "HTTPS://github.COM/owner/name.git"):
        fixture_git(repo, "remote", "set-url", "origin", url)
        assert git.status()["push_to_github"] is True, url
        refused(git.push, "github_token_not_set")
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com.example.org/o/n.git")
    assert git.status()["push_to_github"] is False


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


def test_sync_fast_forwards_a_branch_only_its_upstream_moved_and_pushes_nothing(git, repo, world):
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    world.argvs.clear()
    assert git.sync() == synced(pulled=True)
    assert head_of(repo) == theirs and (repo / "theirs.txt").is_file()
    ran = subcommands(world)
    assert "fetch" in ran and "merge" in ran  # the positive
    assert "push" not in ran


def test_sync_pushes_a_branch_only_it_moved_and_merges_nothing(git, repo, world):
    ours = commit_in(repo, "ours.txt")
    world.argvs.clear()
    assert git.sync() == synced(pushed=True)
    assert on_remote(origin_of(repo), "main") == ours
    ran = subcommands(world)
    assert "fetch" in ran and "push" in ran  # the positive
    assert "merge" not in ran


def test_sync_of_a_branch_that_has_diverged_is_refused_and_changes_nothing(git, repo, world):
    origin = origin_of(repo)
    theirs = commit_on_origin(origin, "main", "theirs.txt")
    ours = commit_in(repo, "ours.txt")
    world.argvs.clear()
    refused(git.sync, "diverged")
    assert head_of(repo) == ours and on_remote(origin, "main") == theirs
    ran = subcommands(world)
    assert "fetch" in ran  # the positive: what came from origin was fetched
    assert "merge" not in ran and "push" not in ran
    assert git.status()["branch"]["ahead"] == 1 and git.status()["branch"]["behind"] == 1


def test_sync_fast_forwards_then_pushes_in_that_order(git, repo, world, tmp_path):
    mirror = pushes_elsewhere(repo, tmp_path / "mirror.git")
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    world.argvs.clear()
    assert git.sync() == synced(pulled=True, pushed=True)
    assert head_of(repo) == theirs and on_remote(mirror, "main") == theirs
    ran = subcommands(world)
    assert ran.index("fetch") < ran.index("merge") < ran.index("push")


def test_where_git_names_no_push_destination_sync_counts_what_is_unpushed_against_the_upstream(
    git, repo, world, tmp_path
):
    """A push remote of its own under push.default simple: git names no @{push}, so what is
    unpushed is what the upstream lacks, as status's push_ahead counts it. A pull leaves
    nothing to push; a commit the upstream lacks is pushed, where git push sends it."""
    mirror = tmp_path / "mirror.git"
    fixture_git(tmp_path, "clone", "-q", "--bare", str(origin_of(repo)), str(mirror))
    fixture_git(repo, "remote", "add", "mirror", str(mirror))
    fixture_git(repo, "fetch", "-q", "mirror")
    fixture_git(repo, "config", "branch.main.pushRemote", "mirror")
    asked = subprocess.run(
        ["git", "rev-parse", "--symbolic-full-name", "@{push}"],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    assert asked.returncode != 0 and b"simple" in asked.stderr  # the positive: no @{push}
    before = on_remote(mirror, "main")
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    world.argvs.clear()
    assert git.sync() == synced(pulled=True)
    assert head_of(repo) == theirs and on_remote(mirror, "main") == before
    assert "merge" in subcommands(world) and "push" not in subcommands(world)
    ours = commit_in(repo, "ours.txt")
    assert git.sync() == synced(pushed=True)
    assert on_remote(mirror, "main") == ours  # git push sends it to the push remote
    assert on_remote(origin_of(repo), "main") == theirs


def test_sync_of_a_branch_level_with_its_upstream_runs_nothing_but_still_prunes(git, repo, world):
    fixture_git(origin_of(repo), "branch", "-D", "feature")
    before = head_of(repo)
    world.argvs.clear()
    assert git.sync() == synced()
    assert head_of(repo) == before
    remote = [b["name"] for b in git.branches()["remote"]["branches"]]
    assert remote == ["origin/main"]  # origin/feature, there before, is pruned
    ran = subcommands(world)
    assert "fetch" in ran and "merge" not in ran and "push" not in ran


def test_sync_publishes_a_branch_with_no_upstream_and_sets_it_to_track(git, repo, world):
    fixture_git(repo, "switch", "-q", "-c", "topic")
    head = commit_in(repo, "topic.txt")
    world.argvs.clear()
    assert git.sync() == synced(
        branch="topic", upstream="origin/topic", pushed=True, upstream_set=True
    )
    assert on_remote(origin_of(repo), "topic") == head
    tracked = fixture_git(repo, "for-each-ref", "--format=%(upstream)", "refs/heads/topic")
    assert tracked.strip() == "refs/remotes/origin/topic"
    published = [a for a in world.argvs if "push" in a]
    assert len(published) == 1 and published[0][-3:] == ["-u", "origin", "HEAD"]
    assert "merge" not in subcommands(world)


def test_sync_of_a_branch_whose_upstream_is_gone_is_refused_and_recreates_nothing(git, repo, world):
    fixture_git(repo, "switch", "-q", "-c", "feature", "--track", "origin/feature")
    commit_in(repo, "feature.txt")
    origin = origin_of(repo)
    fixture_git(origin, "branch", "-D", "feature")
    world.argvs.clear()
    refused(git.sync, "upstream_gone")
    assert "fetch" in subcommands(world)  # the positive: the gone branch was pruned by it
    assert "push" not in subcommands(world)
    assert "feature" not in fixture_git(origin, "branch", "--format=%(refname:short)").split()


def test_sync_is_refused_before_any_fetch_where_a_push_would_be(world, tmp_path):
    repo = make_repo(world.work, origin=tmp_path / "origins" / "repo.git")
    git = world.repository()

    def refused_unfetched(code: str) -> None:
        world.argvs.clear()
        refused(git.sync, code)
        assert subcommands(world), code  # the positive: git was asked something
        assert "fetch" not in subcommands(world), code

    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    refused_unfetched("detached_head")
    fixture_git(repo, "switch", "-q", "--orphan", "trunk")
    fixture_git(repo, "reset", "-q")
    refused_unfetched("no_commits_yet")
    fixture_git(repo, "switch", "-q", "-f", "main")
    fixture_git(repo, "switch", "-q", "-c", "lonely")
    fixture_git(repo, "remote", "rename", "origin", "elsewhere")
    refused_unfetched("no_push_remote")


def test_sync_to_github_is_refused_without_a_token_and_with_one_syncs_with_the_stand_in(
    git, repo, world, github_stand_in
):
    on_github = github_stand_in / "owner" / "name.git"
    fixture_git(world.tmp_path, "clone", "-q", "--bare", str(origin_of(repo)), str(on_github))
    fixture_git(repo, "remote", "set-url", "origin", "https://github.com/owner/name.git")
    world.argvs.clear()
    refused(git.sync, "github_token_not_set")
    assert subcommands(world) and "fetch" not in subcommands(world)
    world.credential(TOKEN)
    theirs = commit_on_origin(on_github, "main", "theirs.txt")
    ours_before = head_of(repo)
    assert git.sync() == synced(pulled=True)
    assert head_of(repo) == theirs != ours_before
    ours = commit_in(repo, "ours.txt")
    assert git.sync() == synced(pushed=True)
    assert on_remote(on_github, "main") == ours
    assert TOKEN not in " ".join(sum(world.argvs, []))


def test_sync_is_refused_where_the_fast_forward_would_overwrite_a_change_not_committed(
    git, repo, world
):
    theirs = commit_on_origin(origin_of(repo), "main", "README.md", "their text\n")
    before = head_of(repo)
    write(repo / "README.md", "my change\n")
    problem = refused(git.sync, "git_failed")
    assert any("would be overwritten" in line for line in problem.extra["git"])
    assert head_of(repo) == before != theirs
    assert (repo / "README.md").read_text() == "my change\n"
    assert "push" not in subcommands(world)


def test_sync_carries_a_change_not_committed_that_the_fast_forward_leaves_alone(git, repo):
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    write(repo / "README.md", "my change\n")
    write(repo / "mine.txt", "untracked\n")
    assert git.sync() == synced(pulled=True)
    assert head_of(repo) == theirs
    assert (repo / "README.md").read_text() == "my change\n"
    assert (repo / "mine.txt").read_text() == "untracked\n"


def test_sync_fast_forwards_whatever_the_persons_pull_and_merge_configuration_says(
    git, repo, world
):
    world.config(AUTHOR + "[pull]\n\trebase = true\n\tff = false\n[merge]\n\tff = false\n")
    fixture_git(repo, "config", "branch.main.mergeOptions", "--no-ff")
    fixture_git(repo, "config", "branch.main.rebase", "true")
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    assert git.sync() == synced(pulled=True)
    assert head_of(repo) == theirs
    parents = fixture_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [fixture_git(repo, "rev-parse", "HEAD^").strip()]  # one parent


def test_sync_from_a_remote_that_needs_a_password_fails_without_asking(
    git, repo, asks_for_a_password
):
    fixture_git(repo, "remote", "set-url", "origin", asks_for_a_password)
    began = time.monotonic()
    problem = refused(git.sync, "git_failed")
    assert any("terminal prompts disabled" in line for line in problem.extra["git"])
    assert time.monotonic() - began < repo_module.GIT_NETWORK_TIMEOUT / 4


def refusing_pushes(bare: Path) -> None:
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho planted refusal of every push >&2\nexit 1\n")
    hook.chmod(0o755)


def test_a_push_refused_after_a_pull_is_the_push_problem_with_what_was_pulled(
    git, repo, world, tmp_path
):
    mirror = pushes_elsewhere(repo, tmp_path / "mirror.git")
    refusing_pushes(mirror)
    before = on_remote(mirror, "main")
    theirs = commit_on_origin(origin_of(repo), "main", "theirs.txt")
    out = git.sync()
    problem = out.pop("push_problem")
    assert out == {k: v for k, v in synced(pulled=True).items() if k != "push_problem"}
    assert (problem["code"], problem["status"]) == ("git_failed", 409)
    assert any("planted refusal of every push" in line for line in problem["git"])
    assert head_of(repo) == theirs and on_remote(mirror, "main") == before
    assert "push" in subcommands(world)  # the positive: the push was tried


def test_a_push_refused_with_nothing_pulled_is_the_syncs_refusal(git, repo):
    origin = origin_of(repo)
    refusing_pushes(origin)
    before = on_remote(origin, "main")
    commit_in(repo, "ours.txt")
    problem = refused(git.sync, "git_failed")
    assert any("planted refusal of every push" in line for line in problem.extra["git"])
    assert on_remote(origin, "main") == before


def test_a_failure_of_the_runtime_in_the_push_half_is_the_push_problem_once_pulled(
    git, repo, world, tmp_path
):
    from rcman.channel import ChannelBroken, ChannelTimeout

    pushes_elsewhere(repo, tmp_path / "mirror.git")
    cases = [
        (rt.RuntimeFailed("boom"), "workspace_failed"),
        (ChannelBroken("gone"), "workspace_failed"),
        (ChannelTimeout(), "timeout"),
        (rt.RuntimeUnavailable("down"), "runtime_unavailable"),
        (Problem("git_timeout"), "git_timeout"),
    ]
    real = world.run_stdin
    for n, (failure, code) in enumerate(cases):
        theirs = commit_on_origin(origin_of(repo), "main", f"theirs{n}.txt")

        def failing(argv, stdin, timeout, failure=failure):
            if "push" in argv:
                raise failure
            return real(argv, stdin, timeout)

        world.run_stdin = failing
        out = world.repository().sync()
        assert head_of(repo) == theirs, code
        assert (out["pulled"], out["pushed"]) == (True, False), code
        assert out["push_problem"]["code"] == code
    world.run_stdin = real
    assert world.repository().sync() == synced(pushed=True)  # positive control: without the fault


def test_a_failure_of_the_runtime_in_the_push_half_with_nothing_pulled_is_not_hidden(
    git, repo, world
):
    commit_in(repo, "ours.txt")
    real = world.run_stdin

    def failing(argv, stdin, timeout):
        if "push" in argv:
            raise rt.RuntimeFailed("boom")
        return real(argv, stdin, timeout)

    world.run_stdin = failing
    with pytest.raises(rt.RuntimeFailed):
        world.repository().sync()


def test_a_programming_error_in_the_push_half_of_a_sync_is_not_hidden(git, repo, world, tmp_path):
    pushes_elsewhere(repo, tmp_path / "mirror.git")
    commit_on_origin(origin_of(repo), "main", "theirs.txt")
    real = world.run_stdin

    def failing(argv, stdin, timeout):
        if "push" in argv:
            raise KeyError("a bug")
        return real(argv, stdin, timeout)

    world.run_stdin = failing
    with pytest.raises(KeyError):
        world.repository().sync()


def test_sync_of_a_branch_tracking_a_remote_that_is_not_origin(world, tmp_path):
    lonely = make_repo(world.work)
    elsewhere = tmp_path / "elsewhere.git"
    fixture_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(elsewhere))
    fixture_git(lonely, "remote", "add", "elsewhere", str(elsewhere))
    fixture_git(lonely, "push", "-q", "-u", "elsewhere", "main")
    git = world.repository()
    theirs = commit_on_origin(elsewhere, "main", "theirs.txt")
    assert git.sync() == synced(upstream="elsewhere/main", pulled=True)
    assert head_of(lonely) == theirs
    ours = commit_in(lonely, "ours.txt")
    assert git.sync() == synced(upstream="elsewhere/main", pushed=True)
    assert on_remote(elsewhere, "main") == ours
    fixture_git(lonely, "switch", "-q", "-c", "topic")
    world.argvs.clear()
    refused(git.sync, "no_push_remote")  # no upstream, and no origin to publish it to
    assert "fetch" not in subcommands(world)


# --- how git is run ------------------------------------------------------------------


def test_git_runs_with_the_allowlisted_environment(git, repo, world):
    dumped = world.tmp_path / "env"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nenv > {dumped}\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    git.stage(["a.txt"])
    git.commit("m")
    seen = dict(line.split("=", 1) for line in dumped.read_text().splitlines() if "=" in line)
    probe = world.run([*world.layout.tool("env")], 10)
    assert b"RCMAN_DECOY=present" in probe.out  # positive control: the runner had it
    assert "RCMAN_DECOY" not in seen
    assert seen["HOME"] == world.layout.home and seen["LC_ALL"] == "C"
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and seen["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["GIT_CEILING_DIRECTORIES"] == str(world.work.parent)
    assert seen["PATH"].endswith(world.layout.path)  # git puts its own programs first
    allowed = {"HOME", "PATH", "LC_ALL", "PWD", "OLDPWD", "SHLVL", "_"}
    assert {k for k in seen if not k.startswith("GIT_")} <= allowed


def test_git_runs_in_the_workspaces_folder_and_wrapped_in_a_timeout(git, repo, world):
    git.status()
    ran = [a for a in world.argvs if "status" in a]
    assert ran  # positive control
    for argv in world.argvs:
        text = " ".join(argv)
        assert " git -C " + world.layout.work in text, argv
        assert "-s TERM -k" in text, argv


def test_a_git_past_its_deadline_is_git_timeout(git, repo, monkeypatch):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 30\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    git.stage(["a.txt"])
    monkeypatch.setattr(repo_module, "GIT_TIMEOUT", 1.0)
    monkeypatch.setattr(repo_module, "KILL_GRACE_SECONDS", 1)
    began = time.monotonic()
    refused(lambda: git.commit("m"), "git_timeout")
    assert time.monotonic() - began < 15
    assert fixture_git(repo, "log", "--format=%s") == "First commit\n"  # nothing committed


def test_a_fetch_and_a_push_have_the_network_deadline_read_when_called(repo, world, monkeypatch):
    seen = []
    real_run, real_stdin = world.run, world.run_stdin

    def run(argv, timeout):
        seen.append(("channel", argv, timeout))
        return real_run(argv, timeout)

    def run_stdin(argv, stdin, timeout):
        seen.append(("exec", argv, timeout))
        return real_stdin(argv, stdin, timeout)

    world.run, world.run_stdin = run, run_stdin
    monkeypatch.setattr(repo_module, "GIT_TIMEOUT", 7.0)
    monkeypatch.setattr(repo_module, "GIT_NETWORK_TIMEOUT", 70.0)
    git = world.repository()
    git.status()
    local = max(t for _, _, t in seen)
    assert local < 7 + 30  # positive control: a local verb waits for its own deadline
    seen.clear()
    git.fetch()
    commit_in(repo, "more.txt")
    git.push()
    commit_in(repo, "again.txt")
    git.sync()
    network = [(how, argv, t) for how, argv, t in seen if "fetch" in argv or "push" in argv]
    assert len(network) == 4 and all(t > local for _, _, t in network)
    assert all(t < 70 + 30 for _, _, t in network)


def test_a_fetch_and_a_push_do_not_hold_the_channel_the_workspace_is_reached_through(
    git, repo, world
):
    on_channel = []
    real = world.run

    def run(argv, timeout):
        on_channel.append(argv)
        return real(argv, timeout)

    world.run = run
    git = world.repository()
    git.status()
    assert on_channel  # positive control: reads go through the channel
    on_channel.clear()
    commit_in(repo, "more.txt")
    git.fetch()
    git.push()
    commit_in(repo, "again.txt")
    git.sync()
    assert not [a for a in on_channel if "fetch" in a or "push" in a]
    assert any("rev-parse" in a for a in on_channel)  # its quick reads still do


def test_a_deadline_is_shared_by_every_git_call_of_a_verb(git, repo, monkeypatch, world):
    seen = []
    real = world.run

    def slow(argv, timeout):
        seen.append(timeout)
        time.sleep(0.6)
        return real(argv, timeout)

    world.run = slow
    monkeypatch.setattr(repo_module, "GIT_TIMEOUT", 2.0)
    git = world.repository()
    refused(git.status, "git_timeout")  # more calls than the deadline has time for
    assert len(seen) >= 2
    assert seen == sorted(seen, reverse=True)  # each has less time than the one before


def test_every_git_call_of_a_sync_shares_the_one_network_deadline(git, repo, monkeypatch, world):
    seen = []
    real = world.run

    def slow(argv, timeout):
        seen.append(timeout)
        time.sleep(0.6)
        return real(argv, timeout)

    world.run = slow
    commit_in(repo, "more.txt")
    monkeypatch.setattr(repo_module, "GIT_NETWORK_TIMEOUT", 2.0)
    refused(world.repository().sync, "git_timeout")  # more calls than the deadline has time for
    assert len(seen) >= 2
    assert seen == sorted(seen, reverse=True)  # each has less time than the one before
    assert on_remote(origin_of(repo), "main") != head_of(repo)  # it stopped before the push


def test_no_git_in_the_workspace_is_git_not_installed(world, repo, tmp_path):
    import dataclasses

    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    layout = dataclasses.replace(world.layout, path=str(nowhere))
    refused(world.repository(layout).status, "git_not_installed")
    world.repository().status()  # positive control: with the path it works


def test_the_version_is_what_git_says(git):
    assert git.version() >= (2, 31)


def test_a_status_record_git_is_not_known_to_write_is_git_unrecognised_output(git, world):
    real = world.run

    def odd(argv, timeout):
        if "status" in argv:
            return rt.Result(0, b"z what is this\0", b"")
        return real(argv, timeout)

    world.run = odd
    problem = refused(world.repository().status, "git_unrecognised_output")
    assert problem.extra["git"] == ["z what is this"]


# --- cloning ---------------------------------------------------------------------------

CLONE_URL = "https://github.com/octo/widget"


@pytest.fixture
def widget(github_stand_in, tmp_path) -> Path:
    """A repository on the tests' stand-in for github.com, at the address CLONE_URL."""
    return make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")


def test_a_clone_is_git_clone_into_the_work_folder_in_a_process_of_its_own(world, widget):
    on_channel = []
    real = world.run

    def run(argv, timeout):
        on_channel.append(argv)
        return real(argv, timeout)

    world.run = run
    git = world.repository()
    assert git.version() >= (2, 31)
    assert on_channel  # positive control: a read goes through the channel
    on_channel.clear()
    world.argvs.clear()
    assert git.clone(CLONE_URL) == {}
    (argv,) = [a for a in world.argvs if "clone" in a]
    start = argv.index("git")
    assert argv[start : start + 3] == ["git", "-C", world.layout.work]
    assert argv[argv.index("clone") :] == ["clone", "--", CLONE_URL, "."]
    ended_by = [*world.layout.tool("timeout"), "-s", "TERM"]
    assert argv[argv.index("-k") - len(ended_by) : argv.index("-k")] == ended_by
    assert not [a for a in on_channel if "clone" in a]  # not the channel everything waits for
    assert (world.work / "README.md").is_file() and (world.work / ".git").is_dir()
    assert fixture_git(world.work, "branch", "-r").split() == [
        "origin/HEAD",
        "->",
        "origin/main",
        "origin/feature",
        "origin/main",
    ]  # every branch of the repository, not the default one alone


def test_a_clone_has_its_own_deadline_read_when_called(world, widget, monkeypatch):
    seen = []
    real = world.run_stdin

    def run_stdin(argv, stdin, timeout):
        seen.append((argv, timeout))
        return real(argv, stdin, timeout)

    world.run_stdin = run_stdin
    monkeypatch.setattr(repo_module, "GIT_NETWORK_TIMEOUT", 11.0)
    monkeypatch.setattr(repo_module, "CLONE_TIMEOUT", 70.0)
    world.repository().clone(CLONE_URL)
    ((argv, wait),) = seen
    seconds = int(argv[argv.index("-k") + 2])
    assert 69 <= seconds <= 70  # CLONE_TIMEOUT, not the network deadline of a fetch
    assert wait > seconds


def test_the_token_is_never_in_the_argv_or_the_stdin_of_a_clone(world, widget):
    world.credential(TOKEN)
    seen = []
    real = world.run_stdin

    def run_stdin(argv, stdin, timeout):
        seen.append((argv, stdin))
        return real(argv, stdin, timeout)

    world.run_stdin = run_stdin
    world.repository().clone(CLONE_URL)
    assert seen
    assert not [1 for argv, stdin in seen if TOKEN in " ".join(argv) or TOKEN.encode() in stdin]


def test_a_clone_git_refuses_keeps_its_exit_code_and_everything_it_said(world):
    from rcman.repository import CloneFailed

    answers = iter([rt.Result(128, b"", b"Cloning into '.'...\nfatal: no such place\n")])
    world.run_stdin = lambda argv, stdin, timeout: next(answers)
    with pytest.raises(CloneFailed) as failed:
        world.repository().clone(CLONE_URL)
    assert failed.value.code == 128
    assert failed.value.err == b"Cloning into '.'...\nfatal: no such place\n"
    assert "128" in str(failed.value)


@pytest.mark.parametrize("code", sorted(rt.TIMED_OUT))
def test_a_clone_the_timeout_tool_ended_is_git_timeout(world, code):
    world.run_stdin = lambda argv, stdin, timeout: rt.Result(code, b"", b"")
    refused(lambda: world.repository().clone(CLONE_URL), "git_timeout")


def test_a_clone_with_no_git_in_the_workspace_is_git_not_installed(world):
    world.run_stdin = lambda argv, stdin, timeout: rt.Result(127, b"", b"git: not found\n")
    refused(lambda: world.repository().clone(CLONE_URL), "git_not_installed")


def test_every_verb_is_driven_here():
    """The verbs the routes call are the ones tested: a new public verb needs a test."""
    verbs = {
        n for n in vars(Repository) if not n.startswith("_") and callable(getattr(Repository, n))
    }
    text = Path(__file__).read_text()
    assert len(verbs) >= 10  # non-vacuity
    assert {v for v in verbs if f".{v}" not in text} == set()
