# Installer

How to install, for administrators: [the installation guide](../docs/installation.md).
That guide and `install.sh` are one contract: a change to either changes
both, and `tests/test_install.py` holds the guide's options, variables and
paths to the installer's.

## Parts

| File | What it is |
|---|---|
| `install.sh` | Checks it runs through sudo, updates the clone as `$SUDO_USER` (fast-forward only, refusing a dirty tree or another branch), starts again if that changed `install.sh`, then hands over to the Python installer; with `--rollback` it hands over at once, leaving the clone as it is |
| `installer/install.py` | Everything else, using the standard library and the clone's `helpers/rules.py`, whose rules' sentences explain a refused setting; run with the system interpreter in isolated mode and UTF-8 mode, it writes no bytecode into the clone and takes the clone off its path once it has imported the rules |
| `installer/templates/` | The files it writes, with `@NAME@` placeholders: the app's environment, oauth2-proxy's configuration, both systemd units, and the Caddy site block. A test keeps `TEMPLATES` and the directory equal. Besides the settings, `render` fills in what the installer found out, which `found_out` returns: rcman's uid (`APP_UID`), the default image's name (`WORKSPACE_IMAGE`) and the builder image's (`BUILDER_IMAGE`, written as `RCMAN_BUILDER_IMAGE`) |
| `installer/workspace/Containerfile` | The default workspace image, built with rcman's own Podman: git, `gh`, Podman with crun, catatonit and `uidmap` (its `newuidmap` and `newgidmap` given file capabilities in place of setuid, see [runtime.md](runtime.md#containers-in-a-workspace)) and what Claude Code and rcman need, from Ubuntu's packages, unpinned; a test reads its package list |
| `installer/builder/` | The devcontainer builder image's build context: its `Containerfile`, the Podman configuration it carries (`containers.conf`, `storage.conf`), and `package.json` with `package-lock.json`, which pin the Dev Containers CLI by integrity |

