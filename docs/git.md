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
- **Remove token** takes effect the same way: rcman empties the credential file
  it keeps for you, so git, which reads the token from there, has none from its
  next command on, and a push to GitHub is refused for want of one. The token
  still works on GitHub until you revoke it there. That is the whole of what
  rcman forgets: if you set git up inside a workspace to use another credential
  helper (`credential.helper store` in `~/.gitconfig`, say), what that helper
  saved is yours, in your files, and rcman neither knows of it nor removes it.
  A helper the workspace's image sets for the whole machine is not used for
  GitHub, so that it cannot keep the token where rcman does not forget it.
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

A workspace made without a GitHub address is empty, and an empty workspace is
not a git repository: the section says so quietly, without an error. Claude can
run `git init` there, and the section shows the repository once it exists.

## Making a workspace from GitHub

Give a GitHub address under **New workspace** (see
[Workspaces](workspaces.md)) and rcman clones that repository into the new
workspace, with git, as you: all its branches, and the default one checked out.
The workspace is ready once the clone is done, and its **Changes** section
shows the repository.

- A private repository needs your GitHub token, set in [Settings](settings.md):
  the clone uses it as a push does. Without one, or with one that GitHub
  refuses or that may not read the repository, the workspace fails and says
  which.
- A workspace whose clone fails stays, with the reason in plain words, what
  git said, and the steps rcman took. The reasons are: the clone took too
  long and was stopped; GitHub asked for a sign-in (the repository is private,
  or the address is wrong); GitHub did not accept your token; GitHub could not
  find the repository; rcman could not reach GitHub from the workspace.
  **Delete workspace** removes it; make it again once the cause is mended.
- The panel does not show git's progress. **Delete workspace** stops a clone
  that is running, and deletes the workspace.
- If rcman restarts while a workspace is being made, the workspace fails and
  says so. Delete it and make it again.
- Your token is never in the repository, in the steps shown, or in the error.
- A repository's `devcontainer.json` may list commands that run in the new
  workspace (see [devcontainer.json support](devcontainer.md#lifecycle-commands)).
  Like any command there, they can read your token, which git keeps in the
  credential file all your workspaces share, so make workspaces only from
  repositories you trust.

## Images

A workspace needs git in its image, new enough for rcman's use of it. The
default image has it. For another image
([`RCMAN_WORKSPACE_IMAGE`](configuration.md)), a workspace whose image has no
git, or an older one, fails to be made, and says why in its panel. Cloning from
GitHub also needs git there, and a way to reach github.com from the workspace.
An image a repository's `devcontainer.json` names, or builds, is held to the same
requirement (see [devcontainer.json support](devcontainer.md#the-image)). A build
is made from a copy of the repository's files without its top-level git folder
(see [Building an image](devcontainer.md#building-an-image)).
