# Audit trail

rcman records every request that changes something: who made it, as which
Linux user, what they did, to what, and whether it worked. Requests that only
read are not recorded. Administrators read the trail on the **Audit trail**
page in rcman, under **Admin** in the navigation, newest first.

Each event is also written to the [system journal](journal.md) as one line of
JSON, so it can be read with `journalctl` on the server.

The journal also holds two kinds of line that are not in the audit trail: the
errors people's pages report, and output rcman does not recognise; both are
described in [The system journal](journal.md). A page sends each report as a
request of its own, `POST /api/client-errors`, which is not recorded because it
changes nothing: a page's report of a failure rcman could not see, written to the journal only.

## What is recorded

| Action | Recorded when someone | Target |
|---|---|---|
| `server.start` | starts a server | the folder |
| `server.resume` | resumes a server that stopped | the folder |
| `server.stop` | stops a server | the folder |
| `server.forget` | removes a stopped server from their list | the folder |
| `server.confirm` | answers a server's folder-trust question | the folder |
| `folder.create` | creates a folder | the new folder, as asked |
| `folder.delete` | deletes a folder, stopping and forgetting every server in it or below it | the folder |
| `git.stage` | stages files in a repository (see [Git](git.md)) | the repository's folder |
| `git.unstage` | unstages files | the repository's folder |
| `git.commit` | commits what is staged | the repository's folder — the message is never recorded |
| `git.switch` | switches to a branch | the repository's folder |
| `git.branch` | makes a branch and switches to it | the repository's folder |
| `git.fetch` | refreshes the remote branches with **Refresh branches** in the branch bar | the repository's folder |
| `git.push` | pushes the current branch with **Push** in the branch bar | the repository's folder |
| `git.commit_push` | commits what is staged and pushes it with **Commit & Push**, in the menu beside **Commit** | the repository's folder — the message is never recorded |
| `git.clone` | clones a repository from GitHub | the repository's address, as given — an address with a user or a token in it is refused, and so recorded with none |
| `github.token` | adds or replaces their GitHub token (see [Git](git.md#github-token)) | none — the token is never recorded |
| `github.forget` | removes their GitHub token | none |
| `settings.update` | changes their settings (see [Settings](settings.md)) | the settings they sent, as JSON; none when they were refused |
| `claude.login` | starts connecting their Claude account | the Claude account's email address |
| `claude.connect` | finishes connecting with Claude's code | none — the code is never recorded |
| `claude.upgrade` | rolls out a Claude Code version (administrators) | the version |
| `user.add` | adds or repairs a person (administrators) | the Linux user |
| `user.delete` | removes a person (administrators) | the Linux user |

The result is one of these, or the code of the error the request failed with —
a refused request is recorded too:

| Result | Meaning |
|---|---|
| `ok` | It worked |
| `unrecognised_screen` | It worked, but left the server on a screen rcman does not recognise |
| `committed_not_pushed` | **Commit & Push** made the commit, and the push failed |

Administrators are warned about servers on an unrecognised screen, as described
in [Servers](servers.md#for-administrators).

## How long events are kept

An event cannot be altered once it is recorded; it stays until retention
removes it.

Once a day, and whenever rcman starts, events older than
`RCMAN_AUDIT_RETENTION_DAYS` are deleted, and then the oldest events beyond
`RCMAN_AUDIT_MAX_ROWS`. Both are described in [Configuration](configuration.md).
The journal has its own size limits, set in journald's configuration —
`SystemMaxUse=` and the related settings in `/etc/systemd/journald.conf`.
