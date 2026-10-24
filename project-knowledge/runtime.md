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
- the instructions for Claude, a file of the tools bound read-only at Claude Code's managed
  path (see [Workspace instructions](#workspace-instructions)), in `create` itself and not in
  `_mounts`, which the sign-in container shares;
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
`storage.conf`, `registries.conf`, from `backend/rcman/podman/`) and `publish.sh`, which the
wrapper sources (`PODMAN_PUBLISH`); the installer writes them
(`ensure_podman_tools`, see [installer.md](installer.md)); the folder holds `PODMAN_COMMANDS`
and nothing else, which a test holds it to, since it is `PODMAN_BIN` on every workspace's PATH,
right after Claude's folder (`PODMAN_LAYOUT.path` and `path_prefix`, so a plan's PATH keeps it
first too), before the image's own programs, git among them. The default image holds Podman, crun
and catatonit. `compose` is refused, and an image with no `/usr/bin/podman` is said to have none.

- **A published port.** Where the image's Podman runs, `publish.sh` reads the arguments of `run`,
  `create`, `container run|create` and `pod create` by the rules of Podman's option parser that
  its lists of options let it follow: global options before and after the subcommand, letter
  clusters with a letter's value in the rest of the cluster, the next argument or after an `=`,
  up to the image, the pod's name, `--` or a letter it does not know. It says on standard error
  that a `-p`, `--publish` or `-P` (`--publish-all`, not `=false`) publishes nothing, since the
  container shares the workspace's network; Podman then runs as asked. Nothing is said where
  `--network` or `--net` names anything but `host`, or `--pod` or `--pod-id-file` a pod. Its
  lists of options are those Podman's help shows for those subcommands, with `--net`, an alias
  the help hides, and the wrapper's `VALUED` for the global ones. The helps are recorded from the
  default image's Podman in `tests/fakes/podman-help/`: `tests/test_podman_wrapper.py` holds the
  lists to the recording, and `tests/container/test_nest_in_podman.py` the recording to the
  image's own help.

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
  workspace keeps the image and the container it was made with until it is rebuilt (see
  [Rebuilding](#rebuilding)), which makes its container again with all of this; rcman holds no
  other way to give one made without Podman, the `ROOT_PODMAN_STATE` tmpfs, `UNMASK_PROC`, the seccomp profile or
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

### What the monitor reads

The monitor (`backend/rcman/monitor.py`, see [backend.md](backend.md#monitor)) reads through
four methods of the runtime, each a text it parses itself:

- `show_slices(slices)`: one `systemctl --user show` of `limits.MONITORED` for every slice
  given, through `_systemctl`; a slice that never ran is shown inactive with nothing counted.
- `disk_usage(cancel)`: `podman --remote system df --verbose`, which has no JSON with
  `--verbose`, so its text tables are read. It runs through a `Popen` that is looked at every
  `DISK_POLL_SECONDS` and killed when `cancel` is set (the monitor's stop) or after
  `DISK_TIMEOUT`. Killing it ends only the `podman --remote` client: the service may finish the
  walk it started. The Podman service walks every file of every volume for it, its cost growing
  with the files, and one volume it cannot read, or a file deleted while it walks, fails the
  whole read.
- `inspect_containers(names)`: `podman --remote container inspect` of the workspaces'
  containers, whose `Mounts` name the anonymous volume at `TMP_MOUNT` (`ps` lists only a mount's
  destination). A name that is not there makes Podman fail (`NO_SUCH_CONTAINER`) after it
  printed those that are, which are taken.
- `storage_root()`: `store.graphRoot` of `podman --remote info`, asked once.

The host runtime records each call in `monitor_calls` and answers what a test plants:
`slices_shown` by slice, `disk_report` (otherwise made from its workspaces' folders, each a
volume, in Podman's tables) with `disk_failures` raised first, `inspect_report` (otherwise made
from its workspaces), and its own folder as the storage.
The recordings the tests read are in `tests/fakes/podman-monitor/`, recorded from Podman on the
tests' engine; `tests/container/test_runtime_in_podman.py` holds them to that Podman.

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

### Workspace instructions

What Claude is told in every workspace is a file of rcman's, bound read-only by
`PodmanRuntime.create` at `helpers/instructions.py` `MANAGED`, Claude Code's managed
`CLAUDE.md`, which it reads into every session and which a session cannot leave out. It always
replaces a file an image has there; the image's other files in that folder stay. What the text
says is for colleagues in
[the workspaces guide](../docs/workspaces.md#what-claude-is-told-in-a-workspace).

- **The text.** Fragments in `backend/rcman/instructions/` (`instructions.COMMON` for every
  workspace, `GH` for one of the default image or of an image that carries `TOOLS_LABEL`,
  `TOOLS` for one of an image that carries it) make the files `instructions.FILES`, each its
  fragments in order (`instructions.compose`). The installer writes them into the tools' folder
  `instructions.FOLDER` (`ensure_instructions`, see [installer.md](installer.md)).
- **Which file.** `instructions.file_for`, from the labels of the image a workspace is made
  from, read with `podman image inspect` (`Spec.image_id` where it has one, else `Spec.image`
  or the configured default image): a workspace whose image carries `TOOLS_LABEL`, the default
  image, whether it has no devcontainer.json or one whose `image` names it, or an image built
  from it outside rcman and pulled, which inherits the label, is told the whole text; one of a default image without the label (`Spec.image` unset), of gh too but not
  of the tools; any other, the common text alone. A failure to read the labels fails `create`
  before any volume or container is made.
- **When it is missing.** `Runtime.instructions_ready` says why no workspace container can be
  made, looking only at the tools (`HostRuntime`, which binds nothing, says nothing); `create`
  raises `InstructionsMissing`, a `RuntimeFailed`, before it asks Podman anything. `Workspaces`
  asks before anything is discarded: a workspace being made fails with `INSTRUCTIONS_MISSING`
  and keeps its container and volume, never blamed on its image; a Rebuild leaves the old
  container running and says why in its error, as does the Rebuild of a failed workspace,
  which keeps the container it has; a restore brings nothing back, discarding, making and
  starting nothing, and says why in the journal, its workspaces reading `ready` meanwhile; and
  `main.check_instructions` says so in the journal when the service starts. The files could
  still go between the look and a `create`, which then fails with `InstructionsMissing`
  itself, a Rebuild's putting back of the old container among them.
- **New text.** The installer renames a changed file over the old one, so a container keeps
  the file it was started with until its next start, which binds the new one.
- **An image's own managed path.** `image_facts` reads, through `podman cp`, the first tar
  header of each of `instructions.PATHS` (`/etc`, `/etc/claude-code`, then `MANAGED`) as far
  as each is a folder, stopping podman after it (`ImageFacts.managed`). `podman cp` follows a
  link, so a link reads as what it leads to; the PAX and GNU long-name headers before an entry
  are passed over, within a few blocks, and the entry is judged. `instructions.refused_at`
  names a folder of the path that is a file, or `MANAGED` that is not a regular file, which
  `_read_plan` refuses as `image_managed_folder`; anything else at a folder of the path is not
  judged.
- **Who gets none.** The sign-in, canary, builder, image-probe, folder-preparation and account
  containers.

The bind of a single file at a path the image lacks, readable with swapped ids, over an
image's own folder, and taking a rename-over at the next start, and the probe's reading of
links, are held by `tests/container/test_runtime_in_podman.py`; the text that reaches a real
session is a check of [the real-Claude list](real-claude-checklist.md).

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
  process is one of the workspace's, to be signalled, while its state is not Z, or is Z with
  other threads still running, which is how a process whose first thread has ended reads
  (`terminals.processes`). A pidfile counts only while it is what the launcher writes, two
  bounded numbers, and names the leader of its session by its start time, as the processes are
  read just then; anything else signals nothing. Ending a session sends its processes SIGHUP,
  then SIGKILL (`end_leader`); once the leader has been seen, the session is its own while it
  lasts, as the kernel gives a session's id to no other process while any process is in it. A
  terminal's own cleanup ends the session of the leader its pidfile named when it opened even
  when that leader has ended already, as closing the pty ends it; nothing else ends a session
  whose leader is gone. A process that left the session is left.
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
  fails the reading, so a user is never reported missing because a copy merely failed; the image's labels, user and environment; and what it has at Claude Code's managed path, see [Workspace instructions](#workspace-instructions)); refuse an image that carries a label beginning `rcman.` (`image_labels`) or whose managed path cannot have the instructions bound over it (`image_managed_folder`); make the plan; store it in the
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
  container and volume for the person to look at or rebuild. The row's `failure` records the
  kind of a failure that keeps the volume and its repository (`workspaces.RECOVERABLE`:
  `hook_failed` and `hook_timeout` from the `HookFailed` `_run_commands` raises, and the
  `image_gone`, `built_image_gone` and `default_image_gone` of `_image_gone`); every other
  failure - a clone, a refusal, a restart while being made - leaves it null.
- **The digest.** `devcontainer.digest` is a sha256 of the place the file was found in, its
  bytes and, for a file that builds from a Dockerfile (`dockerfile_of`), the Dockerfile's bytes,
  read by `read_path`: the reader's rules (no link on the way, `MAX_BYTES`, `READ_SECONDS`)
  for any path in the work folder, with refusals of its own (`DOCKERFILE_REFUSALS`); no file at
  all is `NO_FILE_DIGEST`. `_read_plan` makes it as it reads the file, and `_configure` stores
  it in the row's `devcontainer_digest`, for no file too; a rebuild stores it at its commit. It
  does not cover the rest of the build's context, the repository's own features or a tag that
  names another image by now. `Workspaces.devcontainer_changed` (`GET
  /api/workspaces/{id}/devcontainer`) reads it again through the channel
  (`devcontainer.current_digest`, with `_channel_runner` changing to the work folder) and
  compares; it asks nothing of a workspace that is not ready, is being rebuilt or worked on,
  has no container or no digest (`devcontainer_digest` null), and answers null for those and
  for a file that cannot be read or is refused.
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
  is ever held (`exec`'s `keep`). The create commands run when the container is made: in
  `_configure`, again in a rebuild's `_make_new` and its rollback (`_roll_back`), and in a
  restore that settles a rebuild cut short while its container was replaced
  (`_create_commands_again`). `Plan.create_commands` ends with `postStartCommand`, so it runs
  there too. A failing one is a `HookFailed`: in `_configure` the row keeps its `kind` as its
  `failure`, from `_make_new` it rolls a ready workspace's rebuild back (`_roll_back`) and
  keeps a failed one failed, its `kind` the `failure` (`_recover`), and in `_roll_back` and
  `_create_commands_again` it is only logged. `postStartCommand` alone (`_start_commands`) runs
  again after every start or re-creation in a restore, before that workspace's server is
  started, and its failure there is only logged. A container
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
  plan needs, and every `next_images()` of a workspace no rebuild is under way for.
- **Two names.** An image built for a workspace is `built_name(id)`, tagged `BUILT_TAG`, which
  its container is made from; a rebuild builds as `built_name(id, NEXT_TAG)`
  (`BuildRequest.tag`), which a build never touches the other tag of, and `promote_build(id)`
  then, under `_naming`, tags the next image as the built one and untags the next name; it can
  be called again after a promotion that ended part way. `workspace_of_built_name` reads
  either tag, `built_images()` lists the `BUILT_TAG` ones and `next_images()` the others, and
  `remove_build(id, image=True, image_tag=...)` removes one of them by its name, never by force
  and never by id. Deleting a workspace removes both (`_remove`, `_clear`), and the collection
  keeps both names of every workspace. `HostRuntime` does not refuse to remove an image a
  container is made from, as Podman does.
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

## Rebuilding

`Workspaces.rebuild` (`POST /api/workspaces/{id}/rebuild`, see
[backend.md](backend.md#workspaces-and-servers); the administrators' account is
[Rebuilding a workspace](../docs/devcontainer.md#rebuilding-a-workspace)) makes a workspace's
container again on its volume from what the devcontainer.json in its work folder asks for
now. It never goes through `_make` or `_configure`, whose refusal calls `_clear` and removes
the volume; it shares their smaller steps (`_read_plan`, `_pulled`, `_built`,
`_wait_for_build`), which take a `report` that writes to the row's `rebuild_log` instead of
`log`.

- **Admission.** `rebuild_refusal` is the one test, which the listing's `rebuildable` uses too:
  refused while an upgrade of rcman is under way (`rcman_upgrading`), before the restore is
  done (`rcman_starting`), while the workspace is being made or deleted (`workspace_not_ready`),
  or anything works on it (`_working`, `_making`, a `rebuild_state` set: `workspace_rebuilding`); a ready workspace
  needs a `source` (`rebuild_empty`) and its container (`rebuild_no_container`, from
  `Runtime.exists`); a failed one needs a `failure` in `RECOVERABLE`, a `source` and its volume
  (`Runtime.has_volume`), else `workspace_not_ready`. What the row says is looked at first
  (`_rebuild_refusal_by_row`), and the runtime asked only about a workspace that passes it: the
  listing asks `Runtime.labelled()` once for every container instead of `exists` for each
  ready one, and `has_volume` for each failed one that passes. The
  route needs `confirm` (`rebuild_confirm_required`). The runtime's part is asked before any
  lock; the row's part again, and the write of `rebuild_state = preparing`, under `_images_lock`
  and the workspace's lock, so that an upgrade's admission, which holds the images' lock, and
  the rebuild each see the other: `rcman_upgrade._reason` gives `rebuilding` while
  `any_rebuilding()` finds a phase in a row - but, once the restore is done, not a ready
  workspace in `switching` that no thread of the service works on (`_left_in_switch`): the
  upgrade's restart puts it back as `settle_left_over` would. The thread is kept in `_making`, so restore, the
  orphan removals and the collection leave the workspace to it.
- **Preparing** (`_prepare`, under `_working_on` and no lock): the file is read in the old
  container, checked, and its image pulled or built as `NEXT_TAG`; the image read and the new
  plan made, or, for no file, `default_image_id()` taken (none refuses with
  `default_image_gone`). Any refusal or failure only writes `rebuild_error`, in the words of a
  rebuild (`problems.rebuild_advice`), and removes the next image by its name; the container,
  the volume and the old image are untouched. A pulled image is never removed here: the
  collection removes it once nothing names it.
- **Switching** (`_switch`): under the workspace's lock the phase becomes `switching`, from
  which `ready()` refuses `workspace_rebuilding` (git, the servers' verbs, the terminal), the
  listing reads no server and `pinned_servers` and `running` count it without reaching it; the
  server, if tmux says it runs, is stopped with `Server.stop` - never `stop_server`, so
  `wanted` is left as it is - and `claude_version` cleared. A server request that passed
  `ready()` before and reaches the lock after is refused there too: `start_server`,
  `stop_server`, `forget_server` and `confirm` look at the phase under the lock
  (`_not_switching`). Then, without the lock, `on_rebuilding` (terminals.py ends the terminal
  with `workspace_rebuilding`) and the channel closed; `_replace` discards the container, the
  first step whose failure, of whatever kind, is rolled back. `_make_new` makes the new
  container from the plan as built (a built image by its next name, and by id), with `_made_for` after it, which removes
  the container and volume of a workspace deleted meanwhile; checks libc and git; runs every
  create command, then `postStartCommand`. No lock is held through these, nor the person's
  lock taken while a workspace's is held.
- **Commit** (`_commit`): one write under the lock stores the plan by the built image's
  `BUILT_TAG` name (`_Prepared.stored`), the default image's id where there is no plan (else
  null), the digest, and `rebuild_state = promoting`; `_promote` then moves the built name
  (`promote_build`), or removes the old built image by name where the new plan is not built;
  only then is the phase cleared, and the channel closed again. A promotion that fails leaves
  `promoting`, which the service's periodic look (`settle_left_over`, every
  `IDLE_CHECK_SECONDS` once the restore is done) finishes for a row no thread of its works on,
  and the next restore otherwise. Holding no lock, `start_server` starts the server again
  if it ran before the switch (a start: `resume` needs the old tmux session, which is gone);
  one that does not start is logged, and the restore starts it, `wanted` being still set.
- **Rollback** (`_roll_back`): a failure while switching discards the new container, makes
  one again from the stored plan or the workspace's `image_id`, runs the old create commands
  and `postStartCommand` (a failing one only logged), starts the server again if it ran, and
  writes `REBUILD_MESSAGES["rolled_back"]`
  with the cause (a refusal's words, or a command's name, exit code and the tail of what it
  printed, never the file's content). One that fails, and a failure nothing foresaw once the
  phase is `switching` (`_rebuild_failed`), leave the workspace ready in `switching`
  (`_left_to_put_back`): `ready()` refuses its server, git and terminal, no rebuild is offered,
  the row's `server` is cleared (no server runs; `wanted` stays, so the restore starts it), and
  `rollback_failed` with the cause is its `rebuild_error` and the last line of its
  `rebuild_log`, which the page shows while a phase is set. The service's periodic look
  (`settle_left_over`) puts it back as the restore does (see Restarts), each in a thread of its
  own (`_putting_back`), and tries again at the next look while that fails; the restore at a
  start does the same. Once done, the error becomes `put_back_later`, the cause kept, and the
  server is started again if `wanted`. A failure nothing foresaw while preparing writes
  `MESSAGES["workspace_failed"]` as the rebuild's error; in a failed workspace it adds it to the
  steps, keeping `error` and `failure`.
- **Recovery** (`_recover`): a failed workspace is read through its own container, started if
  it stopped, or through one made from the current default image on its volume, which its row
  does not record; then the same steps, with nothing to stop; the commit also makes it
  `ready`, clearing `error`, `failure` and `log`. A failure keeps it `failed`, with the new
  cause in `error` and the kind of a failed command, else the old kind, in `failure`, so it can
  be rebuilt again; nothing is put back, `_clear` is never called and the volume stays; the
  container stays only after a create command failed.
- **Deleting.** `delete` treats a workspace with a rebuild thread, or, in `rcman-admin`, with a
  phase in its row, as one being made: it marks it `deleting`, `_stop_working` ends what runs
  (the watcher does it for `rcman-admin`'s), and the rebuild's end (`_rebuild_ended`) removes
  it; `delete_person` waits for such workspaces as for those being made. `_rebuild_ended` looks
  a second time after its slow steps (`_stopped`, the collection), having first taken its
  thread out of `_making` under the workspace's lock, so that a deletion meanwhile is finished
  by it or, after it, by the deletion itself. A row `rcman-admin` left `deleting` with a phase
  that no thread of the service works on (`promoting` left over, a switch not put back) is
  removed by `settle_left_over`. A deletion after the commit leaves the server unstarted: `start_server` refuses a workspace no longer ready.
- **Restarts** (`_restore_rebuild`, `_restore_failed_rebuild`, before a restore looks at the
  container): `preparing` is cleared, with `REBUILD_MESSAGES["restarted"]`, and the next image
  removed; `switching` discards any container there, whatever image it is of - one of the old
  image may be the new one, with another user, environment or commands -, so that the restore
  makes it again from the old plan and runs its create commands again
  (`_create_commands_again`), and keeps the phase until that is done (`_put_back`:
  `restarted_switching`, or `put_back_later` after `rollback_failed`, `_put_back_error`). A
  container that cannot be made now leaves it in `switching`, for `settle_left_over`; one whose
  image is gone fails the workspace (`_image_gone`) and ends the phase (`_not_put_back`), the
  failure's words kept. `promoting` finishes the promotion. A
  failed workspace's phase is cleared with `restarted` in its `error`, its `failure` kept, its
  container discarded if it was switching.
- **The journal.** Each rebuild ends in one line on `rcman.workspaces`, `workspace <id>: ` and
  the outcome `_rebuild`, `_replace`, `_roll_back` and `_recover` return: what it ended in, and
  the first line of the reason where there is one.

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
  volume, which ends its server, then its row — never the person's sign-in; one still being made
  or rebuilt goes once that work stops (see [Rebuilding](#rebuilding), Deleting). One being cloned, pulled for, built for or running a lifecycle command is
  cancelled by deleting it: `_stop_working` sets its event, which stops a pull, and
  force-removes its builder, which ends a build, and its container, which ends git and the
  commands, and the
  making thread finds it `deleting` and finishes the removal; if the container
  cannot be removed now, the workspace goes when the work ends, at the latest at
  its timeout. Cancelling works across processes through the row: a deletion from
  `rcman-admin`, which has none of the service's events, only marks the row, and while the
  work goes on `_working_on` keeps a thread (`_watch`) that reads the row every
  `WORKING_POLL_SECONDS` and, once it is `deleting` or gone and no deletion in the service has
  stopped the work, calls `_stop_working` as such a deletion does; the thread ends with the
  work. Deleting a person deletes each of theirs (see [people.md](people.md)).
- **Restoring.** When rcman starts, `restore` runs in the background, so rcman
  answers at once: it waits for the runtime, trying again while it is
  unavailable (where the instructions for Claude are missing from the tools, it leaves every
  row as it is and brings no workspace back, and the other steps still run, see
  [Workspace instructions](#workspace-instructions)); finishes rows still `deleting`, and `creating` ones with no `source`, which are
  made again; fails a `creating` one with a `source`, which a restart interrupted
  while it was being made, with `CLONE_MESSAGES["restarted"]` (it is not cloned
  again, nor are its create commands run again, whatever it had done: the person
  deletes it and decides); settles a rebuild a restart cut short (see
  [Rebuilding](#rebuilding), Restarts); makes again a `ready` workspace whose container is
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
  then be the very image; with no digest the workspace is failed, saying to rebuild or delete
  it (`image_gone`, `default_image_gone`, and for an image rcman built `built_image_gone`,
  each recorded as its `failure`). A plan with no id (one stored without) pulls its image's name as it
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
  or rebuilt, but by a rebuild's own thread as it ends, nor when a plan cannot be read, never
  one a container is made from, never by force, and the keep list is every stored plan's image
  and id, each workspace's `image_id`, the image built for each workspace under both its names
  (`BUILT_TAG`, `NEXT_TAG`), and the runtime's own: the configured default image and every image
  of its repository made after it, and every tag of the builder repository, since an upgrade builds
  the next ones beside the service, and the whole default repository where the configured image is
  not there, since nothing then tells the old from the new; an image with no name stays until it
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
