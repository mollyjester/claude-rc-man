# Settings

The **Settings** page holds your own settings. The settings administrators
give rcman itself are in [Configuration](configuration.md).

| Setting | What it sets |
|---|---|
| **Session name prefix** (`prefix`) | How the names of your new Remote Control sessions begin, in claude.ai and the Claude app. Until you set one, it is your Linux user name. |
| **Commit author name** (`author_name`) | The name recorded as the author of commits made from rcman. Once set, it can be changed but not removed. |
| **Commit author email** (`author_email`) | The address recorded with that name. Leave it empty to use the address you sign in with. |

The page is in two parts, **Session names** and **Commit author**, and each
is saved on its own with its own **Save**. Below the commit author, the page
shows the name and address commits are recorded as, or that none is set.

When rcman refuses a value, it says which one and why, next to the part you
saved.
