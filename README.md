# claude-rc-man

A self-hosted web service for controlling Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) servers on one
machine. Colleagues sign in with Google, see the folders under their own Linux
home, make and delete folders there, and start, stop or resume a
`claude remote-control` server in any of them. The conversations themselves
happen in claude.ai/code or the Claude app, under each colleague's own Claude
account.

## Status

Built: `rc-helper`, which controls one person's servers and
connects their Claude account; `rc-admin` and `rcman-user`, which add and
remove people and roll out Claude Code versions; the backend API, with its
audit trail; the web interface, including the administrators' pages; the
installer; and the end-to-end and real-Claude checks. How to install it:
[docs/installation.md](docs/installation.md). Screens of Claude Code that
rcman has no recording of yet are listed in
[project-knowledge/fake-claude.md](project-knowledge/fake-claude.md). The
design decisions and the implementation steps are
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
