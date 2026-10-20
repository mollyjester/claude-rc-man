# Audit trail

rcman records every request that changes something: who made it, what they did,
to what, and whether it worked; and when a **Terminal** tab opens and closes.
Requests that only read are not recorded. Administrators read the trail on the
**Audit trail** page in rcman, in the **Administration** group of the sidebar,
newest first. The page says each action in plain words, with its code as the
cell's tooltip, shows dates in UTC, and marks a request that failed with the
word *Failed* and its error code. On a phone each event is a row of a list: the
action and how it ended, under it who did it and to what, and then when.

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
| `workspace.rebuild` | asks for a workspace to be rebuilt on its volume from its devcontainer.json (see [Workspaces](workspaces.md)) | the workspace's name |
| `server.start` | starts a workspace's server | the workspace's name |
| `server.resume` | resumes a server that stopped | the workspace's name |
| `server.stop` | stops a server | the workspace's name |
| `server.forget` | removes a stopped server from their list | the workspace's name |
| `server.confirm` | answers a question a server asks | the workspace's name |
| `me.sign_out` | signs out of rcman, keeping their running servers or stopping them first | none |
| `settings.update` | changes their settings (see [Settings](settings.md)) | the settings they sent, as JSON; none when they were refused |
| `person.add` | adds a person (administrators; see [Adding and removing people](users.md)) | the person's address |
| `person.delete` | removes a person, with all their workspaces (administrators) | the person's address |
| `person.limits` | sets the limits on what a person's workspaces may use together (administrators) | the person's address and the limits set: `memory`, `cpu` and `pids`, each `none` for no limit |
| `workspace.limits` | sets the limits on one workspace (administrators) | the person's address, a slash and the workspace's name, then the limits set as above |
| `git.stage` | stages files in a workspace's repository | the workspace's name |
| `git.unstage` | unstages files | the workspace's name |
| `git.commit` | commits what is staged | the workspace's name — the message is never recorded |
| `git.switch` | switches to a branch | the workspace's name |
| `git.branch` | makes a branch and switches to it | the workspace's name |
| `git.fetch` | refreshes the remote branches | the workspace's name |
| `git.sync` | brings the current branch up to date with the branch it tracks, then pushes it | the workspace's name |
| `git.push` | pushes the current branch | the workspace's name |
| `git.commit_push` | commits what is staged and pushes it | the workspace's name — the message is never recorded |
| `git.clone` | makes a workspace by cloning a repository from GitHub — recorded when rcman takes the request; whether the clone worked is the workspace's state | the repository's address, as given — an address with a user or a token in it is refused, and so recorded with none |
| `github.token` | adds or replaces their GitHub token | none — the token is never recorded |
| `github.forget` | removes their GitHub token | none |
| `claude.login` | starts connecting their Claude account | the Claude account's email address |
| `claude.connect` | finishes connecting with Claude's code | none — the code is never recorded |
| `claude.disconnect` | signs their Claude account out, at once or once their last running server stops | none |
| `terminal.open` | opens the **Terminal** tab on one of their workspaces — recorded also when the workspace is refused | the workspace's name; none when it was refused as not theirs or not there |
| `terminal.close` | closes a **Terminal** tab that opened, or has it closed for them; the result says why | the workspace's name — nothing typed or shown is ever recorded |
| `claude.upgrade` | rolls out a Claude Code version, or approves an installed one again (administrators; see [Claude Code versions](claude-version.md)) | the version |
| `rcman.upgrade` | asks for rcman to be upgraded from the web interface (administrators; see [Upgrading from the web interface](installation.md#upgrading-from-the-web-interface)) — recorded when rcman takes the request; how the upgrade went is in the [system journal](journal.md#reading-it) and on the **rcman release** page | none |

The result is one of these, or the code of the error the request failed with —
a refused request is recorded too; a terminal closed because its person may not
use rcman any more is `not_allowed`:

| Result | Meaning |
|---|---|
| `ok` | It worked; for `terminal.close`, the shell ended by itself, as `exit` ends it |
| `unrecognised_screen` | It worked, but left the server on a screen rcman does not recognise |
| `committed_not_pushed` | **Commit & Push** made the commit, and the push failed |
| `pulled_not_pushed` | **Sync** brought the branch up to date, and the push failed |
| `terminal_lost` | The page of a terminal closed it, was reloaded, went to another workspace or stopped answering |
| `terminal_replaced` | A terminal opened on the same workspace, in another tab or window, replaced it |
| `workspace_gone` | The terminal's workspace was deleted, or is not ready |
| `workspace_rebuilding` | The terminal's workspace is being rebuilt: its container is being made again |
| `signed_out` | The person signed out of rcman |
| `service_closing` | rcman stopped |

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
