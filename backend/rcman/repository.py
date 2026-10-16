"""A workspace's git repository: the verbs the Changes section is made of.

git runs in the workspace, at the root of its work folder, through the workspace's
runner: reads go through its channel, and what carries a person's text on stdin — the
files to stage, a commit's message — through the runtime's ``exec``, so that none of it
is ever in an argv. Every git call is wrapped in the workspace's ``timeout`` tool, and
the calls of one verb share one deadline. git has only the environment ``env`` gives it.
Its refusals, and everything the verbs refuse before asking it, are
:class:`~rcman.problems.Problem`\\ s.

The commit author and the GitHub credential are not passed here: they are the person's
global git configuration in the workspace (gitaccount.py), which git reads itself.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath

from helpers import git_run
from rcman import problems
from rcman.channel import ChannelBroken, ChannelTimeout, Result
from rcman.problems import Problem
from rcman.runtime import (
    GIT_HOME,
    TIMED_OUT,
    Layout,
    RuntimeFailed,
    RuntimeUnavailable,
    moves_git_config,
)

# How long a verb's git calls may take, all together; fetch and push, over the network.
# Whether the file $1 has a line of a password that is not blank; the shell's exit code is the
# answer, and the file is read by the shell alone.
HAS_PASSWORD = (
    'while IFS= read -r line || [ -n "$line" ]; do '
    'case $line in password=*[![:space:]]*) exit 0;; esac; done < "$1"; exit 1'
)
GIT_TIMEOUT = 30.0
GIT_NETWORK_TIMEOUT = 120.0
# How long a clone may take: a whole repository, over the network.
CLONE_TIMEOUT = 1800.0
# How long after TERM a git that has not ended is left before it is killed, and the time
# a runner is given beyond a call's deadline to answer.
KILL_GRACE_SECONDS = 5
ANSWER_MARGIN_SECONDS = 10
# The longest file name taken, and the most files one stage or unstage takes.
MAX_PATH_LENGTH = 1024
MAX_FILES_PER_CALL = 500
# How many files each of a status's two lists, and branches each list, holds; the rest are
# counted.
MAX_LISTED_FILES = 500
MAX_LISTED_BRANCHES = 200
# The address of the GitHub rcman has a token for.
GITHUB_ADDRESS = "https://github.com/"
# The credential file's name in the git folder.
CREDENTIAL = "credential"

STATUS_ARGS = [
    "status",
    "--porcelain=v2",
    "-z",
    "--branch",
    "--untracked-files=normal",
    "--ignored=no",
]
BRANCHES_ARGS = [
    "for-each-ref",
    f"--format={git_run.BRANCH_FORMAT}",
    "refs/heads",
    "refs/remotes",
]
# for-each-ref's format for where a branch is pushed: its name, upstream and push remote.
PUSH_FORMAT = "%(refname)%00%(upstream)%00%(push:remotename)"
# The remote a branch with no upstream is pushed to, and made to track (git-push).
PUSH_REMOTE = "origin"
# A file list as git reads one: NUL-separated on stdin, each name taken literally.
PATHSPEC_FROM_STDIN = ["--pathspec-from-file=-", "--pathspec-file-nul"]

# The problem a failure of the runtime or the channel is answered as (workspaces.answered).
_RUNTIME_PROBLEMS = {
    RuntimeUnavailable: "runtime_unavailable",
    ChannelTimeout: "timeout",
}

# How a runner is called: its argv and the seconds it may take, and with stdin besides.
Run = Callable[[list[str], float], Result]
RunStdin = Callable[[list[str], bytes, float], Result]


def _verb(deadline: str = "GIT_TIMEOUT"):
    """A verb: one deadline for all its git calls, read from the module's constant named
    ``deadline`` when the verb is called, and git's refusals as problems."""

    def wrap(method):
        @functools.wraps(method)
        def verb(self, *args, **kwargs):
            until = time.monotonic() + globals()[deadline]
            try:
                return method(self, until, *args, **kwargs)
            except git_run.GitFailed as failed:
                raise Problem("git_failed", git=failed.lines, git_more=failed.more) from None
            except git_run.GitUnrecognised as odd:
                raise Problem("git_unrecognised_output", git=odd.lines) from None

        return verb

    return wrap


