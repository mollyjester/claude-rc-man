# CLAUDE.md

A contents list: what this project is and which file answers which question.
Nothing here is duplicated from what it points at; anything longer than a short
description of a destination belongs in the file it names.

## What this is

The repository for a self-hosted web service that controls Claude Code Remote
Control servers. What it does and how far it is implemented: [README.md](README.md).

## Commands

The `make` targets, from `make setup` to `make check-all`, the gating `make check`
and the on-demand runs among them, and what each does:
[project-knowledge/tests.md](project-knowledge/tests.md).

## Where to look

| Question | File |
|---|---|
| The rules that override everything else | [CONTRIBUTING.md](CONTRIBUTING.md) |
| The full standing rules, and where documentation goes | [project-knowledge/working-rules.md](project-knowledge/working-rules.md) |
| Taking an issue, or a task given as plain text, from plan to finished change with subagents | [.claude/skills/manage-issue/SKILL.md](.claude/skills/manage-issue/SKILL.md) |
| Designing or reviewing how a page looks: Anthropic's design guidance, vendored unchanged with its licence, applied within the frontend's design system | [.claude/skills/frontend-design/SKILL.md](.claude/skills/frontend-design/SKILL.md) |
| How tests are organised and run, the tests' own Podman engine, the stubbed installation and the whole stack, what a machine or a workspace needs to run them (the repository's `.devcontainer`), the repository guards, the edit-time and pre-test hooks, the screenshots of every page (`make screenshots`) | [project-knowledge/tests.md](project-knowledge/tests.md) |
| The fake `claude`, its fixtures, and approving a new Claude Code version | [project-knowledge/fake-claude.md](project-knowledge/fake-claude.md) |
| Workspaces: the Podman and host runtimes, each person's shared Claude sign-in, the exec channel, a workspace's server, its pinned Claude Code version and states, the Terminal tab's shell, containers in a workspace and the published-port warning, the instructions for Claude bound into every workspace, the canary, limits on memory, CPU and processes, rebuilding a workspace on its volume, restoring at start | [project-knowledge/runtime.md](project-knowledge/runtime.md) |
| What a workspace, its server, its terminal and the containers in it are, and what Claude is told in a workspace, for colleagues | [docs/workspaces.md](docs/workspaces.md) |
| People, their uid-range slots, the allowed list, `rcman-admin`, their limits | [project-knowledge/people.md](project-knowledge/people.md) |
| Adding and removing people, and limiting what they use, for administrators | [docs/users.md](docs/users.md) |
| What the Monitor page shows of the host, each person and each workspace container, how it is collected, and its settings, for administrators | [docs/monitor.md](docs/monitor.md) |
| What a repository's devcontainer.json may ask of a workspace, how an image of it is built, what is refused, and how a workspace is rebuilt from it, for administrators | [docs/devcontainer.md](docs/devcontainer.md) |
| The backend: layout, requests, each area's routes (Claude Code versions and the terminal's WebSocket among them), audit | [project-knowledge/backend.md](project-knowledge/backend.md) |
| The frontend: formatting and lint, the rules it keeps, the design system, layout, tests | [project-knowledge/frontend.md](project-knowledge/frontend.md) |
| The installer: how it is built and tested | [project-knowledge/installer.md](project-knowledge/installer.md) |
| Checks that need a real Claude account or a real machine: a fresh install or reinstall, a restart of the host, an upgrade | [project-knowledge/real-claude-checklist.md](project-knowledge/real-claude-checklist.md) |
| Installing, upgrading, rolling back and removing rcman, for administrators | [docs/installation.md](docs/installation.md) |
| Settings, for administrators | [docs/configuration.md](docs/configuration.md) |
| The audit trail and its retention, for administrators | [docs/audit.md](docs/audit.md) |
| What rcman writes to the system journal, and how to read it, for administrators | [docs/journal.md](docs/journal.md) |
| Connecting a Claude account, signing out of Claude and of rcman, for colleagues | [docs/claude-account.md](docs/claude-account.md) |
| git in workspaces, for colleagues | [docs/git.md](docs/git.md) |
| A colleague's own settings, for colleagues | [docs/settings.md](docs/settings.md) |
| The Claude Code version workspaces run, and rolling out another, for administrators | [docs/claude-version.md](docs/claude-version.md) |
| The design and the implementation plan | GitHub issues on `mollyjester/claude-rc-man` (label `design`, label `build-step`) |
