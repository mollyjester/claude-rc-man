"""helpers/instructions.py: which file of the instructions for Claude a workspace is told, and
where an image's managed path refuses the bind of it, and what the files say of forwarded ports."""

from pathlib import Path

import pytest

from helpers import instructions as told

ETC, FOLDER_OF, MANAGED = told.PATHS


def test_the_managed_path_is_claude_codes_and_its_folders_are_the_paths_looked_at():
    assert MANAGED == "/etc/claude-code/CLAUDE.md"
    assert (ETC, FOLDER_OF) == ("/etc", "/etc/claude-code")


@pytest.mark.parametrize(
    ("default_image", "labels", "named"),
    [
        (True, {told.TOOLS_LABEL: "1"}, told.DEFAULT_TOOLS),
        (True, {"org.example": "1"}, told.DEFAULT_IMAGE),
        (True, {}, told.DEFAULT_IMAGE),
        # an image of its own that carries the label: one built from the default image
        (False, {told.TOOLS_LABEL: "1"}, told.DEFAULT_TOOLS),
        (False, {"org.example": "1"}, told.OWN_IMAGE),
        (False, {}, told.OWN_IMAGE),
    ],
)
def test_the_file_a_workspace_is_told_follows_its_image_and_the_tools_label(
    default_image, labels, named
):
    assert told.file_for(default_image, labels) == named


@pytest.mark.parametrize(
    ("kinds", "refused"),
    [
        ({}, None),
        ({ETC: told.IS_FOLDER}, None),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FOLDER}, None),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FOLDER, MANAGED: told.IS_FILE}, None),
        ({ETC: told.IS_FILE}, ETC),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FILE}, FOLDER_OF),
        # only a file refuses a folder of the path: what else is there is not judged
        ({ETC: told.IS_LINK}, None),
        ({ETC: told.IS_OTHER}, None),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_LINK}, None),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_OTHER}, None),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FOLDER, MANAGED: told.IS_FOLDER}, MANAGED),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FOLDER, MANAGED: told.IS_LINK}, MANAGED),
        ({ETC: told.IS_FOLDER, FOLDER_OF: told.IS_FOLDER, MANAGED: told.IS_OTHER}, MANAGED),
    ],
)
def test_what_refuses_the_bind_is_a_file_for_a_folder_or_a_file_that_is_not_regular(kinds, refused):
    assert told.refused_at(kinds) == refused


FRAGMENTS = Path(__file__).resolve().parent.parent / "backend" / "rcman" / told.FOLDER


@pytest.mark.parametrize("name", sorted(told.FILES))
def test_every_workspace_is_told_how_its_forwarded_ports_are_reached_and_where_to_listen(name):
    text = told.compose(FRAGMENTS, name)
    assert "/rcman/work" in text  # positive control: the file was made from its fragments
    flat = " ".join(text.split())
    assert "forwardPorts" in flat
    assert "the address the workspace page shows" in flat
    assert "an administrator has turned forwarding on" in flat  # true whether it is on or off
    assert "127.0.0.1" in flat and "0.0.0.0" in flat and "IPv6" in flat
    assert "`-p`, `--publish` and `-P` publish nothing" in flat
    assert "No port of this workspace can be reached" not in flat  # forwarded ports can be
