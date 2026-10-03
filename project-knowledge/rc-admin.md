# rc-admin and rcman-user

User management, run as root. The operations are in `helpers/rc_admin.py`;
`helpers/users_file.py` reads and writes `users.toml` and renders the
allowlist. Two front ends share them:

| Front end | Implementation | For |
|---|---|---|
| `helpers/rc-admin` | `main_json` in `helpers/rc_admin.py` | The application, through sudo. JSON on stdout, never prompts |
| `helpers/rcman-user` | `helpers/rcman_user.py` | An administrator over SSH. Output in words; asks about the home folder |

How administrators use them is in [the user guide](../docs/users.md) and
[Claude Code versions](../docs/claude-version.md). The
command-line reference is `--help`.

## Verbs

| Verb | What it does |
|---|---|
| `rc-admin add <linux-user> <email> [--admin] [--adopt]` | Creates or repairs the account, installs the approved Claude Code, seeds `~/.claude/settings.json` and `~/projects`, writes the mapping |
| `rc-admin del <linux-user> (--delete \| --keep-home) [--requester <email>]` | Stops tmux servers on the `rcman` socket, runs `claude auth logout`, kills the user's processes, removes the account, removes the mapping. `--requester` names the administrator asking, who cannot delete themselves |
| `rc-admin list` | Every mapping, whether its account exists and is in `rcusers`, its home folder, and whether Claude Code is installed there |
| `rc-admin claude-status` | The approved and tested Claude Code versions, and each user's installed one |
| `rc-admin claude-upgrade <version>` | Checks the version in the canary home; only if every check passes, approves it and installs it for everyone whose version differs |

`rc-admin` prints `{"error": {"code", "message"}}` and exits 1 on failure (2
for a command line that does not parse). The codes and messages are
`MESSAGES` in `helpers/rc_admin.py`; for a value refused by a rule — Linux
user names and emails from `helpers/users_file.py`, versions from `VERSION` —
the message is the rule's own sentence, through `RULE_CODES`, as in
[rc-helper](rc-helper.md#contract). `rcman-user` prints the message on
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

## Claude Code versions

`helpers/claude-version` is the **tested** version: the one the fixtures were
recorded from, which a test holds it equal to — see
[fake-claude.md](fake-claude.md). The **approved** version is
`/etc/rcman/claude-version` when it exists and is well formed, and the tested
version otherwise (`approved_version`). `add` installs the approved version
with the official installer (`CLAUDE_INSTALLER_URL`) when the user's installed
version differs.

`claude-upgrade` installs the candidate into the canary home
(`/var/lib/rcman/canary`, recreated each time and owned by the application
user) as the application user, and runs `candidate_checks` from
`helpers/claude_checks.py` there with a throwaway Claude configuration, the
way rc-helper runs Claude: `auth status` through pipes with no terminal, as
rc-helper reads it, so that Claude, which wraps its output at a terminal's
width, does not break a path in its JSON across lines; and the rest on a
pseudo-terminal of the size rc-helper gives Claude's screens, `WIDTH` by
`HEIGHT` in `helpers/rc_helper.py`. Only when no check fails does it write the
approval file and install the version for each user; a user whose
installation fails is reported and the others continue.

## sudo

`helpers/sudoers.rc-admin` lets the application user run `rc-admin`, and
nothing else, as root with the environment reset. The installer
(`sudoers()` in `installer/install.py`) fills in `@ADMIN@` and `@APP_USER@`.

## Test hooks and tests

`RC_ADMIN_ETC` replaces `/etc/rcman`, `RC_ADMIN_CANARY` the canary home, and
`RC_ADMIN_INSTALLER` the official installer with a program run as the user with the version as its
argument. sudo resets the environment, so none of them reaches `rc-admin`
started through sudo.

| Tests | What they prove |
|---|---|
| `tests/test_users_file.py` | Parsing, validation, writing and the allowlist |
| `tests/test_rc_admin.py` | Which commands each operation decides to run, against a recording stand-in for the system; that refusals run none |
| `tests/test_rcman_user.py` | The front end in words: the prompt, its default and refusing without a terminal, `SUDO_USER` as the administrator asking, each verb's output and refusals, and a command line that does not parse |
| `tests/test_claude_checks.py` | The upgrade checks pass the fake and the real installed `claude`, the real one with a configuration path longer than a line, and a broken binary fails every one; `auth status` runs with no terminal and the rest on a terminal of rc-helper's size |
| `tests/container/test_admin_in_container.py` | The real thing, as root in a container — such as accounts, groups, modes, ownership, process cleanup, logout, the prompt on a real terminal, a checked upgrade through the canary home and the version people added afterwards get, the sudoers rules for both helpers, that neither helper's test hooks pass through sudo, and folders and git as a person through them |
