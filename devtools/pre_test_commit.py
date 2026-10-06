#!/usr/bin/env python3
"""PreToolUse hook: commit and push the work before a command runs tests.

A test run that deletes the checkout takes with it whatever exists only there.
Before a Bash command that runs tests, the checkout it runs in has its changes,
untracked files included, committed as ``WIP before tests``, and its branch
pushed to ``origin``.

Which commands run tests is read from the command alone, without running
anything. It is split as the shell splits it, into simple commands joined by
``&&``, ``||``, ``;``, ``|``, ``&`` or a newline; the shell words in
``KEYWORDS``, assignments and the wrappers ``env``, ``timeout``, ``nice``,
``time``, ``exec``, ``command``, ``nohup``, ``uv run``, ``uvx``, ``npx`` and
``python -m`` are taken off. What remains runs tests when it is pytest, Vitest
or ``playwright test`` without an option in ``NOT_A_RUN``; an npm script whose
text, read the same way, does; or a make target whose recipe or prerequisites,
read from the Makefile without running make, do. Where it runs follows a
``cd`` at the start of the command, outside any subshell, and make's ``-C``;
nothing else moves it, and a ``cd`` to a command substitution leaves it where
the session is.

The hook acts only in a checkout of the project's own repository: one whose
git common folder is that of ``$CLAUDE_PROJECT_DIR``. There, it commits
nothing on main, on origin's default branch or on a detached HEAD, nor during
a merge, rebase, cherry-pick or revert, and says so when there are changes; it
leaves out of the commit, and names, each untracked file that is a nested
repository, larger than ``MAX_FILE_BYTES`` or named like a credential
(``CREDENTIAL_NAMES``), and commits and pushes the rest.
It never uses ``--no-verify``: a commit the repository's own hooks refuse is
reported, and the index is put back as it was. It pushes with ``-u`` when the
branch has no upstream or its upstream is on origin, and without when the
upstream is on another remote, comparing with ``origin/<branch>``; an upstream
that is configured but gone is reported, not pushed to again.

It never blocks the command: it makes no permission decision and exits 0, and
the wiring exits 0 without it when the project folder lacks the script. Its
git calls share one deadline, ``DEADLINE``, and the longest it can run,
``LONGEST_RUN``, is inside the timeout the wiring gives it. A failure reaches
the operator through ``systemMessage`` and the agent through
``additionalContext``; a commit or push it made is told to the agent. A
command that runs no tests starts no process.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEADLINE = 90  # seconds, for all of the hook's git calls together
GRACE = 5  # seconds a git call stopped at the deadline, or the index restore, may take
# At the deadline: the call stopped, then HEAD asked and the index put back, each in GRACE.
LONGEST_RUN = DEADLINE + 3 * GRACE
MAX_FILE_BYTES = 5 * 1024 * 1024
CREDENTIAL_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", ".netrc", "*.p12", "*.pfx")
MESSAGE = "WIP before tests"
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
OUTPUT_TAIL_LINES = 30
LISTED_FILES = 20
MAX_DEPTH = 4

SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n"}
PUNCTUATION = "();<>|&\n"
# Shell operators, longest first, as a run of punctuation is split into them.
OPERATORS = sorted(
    SEPARATORS | {">>", "<<", ">&", "<&", "&>", "<>", ">|", "<", ">"}, key=len, reverse=True
)
KEYWORDS = {"!", "{", "do", "then", "else", "elif", "if", "while", "until"}
NOT_A_RUN = {"--version", "--help", "-h", "--collect-only", "--co"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"python[0-9.]*")
MAKE = {"make", "gmake"}
MAKE_VARIABLE = re.compile(r"\$[({]MAKE[)}]")
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
RULE = re.compile(r"^([^:=]+?)\s*::?(?!=)\s*(.*)$")
VARIABLE = re.compile(r"(?:^|[^:?+!])=|:=|\?=|\+=|!=")
PROTECTED = "main"
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
}
# Options of the wrappers that take the next word as their value.
VALUE_OPTIONS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "uv": {
        "--directory",
        "--project",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--package",
        "--group",
        "--extra",
        "--env-file",
        "--index",
        "--cache-dir",
        "--config-file",
    },
    "npx": {"-p", "--package", "-c", "--call"},
    "npm": {"--prefix", "-w", "--workspace"},
}
MAKE_VALUE_OPTIONS = {"-I", "--include-dir", "-o", "--old-file", "-W", "--what-if"}


# --- reading the command ------------------------------------------------------------


def simple_commands(command: str) -> list[tuple[list[str], bool]]:
    """The command's simple commands, each as its words and whether it is outside
    any subshell; quotes and comments as the shell reads them. A command shlex
    cannot read raises ValueError."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[tuple[list[str], bool]] = []
    words: list[str] = []
    depth = 0
    for token in (operator for run in lexer for operator in _operators(run)):
        if token in SEPARATORS or token in ("(", ")"):
            if words:
                commands.append((words, depth == 0))
            words = []
            depth += {"(": 1, ")": -1}.get(token, 0)
        else:
            words.append(token)
    if words:
        commands.append((words, depth == 0))
    return commands


