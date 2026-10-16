# claude-rc-man

A self-hosted web service for controlling Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) servers on one
machine. Colleagues sign in with Google, make workspaces of their own — each a
container, with a person's own range of user ids — and start, stop or resume a
`claude remote-control` server in any of them. The conversations themselves
happen in claude.ai/code or the Claude app.

## Status

Built: people kept by rcman, added and removed on the People page or with
`rcman-admin`; workspaces as containers of rootless Podman run by rcman's own
account, made empty or cloned from GitHub, which keep running while rcman restarts and which rcman
brings back, with their servers, when it starts; a server in each, its screens
read and its trust question answered, and everyone's for administrators; one
Claude sign-in per person, which every workspace of theirs shares; the
backend API with its audit trail; the web interface; the installer; and the
end-to-end checks; git in workspaces, with each person's commit author and
GitHub token shared by all their workspaces and the Changes section of a
workspace's panel. a repository's devcontainer.json read when a workspace is cloned, with an image or
a Dockerfile and features built in a sandbox of their own, user, environment and lifecycle commands,
and what it may not ask for refused. Claude Code versions rolled out by an administrator after checks in a
throwaway container, with servers that keep the version they started with. Not there yet in workspaces: limits. How to install it:
[docs/installation.md](docs/installation.md). Screens of Claude Code that
rcman has no recording of yet are listed in
[project-knowledge/fake-claude.md](project-knowledge/fake-claude.md). The
design decisions and the implementation steps are
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
