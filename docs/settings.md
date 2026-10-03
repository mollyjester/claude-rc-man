# Settings

The **Settings** page holds your own settings. The settings administrators
give rcman itself are in [Configuration](configuration.md).

| Setting | What it sets |
|---|---|
| **Session name prefix** (`prefix`) | How the names of your new Remote Control sessions begin, in claude.ai and the Claude app. Until you set one, it is your Linux user name. |
| **Commit author name** (`author_name`) | The name recorded as the author of commits made from rcman (see [Git](git.md#commit-author)); until it is set, rcman offers no commit. Once set, it can be changed but not removed. |
| **Commit author email** (`author_email`) | The address recorded with that name (see [Git](git.md#commit-author)). Leave it empty to use the address you sign in with. |

The page groups related settings into parts, and each part is saved on its
own with its own **Save**. Under the commit author, the page shows the name
and address commits are recorded as, or that none is set.

The **GitHub token** part stores the token git uses to push to GitHub as
you, from rcman and from your Claude sessions; what it is for, where it is
kept and how to remove it are in [Git](git.md#github-token). The part says
whether a token is stored, never the token itself: its field is always
empty, and **Save** stores what you type in place of any token stored
before. **Remove token**, offered while one is stored, asks before it
removes it.

When rcman refuses a value, it says which one and why, next to the part you
saved.
