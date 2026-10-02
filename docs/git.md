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
both before and after it was staged is in both lists. Each file is shown as
one of these changes:

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

### Commit author

Every commit made from rcman is recorded with the commit author from your
[Settings](settings.md), as its author and as its committer, whatever
`user.name` and `user.email` your git configuration holds.

## Branches

The section lists your local branches, each with the remote branch it
tracks, and the remote branches git knows of. **Switch to** a local branch
checks it out. A remote branch with no local branch of its own can be
switched to as well: that makes a local branch of the same name tracking it,
as `git switch` does. **New branch from here** makes a branch at the current
commit and switches to it; git says which names it takes.

**Refresh branches** runs `git fetch --prune`: it brings the list of remote
branches up to date, adding new ones and dropping those deleted there. It
changes none of your files and none of your local branches. It needs a
remote git can fetch from without asking for a password: git is never
allowed to ask, so a remote that needs one is refused with git's own words.

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
