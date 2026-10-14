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
| `installer/templates/` | The files it writes, with `@NAME@` placeholders: the app's environment, oauth2-proxy's configuration, both systemd units, and the Caddy site block. A test keeps `TEMPLATES` and the directory equal. Besides the settings, `render` fills in what the installer found out: rcman's uid (`APP_UID`) and the default image's name (`WORKSPACE_IMAGE`) |
| `installer/workspace/Containerfile` | The default workspace image, built with rcman's own Podman |

The pinned versions are `UV_VERSION`, `OAUTH2_PROXY_VERSION` and
`TMUX_VERSION` in `installer/install.py`, `frontend/.nvmrc` for Node.js, the
tags of `BUILDER_IMAGE` and of the Containerfile's `FROM`, and
`helpers/claude-version` for Claude Code. Downloads are checked against the
checksums their publishers list; tmux publishes none, so its source is checked
against `TMUX_SHA256`, and Claude Code against its release's manifest. `ensure_toolchain` fills the
toolchain holding an exclusive lock on a file in it, `TOOLCHAIN_LOCK`, so two
callers filling one toolchain at once, such as two test runs sharing the
tests' toolchain cache, take turns and the second finds what the first
unpacked.

## Rootless Podman

`ensure_packages` installs `PACKAGES` that are missing. `ensure_subordinate_ids`
gives `rcman` `SUBORDINATE_COUNT` subordinate uids and gids, a range after every
range already in `/etc/subuid` and `/etc/subgid` (`subordinate_start`), unless it
has one that big; a test holds the count to `MAX_PEOPLE` and `RANGE` (see
[people.md](people.md)). `ensure_podman` enables linger for `rcman`, starts its
user manager, enables `podman.socket` in it and waits for the socket; when the
subordinate ids changed, it tells Podman with a local `podman system migrate`
first. Everything after runs Podman as rcman through that socket
(`as_rcman_podman`). `rcman.service` names the socket's `XDG_RUNTIME_DIR` and
runs with `NoNewPrivileges=yes` and `UMask=0077`.

The installer refuses a machine with an rcman from before workspaces
(`check_not_an_old_install`: `OLD_INSTALL_FILES`, or a database without people),
and `--rollback` refuses a previous release without `WORKSPACES_MARK`: there is
no upgrade from one, as [the installation guide](../docs/installation.md) says.

## What workspaces are made of

- **The tools**, `TOOLS` (`ensure_tools`): tmux, built from its pinned source in
  a throwaway container of `BUILDER_IMAGE`, musl and static, so that it needs
  nothing of any image (`is_static` checks there is no dynamic loader), and
  built again only when the version or checksum it was built from
  (`.tmux-version`) changes; the system's static busybox, and `sh` as a link
  to it; the terminal descriptions `TERMINFO`, from the system's; and
  `backend/rcman/tmux.conf` of the release.
- **The default image** (`ensure_image`): built from `CONTAINERFILE` unless
  Podman has it, named after the file's content (`workspace_image`), so that a
  changed file makes a new image, and the old one stays the workspaces' it is.
- **The Claude Code store**, `CLAUDE_STORE` (`ensure_claude_store`):
  `versions/<version>` for the tested version, downloaded from the
  `CLAUDE_DOWNLOAD` setting's address — `<version>/manifest.json`, whose
  checksum for the platform (`claude_platform`) the binary,
  `<version>/<platform>/claude`, must have; `current`, switched to it in one
  step; and `bin/claude` leading to `current`.

## Releases

A release is `/opt/rcman/releases/<a prefix of the commit>` (`build_release`).
It is built where it will stay, because the virtualenv's scripts name their own
path, and marked with `COMPLETE` last; on a failure it is removed, `switch`
refuses a release without the mark, and the next run builds it again. `switch`
makes it current and the old current
previous, and removes the release that falls off; `rollback` swaps the two.
rcman's unit names paths under `/opt/rcman/current`, and oauth2-proxy's unit
names the toolchain's binary, so a switch needs no change to them. So do the
links `link_commands` puts in `COMMAND_DIR`, on root's path, one for each of
`COMMANDS`, to the release's script of that name: it repairs a link that
points elsewhere and refuses anything there that is not a link. The first
administrator is added with `rcman-admin` (`ensure_admin`), unless they are
there, before the services start.

## The socket

rcman trusts the identity header, so nothing but Caddy may reach it. The unit
runs uvicorn with `--uds` on `SOCKET` and no TCP listener; uvicorn makes the
socket world-writable, so the protection is its directory, `RUN`: owner
`rcman`, group `WEB_GROUP`, mode 0750, made at boot by the `tmpfiles()` line in
`TMPFILES` and made or repaired by `ensure_runtime_dir` on every run. The
group's only member is `CADDY_USER`; `ensure_accounts` takes anyone else out.
`caddy_notices` says when Caddy needs its site block replaced or a restart to
join the group, and the installer warns
when `caddy_admin_exposed` finds Caddy's admin API on loopback, since any
local user could reconfigure Caddy through it. The health check reaches rcman
through the socket.

## Tests

- `tests/test_install.py`: everything the installer decides that needs no
  real machine — such as settings precedence and validation, the rendered
  files, release switching and rollback, the subordinate range, telling a
  static program from another, the tmux source's and Claude Code's checksums,
  the image's tag, the Caddy notices and two callers filling one toolchain at
  once — and the guide held to the installer.
- `tests/test_claude_download.py`: the fake `claude` as a Claude Code release
  (`tests/claude_download.py`), downloaded into a store by the installer's own
  code and run from it.
- `tests/container/test_install_in_container.py`: a real installation in a
  fresh podman host, under systemd, through `install.sh`, with the fake
  `claude` as the release it downloads. The checkout's files, as the container
  suite sees them (see [tests.md](tests.md#running)), become a git origin, and
  a user `dev` clones it and runs `install.sh`. The host fills a toolchain
  cache with the installer's own download code and mounts it in. The suite
  checks what an installation on a real machine does, such as the release's
  contents and ownership, the accounts, rootless Podman for rcman, the tools,
  the image and the store, files and modes, the services answering, the first
  administrator, `rcman-admin` by name as root, a second run changing nothing,
  self-update and rollback, and — with Caddy and stand-ins for oauth2-proxy and
  rcman — that a browser cannot set the identity header itself and that only
  Caddy's account can reach rcman's socket. Then, through the API as Caddy
  passes it on: a person added, a workspace made, its server refused until the
  person connects the fake `claude` once, through the API, after which it and a
  workspace made afterwards both reach ready, each binding the person's own
  credentials file, its processes outside
  `rcman.service`, a restart of rcman leaving them as they were, a stop leaving
  no Claude process, one person unable to reach another's workspace, and
  deleting a person leaving nothing labelled theirs.
  `tests/container/test_restored_in_container.py` then checks, after the whole
  host has restarted, that rcman brought the workspace and its server back.
