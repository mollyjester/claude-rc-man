"""What Claude is told in a workspace: a file of rcman's, bound read-only at Claude Code's
managed path MANAGED in every workspace container, which Claude Code reads into every session
and which a session cannot leave out.

The file is made of fragments, in backend/rcman/instructions (FOLDER): COMMON, for every
workspace; GH, for one of the default image, which has gh signed in with the person's token;
TOOLS, with GH, for one whose image carries TOOLS_LABEL, the default image or one built from
it. The installer writes each file of
FILES, its fragments in order (``compose``), into the folder FOLDER of the tools
(installer/install.py ensure_instructions), and the runtime binds the one ``file_for`` names
(rcman/runtime.py).

TOOLS_LABEL is the default image's label (installer/workspace/Containerfile): an image that
carries it, the default image or one built from it, has the tools the default image has. It is
not one of rcman's own labels (rcman.*), which an image is refused for carrying.

An image that has a file where a folder of MANAGED would be, or MANAGED as anything but a
regular file, is refused: ``refused_at`` says where, from what its probe read there
(rcman/runtime.py image_facts), which reads a link as what it names. Standard library only:
the installer imports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

MANAGED = "/etc/claude-code/CLAUDE.md"
# The folders MANAGED is in, outermost first, and MANAGED itself.
PATHS = ("/etc", "/etc/claude-code", MANAGED)

TOOLS_LABEL = "org.claude-rc-man.tools"

# The fragments' folder in backend/rcman, and the folder of the tools the files are written in.
FOLDER = "instructions"
COMMON, GH, TOOLS = "common.md", "gh.md", "tools.md"
# The files, each with its fragments in order: for a workspace of an image of its own (from its
# devcontainer.json), of a default image without TOOLS_LABEL, and of any image that carries it.
# Each is bound at MANAGED, whose name is what Claude Code goes by.
OWN_IMAGE, DEFAULT_IMAGE, DEFAULT_TOOLS = "own-image", "default-image", "default-tools"
FILES = {
    OWN_IMAGE: (COMMON,),
    DEFAULT_IMAGE: (COMMON, GH),
    DEFAULT_TOOLS: (COMMON, GH, TOOLS),
}

# What a path of an image is, as its probe reads it; a path the image does not have is not read
# as anything.
IS_FOLDER, IS_FILE, IS_LINK, IS_OTHER = "folder", "file", "link", "other"


def compose(fragments: Path, name: str) -> str:
    """The text of the file ``name`` of FILES: its fragments, from the folder ``fragments``, in
    order, a blank line between each and the next."""
    return "\n".join((fragments / part).read_text(encoding="utf-8") for part in FILES[name])


def file_for(default_image: bool, labels: Mapping[str, str]) -> str:
    """The file of FILES a workspace is told: of the default image or not, whose image has
    ``labels``. Any image that carries TOOLS_LABEL, the default image, named by a
    devcontainer.json too, or an image built from it elsewhere and pulled, is told everything;
    a default image without it, an older one, of gh too; any other image, the common text
    alone."""
    if TOOLS_LABEL in labels:
        return DEFAULT_TOOLS
    return DEFAULT_IMAGE if default_image else OWN_IMAGE


def refused_at(kinds: Mapping[str, str]) -> str | None:
    """The first of PATHS where an image whose probe read ``kinds`` (a path to what it is, for
    each one it has) has what MANAGED is not bound over: a file where a folder of MANAGED would
    be, or MANAGED as anything but a regular file. Only a file is refused where a folder would
    be: anything else there is not judged. None where there is none."""
    *folders, managed = PATHS
    for path in folders:
        if kinds.get(path) == IS_FILE:
            return path
    if managed in kinds and kinds[managed] != IS_FILE:
        return managed
    return None
