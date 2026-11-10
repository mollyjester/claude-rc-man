"""helpers/instructions.py: which file of the instructions for Claude a workspace is told, and
what the files say of forwarded ports."""

from pathlib import Path

from helpers import instructions as told


def test_the_file_a_workspace_is_told_follows_its_image_and_the_tools_label():
    assert told.file_for(True, {told.TOOLS_LABEL: "1"}) == told.DEFAULT_TOOLS


FRAGMENTS = Path(__file__).resolve().parent.parent / "backend" / "rcman" / told.FOLDER


def test_every_workspace_is_told_how_its_forwarded_ports_are_reached_and_where_to_listen():
    for name in sorted(told.FILES):
        text = told.compose(FRAGMENTS, name)
        assert "/rcman/work" in text  # positive control: the file was made from its fragments
        flat = " ".join(text.split())
        assert "forwardPorts" in flat
        assert "the address the workspace page shows" in flat
        assert "an administrator has turned forwarding on" in flat  # true whether it is on or off
        assert "127.0.0.1" in flat and "0.0.0.0" in flat and "IPv6" in flat
        assert "`-p`, `--publish` and `-P` publish nothing" in flat
        assert "No port of this workspace can be reached" not in flat  # forwarded ports can be