def _operators(token: str) -> list[str]:
    """``token`` split into shell operators when shlex gave a run of punctuation,
    such as ``);``: ``(`` and ``)`` alone, the others longest first."""
    if not token or any(char not in PUNCTUATION for char in token):
        return [token]
    operators = []
    while token:
        if token[0] in "()":
            size = 1
        else:
            size = next((len(op) for op in OPERATORS if token.startswith(op)), 1)
        operators.append(token[:size])
        token = token[size:]
    return operators


def _path(directory: Path, word: str) -> Path:
    return directory / os.path.expanduser(os.path.expandvars(word))


def _options(tool: str, words: list[str]) -> list[str]:
    """``words`` without the leading options of ``tool``."""
    takes_value = VALUE_OPTIONS.get(tool, set())
    while words and words[0].startswith("-") and words[0] != "-":
        if words[0] == "--":
            return words[1:]
        option = words[0]
        words = words[2:] if "=" not in option and option in takes_value else words[1:]
    return words


def _bare(words: list[str]) -> list[str]:
    """``words`` without leading shell keywords and assignments."""
    while words and (words[0] in KEYWORDS or ASSIGNMENT.match(words[0])):
        words = words[1:]
    return words


def unwrap(words: list[str]) -> list[str]:
    """The command a wrapper runs."""
    while True:
        words = _bare(words)
        if not words:
            return words
        name = Path(words[0]).name
        if name in ("env", "nice", "time", "exec", "command", "nohup"):
            words = _options(name, words[1:])
        elif name == "timeout":
            words = _options(name, words[1:])[1:]  # the duration goes too
        elif name == "uv" and words[1:2] == ["run"]:
            words = _options("uv", words[2:])
        elif name in ("uvx", "npx"):
            words = _options("npx", words[1:])
        elif PYTHON.fullmatch(name) and words[1:2] == ["-m"]:
            words = words[2:]
        else:
            return words


def _npm_script(words: list[str]) -> str | None:
    if not words:
        return None
    if words[0] in ("test", "t", "tst"):
        return "test"
    if words[0] in ("run", "run-script") and len(words) > 1:
        return words[1]
    return None


