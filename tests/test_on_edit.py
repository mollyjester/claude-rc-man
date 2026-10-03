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


def test_module_pairs_with_its_test(tmp_path):
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": PASSING})
    assert on_edit.paired_test(root, root / "devtools/thing.py") == root / "tests/test_thing.py"


def test_test_pairs_with_itself(tmp_path):
    root = make_root(tmp_path, {"tests/test_thing.py": PASSING})
    path = root / "tests/test_thing.py"
    assert on_edit.paired_test(root, path) == path


def test_markdown_pairs_with_the_docs_guard(tmp_path):
    guide = "docs/guide" + ".md"  # built so this file holds no dangling reference
    root = make_root(tmp_path, {guide: "", "tests/test_docs_guard.py": PASSING})
    assert on_edit.paired_test(root, root / guide) == root / "tests/test_docs_guard.py"


def test_unrelated_file_has_no_pair(tmp_path):
    root = make_root(tmp_path, {"pyproject.toml": ""})
    assert on_edit.paired_test(root, root / "pyproject.toml") is None


def test_file_outside_the_root_has_no_pair(tmp_path):
    root = make_root(tmp_path / "repo", {"tests/test_x.py": PASSING})
    assert on_edit.paired_test(root, tmp_path / "elsewhere" / "x.py") is None


# --- running --------------------------------------------------------------------


def test_passing_pair_is_silent(tmp_path):
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": PASSING})
    assert on_edit.handle(payload(root, "devtools/thing.py"), root) is None


def test_failing_pair_is_reported_to_user_and_agent(tmp_path):
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": FAILING})

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


def test_payload_without_a_path_is_silent(tmp_path):
    assert on_edit.handle({"hook_event_name": "PostToolUse", "tool_input": {}}, tmp_path) is None


def test_script_prints_json_on_failure(tmp_path):
    """Through the real invocation path: stdin JSON in, stdout JSON out."""
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": FAILING})
    proc = subprocess.run(
        [sys.executable, str(REPO / "devtools" / "on_edit.py"), "--root", str(root)],
        input=json.dumps(payload(root, "devtools/thing.py")),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "planted failure" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_script_prints_nothing_on_success(tmp_path):
    root = make_root(tmp_path, {"devtools/thing.py": "", "tests/test_thing.py": PASSING})
    proc = subprocess.run(
        [sys.executable, str(REPO / "devtools" / "on_edit.py"), "--root", str(root)],
        input=json.dumps(payload(root, "devtools/thing.py")),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == ""


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
    make_root(worktree, {"devtools/lonely.py": "", "tests/test_thing.py": FAILING})
    return main, worktree


def test_an_edit_in_a_worktree_runs_that_worktrees_paired_test(tmp_path):
    main, worktree = make_worktree(tmp_path)

    out = on_edit.handle(payload(worktree, "devtools/thing.py"), main)

    assert out is not None
    assert "planted failure" in out["hookSpecificOutput"]["additionalContext"]


def test_a_worktree_file_is_paired_within_the_worktree(tmp_path):
    main, worktree = make_worktree(tmp_path)
    make_root(main, {"tests/test_lonely.py": PASSING})  # the main checkout's, not the worktree's

    out = on_edit.handle(payload(worktree, "devtools/lonely.py"), main)

    assert out is not None
    assert out["systemMessage"] == (
        f"{worktree / 'devtools/lonely.py'} has no paired test: expected tests/test_lonely.py"
    )


def test_a_worktrees_test_imports_the_worktrees_own_code(tmp_path):
    """The hook's Python may have the main checkout's packages installed; the
    worktree's own come first."""
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
            "tests/test_thing.py": probe,
        },
    )

    assert on_edit.handle(payload(worktree, "devtools/thing.py"), main) is None


def test_the_script_started_from_the_main_checkout_runs_the_worktrees_test(tmp_path):
    main, worktree = make_worktree(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(REPO / "devtools" / "on_edit.py"), "--root", str(main)],
        input=json.dumps(payload(worktree, "devtools/thing.py")),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "planted failure" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


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


def test_frontend_module_pairs_with_its_test(tmp_path):
    root = make_root(tmp_path, {"frontend/src/Thing.tsx": "", "frontend/src/Thing.test.tsx": ""})
    assert (
        on_edit.paired_test(root, root / "frontend/src/Thing.tsx")
        == root / "frontend/src/Thing.test.tsx"
    )


def test_frontend_ts_module_pairs_too(tmp_path):
    root = make_root(tmp_path, {"frontend/src/api.ts": "", "frontend/src/api.test.ts": ""})
    assert (
        on_edit.paired_test(root, root / "frontend/src/api.ts") == root / "frontend/src/api.test.ts"
    )


def test_frontend_test_pairs_with_itself(tmp_path):
    root = make_root(tmp_path, {"frontend/src/Thing.test.tsx": ""})
    path = root / "frontend/src/Thing.test.tsx"
    assert on_edit.paired_test(root, path) == path


def test_frontend_test_runs_with_vitest(tmp_path, monkeypatch):
    root = make_root(tmp_path, {"frontend/src/Thing.tsx": "", "frontend/src/Thing.test.tsx": ""})
    seen = {}

    def fake_run(argv, cwd, **kw):
        import subprocess

        seen["argv"], seen["cwd"] = argv, cwd
        return subprocess.CompletedProcess(argv, 1, stdout="planted vitest failure", stderr="")

    monkeypatch.setattr(on_edit.subprocess, "run", fake_run)
    out = on_edit.handle(payload(root, "frontend/src/Thing.tsx"), root)
    assert seen["argv"] == ["npx", "vitest", "run", "src/Thing.test.tsx"]
    assert seen["cwd"] == root / "frontend"
    assert "planted vitest failure" in out["hookSpecificOutput"]["additionalContext"]


def test_a_frontend_module_without_a_test_is_not_reported(tmp_path):
    """Components are covered through the screens that use them; only paired files run."""
    root = make_root(tmp_path, {"frontend/src/main.tsx": ""})
    assert on_edit.handle(payload(root, "frontend/src/main.tsx"), root) is None
