# rc-helper

`helpers/rc-helper` controls one user's Claude Remote Control servers. rcman
runs it as that user — `sudo -u <user> rc-helper <verb> ...` — and it is the
only code that touches a user's tmux and Claude processes. The implementation
is `helpers/rc_helper.py`; its tests are `tests/test_rc_helper.py`. The full
command-line reference is `rc-helper --help` and `rc-helper <verb> --help`.

## Contract

- Every verb prints one JSON object on stdout.
- A failure prints `{"error": {"code": ..., "message": ...}}` and exits 1; a
  command line that does not parse exits 2 with code `usage`. Some errors carry
  extra fields, such as the server's screen. The codes and their messages are
  `MESSAGES` in `helpers/rc_helper.py`, the one place they are written; a test
  holds the codes used and the codes listed to each other.
- Paths are relative to the user's home folder. Option values that may start
  with a hyphen are passed as `--option=value`.
- Secrets — the one-time sign-in code — are read from stdin, never from the
  command line, and never printed.

## Verbs

| Verb | What it does |
|---|---|
| `rc-helper tree [path]` | Subfolders of a folder in the home folder (the home folder itself when no path is given); `--depth` for nested levels, `--all` to include dot-folders. Symbolic links leading outside the home folder are left out |
| `rc-helper list` | Every server and its state |
| `rc-helper start <path> --prefix <p>` | Starts a server in a folder, with `<p>` as the session name prefix. Refuses when Claude is not connected or a server already runs there; in a folder with a stopped server it starts that server again |
| `rc-helper resume <path> --prefix <p>` | Starts a server again that is `stopped` or `exited` (`RESUMABLE` in `helpers/rc_helper.py`); refuses with `not_stopped` when there is none |
| `rc-helper stop <path>` | Stops a running server with Ctrl+C and waits for it to exit |
| `rc-helper forget <path>` | Removes a server that has stopped from the list |
| `rc-helper output <path>` | The server's current screen and state |
| `rc-helper confirm <path> --answer y\|n` | Answers the folder-trust question |
| `rc-helper auth-status` | Whether a Claude account is connected, which, and when the sign-in expires (`login_expires_at`, or `null` when unknown) |
| `rc-helper auth-login --email <e>` | Starts connecting a Claude account; returns the sign-in URL |
| `rc-helper auth-code` | Finishes connecting with the code read from stdin — pressing Enter after Claude reports success, as it asks — and returns the account as `auth-status` does |

## Server states

`classify` in `helpers/rc_helper.py` turns a server's screen and exit status
into one of these states. The screen patterns it uses are held to every
recorded fixture by `tests/test_rc_helper.py`, so a re-recording that changes
a screen fails there first.

| State | Meaning |
|---|---|
| `starting` | The server has printed nothing yet |
| `trust_prompt` | Claude asks whether to trust the folder; answer with `confirm` |
| `connected` | The server is up; no session has attached yet |
| `ready` | A session is attached; `session_name` and `environment_url` are set |
| `stopped` | Stopped with its sessions preserved; `start` or `resume` brings them back |
| `trust_declined` | The trust question was answered no; the server ended |
| `logged_out` | Claude refused because no account is connected |
| `exited` | The server ended in a way not listed above, whatever its exit status — it stopped by itself; `exit_code` and `screen` are set, and `start` or `resume` starts it again |
| `unrecognised` | A live screen that matches no known state; `screen` is set |

A screen in transition can match no known state for a moment, so verbs that
wait report `unrecognised` only once it has lasted
`UNRECOGNISED_GRACE_SECONDS`.

The folder-trust question refuses an answer that arrives too quickly, so
`confirm` waits `ANSWER_PACE_SECONDS` before answering and answers once more
if Claude asks again.

## Processes and isolation

- The home folder comes from the password database, never from `$HOME`. A
  folder must resolve, after following symbolic links, inside the home folder
  and must not be the home folder itself.
- Servers run in tmux on a dedicated socket (`TMUX_SOCKET_NAME`), configured by
  `helpers/tmux.conf`, so they never mix with the user's own tmux and keep
  their final screen and exit status after they exit. Each folder has its own
  session, named from a hash of its path, with the path stored in the
  session's `@rcman_path` option; at most one exists per folder.
- Claude Code is `~/.local/bin/claude` of the user. It is started through
  `/usr/bin/env -i` with only the variables `child_env` lists — never through
  a shell, and with self-update disabled. Remote Control sessions start in
  permission mode `default`.
- The sign-in expiry is read from Claude Code's credentials file
  (`CREDENTIALS`), from `claudeAiOauth.refreshTokenExpiresAt` only; no token
  is read into the output. It is an internal file, so anything unexpected
  makes the expiry unknown rather than an error.
- The sign-in code reaches the sign-in prompt through a tmux paste buffer
  loaded from stdin, so it never appears in any process's arguments.
- `rc-helper` refuses to run as root. It runs on the system Python with only
  the standard library and the helpers' own modules; the launcher runs it in
  isolated mode (`-I`) and puts the directory above `helpers/` on its path.

## sudo

`helpers/sudoers.rc-helper` is the sudoers rule template: the application user
may run `rc-helper`, and nothing else, as any member of the `rcusers` group,
with the environment reset. `install.sh` fills in `@HELPER@` and `@APP_USER@`.
A test renders it and validates it with `visudo -c`.

## Test hooks

`RC_HELPER_HOME`, `RC_HELPER_CLAUDE` and `RC_HELPER_TMUX_SOCKET` replace the
home folder, the Claude binary and the tmux socket. The sudoers rule resets
the environment, so they never reach a helper started through sudo. The tests
point them at a temporary home, a private socket and the fake `claude` — see
[fake-claude.md](fake-claude.md).
