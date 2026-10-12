"""The PreToolUse hook that commits and pushes the work before a command runs tests.

Every repository it acts in here is a scratch one under ``tmp_path``, with a
local bare repository as its origin, and git runs with the fixtures'
configuration, which refuses every transport but a local repository's, as
``tests/test_helper_env.py`` shows. Each run names its project folder, as
Claude Code's ``CLAUDE_PROJECT_DIR`` does, and the hook acts only in a
checkout of that project's repository. The autouse
``real_repository_untouched`` holds the hook to all of that: the repository
these tests live in has the same HEAD, branch, upstream and index after each
test as before it, and its planted twin shows the same snapshot sees a commit
and a push.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devtools import pre_test_commit
from tests.git_fixtures import FIXTURE_HOME, fixture_env, fixture_git, make_repo, write

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "devtools" / "pre_test_commit.py"

MAKEFILE = (
    "test:\n\tpytest -q\n"
    "check: lint test\n"
    "lint:\n\truff check .\n"
    "setup:\n\tnpx playwright install chromium\n"
)


def snapshot(repo: Path) -> dict[str, str]:
    """What the hook could change in a checkout: HEAD, its branch, that branch's
    upstream and the index. Refs other checkouts share are left out, so that
    another worktree's work does not count against these tests."""

    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return f"{proc.returncode}:{proc.stdout.strip()}"

    return {
        "head": git("rev-parse", "HEAD"),
        "branch": git("symbolic-ref", "-q", "HEAD"),
        "upstream": git("rev-parse", "@{upstream}"),
        "index": git("ls-files", "--stage"),
    }


@pytest.fixture(autouse=True)
def real_repository_untouched():
    before = snapshot(REPO)
    yield
    assert snapshot(REPO) == before, "the hook changed the real repository"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return tmp_path / "origin.git"


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    """A checkout on the branch ``work``, pushed to its origin and tracking it,
    with a Makefile whose ``test`` and ``check`` run pytest and whose ``lint``
    and ``setup`` do not."""
    path = make_repo(tmp_path / "repo", origin=origin)
    fixture_git(path, "switch", "-q", "-c", "work")
    write(path / "Makefile", MAKEFILE)
    fixture_git(path, "add", "Makefile")
    fixture_git(path, "commit", "-q", "-m", "Makefile")
    fixture_git(path, "push", "-q", "-u", "origin", "work")
    return path


def env(project: Path | None = None) -> dict[str, str]:
    base = fixture_env(FIXTURE_HOME)
    return base if project is None else {**base, "CLAUDE_PROJECT_DIR": str(project)}


def payload(command: str, cwd: Path) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "cwd": str(cwd),
        "tool_input": {"command": command, "description": "Run the tests"},
    }


def run_hook(command: str, cwd: Path, project: Path | None = None) -> tuple[int, dict | None]:
    """Through the real invocation path: stdin JSON in, exit code and stdout JSON
    out. The project folder is ``project``, or else ``cwd``."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload(command, cwd)),
        env=env(project or cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None)


def head(repo: Path, ref: str = "HEAD") -> str:
    return fixture_git(repo, "rev-parse", ref).strip()


def origin_head(origin: Path, branch: str) -> str:
    return fixture_git(origin, "rev-parse", f"refs/heads/{branch}").strip()


def origin_has(origin: Path, branch: str) -> bool:
    return bool(fixture_git(origin, "branch", "--list", branch).strip())


def dirty(repo: Path) -> bool:
    return bool(fixture_git(repo, "status", "--porcelain").strip())


def index(repo: Path) -> str:
    return fixture_git(repo, "ls-files", "--stage")


def make_changes(repo: Path) -> None:
    write(repo / "README.md", "changed\n")
    write(repo / "new" / "untracked.txt", "untracked\n")


def no_decision(out: dict) -> bool:
    """The hook leaves the call to the normal permission flow: it never decides."""
    return "permissionDecision" not in out.get("hookSpecificOutput", {})


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def recording_popen(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every child process the hook starts, with its arguments, recorded."""
    calls: list[tuple[list[str], dict]] = []
    real_popen = subprocess.Popen

    def popen(argv, *args, **kwargs):
        calls.append((list(argv), kwargs))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(pre_test_commit.subprocess, "Popen", popen)
    return calls


# --- committing and pushing -------------------------------------------------------