def _scripts(directory: Path) -> dict:
    try:
        scripts = json.loads((directory / "package.json").read_text(encoding="utf-8"))["scripts"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return scripts if isinstance(scripts, dict) else {}


# --- reading a Makefile ---------------------------------------------------------------


def _logical_lines(text: str) -> list[str]:
    """Lines with their backslash continuations joined."""
    lines: list[str] = []
    pending = ""
    for line in text.splitlines():
        if pending:
            line = pending + " " + line.lstrip()
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        pending = ""
        lines.append(line)
    if pending:
        lines.append(pending)
    return lines


def read_makefile(path: Path) -> tuple[dict[str, tuple[list[str], list[str]]], str | None]:
    """Each target's prerequisites and recipe lines, and the default goal.

    Read as text: variables are not expanded, conditionals are not evaluated,
    and included files are not read. A ``define`` block and a target-specific
    variable are skipped."""
    rules: dict[str, tuple[list[str], list[str]]] = {}
    default = None
    current: list[str] = []
    in_define = False
    for line in _logical_lines(path.read_text(encoding="utf-8", errors="replace")):
        if in_define:
            in_define = line.strip() != "endef"
            continue
        if line.startswith("\t"):
            for target in current:
                rules[target][1].append(line[1:])
            continue
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if text.split()[0] == "define":
            in_define, current = True, []
            continue
        match = RULE.match(text)
        if match is None:
            current = []
            continue
        targets, rest = match.group(1).split(), match.group(2)
        rest, _, inline = rest.partition(";")
        if VARIABLE.search(rest):
            current = []
            continue
        prerequisites = [word for word in rest.split() if word != "|"]
        for target in targets:
            rules.setdefault(target, ([], []))[0].extend(prerequisites)
            if inline.strip():
                rules[target][1].append(inline)
            if default is None and not target.startswith(".") and "%" not in target:
                default = target
        current = targets
    return rules, default


def _make_arguments(words: list[str], directory: Path) -> tuple[Path, str | None, list[str]]:
    """The folder make runs in, the Makefile it is given and the targets."""
    folder, makefile, targets = directory, None, []
    i = 0
    while i < len(words):
        word = words[i]
        following = words[i + 1] if i + 1 < len(words) else ""
        if word in ("-C", "--directory"):
            folder, i = _path(folder, following), i + 1
        elif word.startswith("--directory="):
            folder = _path(folder, word.partition("=")[2])
        elif word.startswith("-C"):
            folder = _path(folder, word[2:])
        elif word in ("-f", "--file", "--makefile"):
            makefile, i = following, i + 1
        elif word.startswith(("--file=", "--makefile=")):
            makefile = word.partition("=")[2]
        elif word.startswith("-f"):
            makefile = word[2:]
        elif word in MAKE_VALUE_OPTIONS:
            i += 1
        elif not word.startswith("-") and not ASSIGNMENT.match(word):
            targets.append(word)
        i += 1
    return folder, makefile, targets


def _make_tests_run_in(words: list[str], directory: Path, depth: int) -> Path | None:
    folder, given, targets = _make_arguments(words, directory)
    candidates = [folder / given] if given else [folder / name for name in MAKEFILES]
    makefile = next((path for path in candidates if path.is_file()), None)
    if makefile is None:
        return None
    try:
        rules, default = read_makefile(makefile)
    except OSError:
        return None
    pending = list(targets or ([default] if default else []))
    seen: set[str] = set()
    while pending:
        target = pending.pop(0)
        if target in seen or target not in rules:
            continue
        seen.add(target)
        prerequisites, recipe = rules[target]
        for line in recipe:
            line = MAKE_VARIABLE.sub("make", line.strip().lstrip("@-+").strip())
            found = test_directory(line, folder, depth + 1)
            if found is not None:
                return found
        pending.extend(prerequisites)
    return None


def _tests_run_in(words: list[str], directory: Path, depth: int) -> Path | None:
    """The folder ``words``, a simple command run in ``directory``, runs tests in,
    or None when it runs none."""
    name = Path(words[0]).name
    if name in ("pytest", "py.test", "vitest") or (name == "playwright" and words[1:2] == ["test"]):
        return None if NOT_A_RUN.intersection(words[1:]) else directory
    if name == "npm":
        script = _npm_script(_options("npm", words[1:]))
        text = _scripts(directory).get(script) if script else None
        return test_directory(text, directory, depth + 1) if isinstance(text, str) else None
    if name in MAKE:
        return _make_tests_run_in(words[1:], directory, depth)
    return None


def test_directory(command: str, cwd: Path, depth: int = 0) -> Path | None:
    """The folder the first tests ``command``, run from ``cwd``, runs are run in;
    None when it runs none."""
    if depth > MAX_DEPTH:
        return None
    try:
        commands = simple_commands(command)
    except ValueError:
        return None
    directory = cwd
    leading = True
    for words, outside in commands:
        words = _bare(words)
        if not words:
            continue
        if leading and outside and words[0] == "cd":
            target = next((w for w in words[1:] if not w.startswith("-")), "~")
            substituted = any("`" in w or "$(" in w or w.endswith("$") for w in words[1:])
            directory = cwd if substituted else _path(directory, target)
            continue
        leading = False
        words = unwrap(words)
        found = _tests_run_in(words, directory, depth) if words else None
        if found is not None:
            return found
    return None


# --- running git ------------------------------------------------------------------


def _run(argv: list[str], cwd: Path, env, timeout: float) -> subprocess.CompletedProcess:
    """Run ``argv``; past ``timeout`` it is asked to stop, so that git removes its
    lock files, and killed only if it does not, then TimeoutExpired is raised.
    Its output is not waited for then: a git hook it started may still hold it."""
    if timeout <= 0:
        raise subprocess.TimeoutExpired(argv, 0)
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class Git:
    """Git in one folder, every call within one deadline."""

    def __init__(self, cwd: Path, env: dict[str, str], end: float):
        self.cwd, self.env, self.end = cwd, env, end

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        return _run(["git", *args], self.cwd, self.env, self.end - time.monotonic())

    def out(self, *args: str) -> str:
        proc = self(*args)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def restore(self, tree: str) -> bool:
        """Put the index back as ``tree``, whatever is left of the deadline."""
        try:
            return _run(["git", "read-tree", tree], self.cwd, self.env, GRACE).returncode == 0
        except subprocess.TimeoutExpired:
            return False


def _tail(proc: subprocess.CompletedProcess) -> str:
    return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-OUTPUT_TAIL_LINES:])


