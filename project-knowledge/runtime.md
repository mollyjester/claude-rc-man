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
  (`runtime.RANGE`; slot 0 is rcman's own ids, so no person has it). A workspace
  made from a devcontainer.json image whose user is not root has its user's ids
  swapped with root's by `PodmanRuntime._ids`, so that the user is the host id
  root has in the person's other workspaces and owns what root owns there (see
  [A repository's devcontainer.json](#a-repositorys-devcontainerjson));
- its volume at `VOLUME_MOUNT`, with its folders `work` (where the server runs),
  `claude` (`CLAUDE_CONFIG_DIR`) and `home` (`HOME`), the volume's root
  readable by the range's root alone;
- the person's credentials file bound over `claude/.credentials.json` (see
  [A person's Claude sign-in](#a-persons-claude-sign-in)), and the person's git
  folder bound at `~/.config/git` (see [The git folder](#the-git-folder));
- the tools (`tools_dir`) at `TOOLS_MOUNT` and the Claude Code store
  (`claude_store`) at `CLAUDE_MOUNT`, both read-only (rcman itself writes the store, see
  [backend.md](backend.md#claude-code-versions));
- a tmpfs at `RUN_MOUNT` for the tmux socket and the channel's files, the user's
  (`U`) when the ids are swapped;
- the mounted busybox's `sleep` as its process, with the image's own entrypoint
  replaced by the mounted busybox, so an image needs nothing of its own to stay
  up, and the volume's mount (`VOLUME_MOUNT`) as its working folder, which exists
  and is the user's, whatever folder the image sets and the user cannot enter;
- the labels `rcman.workspace` and `rcman.person`, by which `labelled` finds
  every workspace there is.

Podman copies an image for each range the first time a range uses it, so a
person's first workspace takes longest to make.

### A person's Claude sign-in

Each person has one sign-in, Claude Code's credentials file (`CREDENTIALS`),
alone in a volume of theirs, `account_volume`, labelled with the person.
`ensure_account` makes it, empty and owner-only, if it is not there: a
container that removes itself runs `ENSURE_SCRIPT` in the person's range, with
no network, so the file is their range's root's with mode 0600; `create`
calls it before making a workspace. `read_account` and `clear_account` are
such containers too, reading the file and emptying it in place.

Every workspace of the person's binds that one file over its own
`claude/.credentials.json`, by the volume's mountpoint, since the Podman the
installer installs refuses the volume `subpath` option; each keeps its own
`CLAUDE_CONFIG_DIR` otherwise. A workspace made before a sign-in sees the empty
file and the sign-in once it is written. A rename over a bound file is refused
(EBUSY), so Claude writes it in place there; nothing replaces it. The volume is
mounted by name by nothing that lasts, so Podman takes it for unused: it must
never be pruned, as [the installation guide](../docs/installation.md) says.
`remove_account` removes it; a person's is removed before their row (see
[people.md](people.md#deleting-a-person)).

`start_sign_in` starts the person's sign-in container, `sign_in_name`:
`podman run --rm --detach`, in the person's range, its process the mounted
busybox's `sleep` for the seconds it is given, so it removes itself even if
rcman does not; a tmpfs at `VOLUME_MOUNT` with a workspace's folders, the
person's file bound as a workspace's, and the tools and the Claude Code store.
It is labelled with the person only, never `rcman.workspace`, so `labelled`
and restore never see it. `end_sign_in` removes it. What runs in it is
[backend.md](backend.md#claude-accounts)'s.

### The git folder

The same volume holds a folder, `GIT_DIR`, which every workspace of the person's
binds at `HOME/GIT_HOME` (`~/.config/git`), by its path in the volume's
mountpoint like the credentials file. It is a folder and not a single bound
file because a bound file cannot be replaced by rename, and `git config
--global` needs to write beside its file. rcman owns two files in it
(`GIT_FILES`): `config`, which git reads as its global configuration (the commit
author, and a credential helper for `https://github.com` that only reads the
other file), and `credential`, the GitHub token in git's credential format,
owner-only, and empty when none is stored (see
[backend.md](backend.md#git-in-workspaces)).

`ENSURE_SCRIPT` makes the folder and an empty `credential` as well, so the
helper always has a file to read. `put_account(person, slot, name, data)` writes
one of `GIT_FILES` from stdin, in a container of the same kind as the others
(`PUT_SCRIPT`: no network, the person's range, removing itself), whole and
owner-only by a rename inside the folder, so a reader never sees half a file and
no copy of a token is left in a temporary one. Nothing about it needs a
workspace to be running, and the data is never in an argv. Forgetting the token
writes an empty `credential`; git in every workspace, running or not, has no
token from its next call. The helper only reads, so git never rewrites the
token. `remove_account` removes the folder with the credentials file. A
container made without the bind, as one made before git was bound is, has no
git configuration of its person's and must be made again; rcman does not bind a
folder into a container that exists.

`HostRuntime` keeps the folder in the person's folder under `ACCOUNTS` and links
`HOME/GIT_HOME` of each workspace to it.

`exists` answers from `podman container exists`: exit 1 alone means missing,
and anything else that is not 0, exit 125 among it, is `RuntimeUnavailable`.
`exec` runs one command with stdin, for what a channel cannot carry, and runs
it once more when conmon says it could not start it (`CONMON_FAILED`), as
nothing ran then. It takes a `timeout` in seconds (default `PODMAN_TIMEOUT`);
a command that has not ended by then is `RuntimeFailed`; and a `cwd`, the folder it
runs in (without one, the host runtime's work folder and the image's own working folder in
Podman, which has no work folder before `create` has made it; a caller that needs one names it). `discard` removes a container and keeps its
volume, so that `create` makes it again on the same files; `remove` takes both.
`pull` pulls an image, passing on each line it prints and stopping when its `cancel`
event is set; an image the runtime already has, such as one built or loaded there, is used
when its pull fails (`USING_LOCAL_IMAGE` is said), so no registry is needed for it, and `image_facts` reads one without running it (`ImageFacts`). The host
runtime has no images: a test plants them in its `images`, and its `specs` hold the
`Spec` each workspace was last made from. git runs under the
layout's `timeout` tool (busybox's applet in a container, the host's program on
the host runtime), which ends it first; `TIMED_OUT` lists the exit statuses
that mean it did.

`HostRuntime` keeps the same interface with a folder per workspace and the
host's own programs — a person's credentials file in `ACCOUNTS` under its
folder, linked to from each workspace's, and a sign-in container a folder that
only `end_sign_in` removes — for the tests and `make e2e`; `tests/runtime_contract.py`
holds both runtimes to the same cases, on the host in `tests/test_runtime.py`
and against real Podman in the podman host in
`tests/container/test_runtime_in_podman.py`, apart from its canary cases (see [The canary](#the-canary)). A `Layout` says where a runtime's
things are, as the programs in a workspace see them: the tools as argv
prefixes (busybox's applets in a container), the tmux socket and
configuration, Claude, the shell, terminal descriptions, and how to kill a
process group (`kill_group`: busybox's `kill` refuses a `--` that procps' needs).
`Layout.claude` is the program that runs whatever the store's `current` names, and
`Layout.claude_root` the store as the workspace sees it; `Layout.pinned(version)` is the layout
with Claude the exact file `versions/<version>`, which is what a server is started with.
A read that runs nothing of Claude's but what it needs, such as a status, goes through
`current` (`Workspaces.server` without a version).

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
  `claude_not_logged_in` when no account is. `Workspaces.start_server` refuses
  before that while the person has not connected their account, and seeds the
  account into the workspace's `.claude.json` (see
  [backend.md](backend.md#claude-accounts)).
- **Pinned window.** Each window is pinned to its size (`Tmux.pin`), so a terminal
  attached to the socket does not resize it; `tmux.conf` sets no global
  `window-size`, since a global `window-size manual` makes tmux 3.4's server
  exit at the next detached `new-session`.
- **Stopping.** `stop` sends Ctrl+C and waits up to `STOP_TIMEOUT` for the
  pane's process to exit; one still running is sent SIGTERM, and then SIGKILL
  to its whole process group, each followed by `END_GRACE_SECONDS`. Whatever
  the server started that is still running afterwards — found by parent from
  the workspace's `/proc` before the stop began and again before each signal,
  while the server still holds it, and still the same process by its start
  time — is killed too, so no Claude process is left, a session's command in a
  session of its own included. The pane's process is read once, and only it is
  signalled. A server still running after all that is `timeout`.
- **Answering.** `confirm` answers the folder-trust question after
  `ANSWER_PACE_SECONDS`, and once more if Claude asks again.
- **Unrecognised screens.** A screen in transition matches no known state, so
  a verb that waits reports `unrecognised` only once it has lasted
  `UNRECOGNISED_GRACE_SECONDS`.

### The version a server runs

`Workspaces.start_server` pins the server to a Claude Code version before it runs: it takes
the approved version, records it as the row's `claude_version` and starts the server with
`Layout.pinned` of it, so that approving another version changes nothing of a server that
runs (the rules and the pruning are [backend.md](backend.md#claude-code-versions)'s). The
record stays until the server is stopped or forgotten, and a start that leaves nothing running
clears it; a server whose state cannot be read keeps it. A restore starts servers the same
way, so after a reboot they run the version approved then.

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

## The canary

`Runtime.run_canary(directory, name, version)` tries a candidate Claude Code version: the file
`CANDIDATE` in `directory`. It runs the checks of `helpers/claude_canary.py` (which are
`helpers/claude_checks.py`'s) and returns the ones the candidate fails, each with its report;
empty means it passes them all. `ClaudeVersions` runs it only for a version never approved before (an earlier approved one still in the store is approved again without it). A canary that could not report (it did not start, did not
finish in `CANARY_SECONDS`, or printed something else) is a `RuntimeFailed`, which says nothing
about the candidate. `end_canary` removes the container.

`PodmanRuntime` runs it as a throwaway container from the default workspace image, in the
builder's uid range (`people.BUILDER_SLOT`), labelled `LABEL_CANARY` only so that no restore
takes it for a workspace's: no network, a read-only root with a tmpfs home, all capabilities
dropped, no new privileges, and mounted read-only only the candidate's folder and rcman's
helpers; one left from before is removed first. The checks need `python3` in the image. Whether
the real binary needs no network for them is a check of
[the real-Claude list](real-claude-checklist.md); if it does, the canary would have to use the
workspace network. `HostRuntime` runs the same program with the host's interpreter and a
clean environment, or a test's `canary_script`. The cases for both are `tests/runtime_contract.py`'s,
and against real Podman `tests/container/test_claude_versions_in_podman.py`'s.

## A repository's devcontainer.json

A workspace made by cloning is read for a `devcontainer.json` after the clone
(`Workspaces._configure`); what rcman honours, ignores and refuses, key by key, is
`devcontainer.POLICY`, and [the administrators' page](../docs/devcontainer.md) holds
the same table, which a test keeps equal. The pure module `backend/rcman/devcontainer.py`
holds the reader (`read`, through the workspace's `exec` in its work folder: a link is
never followed, the size is capped, no content reaches a message), the JSON-with-comments
parser, the policy, the plan (`plan`: the entries of the image's `devcontainer.metadata`
label, then the file's own, merged) and the parsing of an image's passwd and group files.

- **Order.** Read and check the file (`check`: refusals before anything is pulled or built);
  pull the image (`Runtime.pull`, its lines in the log), or for a file that builds one build
  it (see [Building an image](#building-an-image)); read the image without running it
  (`Runtime.image_facts`: a container made and never started, `podman cp` of the passwd and
  group files, which is stopped at `PODMAN_TIMEOUT` or when the workspace is deleted, and in
  which only podman saying the file is not there means the image has none: any other failure
  fails the reading, so a user is never reported missing because a copy merely failed; the image's labels, user and environment); make the plan; store it in the
  row's `environment`; close the channel, `Runtime.discard` the container (the volume
  stays) and make it again from the image as the plan's user; check libc (an image on
  musl is refused) and git; run the create commands. The workspace stays `creating`
  until all that is done.
- **Failure.** A `devcontainer.Refused` - a refused or unknown key, a file that cannot
  be read or parsed, an image that cannot be pulled, built, read, used or run - fails the workspace
  with a message from `problems.DEVCONTAINER_MESSAGES` and removes its container and
  volume, leaving only the failed row. A lifecycle command that fails or takes too long
  fails the workspace with its exit code and the tail of its output, and keeps the
  container and volume for the person to look at.
- **The user.** The plan's user is the image's user for `remoteUser`, else
  `containerUser`, else the image's own. `PodmanRuntime._ids` swaps the user's uid and gid
  with root's in the person's range, so that the user owns the volume, the shared
  credentials file and the git folder as root does in the person's other workspaces;
  `Spec.user` is passed as `--user` (a name or a number keeps the user's groups;
  `uid:gid` does not, so the groups are added with `--group-add`). The folders beside
  the mounts are made first by the ordinary map's root, since Podman would make them as
  the container's root, who in a swapped map is not the user. `HOME` stays the workspace's
  home.
- **The environment.** The image's own variables (but its `PATH`, kept as `image_path`, and
  the names in `devcontainer.RESERVED_ENV`) come first, then `containerEnv`, the container's;
  with `remoteEnv` it is what
  `Layout.env` holds for the workspace (`Workspaces._layout`), and so what git, tmux,
  Claude and the lifecycle commands get besides rcman's own variables.
  `devcontainer.RESERVED_ENV` are the names rcman sets itself that a file may not set.
  `LANG`, `LC_ALL` and `BROWSER` a file may: rcman sets each in only some of its programs
  (`LANG` for tmux and Claude, `LC_ALL` for git, `BROWSER` for Claude), and there its value
  comes after the file's and wins; everywhere else, lifecycle commands included, the file's is
  used. `PATH` may be set, with the folder of rcman's own programs kept first. Without a `PATH`
  of the file's, the image's is used.
- **The image's name.** `image` must be a plain registry reference, `[host[:port]/]path[:tag]
  [@digest]`, as written: no variable in it (it is the name that is pulled), and nothing
  that podman would read as a transport or a file (`docker-archive:`, `oci:`, `dir:` and the
  like), and no name that holds `BUILT_REPOSITORY` or is bare hexadecimal (`HEX_ID`), which
  Podman would take for an image built for a workspace or for any image by its id.
- **Commands.** `Workspaces._run_commands` runs each command in the work folder, as the
  container's user, under the `timeout` tool with `HOOK_TIMEOUT_SECONDS`, with exactly
  `_hook_env`; a string by the image's `/bin/sh` or else the mounted busybox's shell, an
  array as a program. Only the last `HOOK_KEEP_BYTES` of each of a command's output streams
  is ever held (`exec`'s `keep`). The create commands run once, in `_configure`;
  `postStartCommand` runs there too and again after every start or re-creation in a restore,
  before that workspace's server is started, and its failure is only logged. A container
  that kept running through a restart of rcman is not started, so nothing runs.

## Building an image

A file with `build` or `features` is built by `Workspaces._built` instead of pulled; the
administrators' account of it is [the building section of the devcontainer
page](../docs/devcontainer.md#building-an-image).

- **The builder.** `PodmanRuntime.build_image` runs one container, `rcman-build-<id>`
  (`builder.builder_name`), from `PodmanConfig.builder_image` (`RCMAN_BUILDER_IMAGE`), with a
  volume of the same name for its Podman's storage, both labelled `LABEL_BUILDER`.
  `builder.run_argv` is the argument list: the uid range of `people.BUILDER_SLOT`, which no
  person has and `_ids` maps like any other slot, never rcman's own; the network of a
  workspace; `/dev/fuse` and `SYS_ADMIN` and nothing else added; no `--privileged`. The
  installer reserves the range (see [installer.md](installer.md)) and builds the image from
  `installer/builder`.
- **In.** `_copy_in` streams the workspace's files from the workspace's container to the
  builder through rcman, as a tar archive (`builder.source_argv`, `sink_argv`): the top
  folder's git folder is left out on the sending side, the builder's tar being unable to
  exclude, and the files are the builder root's. The count of bytes is capped by
  `MAX_CONTEXT_BYTES`, checked before the CLI runs.
- **What the CLI gets.** `devcontainer.build_config` writes `BUILD_CONFIG_NAME` beside the
  file's own configuration, holding `BUILD_KEYS` only, since the CLI neither refuses nor
  reports what it does not know and the label would otherwise carry the file's own entry. It
  is run with `builder.cli_argv` in an environment of nothing but `PATH`.
- **Out.** The label is read in the builder and `devcontainer.check_metadata` holds each
  feature's entry to the policy, naming the feature, before the copy: `_stage` saves the
  image by id (`ARCHIVE_FORMAT`) in the builder into a private staging folder
  (`STAGING_PREFIX`) under `PodmanConfig.scratch_dir`, the database's parent folder by
  default, through `builder.pump`, which counts bytes, is capped by `MAX_IMAGE_BYTES` and
  `COPY_SECONDS`, and is killed by `cancel`. `builder.verify_archive` accepts the archive
  before anything reads it with `podman load -i` (`_load`): one image, no names, the
  configuration's hash equal to the id, nothing extracted. The image is tagged
  `built_name(id)`, one for each workspace; one loaded and not kept is removed by its id, and
  the staging folder always. The restore sweeps stale staging folders (`_clean_staging`).
  Feature `containerEnv` is in the image's environment, not the label, so the plan has it
  through `image_facts`.
- **Cleanup.** A `finally` removes the builder and its volume, and the image too unless it
  was kept; `remove_build` is also what a deletion calls, which ends a build in progress
  (killing the CLI's client would not). `Workspaces._remove_orphan_builds`, a step of the
  restore, removes `builders()` and `built_images()` that no workspace being made or stored
  plan needs.
- **Failures.** `BuildFailed` carries the redacted end of what the builder said and, where
  the runtime knows it, a `kind`; otherwise `builder.explain` finds the cause in the words
  (`CAUSES`), and `Workspaces._build_refusal` makes the `devcontainer.Refused` from
  `problems.DEVCONTAINER_MESSAGES`. A refusal or failure removes the builder, the volume and
  the image, and the workspace's container and volume.
- **Places.** `Workspaces._builds`, bounded by `MAX_BUILDS`, holds a build from its start;
  one that waits logs `WAITING_FOR_BUILD` and polls its `cancel`.
- **Restoring.** A plan whose image is one rcman built (`workspace_of_built_name`) is
  never pulled: if `built_images()` lacks it, the workspace fails with the
  `built_image_gone` message.

## What rcman keeps, and brings back

The `workspaces` table (`backend/rcman/db.py`) holds each workspace's state —
`creating`, `ready`, `failed` or `deleting` — and what should run: whether a
server is recorded in it (from its start until it is forgotten), whether it
should be running (`wanted`: set by a start or resume, cleared by a stop), and
the prefix it was started with.

- **Making.** `Workspaces.create` records the workspace as `creating` and makes
  it in a thread of its own; it becomes `ready`, or `failed` with what the
  runtime said, or with why the image's git does not do (none, or older than
  `GIT_MIN_VERSION` in `helpers/git_run.py`; see
  [backend.md](backend.md#git-in-workspaces)). Each step is a line of the row's
  `log`, which `ready` clears.
- **Cloning.** A workspace with a `source` is cloned after the git check, into
  its work folder, by `Repository.clone` (see
  [backend.md](backend.md#git-in-workspaces)). The clone runs under no lock, the
  person's and the workspace's alike, so that nothing waits for as long as it
  takes; `_working`, under the service's own lock, holds for each workspace whose
  clone, image pull, image build or lifecycle command is running the event a deletion sets. A
  failure fails the workspace with the cause, the exit code and git's
  words; the workspace and its folder stay until it is deleted. What follows the
  clone, a repository's devcontainer.json, is described under
  [A repository's devcontainer.json](#a-repositorys-devcontainerjson).
- **Deleting.** `delete` marks it `deleting` and removes its container and
  volume, which ends its server, then its row — never the person's sign-in; one still being made goes once
  it is made. One being cloned, pulled for, built for or running a lifecycle command is
  cancelled by deleting it: `_stop_working` sets its event, which stops a pull, and
  force-removes its builder, which ends a build, and its container, which ends git and the
  commands, and the
  making thread finds it `deleting` and finishes the removal; if the container
  cannot be removed now, the workspace goes when the work ends, at the latest at
  its timeout. Deleting a person deletes each of theirs (see [people.md](people.md)).
- **Restoring.** When rcman starts, `restore` runs in the background, so rcman
  answers at once: it waits for the runtime, trying again while it is
  unavailable; finishes rows still `deleting`, and `creating` ones with no `source`, which are
  made again; fails a `creating` one with a `source`, which a restart interrupted
  while it was being made, with `CLONE_MESSAGES["restarted"]` (it is not cloned
  again, nor are its create commands run again, whatever it had done: the person
  deletes it and decides); makes again a `ready` workspace whose container is
  gone, from its stored plan if it has one, and starts one that is stopped, and
  runs the plan's `postStartCommand` after either; removes what an interrupted
  read of an image left (`clean_probes`). The workspaces are brought back up to
  `RESTORE_WORKERS` at once, each in a thread of its own that goes on to start that
  workspace's server if it should be running (pinned to the approved version, as above), so that a workspace that is slow, a start
  command that hangs among the causes, does not keep another from being brought back; it
  holds one of the places until it is done, and the removal of what no row is for, the
  finishing of deletions and the settling of sign-outs wait for the last. A lost container is made again
  only after its plan's image is pulled again (`Runtime.pull`, which uses the image the
  runtime has when the registry cannot be reached). If the registry says the image is not there or
  may not be had (`workspaces.image_is_gone` reads its words), the workspace is failed, saying
  to delete it and make it again; any other failure of the pull - the network, a timeout, a
  registry that is down - leaves it ready without its container, logged, and the next restore
  tries again. What can be slow is done without the
  workspace's lock and under `_working_on`, so a deletion stops it. Restore stops between
  steps once rcman is closing;
  removes every labelled container no row is for; removes each person an
  earlier rcman was deleting, once none of their workspaces is left, their
  sign-in first; finishes each Claude sign-out that waits for servers that
  no longer run; and sweeps what a roll-out of a Claude Code version cut short left and prunes the store
  (`ClaudeVersions.sweep`, `prune`). A step that fails is logged
  on `rcman.workspaces`, and the others go on.

Each person has a lock (`person_lock`) that a start holds from its account
check until its server runs, and signing in and out holds (see
[backend.md](backend.md#claude-accounts)); it is always taken before a
workspace's. Each workspace has a lock of its own, so its server's verbs, its deletion and
restoring it take turns. Restore leaves a workspace a request is making to that
request, brings back each other one as its row is then (the quick steps under its lock,
the slow ones without it, see above), and looks again, under the lock, just before removing a container no row is for, so
a workspace made while it runs is never taken for one left over. Removing the
last workspace of a person being deleted finishes their deletion; one whose
container could not be removed stays `deleting`, for the next start.
