"""tests/collected.py: what pytest collects for a command line, worked out once per run."""

from tests.collected import collect
from tests.hang_guard import HOST_GUARD


def test_a_test_is_collected_with_its_markers_fixtures_and_hang_guard():
    """The selection on the command line honoured; the settings pytest-timeout gives a test,
    from pyproject."""
    this = "tests/test_collected.py::test_uses_tmp_path"
    found = collect(["tests/test_collected.py"])
    assert len(found) > 1 and this in found
    assert "tmp_path" in found[this]["fixtures"]
    expected = {"timeout": HOST_GUARD, "method": "thread", "func_only": False}
    assert found[this]["hang_guard"] == expected
    assert list(collect(["tests/test_collected.py", "-k", "uses_tmp_path"])) == [this]


def test_uses_tmp_path(tmp_path):
    assert tmp_path.is_dir()


def test_a_collection_is_worked_out_once_among_the_processes_sharing_a_folder(
    tmp_path, monkeypatch
):
    """The second to ask for the same command line reads what the first found; another
    command line is worked out again."""
    from tests import collected

    runs, run = [], collected.subprocess.run

    def counting(argv, **kwargs):
        runs.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(collected.subprocess, "run", counting)
    monkeypatch.setattr(collected, "shared", tmp_path / "shared")
    args = ("tests/test_collected.py", "-k", "uses_tmp_path")
    first = collected._shared_collect(args, ())
    assert collected._shared_collect(args, ()) == first and len(runs) == 1
    other = collected._shared_collect(("tests/test_collected.py", "-k", "markers"), ())
    assert len(runs) == 2 and other != first
