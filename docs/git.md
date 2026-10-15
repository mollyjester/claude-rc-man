# Git

Every workspace has git: the default image has it, and Claude Code's sessions
use it there as in any terminal — for a commit, a branch or a push of their
own. Each workspace is its own machine as far as git is concerned: its own
repositories. What it shares with your other workspaces is who you are to git,
below.

## Your commit author and GitHub token

The **commit author** and the **GitHub token** you set in
[Settings](settings.md) are written into git's configuration in all your
workspaces, and apply to every git command there: rcman's own and Claude's,
alike. Commits are recorded as the author, and git pushes to GitHub as you with
the token, without asking for a password.

- A change in Settings reaches your workspaces at once, running or not: the
  next git command in any of them uses it. No server is restarted and no
  session is cut off.
- **Remove token** takes effect the same way: git has no token from its next
  command on, and a push to GitHub is refused for want of one. The token still
  works on GitHub until you revoke it there.
- git only ever reads the stored token; it does not rewrite it. If GitHub
  refuses the token, git keeps it, and the Settings page still says one is
  stored.
- rcman owns the file that holds the author and the credential setting. If you
  run `git config --global` in a workspace, what you set is replaced the next
  time you save the commit author in Settings. To use another author in one
  repository, set it in that repository (`git config user.name`), or in
  `~/.gitconfig` in the workspace: either overrides the author from Settings.

## Changes

The panel of every workspace that is ready has a **Changes** section for the
git repository at the top of the workspace's folder: the branch, the files
changed and not yet staged, and the files staged, with the means to stage and
unstage files, commit, switch or make a branch, fetch, and push. Everything it
does is done in that workspace, as the commit author and with the token above.

- **Commit** needs something staged and a commit author name; the section
  says so otherwise and links to Settings.
- **Push** to GitHub needs a GitHub token. While none is stored, the section
  withdraws Push and says to add one in Settings; **Commit & Push** commits
  nothing in that case, as the push could not follow.
- When git refuses, the section shows what it said, beside what you tried.

A new workspace is empty, and an empty workspace is not a git repository: the
section says so quietly, without an error. Claude can run `git init` there, and the
section shows the repository once it exists.
Cloning a repository from GitHub into a workspace is not something rcman does
yet; ask Claude to clone it, which uses your token for a private one.

## Images

A workspace needs git in its image, new enough for rcman's use of it. The
default image has it. For another image
([`RCMAN_WORKSPACE_IMAGE`](configuration.md)), a workspace whose image has no
git, or an older one, fails to be made, and says why in its panel.
