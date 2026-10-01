# Installer

How to install, for administrators: [the installation guide](../docs/installation.md).
That guide and `install.sh` are one contract: a change to either changes
both, and `tests/test_install.py` holds the guide's options, variables and
paths to the installer's.

## Parts

| File | What it is |
|---|---|
| `install.sh` | Checks it runs through sudo, updates the clone as `$SUDO_USER` (fast-forward only, refusing a dirty tree or another branch), starts again if that changed `install.sh`, then hands over to the Python installer |
| `installer/install.py` | Everything else; Python standard library only, run with the system interpreter in isolated mode |
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

## Tests

- `tests/test_install.py`: settings precedence and validation, rendered files,
  sudoers validated with `visudo`, release switching and rollback, and the
  guide.
- `tests/container/test_install_in_container.py`: a real installation in a
  fresh container. The working tree becomes a git origin and a user `dev`
  clones it and runs `install.sh`. `tests/container/fake-systemctl` stands in
  for systemd, starting each service exactly as its unit file says. The host
  fills a toolchain cache with the installer's own download code and mounts it
  in. The suite checks the release's contents and ownership, the accounts,
  files and modes, the services answering, rcman reaching both helpers through
  sudo, a second run changing nothing, self-update and its restart, rollback,
  the refusals, and — with Caddy and stand-ins for oauth2-proxy and rcman —
  that a browser cannot set the identity header itself.
