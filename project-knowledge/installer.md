# Installer

How to install, for administrators: [the installation guide](../docs/installation.md).
That guide and `install.sh` are one contract: a change to either changes
both, and `tests/test_install.py` holds the guide's options, variables and
paths to the installer's.

## Parts

| File | What it is |
|---|---|
| `install.sh` | Checks it runs through sudo, updates the clone as `$SUDO_USER` (fast-forward only, refusing a dirty tree or another branch), starts again if that changed `install.sh`, then hands over to the Python installer |
| `installer/install.py` | Everything else, using the standard library and the clone's `helpers/users_file.py` and `helpers/rules.py`, whose rules' sentences explain a refused setting; run with the system interpreter in isolated mode, it writes no bytecode into the clone and takes the clone off its path once it has imported the rules |
| `installer/templates/` | The files it writes, with `@NAME@` placeholders: the app's environment, oauth2-proxy's configuration, both systemd units, and the Caddy site block. A test keeps `TEMPLATES` and the directory equal |

The pinned versions are `UV_VERSION` and `OAUTH2_PROXY_VERSION` in
`installer/install.py`, and `frontend/.nvmrc` for Node.js. Downloads are checked
against the checksums their publishers list.

## Releases

A release is `/opt/rcman/releases/<first twelve characters of the commit>`,
built in a temporary directory and renamed into place only when complete, then
marked with `.complete`. `switch` makes it current and the old current
previous, and removes the release that falls off; `rollback` swaps the two.
The sudoers rules and the units name paths under `/opt/rcman/current`, so a
switch needs no change to them.

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

- `tests/test_install.py`: settings precedence and validation, dropping saved
  settings that are not fields, rendered files, sudoers validated with
  `visudo`, release switching and rollback, the Caddy notices, and the guide.
- `tests/container/test_install_in_container.py`: a real installation in a
  fresh container. The working tree becomes a git origin and a user `dev`
  clones it and runs `install.sh`. `tests/container/fake-systemctl` stands in
  for systemd, starting each service exactly as its unit file says. The host
  fills a toolchain cache with the installer's own download code and mounts it
  in. The suite checks the release's contents and ownership, the accounts,
  files and modes, the services answering, rcman reaching both helpers through
  sudo, a second run changing nothing, self-update and its restart, rollback,
  the refusals, and — with Caddy and stand-ins for oauth2-proxy and rcman —
  that a browser cannot set the identity header itself. It also checks that
  only Caddy's account can reach rcman's socket and that a run takes anyone
  else out of its group, that no listener in the container lets a local user
  be someone in rcman, that a Caddy admin API on loopback is warned about,
  that a helper call that times out ends the helper behind sudo, and that
  audit events reach the service's output, which systemd sends to the
  journal.
