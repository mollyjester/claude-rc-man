# rc-admin and rcman-user

User management, run as root. The operations are in `helpers/rc_admin.py`;
`helpers/users_file.py` reads and writes `users.toml` and renders the
allowlist. Two front ends share them:

| Front end | Implementation | For |
|---|---|---|
| `helpers/rc-admin` | `main_json` in `helpers/rc_admin.py` | The application, through sudo. JSON on stdout, never prompts |
| `helpers/rcman-user` | `helpers/rcman_user.py` | An administrator over SSH. Output in words; asks about the home folder |

How administrators use them is in [the user guide](../docs/users.md). The
command-line reference is `--help`.

## Verbs

| Verb | What it does |
|---|---|
| `rc-admin add <linux-user> <email> [--admin] [--adopt]` | Creates or repairs the account, installs the approved Claude Code, seeds `~/.claude/settings.json` and `~/projects`, writes the mapping |
| `rc-admin del <linux-user> (--delete \| --keep-home) [--requester <email>]` | Stops tmux servers on the `rcman` socket, runs `claude auth logout`, kills the user's processes, removes the account, removes the mapping. `--requester` names the administrator asking, who cannot delete themselves |
| `rc-admin list` | Every mapping and whether its account exists and is in `rcusers` |

`rc-admin` prints `{"error": {"code", "message"}}` and exits 1 on failure (2
for a command line that does not parse). The codes and messages are
`MESSAGES` in `helpers/rc_admin.py`. `rcman-user` prints the message on
stderr; as the administrator to protect from self-deletion it uses
`SUDO_USER`.

## Rules it keeps

- Linux user names and emails are validated before anything is touched; every
  refusal happens before the first change.
- Accounts with a UID below `MIN_UID` are system accounts and are refused.
- An existing account is taken over only with `--adopt`, or when it is already
  mapped or already in `rcusers` (a repair).
- An email belongs to one Linux user and a Linux user to one email.
- `del` acts only on accounts that are both mapped and in `rcusers`; a mapping
  whose account no longer exists is simply removed. The last administrator is
  never removed.
- `del` requires an explicit home decision. Processes are killed and must be
  gone before the account is removed; zombies are not waited for.
- `users.toml` and `allowed-emails` are written atomically, owned by root,
  group `rcman`, mode `0640`. The allowlist is regenerated from `users.toml`
  on every change and never edited separately.
- Commands for the user run through `runuser` and `/usr/bin/env -i` with the
  variables `user_argv` lists.

## Claude Code version

`helpers/claude-version` is the approved Claude Code version. `add` installs
it with the official installer (`CLAUDE_INSTALLER_URL`) when the user's
installed version differs. A test holds it equal to the version the fixtures
were recorded from — see [fake-claude.md](fake-claude.md).

## sudo

`helpers/sudoers.rc-admin` lets the application user run `rc-admin`, and
nothing else, as root with the environment reset. `install.sh` fills in
`@ADMIN@` and `@APP_USER@`.

## Test hooks and tests

`RC_ADMIN_ETC` replaces `/etc/rcman` and `RC_ADMIN_INSTALLER` replaces the
official installer with a program run as the user with the version as its
argument. sudo resets the environment, so neither reaches `rc-admin` started
through sudo.

| Tests | What they prove |
|---|---|
| `tests/test_users_file.py` | Parsing, validation, writing and the allowlist |
| `tests/test_rc_admin.py` | Which commands each operation decides to run, against a recording stand-in for the system; that refusals run none |
| `tests/test_rcman_user.py` | The prompt, its default, and refusing without a terminal |
| `tests/container/test_admin_in_container.py` | The real thing, as root in a container: accounts, groups, modes, ownership, process cleanup, logout, the prompt on a real terminal, and the sudoers rules for both helpers — including that test hooks do not pass through sudo |
