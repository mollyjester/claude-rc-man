# Settings

The **Settings** page holds your own settings. The settings administrators
give rcman itself are in [Configuration](configuration.md).

| Setting | What it sets |
|---|---|
| **Session name prefix** (`prefix`) | How the names of your new Remote Control sessions begin, in claude.ai and the Claude app. Leave it empty to begin each with the name of the workspace its server runs in. |
| **Commit author name** (`author_name`) | The name recorded as the author of commits made from rcman and by git in your workspaces; once set, it can be changed but not removed. |
| **Commit author email** (`author_email`) | The address recorded with that name. Leave it empty to use the address you sign in with. |

The page groups related settings into parts, and each part is saved on its
own with its own **Save**. Under the commit author, the page shows the name
and address commits are recorded as, or that none is set. The commit author
applies to commits made from rcman and to those git makes in your workspaces,
Claude's among them; Commit in rcman's **Changes** section needs a name.

The **GitHub token** part says whether a token is stored, and takes a GitHub
personal access token, which it never shows again once saved. Saving one
replaces any stored before. **Remove token** asks first, in a dialog, then forgets it: rcman empties the files
it keeps the token in for git and gh, and nothing a credential helper you set up yourself in a
workspace saved elsewhere (see [Git](git.md)). The
token is what git uses for GitHub, to push, and to clone, fetch and pull private
repositories as you, in all your workspaces and for Claude's git as well as rcman's;
and what `gh` uses there, for GitHub's issues, pull requests and API (see
[gh](git.md#gh)).

Saving the commit author or the token, and removing the token, reach all your
workspaces at once, running or not: the next git or gh command uses them, and no
server is restarted. How they look from inside a workspace, and what a
`git config --global` there does, is in [Git](git.md).

When rcman refuses a value, it says which one and why, next to the part you
saved.