def report(summary: str, detail: str) -> dict:
    """To the operator and to the agent; the tests run all the same."""
    return {
        "systemMessage": summary,
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": detail},
    }


def tell_agent(detail: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": detail}}


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:LISTED_FILES])
    more = len(paths) - LISTED_FILES
    return shown + (f" and {more} more" if more > 0 else "")


def _restored(restored: bool) -> str:
    return "is as it was" if restored else "could not be put back as it was"


# --- deciding -----------------------------------------------------------------------


def _common_dir(folder: Path, env: dict[str, str], end: float) -> Path | None:
    found = Git(folder, env, end).out("rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(found).resolve() if found else None


def _protected(git: Git) -> set[str]:
    default = git.out("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    names = {PROTECTED}
    if default.startswith("origin/"):
        names.add(default.removeprefix("origin/"))
    return names


def _in_progress(top: Path, git: Git) -> str | None:
    for marker, operation in IN_PROGRESS.items():
        path = git.out("rev-parse", "--git-path", marker)
        if path and (top / path).exists():
            return operation
    return None


def _unaddable(top: Path, untracked: list[str]) -> dict[str, str]:
    """The untracked paths ``git add -A`` must not take, each with why."""
    found = {}
    for rel in untracked:
        name = rel.rstrip("/").rsplit("/", 1)[-1].lower()
        if rel.endswith("/"):
            found[rel] = "a nested repository"
        elif any(fnmatch.fnmatch(name, pattern) for pattern in CREDENTIAL_NAMES):
            found[rel] = "named like a credential"
        else:
            try:
                size = (top / rel).lstat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                found[rel] = f"larger than {MAX_FILE_BYTES} bytes"
    return found


def _ahead(git: Git, ref: str) -> bool:
    if not git.out("rev-parse", "--verify", "-q", ref):
        return True
    return git.out("rev-list", "--count", f"{ref}..HEAD") not in ("", "0")


def _commit(top: Path, git: Git) -> tuple[str | None, dict[str, str], dict | None]:
    """Commit everything but the untracked paths it must not add: what was done,
    the paths left out with why, and what to report instead, if anything."""
    failed = f"The commit before tests failed in {top}"
    listing = git.out("ls-files", "--others", "--exclude-standard", "-z")
    untracked = [path for path in listing.split("\0") if path]
    left_out = _unaddable(top, untracked)
    added = [path for path in untracked if path not in left_out]
    excluded = [f":(exclude,literal){path.rstrip('/')}" for path in left_out]
    tree = git.out("write-tree")
    if not tree:
        return None, left_out, report(f"{failed}.", f"{failed}: its index could not be saved.")
    before = git.out("rev-parse", "HEAD")
    step = "git add"
    try:
        proc = git("add", "-A", "--", ".", *excluded)
        if proc.returncode == 0:
            if git("diff", "--cached", "--quiet").returncode == 0:
                return None, left_out, None
            step = "git commit"
            proc = git("commit", "-q", "-m", MESSAGE, "-m", TRAILER)
    except subprocess.TimeoutExpired:
        if step == "git commit" and _head(git) != before:
            return (
                None,
                left_out,
                report(
                    f"{failed}: {step} timed out.",
                    f"{failed}: {step} timed out after it had made the commit. Nothing was"
                    " pushed; the tests run anyway.",
                ),
            )
        return (
            None,
            left_out,
            report(
                f"{failed}: {step} timed out.",
                f"{failed}: {step} timed out, so nothing was committed or pushed; the index"
                f" {_restored(git.restore(tree))}. The tests run anyway.",
            ),
        )
    if proc.returncode != 0:
        return (
            None,
            left_out,
            report(
                f"{failed}.",
                f"{failed}: {step} failed, so nothing was committed or pushed; the index"
                f" {_restored(git.restore(tree))}. The tests run anyway.\n{_tail(proc)}",
            ),
        )
    sha = git.out("rev-parse", "--short=12", "HEAD")
    adding = f", adding the untracked {_listed(added)}" if added else ""
    return f"committed the changes as {sha}{adding}", left_out, None


def _head(git: Git) -> str:
    """HEAD, asked once the deadline may have passed."""
    try:
        proc = _run(["git", "rev-parse", "HEAD"], git.cwd, git.env, GRACE)
    except subprocess.TimeoutExpired:
        return ""
    return proc.stdout.strip()


def _push(top: Path, branch: str, git: Git, done: list[str]) -> dict | None:
    """Push ``branch`` to origin when origin lacks some of it; what to report."""
    before = f"Before that the hook {' and '.join(done) or 'committed nothing'}."
    failed = f"The push before tests failed in {top}"
    remote = git.out("config", "--get", f"branch.{branch}.remote")
    merge = git.out("config", "--get", f"branch.{branch}.merge")
    if merge and not git.out("rev-parse", "--verify", "-q", "@{upstream}"):
        upstream = f"{remote}/{merge.removeprefix('refs/heads/')}"
        return report(
            f"Not pushed before tests: the upstream {upstream} of {branch} is gone.",
            f"Not pushed before tests: {top} is on {branch}, whose upstream {upstream} is"
            " gone, as when a merged branch is deleted, so the hook does not push it"
            f" again. {before} The tests run anyway.",
        )
    on_origin = not merge or remote == "origin"
    if not _ahead(git, "@{upstream}" if merge and on_origin else f"refs/remotes/origin/{branch}"):
        return None
    if not git.out("remote", "get-url", "origin"):
        return report(
            f"{failed}: there is no origin.",
            f"{failed}: there is no remote origin. {before} The tests run anyway.",
        )
    try:
        proc = git("push", *(["-u"] if on_origin else []), "origin", branch)
    except subprocess.TimeoutExpired:
        return report(
            f"{failed}: it timed out.",
            f"{failed}: it timed out, and the work stays only in this checkout."
            f" {before} The tests run anyway.",
        )
    if proc.returncode != 0:
        return report(f"{failed}.", f"{failed}. {before} The tests run anyway.\n{_tail(proc)}")
    done.append(f"pushed {branch} to origin")
    return None


def protect(directory: Path, env: dict[str, str], end: float) -> dict | None:
    """Commit and push the checkout ``directory`` is in, when it is one of the
    project's; what to say about it."""
    project = env.get("CLAUDE_PROJECT_DIR")
    if not project or not directory.is_dir() or not Path(project).is_dir():
        return None
    common = _common_dir(directory, env, end)
    if common is None or common != _common_dir(Path(project), env, end):
        return None
    top = Path(Git(directory, env, end).out("rev-parse", "--show-toplevel"))
    git = Git(top, env, end)
    if not ({"GIT_SSH_COMMAND", "GIT_SSH"} & env.keys()) and not git.out(
        "config", "core.sshCommand"
    ):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

    dirty = bool(git.out("status", "--porcelain", "--untracked-files=normal"))
    branch = git.out("symbolic-ref", "-q", "--short", "HEAD")
    operation = _in_progress(top, git)
    if operation:
        reason = f"is in {operation}"
    elif not branch:
        reason = "has a detached HEAD"
    elif branch in _protected(git):
        reason = f"is on {branch}"
    else:
        reason = None
    if reason:
        if not dirty:
            return None
        unsaved = f"Not committed before tests: {top} {reason}"
        return report(
            f"{unsaved}.",
            f"{unsaved}; its changes exist only in the working tree, and the tests run anyway.",
        )

    done: list[str] = []
    left_out: dict[str, str] = {}
    if dirty:
        committed, left_out, problem = _commit(top, git)
        if problem is not None:
            return problem
        if committed:
            done.append(committed)
    problem = _push(top, branch, git, done)
    note = ""
    if left_out:
        why = [f"{path} ({reason})" for path, reason in left_out.items()]
        note = (
            f" It left out of the commit, untracked, {_listed(why)}; ignore them in"
            " .gitignore, or commit them by hand."
        )
    if problem is not None:
        problem["hookSpecificOutput"]["additionalContext"] += note
        return problem
    did = " and ".join(done) or "committed nothing"
    summary = f"Before the tests, in {top}, the pre-test hook {did}."
    if left_out:
        return report(
            f"Left out of the commit before tests in {top}: {_listed(list(left_out))}.",
            summary + note,
        )
    return tell_agent(summary) if done else None


def handle(payload: dict, env: dict[str, str] | None = None, deadline: float = DEADLINE):
    """What to say, or None, for one PreToolUse payload."""
    end = time.monotonic() + deadline
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    directory = test_directory(command, Path(payload.get("cwd") or os.getcwd()))
    if directory is None:
        return None
    env = {**(os.environ if env is None else env), "GIT_TERMINAL_PROMPT": "0"}
    return protect(directory, env, end)


def main() -> int:
    try:
        out = handle(json.load(sys.stdin))
    except Exception as error:  # the tests run whatever happens here
        out = report(
            f"The pre-test commit hook failed: {error}",
            f"The pre-test commit hook failed, so it may not have committed or pushed: {error!r}."
            " The tests run anyway.",
        )
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
