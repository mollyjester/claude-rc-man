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
  holds the codes used and the codes listed to each other. A value refused by
  one of the module's rules — each a `Rule` (`helpers/rules.py`): a pattern and
  the sentence that explains it — is refused with that rule's sentence:
  `RULE_CODES` names the rule behind each such code, and `MESSAGES` takes the
  sentence from it. A rule's code is raised only where its rule is checked, so
  the sentence is never said of a value the rule accepts. The backend checks
  the same rules and refuses a value one refuses with the same sentence; what
  only the helper can see, it refuses itself in words of its own — a folder
  name longer than the file system allows (`name_invalid`), a path that leads
  home, outside the home or nowhere, a path ending in `.` or `..`
  (`path_ends_in_dot`), a new folder whose path would be too long
  (`folder_path_too_long`). Tests run each code's refusal through the command
  line, hold every rule the module has to a code, but `FOLDER_PATH`, which
  only the backend checks, and find a rule's code raised where its rule is not
  checked.
- Paths are relative to the user's home folder, as `PATH` in
  `helpers/rc_helper.py` says, which the backend's path fields check too;
  where a folder is needed, the backend checks `FOLDER_PATH`, the same
  without the empty path, and rc-helper refuses any path that leads home
  with `path_is_home`.
  Option values that may start with a hyphen are passed as `--option=value`.
