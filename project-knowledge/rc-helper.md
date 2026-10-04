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
  sentence from it. A rule's code is raised only in a function that checks its rule; a check
  the pattern cannot make — a folder name longer than the file system
  allows, a commit message too long, a value that is not UTF-8 — refuses
  with the same code only where the rule's sentence already says it. The
  backend checks the same rules and refuses a value one refuses with the
  same sentence; what only the helper can see, it refuses itself in words of
  its own — a path that leads
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
- Secrets — the one-time sign-in code and the GitHub token — are read from stdin, never from the
  command line, and never printed. So are a commit's message and the files to
  stage or unstage.

## Verbs

| Verb | What it does |
|---|---|
| `rc-helper tree [path]` | Subfolders of a folder in the home folder (the home folder itself when no path is given); `--depth` for nested levels, `--all` to include dot-folders. Symbolic links leading outside the home folder are left out, and so is anything that is, or leads into, a `.git` folder |
| `rc-helper list` | Every server and its state |
| `rc-helper start <path> --prefix <p>` | Starts a server in a folder, with `<p>` as the session name prefix. Refuses when Claude is not connected or a server already runs there; in a folder whose server has ended, whatever its state, it starts that server again |
| `rc-helper resume <path> --prefix <p>` | Starts a server again that is `stopped` or `exited` (`RESUMABLE` in `helpers/rc_helper.py`); refuses with `not_stopped` when there is none |
| `rc-helper stop <path>` | Stops a running server with Ctrl+C and waits for it to exit; ends one that does not. See [Stopping a server](#stopping-a-server) |
| `rc-helper forget <path>` | Removes a server that has stopped from the list |
| `rc-helper output <path>` | The server's current screen and state |
| `rc-helper confirm <path> --answer y\|n` | Answers the folder-trust question |
| `rc-helper auth-status` | Whether a Claude account is connected, which, and when the sign-in expires (`login_expires_at`, or `null` when unknown) |
| `rc-helper auth-login --email <e>` | Starts connecting a Claude account; returns the sign-in URL |
| `rc-helper auth-code` | Finishes connecting with the code read from stdin — pressing Enter after Claude reports success, as it asks — and returns the account as `auth-status` does |
| `rc-helper mkdir [parent] --name=<name>` | Makes a folder named `<name>` in a folder, or in the home folder when no parent is given. The name must match `FOLDER_NAME` in `helpers/rc_helper.py`, whose sentence says what it refuses and which shares its invisible characters with `AUTHOR_NAME`, and be no longer than the file system's `PC_NAME_MAX` in bytes; refuses with `folder_exists` when anything, even a dangling link, has that name |
| `rc-helper delete <path>` | Stops every running server in a folder or below it, forgets every server there, then deletes the folder and everything in it; a link is deleted as the link. Refuses the home folder, anything outside it, a dot-folder or anything in one (`path_hidden`), a path through a link (`path_through_link`), and a path not written as the names on disk (`path_name_differs`). See [Deleting a folder](#deleting-a-folder) |
| `rc-helper git-status <path>` | A repository's branch — its name (`null` while detached), commit (`null` before the first), upstream and how far ahead and behind it is, both `null` while the upstream is configured but gone from the remote-tracking branches, as after a fetch prunes it — and its `staged` and `not_staged` files, each with its change (`CHANGES`), where a rename or copy came from, whether it is a submodule, and `selectable: false` for a name that is not UTF-8; `merging`, whether a merge is under way, which is so until it is committed or aborted; `push_has_remote`, whether a push from it has a remote to go to: true for a branch with an upstream, and otherwise — a branch with no upstream, or no branch checked out — whether git knows a remote `origin`; `push_to_github`, whether a push from it goes to GitHub (see [Push](#push)); `github_token`, for a push to GitHub whether a token is stored, as `github-status` says, and otherwise `null`; and `push_ahead`, how many commits where the branch pushes lacks — its push destination (`@{push}`) when git names one, otherwise its upstream, and `null` with neither or while no branch is checked out. See [Git](#git) |
| `rc-helper git-branches <path>` | The local branches, each with whether it is current and its upstream, and the remote-tracking branches, each with the local branch tracking it or `null`, and `taken_by`, the local branch that keeps git from making the branch `git switch --track` would make to track it, named by what follows the first slash of its name, whatever the remote is called (`tracking_name`) — one with that name, or, as git keeps branch names as paths, one it cannot keep beside it (`blocking_branch`) — or `null`; `origin/HEAD` and other symbolic refs are left out |
| `rc-helper git-stage <path>` | Stages the files named in a JSON list read from stdin; refuses a name outside the repository |
| `rc-helper git-unstage <path>` | Unstages the files named in a JSON list read from stdin, before the first commit too; a staged rename's new name takes the old one with it |
| `rc-helper git-commit <path> --author-name=<n> --author-email=<e>` | Commits what is staged with the message read from stdin, as `<n>` and `<e>` for both author and committer; refuses with `nothing_staged` when nothing is. The name must match `AUTHOR_NAME`, the address `EMAIL` and the message `COMMIT_MESSAGE`, each in `helpers/rc_helper.py` or the module it imports it from. Returns the new commit and the branch |
| `rc-helper git-switch <path> --branch=<b> [--remote]` | Switches to the local branch `<b>`, or with `--remote` makes a local branch tracking the remote-tracking branch `<b>` and switches to it; `branch_not_found` when there is no such branch of that kind, and, before git is asked, `branch_name_taken` when a local branch keeps git from making the new one, as for `taken_by` in `git-branches`. `<b>` must match `BRANCH_NAME` |
| `rc-helper git-branch-new <path> --name=<b>` | Makes the branch `<b>` at the current commit and switches to it. `<b>` must match `BRANCH_NAME`, and is `branch_invalid` unless `git check-ref-format --branch` takes it exactly as given |
| `rc-helper git-fetch <path>` | `git fetch --prune` from the default remote |
| `rc-helper git-push <path> [--check]` | Pushes the current branch, and returns it, its upstream and whether the push set it (`upstream_set`); refuses with `detached_head` while no branch is checked out, with `no_push_remote` for a branch with no upstream and no remote `origin`, with `github_token_not_set` for a push to GitHub with no token stored, and with `no_commits_yet` before the branch's first commit. With `--check`, refuses as it would, but for `no_commits_yet`, and pushes nothing. See [Push](#push) |
| `rc-helper github-token` | Stores the token read from stdin as git's credential for GitHub, in place of any rcman stored before, and has git use its store helper for GitHub. The token must match `GITHUB_TOKEN`. See [GitHub token](#github-token) |
| `rc-helper github-status` | Whether a token rcman stored is there and git is told to use the store helper for GitHub, as `token` |
| `rc-helper github-forget` | Erases the token rcman stored, and nothing else |
| `rc-helper git-clone [parent] --url=<url>` | Clones the repository at `<url>` into a folder, or into the home folder when no parent is given, as `git clone -- <url>` run there makes it: in the folder git names, `clone_folder_name`. `<url>` must match `GITHUB_URL`. Refuses with `folder_exists` when anything has that name, an empty folder too. Returns the new folder as `created`. See [Clone](#clone) |

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
5. It stops every running server whose folder is the path or below it, all
   at once, as `stop` does (see [Stopping a server](#stopping-a-server)),
   ending any that Ctrl+C does not. When any is still running even so, it
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
end it, and exits with no answer: the backend answers `helper_timeout`.
Every process rc-helper runs as its own child that still runs, git and its
hooks among them, is ended on the way out; servers it started in tmux keep
running. A delete ended that way stops where it is: the servers
it had forgotten stay forgotten, and the part of the folder not yet removed
stays; running `delete` again carries on.

It answers with the path deleted, whether it was a link, and the servers
stopped and forgotten.

## Stopping a server

`stop`, and `delete` for every server in the folder, stop servers in one
function, `Helper._end` in `helpers/rc_helper.py`:

1. It sends Ctrl+C, and waits up to `STOP_TIMEOUT` for the server's process
   to exit. It waits on the exit status, not on the screen, which may be one
   rcman does not recognise.
2. A server still running then is ended: its own process, the pane's, is
   sent SIGTERM, and when it is still running `END_GRACE_SECONDS` later, its
   whole process group — which tmux made for it, and which holds what it
   started there — is sent SIGKILL, and waited for as long again.
3. Whatever a server ended that way left running in its process group is
   killed too.

The process each pane runs is read once, when the stop begins, and only that
process is waited for and signalled. `stop` and `resume` share the lock, so
a resume can start a server again in the same pane meanwhile; once the pane
runs another process, the stop takes its own as ended and leaves the new one
alone. A process ID that now belongs to someone else (`PermissionError`) is
taken as gone.

tmux keeps the final screen, so an ended server is `exited`, with the exit
status the signal gave it, and can be resumed or forgotten. `stop` answers
with the server as it is then: `stopped` when Ctrl+C stopped it, `exited`
when it was ended. A server that is still running after SIGKILL is
`timeout`.

Claude Code ignores Ctrl+C at some questions: at the spawn-mode question
(see [Processes and isolation](#processes-and-isolation)), it neither exits
nor takes an answer afterwards, as recorded (`spawn_prompt` in
`devtools/record_claude.py`). The backend waits for a stop and for a delete
longer than all of step 1 and 2 (`tests/test_helpers_client.py`,
`tests/test_folders.py`).

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
| `trust_declined` | The trust question was answered no; the server ended, and `start` starts it again |
| `logged_out` | Claude refused because no account is connected; `start` starts it again |
| `exited` | The server ended in a way not listed above, whatever its exit status — it stopped by itself; `exit_code` and `screen` are set, and `start` or `resume` starts it again |
| `unrecognised` | A live screen that matches no known state; `screen` is set |

A screen in transition can match no known state for a moment, so verbs that
wait report `unrecognised` only once it has lasted
`UNRECOGNISED_GRACE_SECONDS`.

In a git repository the server's status line names the branch; the states
are read from the capacity line under it, so a server there is `connected`
and `ready` as anywhere else. The spawn-mode question Claude asks in a
repository when it is started without `--spawn` is no state of its own:
rc-helper always passes `--spawn`, so a server shows it only if Claude Code
changes, and it is then `unrecognised`, with its screen, and `stop` ends it.

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
- `mkdir` and `git-clone` open the parent folder one part at a time from
  the home folder, refusing to follow a link (`open_dir`), and make the new
  folder through that file descriptor, so a part replaced by a link after
  the path was resolved is refused rather than followed. `git-clone` also
  holds the new folder open, runs git in it and clones into it through that
  descriptor, and removes a failed clone through it (see [Clone](#clone)).
- Starting, resuming, stopping, answering and forgetting a server, making a
  folder, and every `git-*` and `github-*` verb (`git_verb`) take a shared `flock` on the home folder itself,
  and deleting takes it exclusively, so nothing is done to a server or a
  repository in a folder while it is being deleted. A lock held the other way
  is refused at once with `folder_busy`, never waited for. The lock is held
  by `rc-helper` only, never by the servers it starts.
- Servers run in tmux on a dedicated socket (`TMUX_SOCKET_NAME`), configured by
  `helpers/tmux.conf`, so they never mix with the user's own tmux and keep
  their final screen and exit status after they exit. Each folder has its own
  session, named from a hash of its path, with the path stored in the
  session's `@rcman_path` option; at most one exists per folder.
- Claude Code is `~/.local/bin/claude` of the user. It is started through
  `/usr/bin/env -i` with only the variables `child_env` lists — never through
  a shell, and with self-update disabled. Remote Control sessions start in
  permission mode `default`, and in spawn mode `same-dir`
  (`--spawn same-dir`): one server per folder, its sessions all working in
  that folder, as the Changes section shows it. Without `--spawn`, Claude
  asks for a spawn mode in a git repository. The options are
  `server_command`, which a test holds to the recorder's `RC_ARGS`, so that
  the contract tests check each is in Claude's `--help`.
- The sign-in expiry is read from Claude Code's credentials file
  (`CREDENTIALS`), from `claudeAiOauth.refreshTokenExpiresAt` only; no token
  is read into the output. It is an internal file, so anything unexpected
  makes the expiry unknown rather than an error.
- The sign-in code reaches the sign-in prompt through a tmux paste buffer
  loaded from stdin, so it never appears in any process's arguments.
- `rc-helper` refuses to run as root. It runs on the system Python with only
  the standard library and the helpers' own modules; the launcher runs it in
  isolated mode and in UTF-8 mode (`-IXutf8`), as every launcher in
  `helpers/` does, so its arguments, file names and standard streams are
  UTF-8 whatever the locale, and puts the directory above `helpers/` on its
  path.

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
- **Deadlines.** A verb's git calls share one deadline, `GIT_TIMEOUT`; a
  fetch and a push have `GIT_NETWORK_TIMEOUT`, and a clone `CLONE_TIMEOUT`,
  each read when the verb is called (`git_verb` names the constant). Each git runs in a session of its own
  (`start_new_session`); at the deadline, when its output is left unread, or
  on SIGTERM, its whole process group is killed, hooks and remote helpers
  with it. At the deadline the verb answers `git_timeout`; on SIGTERM it
  answers nothing. SIGTERM waits while git is being started, so it cannot
  fall between git starting and rc-helper holding it; git starts with
  SIGTERM unblocked.
- **Refusals.** A git that refuses is `git_failed`, with what it wrote on
  stderr and stdout as `git` — at most `MAX_GIT_MESSAGE_LINES` lines — and
  how many lines more as `git_more`; for a git whose standard output may
  hold a secret, what it wrote on stderr only
  (`secret_output` in `git_run.Git.run`). Output git is not known to write, such
  as an unknown status record, is `git_unrecognised_output`.
- **Repositories.** The folder is resolved as any folder is, and must have a
  `.git` of its own, or it is `not_a_repository` and git is not asked:
  git would find a repository above it, the home folder's among them. Then
  `git rev-parse --show-toplevel` must be the folder itself, or it is
  `not_a_repository`, and its git directory and common directory must
  resolve inside the home folder, or it is `repository_outside_home`, as for
  a worktree of a repository elsewhere.
  `tree` marks every entry with `repository`, found without running git
  (`looks_like_repository`): whether its `.git` is a folder with a `HEAD`, or
  a file whose gitdir line leads to one, either resolving inside the home
  folder.
- **Files** to stage or unstage are a JSON list on stdin, of at most
  `MAX_FILES_PER_CALL`; anything else — not a JSON list of names, empty, or
  longer — is `file_list_invalid`. Each name is checked as written: empty, NUL, too
  long, or an empty or `.` part is `file_invalid`; absolute, a `..` part or
  a `.git` part in any case is `file_outside_repository`; one trailing slash, as an
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

## Push

`git-push` pushes the current branch. One with no upstream is pushed as
`git push -u origin HEAD` (`PUSH_REMOTE`), which makes it track the branch
it makes there; one with an upstream as `git push`, so that the person's
`push.default` and push remotes apply. These are refused before git is
asked to push, in this order: a detached HEAD, `detached_head`; a branch
with no upstream in a repository where git knows no remote `origin`
(`git remote get-url --push` fails), `no_push_remote`, since
`git push -u origin HEAD` has nowhere to go — `git-status` reports the
same as `push_has_remote`; a push to GitHub with no token, below; and a
branch with no commit yet, `no_commits_yet`.

A push goes to GitHub when the push URL of the remote it goes to — the
branch's push remote as git finds it (`%(push:remotename)`), or `origin`
for a branch with no upstream — starts with `Config.github` once git has
rewritten it (`git remote get-url --push`), so that the person's own
`insteadOf` and `pushurl` settings decide it as they decide the push. An
address naming a user (`https://you@github.com/...`) does not: git asks
for that user's credential, which rcman's, stored under `GITHUB_USERNAME`,
is not.
Before a branch's first commit git lists no ref of it, so it names neither
its upstream nor its push remote; both are then read from the branch's
configuration, in the order git push takes them
(`_configured_push_target`): an upstream needs `branch.<b>.remote` and
`branch.<b>.merge`, and the push remote is `branch.<b>.pushRemote`, else
`remote.pushDefault`, else `branch.<b>.remote`. A clone of an empty
repository whose remote is not called `origin` has one such branch.
Only a push to GitHub needs the token, and is refused with
`github_token_not_set` without one, before anything is pushed; a push
elsewhere is tried, and git's refusal, if any, passed on. `git-status`
reports the same as `push_to_github`. `--check` makes every check but the
last, no commit yet, and stops: the backend's Commit & Push checks first,
so that nothing is committed for a push that would not be tried, and its
commit makes a branch's first commit.

## Clone

`git-clone` works in a fixed order, and makes nothing until the last step
before git:

1. It refuses an address `GITHUB_URL` does not take with `url_invalid`,
   before any path is looked at or the folder lock taken. The rule takes no
   user or password, so no token can be in an address, and an address that
   starts with a hyphen is no address.
2. It refuses outright, with `clone_unsafe`, where `shutil.rmtree` does not
   resist symbolic-link attacks: it could not remove a failed clone safely.
3. It takes the folder lock shared, as every git verb does, resolves and
   opens the parent as `mkdir` does, and checks the new folder's name,
   `clone_folder_name`, as `mkdir` checks one.
4. It makes the folder itself, through the parent's file descriptor, and
   opens it without following a link, holding that descriptor until it is
   done. Anything already there with that name — an empty folder too, which
   git would clone into — is `folder_exists`, and git is not run. So is
   finding, once it is made, a link, nothing or a folder that is not empty
   at its name: something moved the new folder away, and where it went
   cannot be known, so the answer lists it as `left` under the path it was
   made at.
5. git runs as `git clone -- <url> .` and nothing else, so the person's
   configuration applies, `GIT_ALLOW_PROTOCOL` among what is not set, and
   submodules are not cloned. Its working folder is the folder made in
   step 4, through its descriptor (`/proc/self/fd/<descriptor>`), and it
   clones into it, `.`: neither a part of the parent's path nor the folder's
   own name, replaced by a link since, can take the clone elsewhere. That
   folder is empty, so nothing in it can be named like the address, which
   git would take as a path. Its deadline is the verb's, `CLONE_TIMEOUT`,
   less `CLONE_CLEANUP_SECONDS`, kept for step 7. git reaches a private
   repository with the stored [GitHub token](#github-token).
6. Once git has finished, what has the folder's name must still be the
   folder made in step 4, by device and inode, and hold a `.git`, or the
   clone is `clone_missing`.
7. On any failure after step 4 — git's refusal, the deadline, SIGTERM or
   anything else — the folder made in step 4 is removed: everything in it
   through its own descriptor, wherever it has been moved, with
   `shutil.rmtree` for a folder in it and unlink for anything else; then
   the folder itself through the parent's descriptor, while what has its
   name is still that folder by device and inode. A folder put in its place
   is left alone; only an empty folder put there in the instant between
   that check and the removal could be removed instead, which no removal by
   name can rule out. When anything in the folder cannot be removed, or it
   is no longer where it was made, the failure's answer lists it as `left`,
   where it is now — read from its descriptor, unless it has no name left,
   having been removed meanwhile.

SIGTERM is held back from just before the folder is made until `git-clone`
answers, but while git runs, so it
cannot fall between the folder being made and its removal being set
going, nor cut the removal short; a SIGTERM more while rc-helper ends is
ignored. One that comes after git has succeeded, once SIGTERM is held back
again, ends rc-helper with no answer and the finished clone kept: the
person is answered `helper_timeout`, and the clone is in the tree. One in
the instant between git's success and SIGTERM being held back again is a
failure like any other, and the clone is removed. A removal that outlasts `CLONE_CLEANUP_SECONDS`
goes on after the backend has stopped waiting, and the person is answered
`helper_timeout` too.

## GitHub token

The token is git's own credential for GitHub in the person's home, so that
any git push as the person, a Claude session's among them, uses it.

- **Where.** `git credential-store store` writes it, read from stdin, for
  the protocol and host of `Config.github` with the username
  `GITHUB_USERNAME`, into the file git's store helper uses —
  `~/.git-credentials`, or `~/.config/git/credentials` when only that one
  exists, since rc-helper's git has no `XDG_CONFIG_HOME` — readable by the
  person only. It replaces an entry
  rcman stored before and goes first in the file, so git's store helper
  offers it before any other credential stored there for GitHub. Then
  `credential.<Config.github, without its final slash>.helper store` is
  added to the person's global git configuration (`git config --global`)
  unless that helper is listed there already. Credential
  helpers the person has set themselves stay, and git asks them in the order
  its configuration lists them.
- **Not through the person's helpers.** rc-helper runs `git credential-store`
  itself, not `git credential approve` and `reject`, so that the token never
  reaches the person's other credential helpers, and `github-forget`
  erases only the entry with rcman's username for that host. It leaves the
  `store` helper in the global configuration.
- **Status.** `github-status` asks `git credential-store get` for rcman's
  entry, keeps only whether a password came back, and checks that the
  `store` helper is listed; its answer from git is read with `secret_output`,
  so that a refusal could not carry it.
- **Never shown.** The token is in no argv, answer or refusal.
- **Refused by GitHub.** When GitHub refuses the token, git erases it
  through the store helper itself, so that `github-status` reports none.

[The guide](../docs/git.md#github-token) says where the token is kept and
what removing it leaves; `token_guide_problems` in `tests/test_rc_helper.py`
holds it to what `github-token` and `github-forget` change in a home of
either layout, run for GitHub itself.

## sudo

`helpers/sudoers.rc-helper` is the sudoers rule template: the application user
may run `rc-helper`, and nothing else, as any member of the `rcusers` group,
with the environment reset. The installer (`sudoers()` in
`installer/install.py`) fills in `@HELPER@` and `@APP_USER@`.
A test renders it and validates it with `visudo -c`.

## Test hooks

`RC_HELPER_HOME`, `RC_HELPER_CLAUDE` and `RC_HELPER_TMUX_SOCKET` replace the
home folder, the Claude binary and the tmux socket. `RC_HELPER_GITHUB`, a
base address ending in a slash, replaces GitHub: `git_env` gives git
`url.<address>.insteadOf https://github.com/` through `GIT_CONFIG_COUNT`, so
every fetch from and push to `https://github.com/` goes there instead. git
looks a credential up for the address it rewrote to, so under the hook the
GitHub token is stored, and its credential helper set, for that address
rather than for `https://github.com`; without the hook, `Config.github` is
`GITHUB_ADDRESS` and git is given nothing.

The sudoers rule resets the environment, so none of them reaches a helper
started through sudo. The tests point them at a temporary home, a private
socket, the fake `claude` — see [fake-claude.md](fake-claude.md) — and the
tests' GitHub, or an address nothing answers on — see [tests.md](tests.md).
