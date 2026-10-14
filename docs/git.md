# Git

Every workspace has git: the default image has it, and Claude Code's sessions
use it there as in any terminal — for a commit, a branch or a push of their
own. Each workspace is its own machine as far as git is concerned: its own
repositories and its own git configuration.

rcman's own git features are not available in workspaces yet: the **Changes**
section, cloning a repository from GitHub into a workspace, and storing a GitHub
token are not offered, and rcman answers their requests that it cannot do this
with workspaces yet. A workspace is made empty.

The commit author you set in [Settings](settings.md) is kept for when
rcman makes commits in workspaces.