The pinned versions are `UV_VERSION`, `OAUTH2_PROXY_VERSION` and
`TMUX_VERSION` in `installer/install.py`, `frontend/.nvmrc` for Node.js, the CLI in
`installer/builder/package.json` and its lock, and `helpers/claude-version` for
Claude Code. Every base image the installer builds from is named by tag and digest:
`TMUX_BUILD_REFERENCE` (the tag `TMUX_BUILD_IMAGE` and `TMUX_BUILD_DIGEST`) and the
`FROM` of each Containerfile under `installer/`; a test reads every Containerfile it
finds there and fails on a `FROM` without both (a stage of the same file is pinned
where it is defined). The digest is the registry's own for the tag (its
`Docker-Content-Digest` header, which is the sha256 of the index document it
returns); moving to another tag or another day's image is a change to the line.
Downloads are checked against a checksum that does not come from the page that
serves them where it can be: Node.js's, uv's and oauth2-proxy's archives against
`NODE_SHA256`, `UV_SHA256` and `OAUTH2_PROXY_SHA256` (Node.js's version is
`frontend/.nvmrc`'s, and its pin names the same), tmux's source against `TMUX_SHA256`, each
keyed by the version it belongs to (and the architecture), so that a version
changed without its checksums fails `ensure_toolchain` (`_pinned`) and a test that
reads the tables; Claude Code is checked against its release's manifest. To move Node.js,
uv or oauth2-proxy to another version, download each architecture's archive, hash it,
compare the hash with the checksum file the project publishes with the release, and
replace the table's entries together with the version. `ensure_toolchain` fills the
toolchain holding an exclusive lock on a file in it, `TOOLCHAIN_LOCK`, so two
callers filling one toolchain at once, such as two test runs sharing the
tests' toolchain cache, take turns and the second finds what the first
unpacked.

## Rootless Podman

`check_kernel`, the first thing `main` does as root, before a rollback too, refuses a kernel
older than `USERNS_OVERLAY_KERNEL`, or one whose release it cannot read: the devcontainer
builder's Podman mounts overlay inside a user namespace, with no FUSE device (see
[runtime.md](runtime.md#building-an-image)); `tests/test_install.py` holds it to both, and to
coming before anything is changed. `ensure_packages` installs `PACKAGES` that are missing. `ensure_subordinate_ids`
gives `rcman` `SUBORDINATE_COUNT` subordinate uids and gids, a range after every
range already in `/etc/subuid` and `/etc/subgid` (`subordinate_start`), unless it
has one that big, so a machine whose range was made for fewer slots gets a second,
full one; a test holds the count to `MAX_PEOPLE`, `BUILDER_SLOT` and `RANGE` (see
[people.md](people.md)). `ensure_podman` enables linger for `rcman`, starts its
user manager, enables `podman.socket` in it and waits for the socket; when the
subordinate ids changed, it tells Podman with a local `podman system migrate`
first. Everything after runs Podman as rcman through that socket
(`as_rcman_podman`). `rcman.service` names the socket's `XDG_RUNTIME_DIR` and
runs with `NoNewPrivileges=yes` and `UMask=0077`.

`PACKAGES` holds `crun`, and `ensure_oci_runtime`, right after `ensure_podman`, asks rcman's
Podman (`podman info`, through `as_rcman_podman`) for its OCI runtime and fails with an
`InstallError` unless it is `OCI_RUNTIME`: the runtime the workspaces and the builder are tested
with. `podman system migrate` stops every running container, so
`check_containers_stopped_before_new_ranges`, which `main` runs before `ensure_subordinate_ids`,
refuses (naming the containers and the remedy, and having changed nothing) when a range is to be
added and rcman's Podman lists a running or paused container; where its socket is not there
nothing can run, and where the ranges are in place it asks nothing. It refuses before the range
is added, not after, because the next run decides from the files whether a range is missing, and
would not migrate.

`ensure_limits`, after `ensure_oci_runtime`, prepares what the limits on memory, CPU and processes
(see [runtime.md](runtime.md#limits)) need. The user manager's drop-in is in rcman's home, so it
is read and written by rcman (`rcman_write`), which makes its folders too; root follows no link
there (see [Root and rcman's paths](#root-and-rcmans-paths)). `limits_problem` says why limits cannot be applied, or
that it cannot tell, and the installer only warns (`warn`) and goes on, since limits are off until
set: cgroup v2 is missing, a controller of `LIMIT_CONTROLLERS` is not among those of
`user@<uid>.service`'s control group, that group is empty (which would be the root cgroup, listing
every controller) or its list cannot be read; delegating a controller is the administrator's to
decide, and setting a limit later fails with `limits_not_applied`. It writes two drop-ins with
`write_if_changed`: `USER_SLICE_DROP_IN` for `user-<uid>.slice`, which lifts the ceiling on
processes that systemd's own defaults put there, read by the daemon-reload that starts the
services, and `USER_MANAGER_DROP_IN` in rcman's home, which lifts the user manager's default for the
scopes of its containers. `daemon-reexec` of rcman's user manager (`as_rcman_systemctl`) runs on every
install after them, so a failed one is not left undone; it restarts nothing and ends nothing the
manager runs. A second run changes nothing. The files are named so that they come after systemd's
own in the order it reads them; `tests/test_install.py` holds that and the order of the steps.

## Root and rcman's paths

Root must never follow a link inside a path rcman can write: rcman can put a link where
anything inside its home or its socket's folder goes, and root would follow it. So:

- The static tools are not in rcman's home: `TOOLS` is under `ROOT`, root's, and
  `RCMAN_TOOLS_DIR` names it. tmux is built by rcman's Podman, and the program comes out on
  the container's standard output, not logged (`--log-driver=none`: a log driver of the
  journal would write the program's bytes into the system journal, where they are not
  UTF-8) (the build says what it has to say on standard error,
  `TMUX_BUILD`, with the program's sha256 on a last line that `ensure_tools` compares with
  the bytes received, since `is_static` reads only the header), which root writes into the
  tools folder after `is_static`; no scratch folder
  of rcman's is mounted for it. `ensure_tools` touches nothing in rcman's home.
- What the installer makes inside rcman's home is made by rcman: `as_rcman` is the one argv
  builder (it builds on `as_account`, the one place that names `runuser`), and `rcman_do`,
  `rcman_has`, `rcman_link`, `rcman_set_link` and `rcman_write` are the file operations
  built on it (`cat`, `stat`, `install`, `mv`, `ln`, `test`, and `WRITE_SCRIPT`, which
  writes standard input to a file by a rename): no command run as rcman opens a descriptor
  root handed it by its path (`/dev/stdin`, `/dev/fd/N`): once the uid has changed that is a
  new open of root's pipe, which is refused, so the tests' stand-in for `runuser` refuses such
  an argument (`opens_a_descriptor_by_path`). They make the allowed list, the user
  manager's drop-in, the whole Claude store, the database's schema version
  (`database_schema_version`: SQLite asked as rcman, since a database root opened would be
  left files of root's), rcman's Podman socket (`podman_socket_exists`), the health check
  (`HEALTH_SCRIPT` on rcman's socket) and rcman-admin, in rcman's clean environment and from
  `/`. The guard treats `/run/user/` as rcman's as well, and a socket connected to a path of
  rcman's as an act on it. The Claude store is downloaded and checked in a folder of root's
  outside it, and rcman copies the checked file in. The built web interface, which the clone's
  owner's npm wrote, is copied only when it holds no link (`copy_built_tree`).
- The two directories that are themselves rcman's (`STATE` and `RUN`, entries of root-owned
  folders) are changed by `own_directory`, through a descriptor opened with `O_NOFOLLOW` and
  `O_DIRECTORY`: a link in their place is refused, and nothing inside is touched.

`tests/test_install.py` holds this by reading `installer/install.py`: the path constants in
rcman's reach are derived from the values (`rcman_writable_constants`: those equal to or
under `STATE` or `RUN`), and an AST walk follows names assigned from them, parameters whose
default is one, and any account's home (`pw_dir`), and finds each `os` call, `shutil` call,
path method, temporary file, call of a function that itself acts on a path it is given, and
command run without `as_account` that is given one. The one exemption, `own_directory`, is
named with its reason and fails when it has no site left. Planted twins show it fires; the
walk is a net for the mistakes that were made, not a proof: a command built by hand and run as
root with a path in it is found only when `run` is called with it in the same function.

There is no uninstall: [the guide's "Removing rcman"](../docs/installation.md#removing-rcman)
lists by hand what the installer makes. `tests/test_install.py` derives that
list from the installer's own constants — every `Path` constant but those of
`NOT_MADE` in the test, which it only reads and names with a reason, the
constants ending `_USER` or `_GROUP`, `UNITS`, `COMMANDS`, rcman's linger entry
and `PACKAGES` — and fails when the section lacks one, so a path, account,
group, unit or package added to the installer is added to the section.

## What workspaces are made of

- **The tools**, `TOOLS` (`ensure_tools`): tmux, built from its pinned source in
  a throwaway container of `TMUX_BUILD_REFERENCE`, musl and static, so that it needs
  nothing of any image (`is_static` checks there is no dynamic loader), and
  built again only when the version or checksum it was built from
  (`.tmux-version`) changes; the system's static busybox, and `sh` as a link
  to it; the terminal descriptions `TERMINFO`, from the system's;
  `backend/rcman/tmux.conf` of the release; and, by `ensure_podman_tools`, the `podman` and
  `docker` wrappers and their configuration from `backend/rcman/podman/` in `PODMAN_TOOLS`, and
  the workspaces' seccomp profile `WORKSPACE_SECCOMP`, which `helpers/seccomp.py` derives from
  Podman's own (`PODMAN_SECCOMP`, which must be there) with its `UTS_CALLS` allowed;
  `tests/test_seccomp.py` holds the derived profile to Podman's with exactly those two calls
  allowed, and plants others, over a recorded copy of Podman's profile
  (`tests/fakes/podman-seccomp.json`) that a container test holds to the machine's (see
  [runtime.md](runtime.md#containers-in-a-workspace)).
- **The default image** (`ensure_image`): built from `CONTAINERFILE` unless
  Podman has it, named after the file's content (`workspace_image`), so that a
  changed file makes a new image, and the old one stays the workspaces' it is.
- **The builder image** (`ensure_devcontainer_builder`): built from `BUILDER_DIR` with
  rcman's Podman unless Podman has it, like the default image, so the installer needs
  the network and there is no offline install. It is named `BUILDER_REPOSITORY` and
  tagged by a hash of every file of the folder, names and content
  (`devcontainer_builder_image`), and rcman gets the name as `RCMAN_BUILDER_IMAGE`; the
  images of older tags are removed as far as Podman allows (`prune_devcontainer_builder_images`),
  and only at the end of `main`, once the services have restarted with the new name and
  answered the health check: the service that ran until then is configured with an older one.
  The installer builds the new one beside it and leaves the others alone (`ensure_devcontainer_builder`).
  What runs in it is in [runtime.md](runtime.md#building-an-image).
- **The Claude Code store**, `CLAUDE_STORE` (`ensure_claude_store`):
  `versions/<version>` for the tested version, downloaded from the
  `CLAUDE_DOWNLOAD` setting's address — `<version>/manifest.json`, whose
  checksum for the platform (`claude_platform`) the binary,
  `<version>/<platform>/claude`, must have, fetched by `helpers/claude_release.py`, which
  rcman fetches a rolled-out version with too; `current`, set to it in one step only
  where the store has none (afterwards rcman switches it to the version an administrator
  approves, and a run of the installer leaves it); and `bin/claude` leading to `current`.
  The store is rcman's and readable by every workspace: rcman makes all of it, root only
  fetching and checking the program in a folder of its own. `CLAUDE_DOWNLOAD` is written to the app's environment as
  `RCMAN_CLAUDE_DOWNLOAD`. How rcman changes the store is
  [backend.md](backend.md#claude-code-versions)'s.

## Releases

A release is `/opt/rcman/releases/<a prefix of the commit>` (`build_release`).
It is built where it will stay, because the virtualenv's scripts name their own
path, and marked with `COMPLETE` last; on a failure it is removed, `switch`
refuses a release without the mark, and the next run builds it again. `switch`
makes it current and the old current
previous, and removes the release that falls off; `rollback` swaps the two, after
`check_rollback` has refused a database newer than the previous release reads: the release's
schema version is the number of `MIGRATIONS` in its `backend/rcman/db.py`, read without
running it (`release_schema_version`), the database's is `pragma user_version` asked of SQLite
as rcman (it lags in the file's header until a checkpoint), and a release whose version cannot
be told is not rolled back to.
rcman's unit names paths under `/opt/rcman/current`, and oauth2-proxy's unit
names the toolchain's binary, so a switch needs no change to them. So do the
links `link_commands` puts in `COMMAND_DIR`, on root's path, one for each of
`COMMANDS`, to the release's script of that name: it repairs a link that
points elsewhere and refuses anything there that is not a link. The first
administrator is added with `rcman-admin` (`ensure_admin`), unless they are
there, before the services start.

## The socket

rcman trusts the identity header, so nothing but Caddy may reach it. The unit runs uvicorn with
`--uds` on `SOCKET` and no TCP listener, and with `--ws` naming the implementation of WebSockets
the Terminal tab is carried by, which uvicorn would pick by itself, written down so that a test
can hold it (see [backend.md](backend.md#the-terminal)); uvicorn makes the socket
world-writable, so the protection is its directory, `RUN`: owner `rcman`, group `WEB_GROUP`,
mode 0750, made at boot by the `tmpfiles()` line in `TMPFILES` and made or repaired by
`ensure_runtime_dir` on every run. The group's only member is `CADDY_USER`; `ensure_accounts`
takes anyone else out. `caddy_notices` says when Caddy needs its site block replaced or a
restart to join the group, and the installer warns when `caddy_admin_exposed` finds Caddy's
admin API on loopback, since any local user could reconfigure Caddy through it. The health check
reaches rcman through the socket.

## Tests

- `tests/test_install.py`: everything the installer decides that needs no
  real machine — such as settings precedence and validation, the rendered
  files, release switching and rollback, the subordinate range, telling a
  static program from another, the tmux source's and Claude Code's checksums,
  the image's tag and the builder image's, a range that is too small getting a
  second one, no range added while a container runs, the rollback refusal, the pins,
  the Caddy notices and two callers filling one toolchain at once, and the steps that
  write in rcman's home with `runuser` stood in for by the test's own user — and the
  guide held to the installer.
- `tests/test_claude_download.py`: the fake `claude` as a Claude Code release
  (`tests/claude_download.py`), downloaded into a store by the installer's own
  code and run from it.
- `tests/container/test_install_in_container.py`: the real installer, through `install.sh`,
  as root in a stubbed installation: a container of the system image on the tests' engine,
  with no network, real accounts, files, modes and `runuser`, and `systemctl`, `loginctl` and
  rcman's `podman` the recorded stand-ins of `tests/container/stand_ins.py`, under which the
  installer's units really run as their accounts (see [tests.md](tests.md#the-stubbed-installation)).
  The checkout's files become a git origin, and a user `dev` clones it and runs `install.sh`,
  with the fake `claude` as the release it downloads. Each test runs its check of
  `tests/container/install_checks.py`, of the same name, in the container: the release's
  contents and ownership, the accounts, rcman's subordinate ids and linger as the installer
  writes them, the limits' drop-ins with the installer's text and owners, the unit's
  `NoNewPrivileges=` and runtime folder, the default and builder images built under the names
  rcman's settings give, a subordinate range that reaches the builder's slot, the tools, the
  store, its files, modes and owner (rcman's, readable by every workspace) and a
  second run leaving `current` on the version rcman approved, the saved settings and the
  configuration files, the first administrator, `rcman-admin` by name as root, the socket's
  folder admitting Caddy's group alone and that group kept to Caddy, no listener (every TCP one
  and every unix socket, tried as an ordinary user) letting a local user be someone in rcman, the warning about a Caddy admin API on loopback,
  the Caddy snippet printed and valid, a second run changing nothing, saved settings needing
  no answers, a dirty clone and one that cannot fast-forward refused, self-update and rollback
  (and a rollback refused for a database newer than the release rolled back to).
- `tests/container/test_stack_in_podman.py`: what an installation does once people and
  workspaces are there, in the whole stack (see [tests.md](tests.md#the-whole-stack)): the
  services answering, the stack's administrator, no listener taking an identity a request gives
  itself and Caddy letting only oauth2-proxy set it, a person added, a workspace made, its
  server refused until the person connects the fake `claude` once, after which it and a
  workspace made afterwards both reach ready, a restart of rcman leaving the server as it was,
  a stop leaving no Claude process, one person unable to reach another's workspace, deleting a
  person leaving nothing labelled theirs, and an administrator's limits, set through the API,
  reaching `systemctl` (`rcman-admin` runs itself again as rcman's account, which the stack's
  container has not; it is run by name as root in the stubbed installation). `tests/container/test_restored_in_container.py`
  checks that rcman, started again after every container has stopped, brings the workspace and
  its server back with the limits set again before the container starts.

What needs systemd, rcman's own rootless Podman, the kernel's limits or the journal is checked
by hand after an installation ([real-claude-checklist.md](real-claude-checklist.md)).

These suites are container tests, so they run on demand, by
`make test-container`, not by `make check`; when that run is required is in
[working-rules.md](working-rules.md#tests-and-guards).
