# claude-rc-man

A self-hosted web service for controlling Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) servers on one
machine. Colleagues sign in with Google, make workspaces of their own — each a
container, with a person's own range of user ids — and start, stop or resume a
`claude remote-control` server in any of them. The conversations themselves
happen in claude.ai/code or the Claude app.

## Status

Implemented and tested:

- **People** kept by rcman, added and removed on the People page or with
  `rcman-admin`.
- **Workspaces** as containers of rootless Podman, run by rcman's own account,
  made empty or cloned from GitHub. They keep running while rcman restarts, and
  rcman brings them back, with their servers, when it starts. Administrators see
  everyone's.
- **A server in each workspace**, its screens read and the questions Claude
  asks answered.
- **One Claude sign-in per person**, which every workspace of theirs shares.
- **A terminal in each workspace**: a shell in the workspace's panel, as the
  container's own user, one per workspace.
- **Containers in a workspace**: Podman, as `podman` and `docker`, in workspaces of the
  default image or of an image with Podman of its own, kept inside the workspace.
- **git and gh in workspaces**: each person's commit author and GitHub token shared by
  all their workspaces, for git and for `gh`, and the Changes section of a workspace's panel.
- **A repository's devcontainer.json**, read when a workspace is cloned: an
  image or a Dockerfile and features, built in a sandbox of their own, with the
  user, the environment and the lifecycle commands; what a workspace may not ask
  for is refused. A changed file is seen, and a workspace is rebuilt from it on
  its own files when its owner asks.
- **Claude Code versions** rolled out by an administrator after checks in a
  throwaway container; servers keep the version they started with.
- **Upgrading rcman from its web interface**, off unless the installer turns it on:
  an administrator's request, carried out by a root service of the installer's.
- **Limits** on memory, CPU and processes, per person and per workspace, set by
  administrators.
- The backend API with its audit trail, the web interface, the installer and the
  end-to-end checks.

How to install it: [docs/installation.md](docs/installation.md). Screens of
Claude Code that rcman has no recording of yet are listed in
[project-knowledge/fake-claude.md](project-knowledge/fake-claude.md). The design
decisions and the implementation steps are
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Licence

claude-rc-man is under the MIT licence: [LICENSE.md](LICENSE.md). The vendored
skill in `.claude/skills/frontend-design` keeps its own Apache-2.0 licence, in
its [LICENSE.txt](.claude/skills/frontend-design/LICENSE.txt).
