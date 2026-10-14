# Settings

The **Settings** page holds your own settings. The settings administrators
give rcman itself are in [Configuration](configuration.md).

| Setting | What it sets |
|---|---|
| **Session name prefix** (`prefix`) | How the names of your new Remote Control sessions begin, in claude.ai and the Claude app. Leave it empty to begin each with the name of the workspace its server runs in. |
| **Commit author name** (`author_name`) | The name recorded as the author of commits made from rcman; once set, it can be changed but not removed. |
| **Commit author email** (`author_email`) | The address recorded with that name. Leave it empty to use the address you sign in with. |

The page groups related settings into parts, and each part is saved on its
own with its own **Save**. Under the commit author, the page shows the name
and address commits are recorded as, or that none is set. rcman makes no
commits in workspaces yet: the commit author is kept for when it does.

The **GitHub token** part says that rcman cannot store a GitHub token in
workspaces yet, and offers nothing else (see [Git](git.md)).

When rcman refuses a value, it says which one and why, next to the part you
saved.
