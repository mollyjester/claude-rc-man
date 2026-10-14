# Workspaces, their runtime and their servers

How rcman runs workspaces and the Claude server in each. What a workspace is
for colleagues: [the workspaces guide](../docs/workspaces.md). The routes are in
[backend.md](backend.md); the people a workspace belongs to in [people.md](people.md).

| Module | What it is |
|---|---|
| `backend/rcman/runtime.py` | Where workspaces run: the `Runtime` interface, `PodmanRuntime` and `HostRuntime` |
| `backend/rcman/channel.py` | The long-lived shell per workspace that commands go through |
| `backend/rcman/claude_server.py` | A workspace's server: its tmux, its states and its verbs |
| `helpers/screens.py` | What Claude Code shows, and the state read from it |
| `backend/rcman/workspaces.py` | The `workspaces` table and the runtime brought into line with it |
| `backend/rcman/tmux.conf` | tmux's configuration, which the installer copies into the tools |

## The runtime

A workspace is one container, `rcman-ws-<id>` after its row, with a volume of
the same name. rcman drives Podman only as `podman --remote` against its own
user manager's socket (`main.podman_socket`), with a fixed argv and the
environment `podman_env` allows, never through a shell. The containers, and
conmon and the network helper with them, live in rcman's user manager, not in
`rcman.service`, so they outlive a restart or an upgrade of rcman.

`PodmanRuntime.create` makes a container with:

