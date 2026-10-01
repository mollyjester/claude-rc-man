# CLAUDE.md

A contents list: what this project is and which file answers which question.
Nothing here is duplicated from what it points at; anything longer than a short
description of a destination belongs in the file it names.

## What this is

The repository for a self-hosted web service that controls Claude Code Remote
Control servers. What it does and how far it is implemented: [README.md](README.md).

## Commands

`make setup`, `make test`, `make lint`, `make format`, `make frontend-test`,
`make e2e`, `make check` — what each does:
[project-knowledge/tests.md](project-knowledge/tests.md).

## Where to look

| Question | File |
|---|---|
| The rules that override everything else | [CONTRIBUTING.md](CONTRIBUTING.md) |
| The full standing rules, and where documentation goes | [project-knowledge/working-rules.md](project-knowledge/working-rules.md) |
| How tests are organised and run, the repository guards, the edit-time hook | [project-knowledge/tests.md](project-knowledge/tests.md) |
| The fake `claude`, its fixtures, and approving a new Claude Code version | [project-knowledge/fake-claude.md](project-knowledge/fake-claude.md) |
| `rc-helper`: verbs, JSON contract, server states, isolation, sudo | [project-knowledge/rc-helper.md](project-knowledge/rc-helper.md) |
| What a server is, for colleagues | [docs/servers.md](docs/servers.md) |
| `rc-admin`, `rcman-user`, `users.toml`, the approved Claude version | [project-knowledge/rc-admin.md](project-knowledge/rc-admin.md) |
| Adding and removing people, for administrators | [docs/users.md](docs/users.md) |
| The backend: layout, identity, errors, helper calls, audit | [project-knowledge/backend.md](project-knowledge/backend.md) |
| The frontend: layout, rules, tests | [project-knowledge/frontend.md](project-knowledge/frontend.md) |
| The installer: how it is built and tested | [project-knowledge/installer.md](project-knowledge/installer.md) |
| Checks that need a real Claude account | [project-knowledge/real-claude-checklist.md](project-knowledge/real-claude-checklist.md) |
| Installing and upgrading, for administrators | [docs/installation.md](docs/installation.md) |
| Settings, for administrators | [docs/configuration.md](docs/configuration.md) |
| The audit trail and its retention, for administrators | [docs/audit.md](docs/audit.md) |
| Connecting a Claude account, for colleagues | [docs/claude-account.md](docs/claude-account.md) |
| Claude Code versions and rolling one out, for administrators | [docs/claude-version.md](docs/claude-version.md) |
| The design and the implementation plan | GitHub issues on `mollyjester/claude-rc-man` (label `design`, label `build-step`) |
