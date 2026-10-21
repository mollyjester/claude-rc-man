"""helpers/instructions.py: which file of the instructions for Claude a workspace is told, and
where an image's managed path refuses the bind of it."""

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