- `--init`, so orphans are reaped;
- the network `slirp4netns:allow_host_loopback=false`, and no port published;
- `--uidmap` and `--gidmap` `0:<slot × RANGE>:RANGE`, the person's range
  (`runtime.RANGE`; slot 0 is rcman's own ids, so no person has it);
- its volume at `VOLUME_MOUNT`, with its folders `work` (where the server runs),
  `claude` (`CLAUDE_CONFIG_DIR`) and `home` (`HOME`), the volume's root
  readable by the range's root alone;
- the tools (`tools_dir`) at `TOOLS_MOUNT` and the Claude Code store
  (`claude_store`) at `CLAUDE_MOUNT`, both read-only;
- a tmpfs at `RUN_MOUNT` for the tmux socket and the channel's files;
- the mounted busybox's `sleep` as its process, so an image needs nothing of its
  own to stay up;
- the labels `rcman.workspace` and `rcman.person`, by which `labelled` finds
  every workspace there is.

Podman copies an image for each range the first time a range uses it, so a
person's first workspace takes longest to make.

`exists` answers from `podman container exists`: exit 1 alone means missing,
and anything else that is not 0, exit 125 among it, is `RuntimeUnavailable`.
`exec` runs one command with stdin, for what a channel cannot carry, and runs
it once more when conmon says it could not start it (`CONMON_FAILED`), as
nothing ran then.

`HostRuntime` keeps the same interface with a folder per workspace and the
host's own programs, for the tests and `make e2e`; `tests/runtime_contract.py`
holds both runtimes to the same cases, on the host in `tests/test_runtime.py`
and against real Podman in the podman host in
`tests/container/test_runtime_in_podman.py`. A `Layout` says where a runtime's
things are, as the programs in a workspace see them: the tools as argv
prefixes (busybox's applets in a container), the tmux socket and
configuration, Claude, the shell, terminal descriptions, and how to kill a
process group (`kill_group`: busybox's `kill` refuses a `--` that procps' needs).

## The channel

Starting a process in a container costs hundreds of milliseconds, too much for
polls and screen reads, so each workspace's commands go through one shell that
stays open: `podman --remote exec -i <container> busybox sh -s`. A request is
one line, the argv quoted for the shell, run with stdin from `/dev/null` and
its output streams into files of the channel's own in `RUN_MOUNT`; the shell
answers with a header — the request's nonce, the exit code and the two sizes —
and the two files' bytes. An argument with a newline or NUL is refused before
anything is sent. Requests are answered one at a time, each within its
timeout; one that is not is `ChannelTimeout`, and the channel is closed.

Closing a channel does not wait for a request in flight: it kills the shell,
and the request fails as broken. A channel is opened once more when conmon's
error kept it from opening, and
when it was found dead before a request was written; a request that was
written is never sent again, since it may have run (`ChannelBroken`). A channel
opens when a workspace's server is first reached and closes once it has been
idle for `CHANNEL_IDLE_SECONDS` (`workspaces.py`); a workspace whose row records
no server is listed without reaching it. `LATENCY_P95_SECONDS` in
`channel.py` is what a request may cost, and the podman host's suite holds the
podman client of a channel to a memory bound of its own.

## A workspace's server

`claude_server.Server` runs everything in the workspace through the channel:
tmux, Claude, the reads of `/proc` and every signal, since the processes and
their ids are the workspace's. tmux runs on the workspace's own socket with
`tmux.conf` and the environment `tmux_env` gives it, its shell and terminal
descriptions the mounted ones, since a slim image has none. The server runs in
the session `SESSION`, in the window size `WIDTH` by `HEIGHT`
(`helpers/screens.py`), started through `env -i` with exactly `child_env`:
the workspace's `CLAUDE_CONFIG_DIR` and `HOME`, and `DISABLE_AUTOUPDATER=1`,
since Claude would otherwise install a version of its own into the workspace,
the read-only store notwithstanding. Its options are `server_command`, which a
test holds to the recorder's, so the contract tests check each is in Claude's
`--help`.

- **Signed in.** `start` asks Claude with `auth status`, and refuses with
  `claude_not_logged_in` when no account is. Nothing signs Claude in in a
  workspace yet, so a start answers that until it does.
- **Pinned.** Each window is pinned to its size (`Tmux.pin`), so a terminal
  attached to the socket does not resize it; `tmux.conf` sets no global
  `window-size`, since a global `window-size manual` makes tmux 3.4's server
  exit at the next detached `new-session`.
- **Stopping.** `stop` sends Ctrl+C and waits up to `STOP_TIMEOUT` for the
  pane's process to exit; one still running is sent SIGTERM, and then SIGKILL
  to its whole process group, each followed by `END_GRACE_SECONDS`. Whatever
  the server started that is still running afterwards — found from the
  workspace's `/proc` before the stop began, and still the same process by its
  start time — is killed too, so no Claude process is left, one in a session
  of its own included. The pane's process is read once, and only it is
  signalled. A server still running after all that is `timeout`.
- **Answering.** `confirm` answers the folder-trust question after
  `ANSWER_PACE_SECONDS`, and once more if Claude asks again.
- **Unrecognised screens.** A screen in transition matches no known state, so
  a verb that waits reports `unrecognised` only once it has lasted
  `UNRECOGNISED_GRACE_SECONDS`.

### Server states

`classify` in `helpers/screens.py` turns a server's screen and exit status into
one of these states. Its screen patterns are held to every recorded fixture and
every observed screen (see [fake-claude.md](fake-claude.md)) by
`tests/test_screens.py`.

| State | Meaning |
|---|---|
| `starting` | The server has printed nothing yet |
| `trust_prompt` | Claude asks whether to trust the folder; answer with `confirm` |
| `connected` | The server is up; no session has attached yet |
| `ready` | A session is attached; `session_name` and `environment_url` are set |
| `stopped` | Stopped with its sessions preserved; `start` or `resume` brings them back |
| `trust_declined` | The trust question was answered no; the server ended, and `start` starts it again |
| `logged_out` | Claude refused because no account is signed in; `start` starts it again |
| `exited` | The server ended in a way not listed above; `exit_code` and `screen` are set |
| `reconnected` | A live screen of Claude's log lines only, the last saying Remote Control has reconnected; `screen` is set |
| `unrecognised` | A live screen that matches no known state; `screen` is set |

## What rcman keeps, and brings back

The `workspaces` table (`backend/rcman/db.py`) holds each workspace's state —
`creating`, `ready`, `failed` or `deleting` — and what should run: whether a
server is recorded in it (from its start until it is forgotten), whether it
should be running (`wanted`: set by a start or resume, cleared by a stop), and
the prefix it was started with.

- **Making.** `Workspaces.create` records the workspace as `creating` and makes
  it in a thread of its own; it becomes `ready`, or `failed` with what the
  runtime said.
- **Deleting.** `delete` marks it `deleting` and removes its container and
  volume, which ends its server, then its row; one still being made goes once
  it is made. Deleting a person deletes each of theirs (see
  [people.md](people.md)).
- **Restoring.** When rcman starts, `restore` runs in the background, so rcman
  answers at once: it waits for the runtime, trying again while it is
  unavailable; finishes rows still `creating` or `deleting`; makes again a
  `ready` workspace whose container is gone, and starts one that is stopped;
  starts each server that should be running and is not, with its prefix;
  removes every labelled container no row is for; and removes each person an
  earlier rcman was deleting, once none of their workspaces is left. A step that fails is logged
  on `rcman.workspaces`, and the others go on.

Each workspace has a lock of its own, so its server's verbs, its deletion and
restoring it take turns. Restore leaves a workspace a request is making to that
request, brings back each other one under its lock as its row is then, and
looks again, under the lock, just before removing a container no row is for, so
a workspace made while it runs is never taken for one left over. Removing the
last workspace of a person being deleted finishes their deletion; one whose
container could not be removed stays `deleting`, for the next start.
