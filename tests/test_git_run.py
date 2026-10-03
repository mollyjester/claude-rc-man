"""helpers/git_run.py: starting git, ending it, and reading what it writes, against real git."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from helpers import git_run as g
from tests.git_fixtures import (
    add_submodule,
    fixture_git,
    make_repo,
    push_branch_to_origin,
    write,
)

ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "A",
    "GIT_AUTHOR_EMAIL": "a@example.com",
    "GIT_COMMITTER_NAME": "A",
    "GIT_COMMITTER_EMAIL": "a@example.com",
}


def soon(seconds: float = 20.0) -> float:
    return time.monotonic() + seconds


def status_records(repo: Path) -> list[bytes]:
    args = ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal"]
    return list(g.Git(ENV).stream(repo, args, sep=b"\0", deadline=soon()))


def parsed(repo: Path) -> tuple[g.Branch, list[g.Change]]:
    branch = g.Branch()
    return branch, list(g.parse_status(status_records(repo), branch))


def hook(repo: Path, name: str, body: str) -> Path:
    path = repo / ".git" / "hooks" / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


# --- what git writes ------------------------------------------------------------


def every_state(tmp_path) -> list[Path]:
    """Repositories that between them make git write every kind of record."""
    fresh = make_repo(tmp_path / "unborn-parent") / "unborn"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    write(fresh / "new.txt")
    fixture_git(fresh, "add", "new.txt")

    origin = tmp_path / "origins" / "o.git"
    repo = make_repo(tmp_path / "repo", origin=origin)
    fixture_git(repo, "config", "status.renames", "copies")
    fixture_git(repo, "config", "diff.renames", "copies")
    write(repo / "modify.txt")
    write(repo / "delete.txt")
    write(repo / "type.txt")
    write(repo / "rename.txt", g_body("rename"))
    write(repo / "copy.txt", g_body("copy"))
    fixture_git(repo, "add", ".")
    fixture_git(repo, "commit", "-q", "-m", "files")
    write(repo / "local.txt")
    fixture_git(repo, "add", "local.txt")
    fixture_git(repo, "commit", "-q", "-m", "local")  # two ahead of origin
    write(repo / "modify.txt", "changed\n")  # modified, not staged
    (repo / "delete.txt").unlink()  # deleted, not staged
    (repo / "type.txt").unlink()
    (repo / "type.txt").symlink_to("modify.txt")  # type changed
    fixture_git(repo, "mv", "rename.txt", "renamed.txt")  # renamed, staged
    write(repo / "copied.txt", g_body("copy"))
    write(repo / "copy.txt", g_body("copy") + "a source must change to be seen as one\n")
    fixture_git(repo, "add", "copied.txt", "copy.txt")  # copied, staged
    write(repo / "added.txt")
    fixture_git(repo, "add", "added.txt")  # added
    write(repo / "untracked.txt")
    sub_source = make_repo(tmp_path / "sub-source")
    add_submodule(repo, sub_source, "sub")

    conflict = make_repo(tmp_path / "conflict")
    fixture_git(conflict, "switch", "-q", "-c", "other")
    write(conflict / "README.md", "theirs\n")
    fixture_git(conflict, "commit", "-q", "-am", "theirs")
    fixture_git(conflict, "switch", "-q", "main")
    write(conflict / "README.md", "ours\n")
    fixture_git(conflict, "commit", "-q", "-am", "ours")
    subprocess.run(
        ["git", "merge", "-q", "other"], cwd=conflict, env=fixture_git_env(), capture_output=True
    )

    detached = make_repo(tmp_path / "detached")
    fixture_git(detached, "switch", "-q", "--detach", "HEAD")
    return [fresh, repo, conflict, detached]


def fixture_git_env():
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    return fixture_env(FIXTURE_HOME)


def g_body(seed: str) -> str:
    return "".join(f"{seed} line {i} long enough for git to find it again\n" for i in range(30))


def test_every_record_kind_real_git_writes_is_read(tmp_path):
    repos = every_state(tmp_path)
    kinds, changes, branches = set(), [], []
    for repo in repos:
        records = status_records(repo)
        kinds |= {r[:1] for r in records if r}
        branch = g.Branch()
        changes += list(g.parse_status(iter(records), branch))
        branches.append(branch)
    # every kind is produced at least once, so the parser is driven over each
    assert {b"#", b"1", b"2", b"u", b"?"} <= kinds
    assert {c.change for c in changes} == set(g.CHANGES)
    fresh, repo, _conflict, detached = branches
    assert (fresh.name, fresh.commit) == ("main", None)
    assert repo.name == "main" and repo.upstream == "origin/main"
    assert (repo.ahead, repo.behind) == (2, 0)
    assert len(repo.commit) >= 40
    assert detached.name is None and detached.commit
    renamed = next(c for c in changes if c.change == "renamed")
    assert (renamed.path, renamed.origin, renamed.staged) == (b"renamed.txt", b"rename.txt", True)
    copied = next(c for c in changes if c.change == "copied")
    assert (copied.path, copied.origin) == (b"copied.txt", b"copy.txt")
    assert next(c for c in changes if c.change == "type_changed").path == b"type.txt"
    assert any(c.submodule for c in changes)
    assert not any(c.submodule for c in changes if c.path != b"sub")
    unmerged = next(c for c in changes if c.change == "unmerged")
    assert (unmerged.path, unmerged.staged) == (b"README.md", False)


def test_a_file_changed_in_both_places_is_in_both_lists(tmp_path):
    repo = make_repo(tmp_path / "r")
    write(repo / "README.md", "staged\n")
    fixture_git(repo, "add", "README.md")
    write(repo / "README.md", "and again\n")
    _, changes = parsed(repo)
    assert [(c.path, c.change, c.staged) for c in changes] == [
        (b"README.md", "modified", True),
        (b"README.md", "modified", False),
    ]


def test_names_with_spaces_and_newlines_are_read_whole(tmp_path):
    repo = make_repo(tmp_path / "r")
    write(repo / "a b\nc.txt")
    _, changes = parsed(repo)
    assert [c.path for c in changes] == [b"a b\nc.txt"]


@pytest.mark.parametrize(
    "records",
    [
        [b"# branch.oid (initial)", b"z unknown record"],
        [b"1 Z. N... 100644 100644 100644 0 0 x"],
        [b"1 .Q N... 100644 100644 100644 0 0 x"],
        [b"1 M. N..."],
        [b"2 R. N... 100644 100644 100644 0 0 R100 new"],  # no origin record follows
    ],
)
def test_a_record_git_is_not_known_to_write_is_refused(records):
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_status(iter(records), g.Branch()))


def test_the_refusal_check_reads_a_known_record():
    """Positive control for the plant above: the same record with a known letter is read."""
    [change] = g.parse_status(iter([b"1 M. N... 100644 100644 100644 0 0 x"]), g.Branch())
    assert (change.path, change.change, change.staged) == (b"x", "modified", True)


def test_an_unknown_header_is_left_alone():
    branch = g.Branch()
    assert list(g.parse_status(iter([b"# branch.future x", b"# stash 1"]), branch)) == []


def test_branches_are_read_and_symbolic_refs_left_out(tmp_path):
    origin = tmp_path / "origins" / "o.git"
    repo = make_repo(tmp_path / "r", origin=origin)
    push_branch_to_origin(repo, origin, "later")
    fixture_git(repo, "fetch", "-q", "origin")
    fixture_git(repo, "remote", "set-head", "origin", "main")
    assert "refs/remotes/origin/HEAD" in fixture_git(repo, "for-each-ref")  # the plant
    args = ["for-each-ref", f"--format={g.BRANCH_FORMAT}", "refs/heads", "refs/remotes"]
    records = g.Git(ENV).stream(repo, args, sep=b"\n", deadline=soon())
    refs = list(g.parse_branches(records))
    assert [(r.remote, r.name, r.current, r.upstream) for r in refs] == [
        (False, "main", True, "refs/remotes/origin/main"),
        (True, "origin/feature", False, None),
        (True, "origin/later", False, None),
        (True, "origin/main", False, None),
    ]


def test_a_branch_record_of_another_shape_is_refused():
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_branches(iter([b"refs/heads/main\0\0"])))
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_branches(iter([b"refs/tags/v1\0\0\0 "])))


# --- starting and ending git --------------------------------------------------------


def test_input_reaches_git_and_its_answer_comes_back(tmp_path):
    repo = make_repo(tmp_path / "r")
    expected = fixture_git(repo, "hash-object", "--stdin", input=b"hello\n")
    assert g.Git(ENV).run(repo, ["hash-object", "--stdin"], deadline=soon(), input=b"hello\n") == (
        expected
    )


def test_extra_variables_reach_git(tmp_path):
    repo = make_repo(tmp_path / "r")
    out = g.Git(ENV).run(
        repo, ["var", "GIT_AUTHOR_IDENT"], deadline=soon(), extra_env={"GIT_AUTHOR_NAME": "Zed"}
    )
    assert out.startswith("Zed <")


def test_a_refusal_carries_what_git_said_and_its_exit_status(tmp_path):
    repo = make_repo(tmp_path / "r")
    with pytest.raises(g.GitFailed) as failed:
        g.Git(ENV).run(repo, ["switch", "no-such-branch"], deadline=soon())
    assert failed.value.exit_code != 0
    assert any("no-such-branch" in line for line in failed.value.lines)
    assert failed.value.more == 0


def test_what_git_wrote_on_standard_output_is_part_of_its_refusal(tmp_path):
    repo = make_repo(tmp_path / "r")
    with pytest.raises(g.GitFailed) as failed:
        g.Git(ENV).run(repo, ["commit", "-q", "-m", "nothing"], deadline=soon())
    assert any("nothing to commit" in line for line in failed.value.lines), failed.value.lines


def test_gits_message_is_capped_and_says_how_much_more_there_was(tmp_path):
    repo = make_repo(tmp_path / "r")
    total = g.MAX_GIT_MESSAGE_LINES + 7
    hook(
        repo,
        "pre-commit",
        f"i=0; while [ $i -lt {total} ]; do echo line $i >&2; i=$((i+1)); done; exit 1\n",
    )
    write(repo / "x.txt")
    fixture_git(repo, "add", "x.txt")
    with pytest.raises(g.GitFailed) as failed:
        g.Git(ENV).run(repo, ["commit", "-q", "-m", "x"], deadline=soon())
    assert failed.value.lines == [f"line {i}" for i in range(g.MAX_GIT_MESSAGE_LINES)]
    assert failed.value.more == 7


def test_a_call_past_its_deadline_ends_everything_it_started(tmp_path):
    repo = make_repo(tmp_path / "r")
    pid_file = tmp_path / "hook.pid"
    hook(repo, "pre-commit", f"echo $$ > {pid_file}; exec sleep 60\n")
    write(repo / "x.txt")
    fixture_git(repo, "add", "x.txt")
    began = time.monotonic()
    with pytest.raises(g.GitTimedOut):
        g.Git(ENV).run(repo, ["commit", "-q", "-m", "x"], deadline=time.monotonic() + 2)
    assert time.monotonic() - began < 10
    pid = int(pid_file.read_text())  # positive control: the hook ran
    assert gone(pid), f"the hook {pid} still runs"


def test_a_stream_left_early_ends_git(tmp_path):
    repo = make_repo(tmp_path / "r")
    for i in range(50):
        write(repo / f"f{i}.txt")
    started = []

    def popen(*args, **kwargs):
        started.append(subprocess.Popen(*args, **kwargs))
        return started[-1]

    records = g.Git(ENV, popen=popen).stream(
        repo, ["status", "--porcelain=v2", "-z"], sep=b"\0", deadline=soon()
    )
    assert next(records)
    records.close()
    assert started and started[0].returncode is not None


def test_git_is_looked_for_on_the_system_path_only(tmp_path):
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / "decoy-ran"
    decoy = bin_dir / "git"
    decoy.write_text(f"#!/bin/sh\ntouch {marker}\necho decoy\n", encoding="utf-8")
    decoy.chmod(0o755)
    repo = make_repo(tmp_path / "r")
    env = {**ENV, "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"}
    out = g.Git(env).run(repo, ["rev-parse", "--is-inside-work-tree"], deadline=soon())
    assert out.strip() == "true" and not marker.exists()
    assert Path(g.find_git()).parent.as_posix() in g.GIT_SEARCH_PATH.split(":")
    subprocess.run([str(decoy)], check=True)  # positive control: the decoy does run when called
    assert marker.exists()


def test_no_git_on_the_system_path_is_git_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "GIT_SEARCH_PATH", str(tmp_path))
    with pytest.raises(g.GitMissing):
        g.Git(ENV).run(tmp_path, ["--version"], deadline=soon())


def test_the_system_git_is_at_least_the_version_rcman_needs():
    assert g.version_of(g.Git(ENV).run(Path("/"), ["--version"], deadline=soon())) >= (
        g.GIT_MIN_VERSION
    )


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("git version 2.43.0\n", (2, 43)),
        ("git version 2.31.1.windows.1\n", (2, 31)),
        ("git version 3.0\n", (3, 0)),
    ],
)
def test_a_version_is_read_from_gits_answer(text, version):
    assert g.version_of(text) == version


def test_an_answer_with_no_version_is_unrecognised():
    with pytest.raises(g.GitUnrecognised):
        g.version_of("hello\n")


def test_a_line_git_says_is_cut_at_its_length(tmp_path):
    repo = make_repo(tmp_path / "r")
    long = g.MAX_GIT_LINE_LENGTH * 10
    hook(repo, "pre-commit", f"printf '%{long}s\\n' '' | tr ' ' x >&2; echo after >&2; exit 1\n")
    write(repo / "x.txt")
    fixture_git(repo, "add", "x.txt")
    with pytest.raises(g.GitFailed) as failed:
        g.Git(ENV).run(repo, ["commit", "-q", "-m", "x"], deadline=soon())
    assert failed.value.lines == ["x" * g.MAX_GIT_LINE_LENGTH, "after"]


def test_git_is_ended_whatever_stops_the_call_once_it_has_started(tmp_path, monkeypatch):
    """Even an exception right after git starts, as SIGTERM would raise there, ends it."""
    repo = make_repo(tmp_path / "r")
    started = []

    def popen(*args, **kwargs):
        started.append(subprocess.Popen(*args, **kwargs))
        return started[-1]

    class Stopped(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stopped

    monkeypatch.setattr(g, "_Message", stop)
    with pytest.raises(Stopped):
        g.Git(ENV, popen=popen).run(repo, ["hash-object", "--stdin"], deadline=soon())
    [proc] = started  # positive control: git started, and waits on its stdin
    assert proc.poll() is not None, "git was left running"


def test_what_is_kept_of_gits_message_is_bounded_however_much_it_says():
    """A line that never ends, and lines beyond the cap, are not held in memory."""
    message = g._Message()
    for _ in range(100):
        message.feed(b"x" * 65536)  # one line, never ended
    assert len(message._partial) <= g.MAX_GIT_LINE_LENGTH * 4
    message.feed(b"\n" + b"line\n" * (g.MAX_GIT_MESSAGE_LINES * 3))
    message.close()
    assert len(message.lines) == g.MAX_GIT_MESSAGE_LINES
    assert message.lines[0] == "x" * g.MAX_GIT_LINE_LENGTH
    assert message.more == g.MAX_GIT_MESSAGE_LINES * 3 - (g.MAX_GIT_MESSAGE_LINES - 1)
