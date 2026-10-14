# Claude Code versions

Every workspace runs Claude Code from one store, `/var/lib/rcman/claude`,
which every workspace has, read-only. The installer fills it with the **tested**
version: the one rcman's own recorded tests were made with, named in
`helpers/claude-version`. It downloads that version from Claude Code's
releases and checks it against the checksum the release publishes; an upgrade
of rcman that changes the tested version puts the new one in the store, and new
servers start it.

Claude Code's own self-update is turned off in every workspace, so a workspace
runs only the store's version. A server that is running keeps the version it
started with until it is stopped and started again.

Rolling out another version from rcman is not available yet: the **Claude
Code** page is not offered, and rcman answers its requests that it cannot do
this with workspaces yet.
