# claude-rc-man

A self-hosted web service for controlling Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) servers on one
machine. Colleagues sign in with Google, see the folders under their own Linux
home, make and delete folders there, clone repositories from GitHub into them,
start, stop or resume a
`claude remote-control` server in any of them, and stage, commit, switch
branches and push in the git repositories among them, to GitHub with a token
of their own that their Claude sessions use too. The conversations themselves
happen in claude.ai/code or the Claude app, under each colleague's own Claude
account.

## Status

Built: `rc-helper`, which does everything rcman does as one person — their
servers, folders, Claude account, git repositories and GitHub token;
`rc-admin` and `rcman-user`, which add and remove people and roll out Claude
Code versions; the backend API, with its
audit trail; the web interface, including the administrators' pages; the
installer; and the end-to-end and real-Claude checks. How to install it:
[docs/installation.md](docs/installation.md). Screens of Claude Code that
rcman has no recording of yet are listed in
[project-knowledge/fake-claude.md](project-knowledge/fake-claude.md). The
design decisions and the implementation steps are
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
