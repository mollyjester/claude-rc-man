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
- the network `PodmanConfig.network`, `NETWORK` (slirp4netns with no way to the host's
  loopback) unless it is given another, and no port published;
- `--uidmap` and `--gidmap` `0:<slot × range>:range`, the person's range, where `range` is
  `PodmanConfig.range`, `RANGE` unless it is given another (slot 0 is rcman's own ids, so no
  person has it). A workspace
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
- an anonymous volume at `TMP_MOUNT` (`/tmp`), on rcman's disk and not on the container's
  overlay root, so that overlay storage a Podman in the workspace keeps under `/tmp` is native
  (see [Containers in a workspace](#containers-in-a-workspace)). Podman mounts it `nosuid` and
  `nodev`, and rcman adds no `noexec` of its own; it keeps what it holds across a stop and a
  start, and goes with the container (`_remove_container`), so a container made again, by
  `discard` and `create`, has an empty one. Right after the `run`, `_own_tmp`, as the container's
  root even where the ids are swapped, first checks that `/tmp` is a folder and not a link, and
  fails `create` with `TMP_REFUSED` before it changes anything if the check answers so (Podman's
  exec failing itself fails it with what Podman said), since what it changes
  would reach where a link leads; then it makes it `0:0` and `1777`, whoever Podman made the
  volume's. `create` fails with what was said if it cannot. A devcontainer image refused so is
  `image_unrunnable`, and its container and volume are cleared (see
  [A repository's devcontainer.json](#a-repositorys-devcontainerjson)); a workspace of the
  default image is left failed, as with any other failure of `create`. `create` makes a
  container only where none of the workspace's name is (`NAME_TAKEN`), and when the `run` fails
  (Podman makes the container before it starts it), or this check or the making of the folders
  after it does, it removes the container it made, keeping the volume, and logs what Podman said
  if that removal fails; so no container that was not made whole is left for a restore to start
  or take for the workspace's. A lost container a restore cannot make again so leaves the
  workspace ready without one, made again the next time rcman starts, as a failed pull does;
- the mounted busybox's `sleep` as its process, with the image's own entrypoint
  replaced by the mounted busybox, so an image needs nothing of its own to stay
  up, and the volume's mount (`VOLUME_MOUNT`) as its working folder, which exists
  and is the user's, whatever folder the image sets and the user cannot enter;
- `--cgroup-parent` the workspace's slice, unless `PodmanConfig.slices` is off, and
  `--pids-limit=-1`, so that the slice's limits are the only limits (see [Limits](#limits));
- a tmpfs at `ROOT_PODMAN_STATE`, the container root's, for the record of root's Podman;
- `UNMASK_PROC` and the seccomp profile `SECCOMP` of the tools, so that a container nested in
  the workspace can mount a `/proc` of its own and name its own UTS namespace (see
  [Containers in a workspace](#containers-in-a-workspace)); no other container rcman starts has
  either;
- the labels `rcman.workspace` and `rcman.person`, by which `labelled` finds
  every workspace there is.

A volume of the workspace's name that is there already is adopted only if its labels, read from the volume
(`volume inspect`) and compared by rcman, not by a filter of Podman's, are for the same person **and** the same database (`_own_volume`: `LABEL_PERSON` and
`LABEL_INSTALL`, the id in the `meta` table that `db.install_id` reads, made with the database and
put in the labels of the volume and container of every workspace); the host runtime keeps both in a
file of the folder. One made for another person or another database, or with no install label, is
`VOLUME_OF_ANOTHER`. Ids are never given again (`autoincrement` in `db.py`), so a name does not
come back for someone else within a database; the install id guards against a database restored
from a backup, which hands the ids out again. A container made again is made from an image **id**
(`Spec.image_id`, never a name that may have moved on): the plan's, or, for a workspace of the
default image, the one recorded in the row when it was first made, `workspaces.image_id`.
Every container removed goes with its anonymous volumes (`_remove_container`, `--volumes`): its
`/tmp` and any its image declares. They are the only volumes of a workspace with no name of
rcman's and no label; the sweep of volumes nothing uses (`_clean_volumes`) never takes a
workspace's `/tmp`, which Podman attaches in the same `run` that makes it.

Podman copies an image for each range the first time a range uses it, so a
person's first workspace takes longest to make.

`PodmanConfig`'s fields `INJECTED` (`range`, `network` and `slices`) are keyword-only and hold
the product's values by default; so do the other sizes, `Workspaces(builder_slot=…)` (also
`create_app`'s), `HostRuntime(range=…)`, `people.add(last_slot=…)`, which the people route
gives as one below the service's builder slot, and the plan's `size`, which `Workspaces` takes
from the runtime's `id_range` and `devcontainer.resolve_user` holds a user's ids to. The tests'
engine gives them its own (see
[tests.md](tests.md#the-tests-own-engine)), and `tests/test_main.py` holds the service and `rcman-admin` to
the defaults and every product source to giving none of them. The sign-in container takes the
network too; the account containers, the folders' preparation and the canary have none.

### Containers in a workspace

What a colleague is told of it is [the workspaces guide](../docs/workspaces.md#containers).
The tools hold `podman` and `docker` (`backend/rcman/podman/podman`, `docker` a link to it) in
their folder `podman/bin`, with the configuration beside them (`containers.conf`,
`storage.conf`, `registries.conf`, from `backend/rcman/podman/`); the installer writes them
(`ensure_podman_tools`, see [installer.md](installer.md)); the folder holds `PODMAN_COMMANDS`
and nothing else, which a test holds it to, since it is `PODMAN_BIN` on every workspace's PATH,
right after Claude's folder (`PODMAN_LAYOUT.path` and `path_prefix`, so a plan's PATH keeps it
first too), before the image's own programs, git among them. The default image holds Podman, crun
and catatonit. `compose` is refused, and an image with no `/usr/bin/podman` is said to have none.

- **The configuration.** A nested container has pid, UTS and IPC namespaces of its own and
  shares the workspace's network, user and cgroup namespaces; cgroups are disabled (the
  workspace has none to give), catatonit is the init, events and logs go to files. A short name
  is looked for on Docker Hub.
- **Root.** Podman runs in a *nest*: a user and mount namespace the workspace's root makes with
  busybox's `unshare`, whose maps mirror the workspace's own `uid_map` and `gid_map` line by
  line, and in whose mount namespace tmpfs is mounted over root's run folders (`/run/libpod`,
  `/run/containers`). Every mount Podman makes is in the nest, never in the workspace's own
  mount namespace, where rcman's exec, channel and tools are. Root's storage is
  `/rcman/containers/storage` in the volume, so overlay is native; so is storage that a Podman of
  a program's own, root's or another user's, keeps under `/tmp`, which is a volume of its own
  (`TMP_MOUNT`), where on the container's root (`/var/tmp`, say) overlay over overlayfs is
  refused. The maps are written only
  once the child is in a user namespace of its own, which it may not be yet when it has
  started. The nest ignores the signals a terminal or a stop sends, and lasts as long as the
  container; it is recorded in `STATE/nest` as its pid and start time and the inodes of its user
  and mount namespaces, digits only. `STATE` is `ROOT_PODMAN_STATE`, a tmpfs every workspace
  container gets, the container root's even where the ids are swapped (`RUN_MOUNT` is the user's
  there), empty at every start, and a mount point, which no one can rename away; the wrapper
  refuses a `STATE` that is not a mount point (a container made without the tmpfs) or that is another's or
  one others may write. A record is taken only while its process runs with
  that start time and those namespaces, and they are not the caller's own, since a namespace's
  inode number is given again once it is gone; otherwise the nest is made again under a lock.
  The lock is a link, `STATE/lock`, to a folder named after its maker's pid and start time; a
  holder that is gone is followed by one claimer, whose claim is the next link in that folder,
  made in one step (the folder made first if its maker went before making it), and only the
  holder removes the link, so no two make a nest at once. Every try that does not take it waits,
  within a bound. The holder removes what holders that are gone left before it lets go, while no
  other folder can be the one the link names. The wrapper enters with
  `nsenter` and then changes into the caller's folder, as `nsenter -w` would leave the folder of
  the namespace it left.
- **Another user** runs Podman rootless with the same configuration and Podman's default
  storage in their home. The default image's `newuidmap` and `newgidmap` hold the file
  capabilities `cap_setuid` and `cap_setgid` instead of setuid: a workspace has no
  CAP_SYS_ADMIN, which a setuid one would need to write the map of a user's namespace.
- **The seccomp profile** (`SECCOMP`, derived by `helpers/seccomp.py` from Podman's default
  when the installer runs) allows `sethostname` and `setdomainname` whatever the capabilities, which
  Podman's default allows only with CAP_SYS_ADMIN. The kernel still refuses them in the
  workspace's own UTS namespace.
- **`UNMASK_PROC`** removes the paths Podman covers in `/proc`: the kernel refuses a new
  `/proc` where any part of the one there is covered. The kernel's own checks still keep
  `/proc/kcore`, `/proc/timer_list`, `/proc/sysrq-trigger` and the machine's sysctls from the
  workspace's root, which may change only those of its own namespaces (an IPC setting, say);
  what it can read besides is the administrators' guide's
  ([What a workspace can reach](../docs/installation.md#what-a-workspace-can-reach)).
- **Staleness.** The service, at its start with its own Podman (`main.check_seccomp`), says in
  the journal when the profile in the tools is missing or is not the one `helpers/seccomp.py`
  derives from Podman's current default; it changes nothing. A missing one makes `create` fail,
  as Podman refuses a profile that is not there.
- **What is accepted.** A nested container is no boundary inside the workspace, and shares its
  network; what nested containers store counts against no limit, nor does what `/tmp` holds. A
  workspace keeps the image and the container it was made with, and rcman holds no way to give
  one made without Podman, the `ROOT_PODMAN_STATE` tmpfs, `UNMASK_PROC`, the seccomp profile or
  its own `/tmp` (one whose `/tmp` is on its root) what it lacks: the wrapper
  refuses one without the tmpfs (its image may have Podman), says there is no Podman in one whose
  image has none, and a new workspace is made in its place.
- **The order on the PATH.** Where the image has no `/usr/bin/podman`, the wrapper runs the next
  program of the name it was called by on the PATH after `PODMAN_BIN`, never itself (an image's
  own `docker`, say), and says there is no Podman where there is none.
- **A server that ran across an upgrade of rcman** keeps the PATH it was started with, without
  the wrappers, until it is stopped and started again.

### Limits

What the administrators' guide says of them is [Limits](../docs/users.md#limits); the
words and units are `backend/rcman/limits.py`, whose rules (`MEMORY`, `CPU`, `PIDS`, and the
minimums) are in `helpers/rules.py`.

- **Where they are kept.** In the database, as columns of `people` and `workspaces`, null
  for no limit (see [people.md](people.md#the-table)); `Workspaces.set_limits` is the one way
  in, for the routes and for `rcman-admin`.
- **The slices.** A person's limits are properties of the systemd slice `person_slice`, a
  workspace's of `workspace_slice` beneath it, both named by ids, never slots, in rcman's user
  manager. `PodmanRuntime.apply_limits` sets them with `systemctl --user set-property
  --runtime` (`_systemctl`: an argv, the `podman_env` environment), always all of
  `PROPERTIES`, each reset where there is no limit; a memory limit comes with no swap. The
  slice need not exist yet or be running, and a container made in it with `--cgroup-parent`
  is held by its own slice's limits and its person's, the stricter in force.
- **What is in a slice.** A workspace's container, and the builder, in the slice of the person
  who asked for the build (`builder.run_argv`). Nothing else: not the sign-in and
  housekeeping containers (so a full limit never blocks a sign-out), not the canary; and the
  network helper is outside every slice.
- **Usage.** `usage` reads `MemoryCurrent` and `TasksCurrent` of the slice (`limits.read_usage`),
  a person's including everything of theirs beneath it; a slice not active has none and uses
  nothing, and output it cannot read is a failure, never none. Memory in use includes the
  kernel's cache.
- **Refusing before setting.** `Workspaces._refuse_below_use` reads the slice's own caps
  (`PodmanRuntime.caps`, from the unit manager; none is the highest), not the table's, since
  they differ after a limit that could not be applied, and only for a memory or process limit
  that is given. A limit lower than the cap and lower than what is in use is `limit_below_use`:
  lowering memory below use ends the largest process in the group, and lowering processes below
  use stops new commands, rcman's own among them. A value equal to the cap, a removal and a CPU
  change lower nothing and read no usage; but the whole set is applied each time, so a CPU-only
  edit carries the stored memory and process values and is refused if they are lower than the
  slice's caps and below use. Usage is memory plus swap, memory including page cache; where the
  unit manager cannot say what an active slice uses, a tightening is refused as `usage_unknown`.
  There is no force. A set that lowers a memory or process cap is applied before it is
  recorded, so one that could not be applied (`limits_not_applied`) changes nothing, and a
  crash between the two leaves the table above the slice; a set that only raises or removes
  caps is recorded first and applied after, so a crash between leaves the slice below the table,
  never the table below the slice, and one that cannot be applied is taken back. Reading and
  writing take `_limits_lock`, a lock file beside the database as well (`filelock.py`,
  `LOCK_FILE`), since `rcman-admin` changes limits in a process of its own.
- **Restoring.** The user manager forgets runtime properties at a restart of the
  machine, so `restore` sets the slice of every person and every workspace from the table
  (`_restore_limits`), a row with no limits resetting its slice, before it starts any container.
  A workspace made, or made again, has its stored set applied first (`_apply_stored`), an empty
  set resetting the slice. Both judge as an edit does (`_not_below_use`): a memory or process
  limit that would lower the slice's own cap below what is in use stays at that cap, and so does
  one when the use is not counted; the table is left as it is. The report of usage (`limits_report`) has a total deadline,
  `LIMITS_REPORT_SECONDS`, past which readings show as could not be read.
- **Dropping.** Deleting a workspace or a person, and removing an orphaned container at restore,
  call `drop_limits`, which reverts the slice's properties and never stops it, as stopping a
  slice stops the containers in it; a slice is named by ids, and a person's slot is given
  again, so a slice left would hold the next. It never fails the deletion.
- **What the installer prepares.** The controllers, and the lifted default ceiling on
  processes: `ensure_limits`, see [installer.md](installer.md).

### A person's Claude sign-in

Each person has one sign-in, Claude Code's credentials file (`CREDENTIALS`), in a volume
of theirs, `account_volume`, labelled with the person, with the git folder (see below).
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

**An accepted risk.** Claude Code's lock on a refresh is a file in the configuration folder,
and each workspace has a folder of its own, so two workspaces of one person can refresh the
shared file at the same moment; Claude writes it in place, truncating first, and the file can
be read empty, or be left with the tail of a longer text. rcman does not serialise refreshes.
It notices instead: `credentials_state` reads the file as `signed_in`, `empty` or
`unreadable`, and `Accounts` reports `signed_out_unexpectedly` when SQLite has the account
connected, no sign-out waits, and the file is not a sign-in. The container suites measure
concurrent refreshes and hold turn-taking ones to whole files.

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
--global` needs to write beside its file. rcman owns four files in it
(`ACCOUNT_FILES`). Two are git's (`GIT_FILES`): `config`, which git reads as its global
configuration (the commit author, and a credential helper for `https://github.com` that
only reads `credential`, after an empty `helper` that drops every helper the image or the
person listed before it), and `credential`, the GitHub token in git's credential format,
owner-only, and empty when none is stored. Two are gh's (`GH_FILES`), which gh reads there
because `GH_CONFIG_DIR` names the folder: `config.yml` (`GH_CONFIG_FILE`), which is
`GH_CONFIG` and nothing else, the version of gh's configuration, so that gh takes it as
current and never migrates the folder, a migration that rewrites it and, with a token and no
login named, fails every command for want of the account's name; and `hosts.yml`
(`GH_HOSTS_FILE`), the token for `github.com`, owner-only, and empty when none is stored
(see [backend.md](backend.md#git-in-workspaces)).

`ENSURE_SCRIPT` makes the folder and an empty `credential` as well, so the
helper always has a file to read, and `config.yml` where no name is there, never
replacing one that is: written whole into a temporary file of its own and linked into place,
so runs at once, as workspaces made outside the person's lock are, all succeed and none sees
it empty or half written. `HostRuntime.ensure_account` does the same.
`put_account(person, slot, name, data)` writes
one of `ACCOUNT_FILES` from stdin, in a container of the same kind as the others
(`PUT_SCRIPT`: no network, the person's range, removing itself), whole and
owner-only by a rename inside the folder, so a reader never sees half a file and
no copy of a token is left in a temporary one. Nothing about it needs a
workspace to be running, and the data is never in an argv. Forgetting the token
writes an empty `hosts.yml` and `credential`; git and gh in every workspace, running or not,
have no token from their next command. The helper only reads, so git never rewrites the
token; gh, with `config.yml` as rcman writes it, only reads its files too, while what its
own commands write there (`gh auth login`, `gh config set` and their kin) is replaced at
the next write of that file (see [backend.md](backend.md#git-in-workspaces)). `remove_account` removes the folder with the credentials file. A
container made without the bind, as one made before git was bound is, has no
git configuration of its person's and must be made again; rcman does not bind a
folder into a container that exists.

`HostRuntime` keeps the folder in the person's folder under `ACCOUNTS` and links
`HOME/GIT_HOME` of each workspace to it.

`exists` answers from `podman container exists`: exit 1 alone means missing,
and anything else that is not 0, exit 125 among it, is `RuntimeUnavailable`.
`exec` runs one command with stdin, for what a channel cannot carry, and runs
it once more when conmon says it could not start it (`CONMON_FAILED`), as
nothing ran then. Podman's remote exec sometimes, under load, ends with the program's
exit code and nothing of what it wrote on either stream, so in Podman the command is run
by `marked_exec`: the mounted busybox's shell writes `EXEC_MARK` on stderr, puts back the
variables it sets of its own (`SHLVL`, `PWD`, `IFS`, `OPTIND`, `PPID`, and `PATH` when there is
none) as the environment it was started with has them, so the program's environment is
Podman's, then becomes the program, a bare name looked up on the container's PATH and run by
its path (busybox's shell would run its own applet of that name), and the first mark is taken
out of the answer; one the program writes itself is its own. The lookup is Podman's for an
executable file on a PATH that is set; with no PATH a bare name is not found, and a file that
is neither a binary nor starts with `#!` is run by the shell as a script, where Podman refuses
it. An answer with no mark, whatever else Podman's client wrote on stderr, lost the program's
output, unless Podman refused it (`UNAVAILABLE`) or conmon could not start it: with `again`, which a caller gives a program that changes nothing (the
probes of an image's libc and shell, the read of `.claude.json` before a server starts), it
is run once more, and a second loss is `RuntimeFailed`; without it the answer is the exit
code alone, and nothing is run twice. A kept tail (`keep`) is not checked: its mark may be
cut off. It takes a `timeout` in seconds (default `PODMAN_TIMEOUT`);
a command that has not ended by then is `RuntimeFailed`; and a `cwd`, the folder it
runs in (without one, the host runtime's work folder and the image's own working folder in
Podman, which has no work folder before `create` has made it; a caller that needs one names it). `discard` removes a container and keeps its
volume, so that `create` makes it again on the same files; `remove` takes both.
`pull` pulls an image, never one named `localhost/...` (podman would read it as the registry
on the loopback of the machine: it is in rcman's storage or nowhere), passing on each line it prints and stopping when its `cancel`
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
and against real Podman on the tests' engine in
`tests/container/test_runtime_in_podman.py`, apart from its canary cases (see [The canary](#the-canary)). A `Layout` says where a runtime's
things are, as the programs in a workspace see them: the tools as argv
prefixes (busybox's applets in a container), the tmux socket and
configuration, Claude, the shell, terminal descriptions, the runtime folder that holds the
terminals' pidfiles (see [The terminal's shell](#the-terminals-shell)), and how to kill a
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
opens when rcman first runs a command in the workspace through it, and closes
once it has been idle for `CHANNEL_IDLE_SECONDS` (`workspaces.py`); a workspace
whose row records no server is listed without reaching it. `LATENCY_P95_SECONDS` in
`channel.py` is what a request may cost, and the container suites hold the
podman client of a channel to a memory bound of its own.

## A workspace's server

`claude_server.Server` runs everything in the workspace through the channel:
tmux, Claude, the reads of `/proc` and every signal, since the processes and
their ids are the workspace's. tmux runs on the workspace's own socket with
`tmux.conf` and the environment `tmux_env` gives it, its shell and terminal
descriptions the mounted ones, since a slim image has none. The server runs in
the session `SESSION`, in the window size `WIDTH` by `HEIGHT`
(`helpers/screens.py`), started through `env -i` with exactly `child_env` (`runtime.py`'s):
the workspace's `CLAUDE_CONFIG_DIR` and `HOME`, `GH_CONFIG_DIR` naming the person's git
folder (`gh_config_dir`), so that gh reads the token rcman writes there on every command and
a change of it reaches a server that runs, and `DISABLE_AUTOUPDATER=1`,
since Claude would otherwise install a version of its own into the workspace,
the read-only store notwithstanding. A server keeps the environment it started with: a
change to `child_env` reaches it when it is next started or resumed, with no restart of its
container, and a terminal at its next start. rcman sets no `GH_TOKEN` or `GITHUB_TOKEN`; one a plan sets
comes first, and gh then takes it over `hosts.yml`. Its options are `server_command`, which a
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
- **Answering.** `confirm` answers the question the server is asking, one of
  `QUESTIONS` in `helpers/screens.py`, after `ANSWER_PACE_SECONDS`, and once more if Claude asks
  the same question again because the answer was too fast. It is done once the
  state is no longer the question that was asked, so an answer to the trust
  question that lands on the Remote Control question is accepted.
- **Unrecognised screens.** A screen in transition matches no known state, so
  a verb that waits reports `unrecognised` only once it has lasted
  `UNRECOGNISED_GRACE_SECONDS`.

### The terminal's shell

The **Terminal** tab's shell (backend.md's [The terminal](backend.md#the-terminal)) is started by
`Runtime.terminal_argv(name, layout, pidfile)`, with a pty of rcman's own as its standard
streams. It crosses no boundary the workspace's server does not: it runs as the container's own
user - root of the person's range in the default image, the plan's user in a workspace made from
a devcontainer.json (see [The user](#a-repositorys-devcontainerjson)), rcman's own on the host
runtime - and rcman adds no restriction and takes none away.

- **The pty's owner.** `setsid --ctty` (util-linux's) comes first, so the pty is the
  controlling terminal of what it starts: ^C, job control and a resize (`TIOCSWINSZ`, which
  sends SIGWINCH) reach the shell, with nothing run between fork and exec.
- **In Podman** it starts `podman --remote exec --interactive --tty --detach-keys=
  --workdir=<work> <container>`, so the shell has a tty of the container's own, which the
  client, the pty's controlling process, passes everything to and resizes; with no detach keys,
  ^P^Q reach the shell. The host runtime starts the launcher itself.
- **The launcher** (`TERMINAL_LAUNCHER`, a fixed program the workspace's shell runs with `-c`,
  through `env -i` with exactly `child_env` and the terminal descriptions, `TERMINFO`, where the
  layout has them) goes to the work folder, writes the shell's id and its start time from
  `/proc` to the pidfile, and execs the user's shell from the image's passwd, or the layout's
  shell where that names none it can run or names `nologin` or `false`, as an interactive shell
  (`-i`, not a login shell), and `SHELL` naming it.
- **Pidfiles** are `TERMINALS/<token>.pid` in the runtime folder (`RUN_MOUNT`, a tmpfs, in a
  container; `run` of the folder on the host runtime), one per terminal. The processes of the
  workspace are read through the channel, each `/proc/<pid>/stat` by itself with its path and a
  NUL after each, since a program's name may hold a newline (`terminals.process_table`). A
  pidfile counts only while it is what the launcher writes, two bounded numbers, and names the
  leader of its session by its start time, as the processes are read just then; anything else
  signals nothing. Ending a session sends its processes SIGHUP, then SIGKILL (`end_leader`);
  once the leader has been seen, the session is its own while it lasts, as the kernel gives a
  session's id to no other process while any process is in it. A terminal's own cleanup ends the
  session of the leader its pidfile named when it opened even when that leader has ended
  already, as closing the pty ends it; nothing else ends a session whose leader is gone. A process
  that left the session is left.
- **Sweeping.** What earlier terminals left is ended each time a terminal opens on the
  workspace, and, for a container that ran on while rcman did not, by `_restore_workspace`
  through `on_kept_running`: every session whose pidfile is not a live terminal's and whose
  leader still runs (`terminals.sweep`). A container that stopped lost its tmpfs, and its
  processes with it.
- **Deleting.** `_remove` calls `on_removing` before the container goes, which ends the
  workspace's terminal through its channel; on the host runtime, whose removal ends only tmux,
  that is what ends the shell.

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
| `remote_control_prompt` | Claude asks whether to enable Remote Control, until it is enabled for the account; answer with `confirm`. `y` starts the server; after `n` Claude exits and asks again next time (as Claude Code's documentation says; not observed), and the server is then shown as its screen and exit status say: `exited` unless Claude prints one of the screens that end a server and are recognised as such (what it prints is not observed) |
| `connected` | The server is up; no session has attached yet |
| `ready` | A session is attached; `session_name` and `environment_url` are set |
| `stopped` | Stopped with its sessions preserved; `start` or `resume` brings them back |
| `trust_declined` | The trust question was answered no; the server ended, and `start` starts it again |
| `logged_out` | Claude refused because no account is signed in; `start` starts it again |
| `exited` | The server ended in a way not listed above; `exit_code` and `screen` are set |
| `reconnected` | A live screen of Claude's log, the last line saying Remote Control has reconnected. Above the log may be the text Claude showed at its start, its questions answered: when the screen begins with Claude's first line, the wording before the first answer is free; from there on, and on a screen that begins elsewhere because the pane scrolled, every line is a log line, an answered question or one of `STARTUP_LINES` (`helpers/screens.py`). An unanswered question or any other line makes it `unrecognised`. `screen` is set |
| `unrecognised` | A live screen that matches no known state; `screen` is set |

## The canary

`Runtime.run_canary(directory, name, version)` tries a candidate Claude Code version: the file
`CANDIDATE` in `directory`. It runs the checks of `helpers/claude_canary.py` (which are
`helpers/claude_checks.py`'s) and returns the ones the candidate fails, each with its report;
empty means it passes them all. `ClaudeVersions` runs it only for a version never approved before (an earlier approved one still in the store is approved again without it). A canary that could not report (it did not start, did not
finish in `CANARY_SECONDS`, or printed something else) is a `RuntimeFailed`, which says nothing
about the candidate. `end_canary` removes the container.

`PodmanRuntime` runs it as a throwaway container from the default workspace image, in the
builder's uid range (`Workspaces.builder_slot`, which `ClaudeVersions` passes as `slot`;
`people.BUILDER_SLOT` unless a test engine gives another), labelled `LABEL_CANARY` only so that no restore
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
  until all that is done. The plan holds the image's id and, for a pulled one, its digest
  (`ImageFacts.id`, `digest`); the container is made from the id (`Spec.image_id`).
- **Failure.** A `devcontainer.Refused` - a refused or unknown key, a file that cannot
  be read or parsed, an image that cannot be pulled, built, read, used or run - fails the workspace
  with a message from `problems.DEVCONTAINER_MESSAGES` and removes its container and
  volume, leaving only the failed row; a runtime that cannot be reached meanwhile is not a
  refusal, and fails the workspace in plain words (`runtime_gone`) and keeps them. A lifecycle
  command that fails or takes too long
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
  `devcontainer.RESERVED_ENV` are the names rcman sets itself that a file may not set, and no
  file may set a name that moves git's configuration from the git folder (`moves_git_config`:
  `XDG_CONFIG_HOME` and the `GIT_CONFIG` ones); an image's are not taken, `Layout.for_plan`
  drops them and so does `Repository.env`. `Layout.for_plan` drops `GH_CONFIG_DIR` too, from
  a stored plan that holds it, and `Workspaces._hook_env` sets it after the plan's variables,
  so lifecycle commands have gh pointed at the git folder as Claude's does.
  `LANG`, `LC_ALL` and `BROWSER` a file may: rcman sets each in only some of its programs
  (`LANG` for tmux and Claude, `LC_ALL` for git, `BROWSER` for Claude), and there its value
  comes after the file's and wins; everywhere else, lifecycle commands included, the file's is
  used. `PATH` may be set, with the folders of rcman's own programs (`path_prefix`: Claude's
  and `PODMAN_BIN`) kept first. Without a `PATH`
  of the file's, the image's is used.
- **The image's name.** `image` must be a plain registry reference, `[host[:port]/]path[:tag]
  [@digest]`, as written: no variable in it (it is the name that is pulled), and nothing
  that podman would read as a transport or a file (`docker-archive:`, `oci:`, `dir:` and the
  like), and no name that holds `BUILT_REPOSITORY` or is bare hexadecimal (`HEX_ID`), which
  Podman would take for an image built for a workspace or for any image by its id. Nor a
  registry on this machine or its network (`_on_a_registry_of_this_machine`): a pull runs in the
  host's network, where loopback and private addresses are the host's own services. A host that
  is `localhost` with a port, a loopback, private or link-local address (also in the old forms a
  resolver reads), or a name ending in one of `LOCAL_SUFFIXES` is refused; a name that resolves
  to such an address cannot be told without resolving it. `localhost/name` with no port names an
  image of rcman's storage, and `PodmanRuntime.pull` never pulls it.
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
  `builder.run_argv` is the argument list, with the slice of the person who asked for the build
  as its cgroup parent unless `PodmanConfig.slices` is off (see [Limits](#limits)): the uid
  range of `Workspaces.builder_slot` (`people.BUILDER_SLOT` unless a test engine gives another),
  which no person has and `_ids` maps like any other slot, never
  rcman's own; the network of a workspace (`PodmanConfig.network`); `SYS_ADMIN` and nothing
  else added, no device, so that the Podman inside mounts overlay itself (native overlay, its
  `storage.conf` naming no mount program); no `--privileged`. The
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
  `COPY_SECONDS`, and is killed by `cancel`. `builder.repack_archive` then makes the archive that
  is loaded (`_load`, `podman load -i`) from the saved one, which is removed: `verify_archive`
  accepts it first - no part of an OCI layout (Podman reads that before a docker-archive and
  applies the names its index carries), one image, no names, the configuration's hash equal to
  the id, the configuration listing as many layers (`rootfs.diff_ids`) as the manifest, the
  manifest, configuration and layers as plain files with plain headers (a size
  field that Python's tar and Go's read differently is refused) - and only the manifest (written
  again, with no names), the configuration and the layers are written to the new archive; the
  other members, a docker-archive's legacy folders among them, are read past and not carried
  over. Each layer is hashed as it is copied and refused unless its bytes are the digest the
  configuration lists: the storage takes a layer it already has by that digest without reading
  the archive's bytes for it. Nothing is extracted. A build holds `_naming` from the reading of rcman's own names
  (the default image's, the builder's and every `built_name`) to the naming of the new image;
  after the load `_hold_names` finds each still on the image it was on, puts back by id any
  that moved, takes every name off the new image, and refuses the build
  (`build_image_refused`) if anything was wrong. The image is tagged
  `built_name(id)`, one for each workspace; one loaded and not kept is removed by its id, and
  the staging folder always. The restore, and the end of a build when no other build runs,
  clear every staging folder (`_clean_staging`); while a build runs only those old enough that
  no build can be using them go.
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
- **Deleting.** `delete` marks it `deleting`, ends its terminal (see
  [The terminal's shell](#the-terminals-shell)), and removes its container and
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
  runs the plan's `postStartCommand` after either; ends what earlier terminals left
  in one that kept running (see [The terminal's shell](#the-terminals-shell)); removes what an interrupted
  read of an image left (`clean_probes`: probe containers, staging folders and the anonymous
  volumes nothing uses, `_clean_volumes`); applies the stored limits to the slices (see
  [Limits](#limits)), before any container is started; and once the workspaces are back,
  runs `Workspaces.restore_hooks`, each a step of its own, before what no row is for is
  removed: what a module that imports `workspaces.py` brings back at a start.
  `GitAccount.write_gh_for_everyone` is one (`main.py`): `config.yml` for every person let in,
  and `hosts.yml` for each with a token stored, under the person's lock, which makes the
  account volume of anyone who had none. The workspaces are brought back up to
  `RESTORE_WORKERS` at once, each in a thread of its own that goes on to start that
  workspace's server if it should be running (pinned to the approved version, as above), so that a workspace that is slow, a start
  command that hangs among the causes, does not keep another from being brought back; it
  holds one of the places until it is done, and the removal of what no row is for, the
  finishing of deletions and the settling of sign-outs wait for the last. A lost container is made again
  from the image **id** its plan holds (`Workspaces._pinned_image_is_there`), or, for a
  workspace of the default image, from `workspaces.image_id`, recorded when it was first made
  (`_pinned`); a newer default image is for new workspaces. An id the storage no longer has
  is pulled again by the plan's digest (`runtime.repository_of` and the digest) and must
  then be the very image; with no digest the workspace is failed, saying to delete it and
  make it again (`image_gone`, `default_image_gone`, and for an image rcman built
  `built_image_gone`). A plan with no id (one stored without) pulls its image's name as it
  always did (`Runtime.pull`, which uses the image the runtime has when the registry cannot be
  reached). If the registry says the image is not there or
  may not be had (`workspaces.image_is_gone` reads its words), the workspace is failed likewise;
  any other failure of the pull - the network, a timeout, a
  registry that is down - leaves it ready without its container, logged, and the next restore
  tries again. What can be slow is done without the
  workspace's lock and under `_working_on`, so a deletion stops it. Restore stops between
  steps once rcman is closing;
  discards (the volume stays) every labelled container no row is for, and removes nothing at all
  - no container, build, image or Claude Code version - when the table has no workspace but the
  runtime has some, which is logged as an error (`_table_is_empty_of_workspaces_there_are`);
  collects the images nothing refers to (`_collect_images`: not while a workspace is being made
  or a plan cannot be read, never one a container is made from, never by force, and the keep
  list is every stored plan's image and id, each workspace's `image_id`, the image built for
  each workspace, and the runtime's own: every tag of the default and the builder repositories,
  since an upgrade builds the next ones beside the service; an image with no name stays until it
  is `IMAGE_GRACE_SECONDS` old, being perhaps a layer of a build under way; names are compared
  fully qualified, as Podman lists them, `qualified_name`), which it does after a deletion too. It
  holds `_images_lock`, which `create` holds while it records a workspace, so a workspace is either
  seen by a collection or recorded after it; `rcman-admin` does not collect; removes each person an
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
