# Git

rcman works with the git repositories in your home folder as you would in a
terminal: as you, with your own git configuration and your own hooks.

A folder that is a git repository carries a **git** marker in the folder
tree, and its panel on the Servers page has a **Changes** section below its
Claude server. A folder counts as a repository when it is the top folder of
one — a folder inside a repository is not one of its own — and when the
repository keeps its git data in your home folder.

## Changes

The section shows the branch you are on, and the files git lists in two
lists: **Staged**, what the next commit holds, and **Not staged**, what has
changed besides, new files git does not track yet among them. A file changed
both before and after it was staged is in both lists. Each list's heading
says how many files it holds. While a merge is under way, until it is
committed, the section says so. Each file is shown as one of these changes:

| Change | Shown as | Meaning |
|---|---|---|
| `modified` | Modified | Its content changed |
| `type_changed` | Type changed | It became a link, or stopped being one |
| `added` | Added | It is new in what is staged |
| `deleted` | Deleted | It was deleted |
| `renamed` | Renamed | It was renamed; the name it had is shown too |
| `copied` | Copied | It is a copy of another file, which is shown too |
| `unmerged` | Conflict | A merge left it with a conflict to resolve |
| `untracked` | Untracked | git does not track it yet |

A submodule is marked as one. A very long list shows its first files and
says how many more there are. A file whose name is not UTF-8 is listed but
cannot be selected; stage it in a terminal.

### Staging

Check the files and choose **Stage selected** under **Not staged**, or
**Unstage selected** under **Staged**; **Select all** checks every file of
its list. A deleted file is staged as deleted, and a new folder with
everything in it. Unstaging a renamed file unstages both its new and its
old name, so that neither is left half staged.

### Commit

With something staged, write a message and choose **Commit**. The commit is
made as your [commit author](settings.md), the name and address the
Settings page shows; until you have set a name there, Commit is not offered
and the section links to Settings. Your repository's hooks run as they would
in a terminal, and a hook that refuses stops the commit with its own words.

### Commit & Push

**Commit & Push**, beside **Commit**, commits and then pushes, as
[Push](#push) below does. It is offered where both Commit and Push are.
When the push would be refused before it is tried — you are not on a
branch, or it goes to GitHub and no [GitHub token](#github-token) is
stored — nothing is committed. When the commit is made and the push then
fails, the section says both: the commit made, and what stopped the push.
Push again once that is dealt with.

### Commit author

Every commit made from rcman is recorded with the commit author from your
[Settings](settings.md), as its author and as its committer, whatever
`user.name` and `user.email` your git configuration holds.

## Branches

The section lists your **Local branches**, each with the remote branch it
tracks, and the **Remote branches** git knows of, each with the local branch
that tracks it. **Switch to** a local branch checks it out. A remote branch
with no local branch tracking it can be switched to as well: that makes a
local branch of the same name tracking it, as `git switch` does.
**New branch** asks for a name and makes a branch of that name at the
current commit, then switches to it; git says which names it takes.

**Refresh branches** runs `git fetch --prune`: it brings the list of remote
branches up to date, adding new ones and dropping those deleted there. It
changes none of your files and none of your local branches. It needs a
remote git can fetch from without asking for a password: git is never
allowed to ask, so a remote that needs one is refused with git's own words.
Over SSH, git here has no terminal to ask for a key's passphrase and, by
default, does not reach your `ssh-agent`, so a key with a passphrase works
only through an agent your SSH configuration names with `IdentityAgent`; a
key without one works as it is.

## Push

**Push** pushes the branch you are on. A branch that does not track a
remote branch yet is pushed to `origin` under its own name and set to
track it there, as `git push -u origin HEAD` does; one that does is pushed
as `git push` pushes it, so your own git configuration decides where. Push
is not offered while the remote branch already has every commit of yours.
While you are not on a branch there is nothing to push, and the section says
so.

A push to GitHub — to a remote whose address begins with
`https://github.com/` — needs your [GitHub token](#github-token); until one
is stored, Push and Commit & Push are not offered there, and the section
links to Settings. An address that names a user, such as
`https://you@github.com/...`, makes git ask for that user's password, which
your token is not stored under, so rcman does not treat it as GitHub; take
the user out of the address (`git remote set-url`) to push with your token.
A push anywhere else is offered without one, and needs
whatever that remote needs without asking for a password; when git refuses,
the section shows its words, as for a push GitHub rejects because someone
pushed first.

## GitHub token

To push to GitHub, git needs a GitHub personal access token. Add one on the
[Settings](settings.md) page. A fine-grained token, limited to the
repositories you work on and with **Contents** set to read and write, is the
safest; a classic token with the `repo` scope works too.

- **Claude uses it too.** rcman stores the token as git's own credential
  for `https://github.com` in your account, so every git push to GitHub
  that runs as you — from rcman, from your Claude sessions, or from a
  terminal — uses it.
- **Where it is kept.** In plain text, readable by you only, in the file
  git's `store` credential helper uses: `~/.git-credentials`, or
  `~/.config/git/credentials` when only that one exists. rcman
  also adds one line to `~/.gitconfig`, which tells git to use that helper
  for `https://github.com`. Credential helpers you have set yourself stay;
  git asks them in the order your configuration lists them, and uses the
  first credential one gives.
- **Never shown.** Once saved, rcman never shows the token again, nor
  records it in the audit trail or its logs. Settings only says whether one
  is stored. Saving another replaces it.
- **When GitHub refuses it** — it has expired or been revoked — git forgets
  it: the Changes section stops offering a push to GitHub, and Settings
  says no token is stored; add a new one.
- **Removing it.** **Remove token** in Settings removes the token rcman
  stored, and nothing else: the line in `~/.gitconfig` stays, as do other
  credentials you stored yourself. The token itself still works on GitHub
  until you revoke it there, under **Settings**, **Developer settings**,
  **Personal access tokens**; do that too once you are done with it.

A push to anywhere other than GitHub needs no token from rcman.

## What Claude changes

Claude works in the same folders. While a repository's panel is open, its
Changes section is refreshed every few seconds and after everything you do
there, so files Claude edits, and a branch it switches to, appear without
anything being done; **Refresh changes** refreshes it at once.

## When git refuses

When git refuses, the section shows what git said, beside what you tried.
Two refusals are common:

- **Local changes would be overwritten.** A switch to another branch would
  lose changes you have not committed. Commit them first, or deal with them
  in a terminal, then switch again.
- **`index.lock` exists.** Another git — often Claude's, in the same
  folder — is working in the repository. Wait a moment and try again. If it
  stays, no git is running there any more and the file is left over; remove
  `.git/index.lock` in a terminal.

git is given a time limit; one that does not finish in it, such as one
waiting for a hook, is stopped, and the section says so.

## Your git configuration

git runs as you, with your own global configuration and the repository's,
so what you set there applies: what is ignored, how renames are found, your
hooks and your credential helpers. The one exception is the commit author,
above. The installer checks that this machine's git is new enough for what
rcman runs (see [Installing rcman](installation.md)).

## Not here

rcman does not show what changed inside a file, does not discard changes,
and does not pull. Use a terminal, or ask Claude.
