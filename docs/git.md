# Git

Every workspace has git: the default image has it, and Claude Code's sessions
use it there as in any terminal — for a commit, a branch or a push of their
own. Each workspace is its own machine as far as git is concerned: its own
repositories. What it shares with your other workspaces is who you are to git,
below. The default image also has `gh`, GitHub's command-line tool, which uses
the same GitHub token (see [gh](#gh)).

## Your commit author and GitHub token

The **commit author** and the **GitHub token** you set in
[Settings](settings.md) are written into git's configuration in all your
workspaces, and apply to every git command there: rcman's own and Claude's,
alike. Commits are recorded as the author, and git clones, fetches, pulls and
pushes on GitHub as you with the token, without asking for a password.

- A change in Settings reaches your workspaces at once, running or not: the
  next git or gh command in any of them uses it. No server is restarted and no
  session is cut off.
- **Remove token** takes effect the same way: rcman empties the files it keeps
  the token in for git and for gh, so git and gh, which read it from there,
  have none from their next command on, and a push to GitHub is refused for
  want of one. The token
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

## gh

`gh`, in the default image, uses your GitHub token from Settings in all your
workspaces: in the **Terminal** tab, in Claude's sessions, and in a
repository's lifecycle commands. `gh issue create`, `gh pr create` or
`gh api` work there as you, with no flags and no `gh auth login`. Claude's sessions in a
workspace of the default image, or of another image that carries its label, are told that gh is there and
to use it for GitHub work (see
[What Claude is told in a workspace](workspaces.md#what-claude-is-told-in-a-workspace)).
rcman points gh at the folder that holds git's configuration,
`~/.config/git` (`GH_CONFIG_DIR` names it), and writes gh's configuration and
the token there, beside git's.

- The token reaches gh as it reaches git: a change in Settings, or **Remove
  token**, applies from the next gh command on, and nothing is restarted.
- Only git and gh get the token. `curl` and other programs that call GitHub's
  API get none from rcman; make such a call with `gh api`. A token you copy
  out of gh, with `gh auth token` say, is yours: **Remove token** does not
  forget a copy.
- rcman owns gh's files in that folder. What gh writes there itself, as
  `gh auth login`, `gh auth logout`, `gh auth switch`, `gh config set` and
  `gh alias set` do, does not last: gh's configuration is written again each
  time you save the token and each time rcman starts, and its token each time
  you save or remove the token and each time rcman starts while one is stored.
- `gh auth setup-git` writes a credential helper into `~/.gitconfig`, which
  comes after rcman's, so git still uses the token from Settings.
- `gh auth status` names no account, since rcman does not know your GitHub
  login; `gh api user` asks GitHub whose the token is.
- If gh's configuration file, `config.yml`, is removed from `~/.config/git`
  while a token is stored, every gh command fails, saying `couldn't get user
  name`, until you save the token again or rcman starts again.
- A repository's `devcontainer.json` that sets `GH_TOKEN` or `GITHUB_TOKEN`
  gives gh that token in place of yours, and **Remove token** does not remove
  it. It may not set `GH_CONFIG_DIR` (see
  [devcontainer.json support](devcontainer.md#environment)).

## Changes

The panel of every workspace that is ready has, on its **Overview** tab, a
**Changes** card for the git repository at the top of the workspace's folder.
Its head names the branch, with the buttons to switch or make a branch, fetch or
sync, how far the branch is ahead of and behind the one it tracks, and **Push**;
below are the files staged and the files changed and not yet staged, each list
folding under its heading with **Unstage selected** or **Stage selected** beside
it; and at its foot, **Commit** and when the status was last checked. Everything
it does is done in that workspace, as the commit author and with the token
above.

- **Commit** needs something staged and a commit author name; the card
  says so otherwise and links to Settings.
- **Push** to GitHub needs a GitHub token. While none is stored, the card
  withdraws Push and says to add one in Settings; **Commit & Push** commits
  nothing in that case, as the push could not follow.
- The button beside the branch is **Sync** where a sync can run, and
  **Refresh branches** otherwise. **Refresh branches** only fetches: it brings
  the list of remote branches up to date and changes no file and no branch.
  **Sync** fetches, brings the branch up to date with the branch it tracks, and
  pushes what the remote lacks; a branch that tracks none is published to
  `origin` and set to track it there.
- **Sync** is offered on a branch with a commit, where a push could be tried
  (as for **Push**), where the branch and the branch it tracks have not
  diverged, where the branch it tracks is still on the remote as far as the
  card knows (the bar says when it is gone), and while no merge is under
  way. It only fast-forwards: whatever your own pull or merge settings
  say, it makes no merge commit and rebases nothing. Where both have commits
  the other lacks, the card says the branch has diverged and needs merging
  or rebasing, and offers **Refresh branches** instead; ask Claude, or run git
  in the workspace, to merge or rebase it.
- **Sync** keeps changes you have not committed, unless bringing the branch up
  to date would overwrite one, in which case git refuses and changes nothing.
  A branch whose tracked branch turns out to be gone from the remote when
  **Sync** fetches is refused, and nothing is recreated there.
- When **Sync** brought the branch up to date and its push then failed, the
  card says both: what was pulled stays.
- When git refuses, the card shows what it said, beside what you tried.

A workspace made without a GitHub address is empty, and an empty workspace is
not a git repository: the card says so quietly, without an error. Claude can
run `git init` there, and the card shows the repository once it exists.

## Making a workspace from GitHub

Give a GitHub address in the **New workspace** dialog (see
[Workspaces](workspaces.md#the-workspaces-page)) and rcman clones that repository into the new
workspace, with git, as you: all its branches, and the default one checked out.
The workspace is ready once the clone is done, and its **Changes** card
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
  Its trash button in the list removes it; make it again once the cause is mended.
- The panel does not show git's progress. The workspace's trash button stops a
  clone that is running, and deletes the workspace.
- If rcman restarts while a workspace is being made, the workspace fails and
  says so. Delete it and make it again.
- Your token is never in the repository, in the steps shown, or in the error.
- A repository's `devcontainer.json` may list commands that run in the new
  workspace (see [devcontainer.json support](devcontainer.md#lifecycle-commands)).
  Like any command there, they can read your token, which git and gh read
  from files all your workspaces share, so make workspaces only from
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

The default image has `gh` too, from Ubuntu's packages. A workspace keeps the
image it was made from, so one made from a default image without `gh` has
none: rebuild a workspace cloned from a repository with no `devcontainer.json`,
which makes it from the current default image (see
[Rebuilding a workspace](devcontainer.md#rebuilding-a-workspace)), make it again,
or install it there yourself
(`apt-get update && apt-get install gh` in the **Terminal** tab), which lasts
until its container is made again. Another image, `RCMAN_WORKSPACE_IMAGE` or
one a repository's `devcontainer.json` names or builds, has `gh` only if it
brings its own.
