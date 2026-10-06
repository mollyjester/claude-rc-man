"""tests/collected.py: what pytest collects for a command line."""

from tests.collected import _selection_args, child_env, collect


def test_a_test_is_collected_with_its_markers_and_fixtures():
    found = collect(["tests/test_collected.py"])
    this = "tests/test_collected.py::test_a_test_is_collected_with_its_markers_and_fixtures"
    assert this in found
    assert "tmp_path" in found["tests/test_collected.py::test_uses_tmp_path"]["fixtures"]


def test_uses_tmp_path(tmp_path):
    assert tmp_path.is_dir()


def test_the_selection_on_the_command_line_is_honoured():
    every = collect(["tests/test_collected.py"])
    one = collect(["tests/test_collected.py", "-k", "uses_tmp_path"])
    assert len(every) > 1 and list(one) == ["tests/test_collected.py::test_uses_tmp_path"]


def test_the_configured_deselection_applies():
    """pyproject's addopts leave real_claude out, as in a run; asked for, it is there."""
    plain = collect(["tests/test_claude_real_account.py"])
    asked = collect(["tests/test_claude_real_account.py", "-m", "real_claude"])
    assert asked and all("real_claude" in t["markers"] for t in asked.values())
    assert plain == {}


def test_options_that_only_choose_how_tests_run_are_dropped():
    args = ("-n", "auto", "--dist", "loadfile", "-m", "x", "--dist=load", "-n4", "tests")
    assert _selection_args(args) == ["-m", "x", "tests"]
    assert _selection_args(("-n=2", "--numprocesses", "3", "-k", "y")) == ["-k", "y"]


def test_a_run_is_steered_only_by_what_the_caller_gives(monkeypatch):
    monkeypatch.setenv("RCMAN_REQUIRE_CONTAINER", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    env = child_env({"RCMAN_X": "1"})
    assert "RCMAN_REQUIRE_CONTAINER" not in env and "PYTEST_XDIST_WORKER" not in env
    assert env["RCMAN_X"] == "1" and env["PATH"]


def test_collection_writes_down_what_it_found_in_a_held_folder(monkeypatch):
    """So a run killed while collecting leaves it for the next run to clear."""
    from pathlib import Path

    from devtools import held_folders as hf
    from tests import collected

    seen, run = [], collected.subprocess.run

    def running(argv, **kwargs):
        folder = Path(argv[3]).parent
        lock = folder / hf.LOCK
        seen.append(
            hf.made_with("rccol-")(folder.name) and lock.is_file() and not hf.unheld(folder)
        )
        return run(argv, **kwargs)

    monkeypatch.setattr(collected.subprocess, "run", running)
    found = collected._collect.__wrapped__(("tests/test_collected.py", "-k", "uses_tmp_path"), ())
    assert list(found) == ["tests/test_collected.py::test_uses_tmp_path"]
    assert seen == [True]
