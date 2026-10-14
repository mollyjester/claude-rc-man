# Audit trail

rcman records every request that changes something: who made it, what they
did, to what, and whether it worked. Requests that only
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
| `workspace.create` | makes a workspace (see [Workspaces](workspaces.md)) | the workspace's name |
| `workspace.delete` | deletes a workspace, with everything in it and its server | the workspace's name |
| `server.start` | starts a workspace's server | the workspace's name |
| `server.resume` | resumes a server that stopped | the workspace's name |
| `server.stop` | stops a server | the workspace's name |
| `server.forget` | removes a stopped server from their list | the workspace's name |
| `server.confirm` | answers a server's folder-trust question | the workspace's name |
| `me.sign_out` | signs out of rcman, keeping their running servers or stopping them first | none |
| `settings.update` | changes their settings (see [Settings](settings.md)) | the settings they sent, as JSON; none when they were refused |
| `person.add` | adds a person (administrators; see [Adding and removing people](users.md)) | the person's address |
| `person.delete` | removes a person, with all their workspaces (administrators) | the person's address |
| `git.stage` | stages files in a repository | the repository's folder |
| `git.unstage` | unstages files | the repository's folder |
| `git.commit` | commits what is staged | the repository's folder — the message is never recorded |
| `git.switch` | switches to a branch | the repository's folder |
| `git.branch` | makes a branch and switches to it | the repository's folder |
| `git.fetch` | refreshes the remote branches | the repository's folder |
| `git.push` | pushes the current branch | the repository's folder |
| `git.commit_push` | commits what is staged and pushes it | the repository's folder — the message is never recorded |
| `git.clone` | clones a repository from GitHub | the repository's address, as given — an address with a user or a token in it is refused, and so recorded with none |
| `github.token` | adds or replaces their GitHub token | none — the token is never recorded |
| `github.forget` | removes their GitHub token | none |
| `claude.login` | starts connecting their Claude account | the Claude account's email address |
| `claude.connect` | finishes connecting with Claude's code | none — the code is never recorded |
| `claude.disconnect` | signs their Claude account out, at once or once their last running server stops | none |
| `claude.upgrade` | rolls out a Claude Code version (administrators) | the version |

git, the GitHub token and rolling out a Claude Code version are not available in
workspaces yet: rcman answers each of those
requests with `not_available_yet`, and records it with that result.

The result is one of these, or the code of the error the request failed with —
a refused request is recorded too:

| Result | Meaning |
|---|---|
| `ok` | It worked |
| `unrecognised_screen` | It worked, but left the server on a screen rcman does not recognise |
| `committed_not_pushed` | **Commit & Push** made the commit, and the push failed |

Administrators are warned about servers on an unrecognised screen, as described
in [Workspaces](workspaces.md#for-administrators).

## How long events are kept

An event cannot be altered once it is recorded; it stays until retention
removes it.

Once a day, and whenever rcman starts, events older than
`RCMAN_AUDIT_RETENTION_DAYS` are deleted, and then the oldest events beyond
`RCMAN_AUDIT_MAX_ROWS`. Both are described in [Configuration](configuration.md).
The journal has its own size limits, set in journald's configuration —
`SystemMaxUse=` and the related settings in `/etc/systemd/journald.conf`.
