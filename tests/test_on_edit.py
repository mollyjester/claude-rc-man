"""The PostToolUse hook that runs a file's paired test when the file changes."""

import json
import subprocess
import sys
from pathlib import Path

from devtools import on_edit

REPO = Path(__file__).resolve().parent.parent

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False, 'planted failure'\n"


def make_root(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def payload(root: Path, rel: str) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_input": {"file_path": str(root / rel)}}


# --- pairing ------------------------------------------------------------------


def test_a_module_a_test_and_markdown_have_their_pairs_and_another_file_none(tmp_path):
    guide = "docs/guide" + ".md"  # built so this file holds no dangling reference
    root = make_root(
        tmp_path,
        {"devtools/thing.py": "", "tests/test_thing.py": PASSING, guide: "", "pyproject.toml": ""},
    )
    test = root / "tests/test_thing.py"
    assert on_edit.paired_test(root, root / "devtools/thing.py") == test
    assert on_edit.paired_test(root, test) == test
    assert on_edit.paired_test(root, root / guide) == root / "tests/test_docs_guard.py"
    assert on_edit.paired_test(root, root / "pyproject.toml") is None


# --- running --------------------------------------------------------------------


def test_failing_pair_is_reported_to_user_and_agent_and_a_passing_one_is_silent(tmp_path):
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": PASSING})
    assert on_edit.handle(payload(root, "devtools/thing.py"), root) is None
    make_root(tmp_path, {"tests/test_thing.py": FAILING})

    out = on_edit.handle(payload(root, "devtools/thing.py"), root)

    assert out is not None
    assert "tests/test_thing.py" in out["systemMessage"]
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "planted failure" in context


def test_missing_pair_is_reported_not_skipped(tmp_path):
    root = make_root(tmp_path, {"devtools/lonely.py": ""})

    out = on_edit.handle(payload(root, "devtools/lonely.py"), root)

    assert out is not None
    assert "tests/test_lonely.py" in out["systemMessage"]


# --- worktrees ------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def make_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout whose paired test passes, and a worktree of it under
    .claude/worktrees/ whose paired test fails, as Claude Code lays them out.
    The hook is started from the main checkout, as the wiring starts it."""
    main = make_root(tmp_path / "main", {"devtools/thing.py": "", "tests/test_thing.py": PASSING})
    git(main, "init", "-q")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "start")
    worktree = main / ".claude" / "worktrees" / "agent"
    git(main, "worktree", "add", "-q", "-b", "agent", str(worktree))
    make_root(worktree, {"tests/test_thing.py": FAILING})
    return main, worktree


def test_the_script_started_from_the_main_checkout_runs_the_worktrees_test_on_its_own_code(
    tmp_path,
):
    """Through the real invocation path, stdin JSON in and stdout JSON out: the worktree's test
    runs, and imports the worktree's own code first, though the hook's Python may have the main
    checkout's packages installed."""
    main, worktree = make_worktree(tmp_path)
    probe = (
        "import helpers, rcman\n"
        "def test_own_code():\n"
        f"    assert rcman.__file__.startswith({str(worktree)!r}), rcman.__file__\n"
        f"    assert helpers.__file__.startswith({str(worktree)!r}), helpers.__file__\n"
    )
    make_root(
        worktree,
        {
            "pyproject.toml": (REPO / "pyproject.toml").read_text(encoding="utf-8"),
            "backend/rcman/__init__.py": "",
            "helpers/__init__.py": "",
            "tests/test_thing.py": probe + FAILING,
        },
    )
    proc = subprocess.run(
        [sys.executable, str(REPO / "devtools" / "on_edit.py"), "--root", str(main)],
        input=json.dumps(payload(worktree, "devtools/thing.py")),
        capture_output=True,
        text=True,
        check=True,
    )
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "planted failure" in context and "test_own_code" not in context, context


# --- wiring ---------------------------------------------------------------------


def test_settings_route_file_edits_to_the_hook():
    """What is enforced is what the wiring hands over, not what the script can do."""
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entries = settings["hooks"]["PostToolUse"]
    wired = [
        (entry["matcher"], hook["command"])
        for entry in entries
        for hook in entry["hooks"]
        if "devtools/on_edit.py" in hook["command"]
    ]
    assert wired, "no PostToolUse hook runs devtools/on_edit.py"
    matchers = {tool for matcher, _ in wired for tool in matcher.split("|")}
    assert {"Edit", "Write"} <= matchers


# --- frontend ---------------------------------------------------------------------


def test_a_frontend_module_pairs_with_its_test_run_with_vitest(tmp_path, monkeypatch):
    root = make_root(tmp_path, {"frontend/src/Thing.tsx": "", "frontend/src/Thing.test.tsx": ""})
    seen = {}

    def fake_run(argv, cwd, **kw):
        seen["argv"], seen["cwd"] = argv, cwd
        return subprocess.CompletedProcess(argv, 1, stdout="planted vitest failure", stderr="")

    monkeypatch.setattr(on_edit.subprocess, "run", fake_run)
    out = on_edit.handle(payload(root, "frontend/src/Thing.tsx"), root)
    assert seen["argv"] == ["npx", "vitest", "run", "src/Thing.test.tsx"]
    assert seen["cwd"] == root / "frontend"
    assert "planted vitest failure" in out["hookSpecificOutput"]["additionalContext"]
