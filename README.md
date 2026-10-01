# claude-rc-man

A self-hosted web service for controlling Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) servers on one
machine. Colleagues sign in with Google, see the folders under their own Linux
home, and start, stop or resume a `claude remote-control` server in any of
them. The conversations themselves happen in claude.ai/code or the Claude app,
under each colleague's own Claude account.

## Status

Built so far: `rc-helper`, which controls one person's servers and connects
their Claude account; `rc-admin` and `rcman-user`, which add and remove people
and roll out Claude Code versions; the backend API; and the web interface. The
installer is not built yet.
The design and the ordered implementation steps are tracked as
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