class CloneFailed(Exception):
    """git clone ended with a refusal: its exit code, and everything it said on standard
    error and standard output, which the caller keeps the tail of."""

    def __init__(self, code: int, out: bytes, err: bytes):
        super().__init__(f"git clone exited with {code}")
        self.code, self.out, self.err = code, out, err


def _name(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _file(change: git_run.Change) -> dict:
    """A changed file as the API lists it."""
    listed = {"path": _name(change.path), "change": change.change}
    if change.origin is not None:
        listed["from"] = _name(change.origin)
    if change.submodule:
        listed["submodule"] = True
    if not _is_utf8(change.path):
        listed["selectable"] = False
    return listed


def tracking_name(remote_branch: str) -> str:
    """The local branch git switch --track makes for a remote-tracking branch, by its
    short name: what follows the first slash, whatever the remote is called, as git
    takes it - a/b/feature, of the remote a/b, makes b/feature."""
    return remote_branch.partition("/")[2]


def blocking_branch(name: str, local: Iterable[str]) -> str | None:
    """The local branch that keeps git from making the branch ``name``, or None: one of
    that name, or, as git keeps branches as paths, one in a folder of that name
    (feature/x for feature) or whose name is a folder of it (topic for topic/x). The
    same name first, then the first in order."""
    names = set(local)
    if name in names:
        return name
    clashes = [n for n in names if n.startswith(f"{name}/") or name.startswith(f"{n}/")]
    return min(clashes, default=None)


def _short(ref: str | None) -> str | None:
    """A branch's full name without refs/heads/ or refs/remotes/."""
    if ref is None:
        return None
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


class _Capped:
    """A list holding at most ``limit`` items, counting those beyond."""

    def __init__(self, limit: int, key: str):
        self.limit, self.key = limit, key
        self.items: list = []
        self.more = 0

    def add(self, item) -> None:
        if len(self.items) < self.limit:
            self.items.append(item)
        else:
            self.more += 1

    def answer(self) -> dict:
        return {self.key: self.items, "more": self.more}


def check_file(name: str) -> bytes:
    """A file name in a repository, as git gets it; refused when it could reach outside.

    Lexical only: a name that leaves the repository, or names git's own folder, is
    refused here, and a link out of the repository is refused by git itself (``beyond a
    symbolic link``), which surfaces as ``git_failed``. A folder, which an untracked
    folder is listed as, may end in one slash.
    """
    if not name or "\0" in name or len(name) > MAX_PATH_LENGTH:
        raise Problem("file_invalid")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:  # a name that is not UTF-8 is listed, but not selectable
        raise Problem("file_invalid") from None
    if name.startswith("/"):
        raise Problem("file_outside_repository")
    parts = name.removesuffix("/").split("/")
    if any(part in ("", ".") for part in parts):
        raise Problem("file_invalid")
    if any(part == ".." or part.casefold() == ".git" for part in parts):
        raise Problem("file_outside_repository")
    return encoded


def check_branch(name: str) -> str:
    """A branch name that cannot be taken for an option or break a command line."""
    if not name or name.startswith("-") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise Problem("branch_invalid")
    return name


class Repository:
    """The git repository at the root of a workspace's work folder.

    ``run`` runs an argv in the workspace and answers its result within the seconds it is
    given (a channel's ``run``); ``run_stdin`` does the same with bytes on its standard
    input (the runtime's ``exec``).
    """

    def __init__(self, layout: Layout, run: Run, run_stdin: RunStdin):
        self.layout, self.run, self.run_stdin = layout, run, run_stdin

    @property
    def env(self) -> dict[str, str]:
        """The only environment git is run with: the variables the workspace's plan sets but for
        every one of git's own (``GIT_...``: a plan could move its configuration, make it print
        what it sends or name a program for it to run), and the few of git's rcman sets: git
        unable to ask anything on a terminal, taking no lock it does not need, and unable to
        find a repository above the workspace's work folder."""
        return {
            **{
                k: v
                for k, v in self.layout.env.items()
                if not (k.startswith("GIT_") or moves_git_config(k))
            },
            "HOME": self.layout.home,
            "PATH": self.layout.path,
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CEILING_DIRECTORIES": str(PurePosixPath(self.layout.work).parent),
        }

    # --- running git -------------------------------------------------------------------

    def _seconds(self, deadline: float) -> int:
        left = math.ceil(deadline - time.monotonic())
        if left < 1:
            raise Problem("git_timeout")
        return left

    def _git(
        self,
        args: list[str],
        deadline: float,
        stdin: bytes | None = None,
        check: bool = True,
        network: bool = False,
    ) -> Result:
        """git with ``args`` in the work folder, ended with TERM and then KILL if it is still
        running at the deadline. A git that is not there, a timeout and a work folder that
        is no repository are problems; any other refusal is git_run's GitFailed, unless
        ``check`` is off.

        With ``stdin``, or for ``network`` — a call that may take minutes — git is run
        through ``run_stdin``, a process of its own, so that it does not hold the
        workspace's channel, which everything else in the workspace waits for.
        """
        seconds = self._seconds(deadline)
        command = [
            *self.layout.tool("timeout"),
            "-s",
            "TERM",
            "-k",
            str(KILL_GRACE_SECONDS),
            str(seconds),
            "git",
            "-C",
            self.layout.work,
            *args,
        ]
        argv = self.layout.with_env(self.env, command)
        wait = seconds + KILL_GRACE_SECONDS + ANSWER_MARGIN_SECONDS
        if stdin is None and not network:
            result = self.run(argv, wait)
        else:
            result = self.run_stdin(argv, stdin or b"", wait)
        if result.code in TIMED_OUT:
            raise Problem("git_timeout")
        if result.code == 127:
            raise Problem("git_not_installed")
        if result.code == 128 and b"not a git repository" in result.err:
            raise Problem("not_a_repository")
        if check and result.code != 0:
            raise git_run.failure(result.code, result.out, result.err)
        return result

    def _out(self, args: list[str], deadline: float, **kwargs) -> str:
        return self._git(args, deadline, **kwargs).out.decode("utf-8", "replace")

    def _records(self, args: list[str], sep: bytes, deadline: float) -> list[bytes]:
        records = self._git(args, deadline).out.split(sep)
        if records and records[-1] == b"":
            records.pop()
        return records

    def _changes(self, deadline: float, branch: git_run.Branch):
        return git_run.parse_status(self._records(STATUS_ARGS, b"\0", deadline), branch)

    def _branches(self, deadline: float):
        return git_run.parse_branches(self._records(BRANCHES_ARGS, b"\n", deadline))

    def _current(self, deadline: float) -> str | None:
        return self._out(["branch", "--show-current"], deadline).strip() or None

    def _token_stored(self, deadline: float) -> bool:
        """Whether the credential file git is told to use holds a password that is not blank,
        asked of the workspace's shell, which answers by its exit code: no byte of the file
        passes through the channel, whose answers are held in memory."""
        path = f"{self.layout.home}/{GIT_HOME}/{CREDENTIAL}"
        found = self.run(
            [self.layout.shell, "-c", HAS_PASSWORD, "sh", path], self._seconds(deadline)
        )
        return found.code == 0

    # --- reading -----------------------------------------------------------------------

    @_verb()
    def version(self, deadline: float) -> tuple[int, int]:
        """The major and minor version of the workspace's git."""
        return git_run.version_of(self._out(["--version"], deadline))

    @_verb()
    def status(self, deadline: float) -> dict:
        branch = git_run.Branch()
        lists = {
            True: _Capped(MAX_LISTED_FILES, "files"),
            False: _Capped(MAX_LISTED_FILES, "files"),
        }
        for change in self._changes(deadline, branch):
            lists[change.staged].add(_file(change))
        # MERGE_HEAD stays until the merge is committed or aborted, also once no file is
        # left unmerged.
        merging = self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], deadline, check=False)
        _, has_remote, to_github = self._push_facts(branch.name, deadline)
        return {
            "branch": {
                "name": branch.name,
                "commit": branch.commit,
                "upstream": branch.upstream,
                "ahead": branch.ahead,
                "behind": branch.behind,
            },
            "merging": merging.code == 0,
            "staged": lists[True].answer(),
            "not_staged": lists[False].answer(),
            "push_has_remote": has_remote,
            "push_to_github": to_github,
            "github_token": self._token_stored(deadline) if to_github else None,
            "push_ahead": self._push_ahead(branch, deadline),
        }

    def _push_ahead(self, branch: git_run.Branch, deadline: float) -> int | None:
        """How many commits of the branch where it pushes lacks: its push destination
        (@{push}) when git knows one, otherwise its upstream; None with neither."""
        if branch.name is None:
            return None
        counted = self._git(["rev-list", "--count", "@{push}..HEAD"], deadline, check=False)
        if counted.code != 0:
            return branch.ahead
        return int(counted.out)

    @_verb()
    def branches(self, deadline: float) -> dict:
        local = _Capped(MAX_LISTED_BRANCHES, "branches")
        remote = _Capped(MAX_LISTED_BRANCHES, "branches")
        tracking: dict[str, str] = {}  # a remote branch's full name -> a local branch on it
        remotes = []
        names = set()  # every local branch, those beyond the cap too
        for ref in self._branches(deadline):
            if ref.remote:
                remotes.append(ref.name)
                continue
            names.add(ref.name)
            local.add({"name": ref.name, "current": ref.current, "upstream": _short(ref.upstream)})
            if ref.upstream and ref.upstream.startswith("refs/remotes/"):
                tracking.setdefault(ref.upstream, ref.name)
        for name in remotes:
            remote.add(
                {
                    "name": name,
                    "local": tracking.get(f"refs/remotes/{name}"),
                    "taken_by": blocking_branch(tracking_name(name), names),
                }
            )
        return {"local": local.answer(), "remote": remote.answer()}

    # --- the index and commits ---------------------------------------------------------

    @staticmethod
    def _names(files: list[str]) -> bytes:
        return b"".join(check_file(name) + b"\0" for name in files)

    @_verb()
    def stage(self, deadline: float, files: list[str]) -> dict:
        names = self._names(files)
        self._git(["--literal-pathspecs", "add", *PATHSPEC_FROM_STDIN], deadline, stdin=names)
        return {}

    @_verb()
    def unstage(self, deadline: float, files: list[str]) -> dict:
        """Unstage files; a staged rename's target takes its origin with it, which would
        otherwise stay staged as deleted."""
        names = self._names(files)
        wanted = {check_file(name) for name in files}
        renamed = {
            change.path: change.origin
            for change in self._changes(deadline, git_run.Branch())
            if change.staged and change.change == "renamed" and change.path in wanted
        }
        names += b"".join(origin + b"\0" for origin in renamed.values())
        self._git(
            ["--literal-pathspecs", "reset", "-q", *PATHSPEC_FROM_STDIN], deadline, stdin=names
        )
        return {}

    @_verb()
    def commit(self, deadline: float, message: str) -> dict:
        """Commit what is staged, with the message on stdin, as the global git
        configuration says; the message is never part of an answer."""
        if not any(c.staged for c in self._changes(deadline, git_run.Branch())):
            raise Problem("nothing_staged")
        self._git(["commit", "-q", "-F", "-"], deadline, stdin=message.encode("utf-8"))
        commit = self._out(["rev-parse", "HEAD"], deadline).strip()
        return {"commit": commit, "branch": self._current(deadline)}

    # --- branches ----------------------------------------------------------------------

    @_verb()
    def switch(self, deadline: float, branch: str, remote: bool = False) -> dict:
        """Switch to a local branch, or make a local branch tracking a remote one, which is
        refused before git is asked when a local branch keeps git from making it."""
        check_branch(branch)
        refs = list(self._branches(deadline))
        if not any(r.remote is remote and r.name == branch for r in refs):
            raise Problem("branch_not_found")
        local = [r.name for r in refs if not r.remote]
        if remote and blocking_branch(tracking_name(branch), local) is not None:
            raise Problem("branch_name_taken")
        args = ["switch", "--track", branch] if remote else ["switch", branch]
        self._git(args, deadline)
        return {"branch": self._current(deadline)}

    @_verb()
    def new_branch(self, deadline: float, name: str) -> dict:
        """Make a branch at the current commit and switch to it.

        The name must be one git takes as it is: check-ref-format would expand @{-1} and
        the like into another branch's name.
        """
        check_branch(name)
        try:
            checked = self._out(["check-ref-format", "--branch", name], deadline)
        except git_run.GitFailed:
            raise Problem("branch_invalid") from None
        if checked.rstrip("\n") != name:
            raise Problem("branch_invalid")
        self._git(["switch", "-c", name], deadline)
        return {"branch": self._current(deadline)}

    # --- remotes -----------------------------------------------------------------------

    @_verb("GIT_NETWORK_TIMEOUT")
    def fetch(self, deadline: float) -> dict:
        """git fetch --prune from the default remote: no files and no local branch change."""
        self._git(["fetch", "--prune"], deadline, network=True)
        return {}

    @_verb("CLONE_TIMEOUT")
    def clone(self, deadline: float, url: str) -> dict:
        """git clone of ``url`` into the work folder, which is empty: every branch, the
        default one checked out. Its own process, as a fetch's, so that it does not hold
        the workspace's channel for as long as it takes. The person's token reaches git
        through the credential helper alone. A refusal is :class:`CloneFailed`, with the
        exit code and all git said."""
        result = self._git(["clone", "--", url, "."], deadline, network=True, check=False)
        if result.code != 0:
            raise CloneFailed(result.code, result.out, result.err)
        return {}

    def _push_target(self, branch: str | None, deadline: float):
        """A branch's upstream (its full name, or None) and the remote git-push pushes it to:
        its push remote, as git push finds it, or PUSH_REMOTE when it has no upstream or
        when there is no branch.

        Before its first commit a branch is no ref, so git names neither; both are then
        read from its configuration, as git reads them, and the upstream is the branch it
        merges on its remote (branch.<b>.merge), which says only that there is one.
        """
        if branch is None:
            return None, PUSH_REMOTE
        ref = f"refs/heads/{branch}"
        for line in self._out(["for-each-ref", f"--format={PUSH_FORMAT}", ref], deadline).split(
            "\n"
        ):
            name, _, rest = line.partition("\0")
            if name == ref:  # the pattern is a prefix to git, so only this branch's line
                upstream, _, remote = rest.partition("\0")
                return (upstream, remote or PUSH_REMOTE) if upstream else (None, PUSH_REMOTE)
        return self._configured_push_target(branch, deadline)

    def _configured_push_target(self, branch: str, deadline: float):
        """_push_target for a branch that is no ref yet, from its configuration: an upstream
        needs both branch.<b>.remote and branch.<b>.merge, and the push remote is
        branch.<b>.pushRemote, else remote.pushDefault, else branch.<b>.remote, the order
        git push takes them in."""
        remote = self._config(f"branch.{branch}.remote", deadline)
        merge = self._config(f"branch.{branch}.merge", deadline)
        if remote is None or merge is None:
            return None, PUSH_REMOTE
        push = (
            self._config(f"branch.{branch}.pushRemote", deadline)
            or self._config("remote.pushDefault", deadline)
            or remote
        )
        return merge, push

    def _config(self, key: str, deadline: float) -> str | None:
        """A configuration value as git config --get reads it in the repository, or None
        while it is not set (git config's exit 1)."""
        found = self._git(["config", "--get", key], deadline, check=False)
        if found.code == 1:
            return None
        if found.code != 0:
            raise git_run.failure(found.code, found.out, found.err)
        return found.out.decode("utf-8", "replace").rstrip("\n") or None

    def _push_url(self, remote: str, deadline: float) -> str | None:
        """The URL a push to ``remote`` goes to, as git rewrites it with the person's
        configuration; None for a remote git does not know."""
        found = self._git(["remote", "get-url", "--push", "--", remote], deadline, check=False)
        if found.code != 0:
            return None
        return found.out.decode("utf-8", "replace").strip()

    def _push_facts(self, branch: str | None, deadline: float):
        """For a push of ``branch``: its upstream (_push_target), whether it has a remote to
        go to, and whether it goes to GitHub, from one push URL, that of the remote it
        goes to.

        One with an upstream has a remote: it goes where git push sends it. One without,
        and no branch, go to PUSH_REMOTE, so they have one only if git knows a remote of
        that name. A push goes to GitHub when that remote's push URL is under
        GITHUB_ADDRESS; a remote git does not know is no push to GitHub.
        """
        upstream, remote = self._push_target(branch, deadline)
        url = self._push_url(remote, deadline)
        has_remote = upstream is not None or url is not None  # remote is PUSH_REMOTE here
        to_github = url is not None and url.lower().startswith(GITHUB_ADDRESS)
        return upstream, has_remote, to_github

    @_verb("GIT_NETWORK_TIMEOUT")
    def push(self, deadline: float, check: bool = False) -> dict:
        """Push the current branch: one with no upstream as git push -u origin HEAD, and
        otherwise as git push, as the person's push.default says.

        Refused while HEAD is detached, with no remote to push to, for a push to GitHub
        without a token, and before the branch's first commit, before git is asked to
        push. With ``check``, only the first three are checked: Commit & Push's commit
        makes a first commit.
        """
        branch = self._current(deadline)
        if branch is None:
            raise Problem("detached_head")
        upstream, has_remote, to_github = self._push_facts(branch, deadline)
        if not has_remote:
            raise Problem("no_push_remote")
        if to_github and not self._token_stored(deadline):
            raise Problem("github_token_not_set")
        if check:
            return {"branch": branch}
        if self._git(["rev-parse", "--verify", "-q", "HEAD"], deadline, check=False).code != 0:
            raise Problem("no_commits_yet")
        args = ["push"] if upstream else ["push", "-u", PUSH_REMOTE, "HEAD"]
        self._git(args, deadline, network=True)
        return {
            "branch": branch,
            "upstream": _short(self._push_target(branch, deadline)[0]),
            "upstream_set": upstream is None,
        }

    def commit_push(self, message: str) -> dict:
        """Commit what is staged, then push it.

        Nothing is committed when the push would be refused before it was tried: no branch
        checked out, nowhere to push, or a push to GitHub with no token. A branch with no
        commit yet is committed to and pushed. When the commit is made and the push fails,
        the answer is the commit and the push's problem (``push_problem``, as a problem's
        body), and ``push`` is None.
        """
        self.push(check=True)
        made = self.commit(message)
        answer = {"commit": made["commit"], "push": None, "push_problem": None}
        try:
            answer["push"] = self.push()
        except Problem as failed:
            answer["push_problem"] = problems.body(
                failed.code, failed.status, failed.title, **failed.extra
            )
        except (RuntimeUnavailable, RuntimeFailed, ChannelBroken, ChannelTimeout) as failed:
            # The commit is made whatever became of the workspace since: its answer stays.
            code = _RUNTIME_PROBLEMS.get(type(failed), "workspace_failed")
            answer["push_problem"] = problems.body(
                code, problems.STATUS[code], problems.MESSAGES[code]
            )
        return answer
