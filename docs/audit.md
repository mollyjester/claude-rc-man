# Audit trail

rcman records every request that changes something: who made it, as which
Linux user, what they did, to what, and whether it worked. Requests that only
read are not recorded. Administrators read the trail on the **Audit trail**
page in rcman, newest first.

Each event is also written to the system journal as one line of JSON, so it
can be read with `journalctl` on the server.

## What is recorded

| Action | Recorded when someone | Target |
|---|---|---|
| `server.start` | starts a server | the folder |
| `server.resume` | resumes a stopped server | the folder |
| `server.stop` | stops a server | the folder |
| `server.forget` | removes a stopped server from their list | the folder |
| `server.confirm` | answers a server's folder-trust question | the folder |
| `settings.update` | changes their session name prefix | the new prefix |
| `claude.login` | starts connecting their Claude account | the Claude account's email address |
| `claude.connect` | finishes connecting with Claude's code | none — the code is never recorded |
| `claude.upgrade` | rolls out a Claude Code version (administrators) | the version |
| `user.add` | adds or repairs a person (administrators) | the Linux user |
| `user.delete` | removes a person (administrators) | the Linux user |

The result is `ok`, or the code of the error the request failed with — a
refused request is recorded too.

## How long events are kept

An event cannot be altered once it is recorded; it stays until retention
removes it.

Once a day, and whenever rcman starts, events older than
`RCMAN_AUDIT_RETENTION_DAYS` are deleted, and then the oldest events beyond
`RCMAN_AUDIT_MAX_ROWS`. Both are described in [Configuration](configuration.md).
The journal has its own size limits, set in journald's configuration —
`SystemMaxUse=` and the related settings in `/etc/systemd/journald.conf`.
