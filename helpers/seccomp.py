"""The seccomp profile of every workspace container: Podman's own default, with the two calls a
container nested in a workspace makes to name its own UTS namespace allowed whatever the
capabilities, as Podman's default allows them only with CAP_SYS_ADMIN, which a workspace has not.

The installer writes it into the tools (installer/install.py ensure_podman_tools); the service
checks at its start that the one there is still the one derived from Podman's current default
(rcman/main.py), since an upgrade of Podman's profile leaves the tools' as they were. Standard
library only: the installer imports it.
"""

from __future__ import annotations

import json
from pathlib import Path

# Podman's own default profile, from containers-common.
PODMAN_DEFAULT = Path("/usr/share/containers/seccomp.json")
# Its name in the tools (rcman/runtime.py SECCOMP).
NAME = "seccomp.json"
UTS_CALLS = ("sethostname", "setdomainname")


def workspace_profile(default: str) -> str:
    """The workspaces' profile, as text, from the text of Podman's ``default``: every rule as it
    is but for UTS_CALLS, and one rule that allows exactly them with no condition. The kernel
    still refuses them in the workspace's own UTS namespace."""
    profile = json.loads(default)
    rules = []
    for rule in profile["syscalls"]:
        names = [n for n in rule.get("names", []) if n not in UTS_CALLS]
        if names:
            rules.append({**rule, "names": names})
    rules.append(
        {
            "names": list(UTS_CALLS),
            "action": "SCMP_ACT_ALLOW",
            "args": [],
            "comment": "rcman: a container nested in a workspace names its own UTS namespace",
            "includes": {},
            "excludes": {},
        }
    )
    return json.dumps({**profile, "syscalls": rules}, indent=1) + "\n"


def stale(tools: Path, default: Path = PODMAN_DEFAULT) -> str | None:
    """Why the profile in ``tools`` is not the one derived from ``default`` now, or None."""
    installed = tools / NAME
    try:
        wanted = workspace_profile(default.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as error:
        return f"Podman's seccomp profile {default} cannot be read ({error})"
    try:
        found = installed.read_text(encoding="utf-8")
    except OSError:
        return f"the workspaces' seccomp profile {installed} is not there"
    if found != wanted:
        return (
            f"the workspaces' seccomp profile {installed} is not the one derived from "
            f"Podman's current {default}"
        )
    return None
