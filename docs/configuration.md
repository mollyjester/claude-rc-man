# Configuration

rcman reads its settings from environment variables. The installer writes
them to the service's environment file; change them there and restart the
service.

Each variable's default and allowed values are defined in the settings model,
`backend/rcman/settings.py`, which is the reference. rcman refuses to start
when a value is invalid.

The limits on what a person's workspaces may use are not settings of this kind:
administrators set them per person and per workspace, and rcman keeps them in
its database (see [Limits](users.md#limits)).

| Variable | What it sets |
|---|---|
| `RCMAN_PUBLIC_ORIGIN` | The address browsers use to reach rcman — scheme, host and port, with no path, such as `https://rcman.example.com`. Required. Requests that change something are accepted only when they come from this origin |
| `RCMAN_DATABASE` | The SQLite database: the people who may use rcman, their workspaces and settings, and the audit trail |
| `RCMAN_ALLOWED_EMAILS` | The list of addresses oauth2-proxy lets sign in, which rcman writes from its people (see [Adding and removing people](users.md)) |
| `RCMAN_PODMAN_SOCKET` | The socket of the Podman service workspaces run in: rcman's own user manager's, under `XDG_RUNTIME_DIR`, when unset |
| `RCMAN_WORKSPACE_IMAGE` | The image new workspaces are made from; the installer names its default image here. It must hold a git new enough for rcman, or a workspace made from it fails (see [Git](git.md#images)). `gh` works in its workspaces only if it holds `gh` too. A workspace cloned from a repository whose `devcontainer.json` names an image is made from that image instead (see [devcontainer.json support](devcontainer.md)) |
| `RCMAN_BUILDER_IMAGE` | The image a build of a repository's `devcontainer.json` runs in, started for one build and removed after it; the installer builds it and names it here (see [devcontainer.json support](devcontainer.md#building-an-image)) |
| `RCMAN_TOOLS_DIR` | The tools every workspace has, read-only: tmux, busybox and terminal descriptions, which the installer makes |
| `RCMAN_CLAUDE_STORE` | The Claude Code store every workspace has, read-only, which the installer makes and rcman then writes (see [Claude Code versions](claude-version.md)) |
| `RCMAN_CLAUDE_DOWNLOAD` | The address Claude Code's releases are downloaded from, as the installer downloads them and as rcman does when an administrator rolls out another version (see [Claude Code versions](claude-version.md)) |
| `RCMAN_DOCS_DIR` | Where the documentation pages shown in rcman are |
| `RCMAN_FRONTEND_DIR` | Where the built web interface is |
| `RCMAN_AUDIT_RETENTION_DAYS` | How long audit events are kept (see [Audit trail](audit.md)) |
| `RCMAN_AUDIT_MAX_ROWS` | The most audit events kept at once (see [Audit trail](audit.md)) |
| `RCMAN_LOGIN_WARNING_DAYS` | How many days before a Claude sign-in expires people are warned (see [Connecting your Claude account](claude-account.md)) |