def test_a_test_command_commits_everything_and_pushes_it(repo, origin):
    make_changes(repo)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) != before
    assert not dirty(repo)
    assert fixture_git(repo, "show", "HEAD:new/untracked.txt") == "untracked\n"
    assert fixture_git(repo, "show", "HEAD:README.md") == "changed\n"
    assert origin_head(origin, "work") == head(repo)
    assert out is not None
    assert "systemMessage" not in out
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert head(repo)[:12] in context
    assert "pushed" in context
    assert "new/untracked.txt" in context, "the untracked files added are listed"


def test_the_commit_message_is_fixed_and_holds_nothing_of_the_command(repo):
    make_changes(repo)

    run_hook("GITHUB_TOKEN=ghp_planted_secret make test", repo)

    message = fixture_git(repo, "log", "-1", "--format=%B").strip()
    assert message == f"WIP before tests\n\n{pre_test_commit.TRAILER}"
    assert "ghp_planted_secret" not in fixture_git(repo, "log", "--format=%B")


def test_a_branch_ahead_of_origin_is_pushed_without_a_new_commit(repo, origin):
    write(repo / "committed.txt")
    fixture_git(repo, "add", "committed.txt")
    fixture_git(repo, "commit", "-q", "-m", "Local only")
    before = head(repo)

    code, out = run_hook("uv run pytest -q", repo)

    assert code == 0
    assert head(repo) == before
    assert origin_head(origin, "work") == before
    assert "pushed" in out["hookSpecificOutput"]["additionalContext"]