- Secrets — the one-time sign-in code — are read from stdin, never from the
  command line, and never printed. So are a commit's message and the files to
  stage or unstage.

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
| `rc-helper mkdir [parent] --name=<name>` | Makes a folder named `<name>` in a folder, or in the home folder when no parent is given. The name must match `FOLDER_NAME` in `helpers/rc_helper.py`, whose sentence says what it refuses and which shares its invisible characters with `AUTHOR_NAME`, and be no longer than the file system's `PC_NAME_MAX` in bytes; refuses with `folder_exists` when anything, even a dangling link, has that name |
| `rc-helper delete <path>` | Stops every running server in a folder or below it, forgets every server there, then deletes the folder and everything in it; a link is deleted as the link. Refuses the home folder, anything outside it, a dot-folder or anything in one (`path_hidden`), a path through a link (`path_through_link`), and a path not written as the names on disk (`path_name_differs`). See [Deleting a folder](#deleting-a-folder) |
| `rc-helper auth-code` | Finishes connecting with the code read from stdin — pressing Enter after Claude reports success, as it asks — and returns the account as `auth-status` does |
| `rc-helper git-status <path>` | A repository's branch — its name (`null` while detached), commit (`null` before the first), upstream and how far ahead and behind it is — and its `staged` and `not_staged` files, each with its change (`CHANGES`), where a rename or copy came from, whether it is a submodule, and `selectable: false` for a name that is not UTF-8. See [Git](#git) |
| `rc-helper git-branches <path>` | The local branches, each with whether it is current and its upstream, and the remote-tracking branches, each with the local branch tracking it or `null`; `origin/HEAD` and other symbolic refs are left out |
| `rc-helper git-stage <path>` | Stages the files named in a JSON list read from stdin; refuses a name outside the repository |
| `rc-helper git-unstage <path>` | Unstages the files named in a JSON list read from stdin, before the first commit too; a staged rename's new name takes the old one with it |
| `rc-helper git-commit <path> --author-name=<n> --author-email=<e>` | Commits what is staged with the message read from stdin, as `<n>` and `<e>` for both author and committer; refuses with `nothing_staged` when nothing is. The name must match `AUTHOR_NAME`, the address `EMAIL` and the message `COMMIT_MESSAGE`, each in `helpers/rc_helper.py` or the module it imports it from. Returns the new commit and the branch |
| `rc-helper git-switch <path> --branch=<b> [--remote]` | Switches to the local branch `<b>`, or with `--remote` makes a local branch tracking the remote-tracking branch `<b>` and switches to it; `branch_not_found` when there is no such branch of that kind. `<b>` must match `BRANCH_NAME` |
| `rc-helper git-branch-new <path> --name=<b>` | Makes the branch `<b>` at the current commit and switches to it. `<b>` must match `BRANCH_NAME`, and is `branch_invalid` unless `git check-ref-format --branch` takes it exactly as given |
| `rc-helper git-fetch <path>` | `git fetch --prune` from the default remote |

## Deleting a folder

`delete` is the most destructive verb, so it works in a fixed order and stops
at the first step that fails:

1. It refuses outright, with `delete_unsafe`, where `shutil.rmtree` does not
   resist symbolic-link attacks.
2. It finds the entry by its parent, resolved as any folder is, and its own
   name, which is never resolved (`resolve_entry`), so a link is the link and
   not where it leads. A path with any part that begins with a dot, as given
   or as resolved, is refused with `path_hidden`; that keeps `~/.local`,
   `~/.claude` and the like out of reach. A path whose resolved path
   differs from the path as written, normalised against the home folder, is
   refused with `path_through_link`, and one whose parts are not the names
   on disk letter for letter with `path_name_differs` (see below): servers
   are found by their folders' recorded paths, and either would reach
   servers other than those found.
3. It takes the folder lock exclusively (see below).
4. It surveys what it would remove: for a folder, it walks it without
   following links, noting each entry's path, device and inode, and refuses
   with `delete_mount_inside`, having done nothing, when anything inside is
   on another device: `shutil.rmtree` would cross into a mounted file
   system.
5. It interrupts every running server whose folder is the path or below it,
   then waits for all of them to exit within one `STOP_TIMEOUT`. It waits on
   the processes' exit, not on their screens. When any is still running, it
   refuses with `delete_stop_failed`, naming that server with its screen and
   listing those that stopped; nothing is forgotten or deleted.
6. It forgets every server there and checks that each is gone; otherwise
   `delete_forget_failed`, naming the server, and nothing is deleted.
7. It unlinks a link, or removes a folder with `shutil.rmtree` through the
   parent's file descriptor. Whatever could not be removed is reported as
   `left`, sorted and at most `DELETE_REPORT_LIMIT` paths, along with the
   servers `forgotten`: with `delete_failed` when nothing was removed —
   everything step 4 surveyed is still there, whatever else has appeared —
   and with `delete_incomplete` when anything was. No permission is changed to
   force it. Running `delete` again carries on from there.

rc-helper turns SIGTERM, which `run_bounded` sends when a call outlasts its
timeout (see [backend.md](backend.md)), into an exit, as an exception would
end it: a delete ended that way stops where it is. The servers it had forgotten stay forgotten, and the part
of the folder not yet removed stays; running `delete` again carries on.

It answers with the path deleted, whether it was a link, and the servers
stopped and forgotten.

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
  and must not be the home folder itself, and each part of the resolved path
  must be a name on disk letter for letter (`check_names`), or it is
  `path_name_differs`; a file system that ignores case would reach a folder
  under another spelling. So servers are recorded, and folders made, under
  their names on disk only. `stop`, `forget`, `output` and `confirm` also
  reach a server whose folder no longer exists, or is no longer named as it
  was: its path is then followed as far as it exists and must still lead
  inside the home folder.
- `mkdir` opens the parent folder one part at a time from the home folder,
  refusing to follow a link (`open_dir`), and makes the new folder through
  that file descriptor, so a part replaced by a link after the path was
  resolved is refused rather than followed.
- Starting, resuming, stopping, answering and forgetting a server, making
  a folder and every git verb take a shared `flock` on the home folder
  itself, and deleting takes it exclusively, so nothing is done to a server
  or a repository in a folder while it is being deleted. A lock held the other way is refused at once with
  `folder_busy`, never waited for. The lock is held by `rc-helper` only,
  never by the servers it starts.
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

## Git

The git verbs are `Helper.git_*` in `helpers/rc_helper.py`; the mechanics of
running git are `helpers/git_run.py`, tested by `tests/test_git_run.py`.

- **Which git.** git is looked for on `GIT_SEARCH_PATH` only, never on the
  person's own PATH. It is started with `git_env` and nothing else: the
  server's `HOME`, `USER`, `LOGNAME`, `PATH` and `LANG`, `LC_ALL=C.UTF-8`,
  `GIT_TERMINAL_PROMPT=0`, so that git never asks for a password, and
  `GIT_OPTIONAL_LOCKS=0`, so that a status writes nothing into the
  repository while Claude may be working in it. The person's git
  configuration, the system's too, and their hooks apply as they would in a
  terminal; hooks find the person's `~/.local/bin` first on their PATH.
- **Deadlines.** A verb's git calls share one deadline, `GIT_TIMEOUT`, and a
  fetch has `GIT_NETWORK_TIMEOUT`. Each git runs in a session of its own
  (`start_new_session`); at the deadline, when its output is left unread, or
  on SIGTERM, its whole process group is killed, hooks and remote helpers
  with it, and the verb answers `git_timeout`.
- **Refusals.** A git that refuses is `git_failed`, with what it wrote on
  stderr and stdout as `git` — at most `MAX_GIT_MESSAGE_LINES` lines — and
  how many lines more as `git_more`. Output git is not known to write, such
  as an unknown status record, is `git_unrecognised_output`.
- **Repositories.** The folder is resolved as any folder is, and must have a
  `.git` of its own, or it is `not_a_repository` and git is not asked:
  git would find a repository above it, the home folder's among them. Then
  `git rev-parse --show-toplevel` must be the folder itself, and its git
  directory and common directory must resolve inside the home folder, or it
  is `repository_outside_home`, as for a worktree of a repository elsewhere.
  `tree` marks every entry with `repository`: whether it has a `.git`.
- **Files** to stage or unstage are a JSON list on stdin, of at most
  `MAX_FILES_PER_CALL`. Each name is checked as written: empty, NUL, too
  long, or an empty or `.` part is `file_invalid`; absolute, a `..` part or
  a `.git` part is `file_outside_repository`; one trailing slash, as an
  untracked folder is listed, is allowed. Its folder is then resolved and
  must be in the repository, so a link to elsewhere is refused and a link in
  the repository is staged as the link. A name that is not UTF-8 is listed
  with `selectable: false` and refused as `file_invalid`. git reads the names
  NUL-separated on stdin, with `--literal-pathspecs`, so that `*` or
  `:(glob)` in a name is part of the name.
- **Lists** are capped: `MAX_LISTED_FILES` files in each of a status's
  lists and `MAX_LISTED_BRANCHES` branches in each list, with how many more
  as `more`.
- **Commit.** The message is read from stdin as UTF-8 and reaches git on
  stdin (`git commit -F -`); it never appears in any argv or answer. The
  author and committer reach git as `GIT_AUTHOR_*` and `GIT_COMMITTER_*`,
  which beat the person's `user.name` and `user.email`.

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