def test_a_branch_with_no_upstream_is_pushed_and_tracks_origin(repo, origin):
    fixture_git(repo, "switch", "-q", "-c", "fresh")

    code, _ = run_hook("make check", repo)

    assert code == 0
    assert origin_head(origin, "fresh") == head(repo)
    assert fixture_git(repo, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == "origin/fresh"


def test_a_clean_checkout_in_step_with_origin_is_left_alone_silently(repo, origin):
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert (code, out) == (0, None)
    assert head(repo) == before
    assert origin_head(origin, "work") == before


def test_an_upstream_on_another_remote_is_kept_and_origin_still_gets_the_branch(
    tmp_path, repo, origin
):
    fork = tmp_path / "fork.git"
    fixture_git(tmp_path, "init", "-q", "--bare", str(fork))
    fixture_git(repo, "remote", "add", "fork", str(fork))
    fixture_git(repo, "push", "-q", "-u", "fork", "work")
    make_changes(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert not dirty(repo)
    assert origin_head(origin, "work") == head(repo)
    assert fixture_git(repo, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == "fork/work"
    assert "pushed" in out["hookSpecificOutput"]["additionalContext"]

    assert run_hook("make test", repo) == (0, None), "origin/work is now in step"


def test_an_upstream_deleted_on_origin_is_reported_and_not_pushed_again(repo, origin):
    """A branch merged and deleted on origin is not brought back by the hook."""
    fixture_git(repo, "push", "-q", "origin", "--delete", "work")
    make_changes(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert not dirty(repo), "the commit is still made"
    assert not origin_has(origin, "work")
    assert "gone" in out["systemMessage"]
    assert "origin/work" in out["hookSpecificOutput"]["additionalContext"]
    assert no_decision(out)


def test_the_snapshot_sees_a_commit_and_a_push(repo):
    """The planted twin of ``real_repository_untouched``: the same snapshot,
    taken of a checkout the hook does act in, changes."""
    make_changes(repo)
    before = snapshot(repo)

    run_hook("make test", repo)

    after = snapshot(repo)
    assert after["head"] != before["head"]
    assert after["upstream"] != before["upstream"]


# --- what is a test command -------------------------------------------------------


def test_a_command_that_runs_no_tests_does_nothing(repo, origin):
    make_changes(repo)
    before = head(repo)

    code, out = run_hook("git status && ls -la", repo)

    assert (code, out) == (0, None)
    assert head(repo) == before
    assert dirty(repo)


def test_a_command_that_runs_no_tests_starts_no_process(repo, monkeypatch):
    calls = recording_popen(monkeypatch)
    make_changes(repo)

    assert pre_test_commit.handle(payload("git status && ls", repo), env(repo)) is None
    assert calls == []

    pre_test_commit.handle(payload("make test", repo), env(repo))
    assert any(argv[0] == "git" for argv, _ in calls), "the positive control ran git"


@pytest.mark.parametrize(
    "command",
    [
        "make test",
        "make check",
        "make",
        "make -s test",
        "make -j 4 test",
        "make -j test",
        "make V=1 test",
        "timeout 600 make check",
        "pytest",
        "pytest tests/test_x.py -q 2>&1 | tail -20",
        "uv run pytest -n auto",
        "uv run --quiet --with pytest-xdist pytest",
        "FOO=1 uv run pytest",
        "env FOO=1 pytest -x",
        "python -m pytest",
        "python3 -m pytest -q",
        "uv run python -m pytest",
        ".venv/bin/pytest",
        "npx vitest run",
        "npx playwright test",
        "vitest",
        "git status; make test",
        "git status\nmake test",
        "ls || pytest",
        "! pytest",
        "if make test; then echo ok; else echo bad; fi",
        "for f in a b; do pytest $f; done",
        "while true; do pytest; done",
        "(cd a);pytest",
        "(cd a)&& pytest",
        "(cd a && ls)|| pytest",
    ],
)
def test_test_commands_are_recognised(repo, command):
    assert pre_test_commit.test_directory(command, repo) == repo


@pytest.mark.parametrize(
    "command",
    [
        "make lint",
        "make setup",
        "make no-such-target",
        "echo make test",
        "grep -r pytest .",
        "cat Makefile | grep test",
        "npx playwright install chromium",
        "uv sync",
        "git commit -m 'make test'",
        "ls # && make test",
        "echo 'unbalanced",
        "pytest --version",
        "uv run pytest --collect-only -q",
        "pytest --co",
        "pytest -h",
        "npx vitest --help",
        "npx playwright test --help",
        "",
    ],
)
def test_other_commands_are_not(repo, command):
    assert pre_test_commit.test_directory(command, repo) is None


def test_an_npm_script_counts_by_what_it_runs(tmp_path):
    write(
        tmp_path / "package.json",
        json.dumps(
            {"scripts": {"test": "vitest run", "e2e": "playwright test", "lint": "biome ci"}}
        ),
    )
    for command in ("npm test", "npm run e2e", "npm run test -- --reporter dot"):
        assert pre_test_commit.test_directory(command, tmp_path) == tmp_path, command
    for command in ("npm run lint", "npm ci", "npm run missing"):
        assert pre_test_commit.test_directory(command, tmp_path) is None, command


# --- reading a Makefile -------------------------------------------------------------


def test_a_makefile_is_read_without_running_make(tmp_path, monkeypatch):
    """A recipe is read, never run: no process starts, and a recipe that would
    leave a mark leaves none."""
    calls = recording_popen(monkeypatch)
    write(tmp_path / "Makefile", f"test:\n\ttouch {tmp_path / 'ran'}\n\tpytest\n")

    assert pre_test_commit.test_directory("make test", tmp_path) == tmp_path
    assert calls == []
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    ("makefile", "command"),
    [
        ("all: build\nbuild: unit\nunit:\n\tpytest\n", "make all"),
        ("all: build\nbuild: unit\nunit:\n\tpytest\n", "make"),
        (".PHONY: all\nall: unit\nunit:\n\t@uv run pytest\n", "make"),
        ("unit:\n\t-cd sub && \\\n\t  pytest -q\n", "make unit"),
        ("one two: three\nthree:\n\t+pytest\n", "make two"),
        ("check: export X = 1\ncheck: unit ## comment\nunit:\n\tpytest\n", "make check"),
        ("unit: ; pytest\n", "make unit"),
        ("all:\n\t$(MAKE) unit\nunit:\n\tpytest\n", "make all"),
    ],
)
def test_a_target_counts_by_its_recipe_and_its_prerequisites(tmp_path, makefile, command):
    write(tmp_path / "Makefile", makefile)
    assert pre_test_commit.test_directory(command, tmp_path) is not None


@pytest.mark.parametrize(
    ("makefile", "command"),
    [
        ("a: b\nb: a\n\techo loop\n", "make a"),
        ("check: export X = 1\nX:\n\tpytest\ncheck:\n\truff check .\n", "make check"),
        ("define RECIPE\nlint:\n\tpytest\nendef\nlint:\n\truff check .\n", "make lint"),
        ("lint:\n\truff check .\n# lint: unit\nunit:\n\tpytest\n", "make lint"),
    ],
)
def test_a_target_whose_recipe_runs_no_tests_does_not_count(tmp_path, makefile, command):
    write(tmp_path / "Makefile", makefile)
    assert pre_test_commit.test_directory(command, tmp_path) is None


def test_no_makefile_runs_no_tests(tmp_path):
    assert pre_test_commit.test_directory("make test", tmp_path) is None


def test_make_reads_the_makefile_its_file_option_names(tmp_path):
    write(tmp_path / "Makefile", "test:\n\truff check .\n")
    write(tmp_path / "other.mk", "test:\n\tpytest\n")
    assert pre_test_commit.test_directory("make -f other.mk test", tmp_path) == tmp_path
    assert pre_test_commit.test_directory("make test", tmp_path) is None


@pytest.mark.parametrize(
    "command",
    ["make -C sub test", "make -j -C sub test", "make --directory=sub test", "make test -Csub"],
)
def test_make_runs_where_its_directory_option_says(repo, command):
    write(repo / "sub" / "Makefile", MAKEFILE)
    assert pre_test_commit.test_directory(command, repo) == repo / "sub"


@pytest.mark.parametrize(
    ("target", "runs_tests"),
    [
        ("test", True),
        ("test-container", True),
        ("check", True),
        ("e2e", True),
        ("frontend-test", True),
        ("lint", False),
        ("format", False),
        ("setup", False),
        ("frontend-build", False),
    ],
)
def test_this_repositorys_make_targets_count_by_their_recipes(target, runs_tests):
    found = pre_test_commit.test_directory(f"make {target}", REPO)
    assert (found is not None) == runs_tests


def test_this_repositorys_frontend_scripts_count_by_what_they_run():
    frontend = REPO / "frontend"
    assert pre_test_commit.test_directory("npm test", frontend) == frontend
    assert pre_test_commit.test_directory("npm run e2e", frontend) == frontend
    assert pre_test_commit.test_directory("npm run lint", frontend) is None


# --- where it acts ----------------------------------------------------------------


@pytest.mark.parametrize("joiner", [" && ", "; ", ";"])
def test_a_leading_cd_names_the_checkout(tmp_path, repo, joiner):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_changes(repo)

    code, _ = run_hook(f"cd {repo}{joiner}make test", elsewhere, project=repo)

    assert code == 0
    assert not dirty(repo)


def test_a_relative_cd_is_taken_from_the_working_directory(tmp_path, repo):
    make_changes(repo)

    code, _ = run_hook(f"cd {repo.name} && make test", tmp_path, project=repo)

    assert code == 0
    assert not dirty(repo)


@pytest.mark.parametrize(
    "command",
    [
        "git status && cd sub && pytest",
        "(cd sub && pytest)",
        "pushd sub && pytest",
        "cd $(git rev-parse --show-toplevel)/sub && pytest",
        "cd `pwd`/sub && pytest",
    ],
)
def test_only_a_leading_cd_outside_a_subshell_is_followed(repo, command):
    (repo / "sub").mkdir()
    assert pre_test_commit.test_directory("cd sub && pytest", repo) == repo / "sub"
    assert pre_test_commit.test_directory(command, repo) == repo


def test_a_subfolder_commits_the_whole_checkout(repo):
    make_changes(repo)
    write(repo / "sub" / "package.json", json.dumps({"scripts": {"test": "vitest run"}}))

    code, _ = run_hook("cd sub && npm test", repo)

    assert code == 0
    assert not dirty(repo)
    assert fixture_git(repo, "show", "HEAD:new/untracked.txt") == "untracked\n"


def test_a_worktree_of_the_project_commits_and_pushes_its_own_branch(tmp_path, repo, origin):
    worktree = tmp_path / "worktree"
    fixture_git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    make_changes(worktree)
    make_changes(repo)
    before = head(repo)

    code, _ = run_hook("make test", worktree, project=repo)

    assert code == 0
    assert not dirty(worktree)
    assert origin_head(origin, "side") == head(worktree)
    assert head(repo) == before
    assert dirty(repo)


def unrelated(tmp_path: Path) -> Path:
    other = make_repo(tmp_path / "other", origin=tmp_path / "other.git")
    fixture_git(other, "switch", "-q", "-c", "elsewhere")
    write(other / "Makefile", MAKEFILE)
    make_changes(other)
    return other


def test_a_repository_other_than_the_projects_is_left_untouched(tmp_path, repo):
    other = unrelated(tmp_path)
    before = head(other)

    code, out = run_hook(f"cd {other} && make test", repo, project=repo)

    assert (code, out) == (0, None)
    assert head(other) == before
    assert dirty(other)


def test_the_same_repository_as_the_project_is_committed(tmp_path, repo):
    """The positive control of the test above: the same command, with that
    repository as the project, commits."""
    other = unrelated(tmp_path)
    before = head(other)

    code, _ = run_hook(f"cd {other} && make test", repo, project=other)

    assert code == 0
    assert head(other) != before
    assert not dirty(other)


def test_without_a_project_folder_nothing_is_done(repo):
    make_changes(repo)
    before = head(repo)

    assert pre_test_commit.handle(payload("make test", repo), env()) is None
    assert head(repo) == before
    assert pre_test_commit.handle(payload("make test", repo), env(repo)) is not None


# --- where it does not commit -----------------------------------------------------


def test_main_is_not_committed_and_says_so(repo):
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "Makefile", MAKEFILE)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert dirty(repo)
    assert "main" in out["systemMessage"]
    assert "main" in out["hookSpecificOutput"]["additionalContext"]


def test_origins_default_branch_is_not_committed_and_says_so(repo):
    fixture_git(repo, "switch", "-q", "-c", "trunk")
    fixture_git(repo, "push", "-q", "-u", "origin", "trunk")
    fixture_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    make_changes(repo)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert dirty(repo)
    assert "trunk" in out["systemMessage"]


def test_a_detached_head_is_not_committed_and_says_so(repo):
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    make_changes(repo)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert dirty(repo)
    assert "detached" in out["systemMessage"]


def git_stops(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=repo, env=env(), capture_output=True, text=True)
    assert proc.returncode != 0, f"git {args} was meant to stop on a conflict"


def commit_clash(repo: Path, text: str, message: str) -> None:
    write(repo / "clash.txt", text)
    fixture_git(repo, "add", "clash.txt")
    fixture_git(repo, "commit", "-q", "-m", message)


def start_conflict(repo: Path, operation: str) -> None:
    """Leave ``repo`` stopped on a conflict, part-way through ``operation``."""
    commit_clash(repo, "ours\n", "Ours")
    fixture_git(repo, "switch", "-q", "-c", "theirs", "HEAD~1")
    commit_clash(repo, "theirs\n", "Theirs")
    fixture_git(repo, "switch", "-q", "work")
    if operation == "revert":
        commit_clash(repo, "ours again\n", "Ours again")
        git_stops(repo, "revert", "--no-edit", "HEAD~1")
    else:
        git_stops(repo, operation, "theirs")


@pytest.mark.parametrize("operation", ["merge", "rebase", "cherry-pick", "revert"])
def test_an_operation_in_progress_is_not_committed_and_says_so(repo, operation):
    start_conflict(repo, operation)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert operation in out["systemMessage"]


# --- what it will not add ------------------------------------------------------------


def big_file(repo: Path) -> str:
    with open(repo / "big.bin", "wb") as f:
        f.truncate(pre_test_commit.MAX_FILE_BYTES + 1)
    return "big.bin"


def nested_repository(repo: Path) -> str:
    make_repo(repo / "nested")
    return "nested/"


def credential(name: str):
    def plant(repo: Path) -> str:
        write(repo / "conf" / name, "secret\n")
        return f"conf/{name}"

    plant.__name__ = name
    return plant


@pytest.mark.parametrize(
    "plant",
    [
        big_file,
        nested_repository,
        *(
            credential(name)
            for name in (
                ".env",
                ".env.local",
                "server.pem",
                "deploy.KEY",
                "id_rsa",
                "id_rsa.pub",
                ".netrc",
                "cert.p12",
                "cert.pfx",
            )
        ),
    ],
    ids=lambda plant: plant.__name__,
)
def test_an_untracked_file_it_should_not_add_is_left_out_and_the_rest_committed(
    repo, origin, plant
):
    make_changes(repo)
    fixture_git(repo, "add", "README.md")
    path = plant(repo)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) != before
    assert fixture_git(repo, "show", "HEAD:README.md") == "changed\n"
    assert fixture_git(repo, "show", "HEAD:new/untracked.txt") == "untracked\n"
    assert path.rstrip("/") not in fixture_git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    status = fixture_git(repo, "status", "--porcelain", "--untracked-files=all").strip()
    left = status.removeprefix("?? ")
    assert left == path or (path.endswith("/") and left.startswith(path)), status
    assert origin_head(origin, "work") == head(repo), "the push still happens"
    assert path in out["hookSpecificOutput"]["additionalContext"]
    assert "Left out" in out["systemMessage"]
    assert no_decision(out)


@pytest.mark.parametrize(
    ("path", "named"),
    [
        ("conf/.env", True),
        ("deep/er/Server.PEM", True),
        ("id_rsa.pub", True),
        ("id_rsa/notes.txt", False),  # a folder's name is not the file's
        ("environment.txt", False),
    ],
)
def test_named_like_a_credential_reads_the_file_name_in_any_case(path, named):
    """The judgement tests/snapshot.py shares with the hook."""
    assert pre_test_commit.named_like_a_credential(path) is named


def test_only_a_refused_file_commits_nothing_and_says_so(repo, origin):
    before = head(repo)
    write(repo / ".env", "secret\n")

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert origin_head(origin, "work") == before
    assert ".env" in out["hookSpecificOutput"]["additionalContext"]
    assert no_decision(out)


@pytest.mark.parametrize("name", ["github_token.py", "client_secret.json", "api_tokens.txt"])
def test_a_source_file_named_for_tokens_or_secrets_is_committed(repo, name):
    write(repo / "src" / name, "code\n")

    code, out = run_hook("make test", repo)

    assert code == 0
    assert not dirty(repo)
    assert fixture_git(repo, "show", f"HEAD:src/{name}") == "code\n"
    assert "systemMessage" not in out


def test_a_file_just_under_the_size_cap_is_added(repo):
    """The positive control of the size cap."""
    with open(repo / "big.bin", "wb") as f:
        f.truncate(pre_test_commit.MAX_FILE_BYTES)

    run_hook("make test", repo)

    assert not dirty(repo)


# --- failures are reported, and the tests still run -------------------------------


def test_a_push_failure_is_reported_and_the_tests_are_not_blocked(tmp_path, repo):
    fixture_git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    make_changes(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert not dirty(repo), "the commit is made even when the push fails"
    assert "push" in out["systemMessage"].lower()
    assert "missing.git" in out["hookSpecificOutput"]["additionalContext"]
    assert no_decision(out)


def test_a_checkout_with_no_origin_is_committed_and_reported(repo):
    fixture_git(repo, "remote", "remove", "origin")
    make_changes(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert not dirty(repo)
    assert "origin" in out["systemMessage"]
    assert no_decision(out)


def test_a_commit_refused_by_the_repositorys_own_hook_leaves_the_index_as_it_was(repo, origin):
    hook = repo / ".git" / "hooks" / "pre-commit"
    write(hook, "#!/bin/sh\necho planted refusal >&2\nexit 1\n")
    hook.chmod(0o755)
    make_changes(repo)
    fixture_git(repo, "add", "README.md")
    staged = index(repo)
    before = head(repo)

    code, out = run_hook("make test", repo)

    assert code == 0
    assert head(repo) == before
    assert origin_head(origin, "work") == before
    assert index(repo) == staged
    assert "commit" in out["systemMessage"].lower()
    assert "planted refusal" in out["hookSpecificOutput"]["additionalContext"]
    assert no_decision(out)


def hold(path: Path, release: Path, done: Path) -> None:
    """A git hook at ``path`` that waits until ``release`` exists, then marks
    ``done`` and refuses."""
    write(
        path,
        f"#!/bin/sh\nwhile [ ! -e '{release}' ]; do sleep 0.05; done\ntouch '{done}'\nexit 1\n",
    )
    path.chmod(0o755)


def test_a_push_that_overruns_the_deadline_is_reported(tmp_path, repo, origin):
    release, done = tmp_path / "release", tmp_path / "done"
    hold(origin / "hooks" / "pre-receive", release, done)
    make_changes(repo)

    try:
        out = pre_test_commit.handle(payload("make test", repo), env(repo), deadline=3)
    finally:
        release.touch()
        wait_for(done)

    assert not dirty(repo), "the commit is made before the push"
    assert "timed out" in out["systemMessage"]
    assert "stays" in out["hookSpecificOutput"]["additionalContext"]
    assert no_decision(out)


def test_a_commit_that_overruns_the_deadline_leaves_the_index_as_it_was(tmp_path, repo):
    release, done = tmp_path / "release", tmp_path / "done"
    hold(repo / ".git" / "hooks" / "pre-commit", release, done)
    make_changes(repo)
    fixture_git(repo, "add", "README.md")
    staged = index(repo)
    before = head(repo)

    try:
        out = pre_test_commit.handle(payload("make test", repo), env(repo), deadline=3)
    finally:
        release.touch()
        wait_for(done)

    assert head(repo) == before
    assert index(repo) == staged
    assert not (repo / ".git" / "index.lock").exists()
    assert "timed out" in out["systemMessage"]
    assert no_decision(out)


def test_ssh_is_told_never_to_prompt(repo, monkeypatch):
    calls = recording_popen(monkeypatch)
    write(repo / "change.txt")

    pre_test_commit.handle(payload("make test", repo), env(repo))

    (push,) = [kwargs for argv, kwargs in calls if argv[:2] == ["git", "push"]]
    assert push["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


@pytest.mark.parametrize(
    "stdin",
    ["", "not json", "null", json.dumps({"tool_input": {}}), json.dumps({"tool_input": None})],
)
def test_a_payload_it_cannot_read_does_not_block(stdin):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=stdin, env=env(), capture_output=True, text=True
    )
    assert proc.returncode == 0
    if proc.stdout.strip():
        assert no_decision(json.loads(proc.stdout))


def test_a_command_in_a_folder_that_is_no_checkout_does_nothing(tmp_path):
    write(tmp_path / "Makefile", MAKEFILE)

    assert run_hook("make test", tmp_path) == (0, None)


# --- wiring -----------------------------------------------------------------------


def wired_hooks() -> list[tuple[str, dict]]:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return [
        (entry["matcher"], hook)
        for entry in settings["hooks"].get("PreToolUse", [])
        for hook in entry["hooks"]
        if "devtools/pre_test_commit.py" in hook["command"]
    ]


def test_settings_route_bash_commands_to_the_hook():
    """What is enforced is what the wiring hands over, not what the script can do."""
    wired = wired_hooks()
    assert wired, "no PreToolUse hook runs devtools/pre_test_commit.py"
    assert all(matcher == "Bash" for matcher, _ in wired)
    assert all(hook.get("timeout") for _, hook in wired), "the hook has no timeout"
    assert all(hook["timeout"] > pre_test_commit.LONGEST_RUN for _, hook in wired), (
        "the hook can run longer than the timeout the wiring gives it"
    )


def run_wired(project: Path, cwd: Path) -> subprocess.CompletedProcess:
    ((_, hook),) = wired_hooks()
    return subprocess.run(
        ["sh", "-c", hook["command"]],
        input=json.dumps(payload("make test", cwd)),
        env=env(project),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_wired_command_without_the_script_does_nothing_and_does_not_block(tmp_path, repo):
    """A project folder without the script, as a checkout of another branch
    has: the command exits 0 and says nothing. The test below is its positive
    control: the same command, with the script there, acts."""
    empty = tmp_path / "empty"
    empty.mkdir()
    make_changes(repo)

    proc = run_wired(empty, repo)

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert dirty(repo)


def test_the_wired_command_commits_before_tests(repo):
    """The command exactly as the settings give it, run by a shell as Claude Code
    runs it. The project folder is the scratch checkout, holding a copy of the
    script where the wiring looks for it."""
    shutil.copy(SCRIPT, write(repo / "devtools" / "pre_test_commit.py", ""))
    make_changes(repo)

    proc = run_wired(repo, repo)

    assert proc.returncode == 0, proc.stderr
    assert not dirty(repo)
    assert "pushed" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.local.json",
        ".claude/worktrees/agent/README.md",
        ".venv/bin/python",
        "frontend/node_modules/x/index.js",
        "frontend/dist/index.html",
        "frontend/test-results/run.json",
        "frontend/playwright-report/index.html",
    ],
)
def test_what_git_add_must_not_sweep_in_is_ignored(path):
    """What ``git add -A`` would otherwise commit before tests: the local
    settings, Claude Code's worktrees, environments, build and test output."""
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
    assert ignored.returncode == 0, f"{path} is not ignored"


def test_a_path_that_is_not_ignored_is_seen_as_such():
    """The positive control of the test above."""
    proc = subprocess.run(["git", "check-ignore", "-q", "devtools/pre_test_commit.py"], cwd=REPO)
    assert proc.returncode == 1
