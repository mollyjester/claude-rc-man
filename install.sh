#!/bin/bash
# Install or upgrade rcman. Run from a clone, as yourself through sudo:
#
#   sudo ./install.sh
#
# First it takes the installer's lock, so that one run goes at a time, a rollback too. Then the
# clone is brought up to date with main, as the account that owns the clone (so its git
# credentials are used, and git asks nothing); if that changed this script, it starts again with
# the new version, holding the same lock. Then installer/install.py does the rest.
# rcman-upgrade.service runs it the same way, as root with no SUDO_USER (installer/upgrade.py).
# See docs/installation.md.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
self="$repo/install.sh"
root=/opt/rcman
lock="$root/.install.lock"

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run install.sh with sudo"

# The lock is held on fd 9 until the installer ends; install.py checks that it is. A run started
# again after an update has it already (RCMAN_INSTALL_LOCK_FD), and must not open it again; it
# checks that fd 9 is the lock, and holds it.
if [ -n "${RCMAN_INSTALL_LOCK_FD:-}" ]; then
  [ /proc/self/fd/9 -ef "$lock" ] && flock -n 9 \
    || die "fd 9 is not the installer's lock $lock held by this run; run install.sh as it is"
else
  mkdir -p -m 0755 "$root"
  [ ! -L "$root" ] && [ -O "$root" ] && [ -z "$(find "$root" -maxdepth 0 -perm /022)" ] \
    || die "$root must be a folder of root's that only root can write"
  [ ! -L "$lock" ] || die "$lock is a link; remove it and run install.sh again"
  exec 9>>"$lock"
  flock -n 9 || die "another install.sh is running, holding $lock; wait for it to end, then run it again"
  export RCMAN_INSTALL_LOCK_FD=9
fi

case " $* " in *" --rollback "*) exec /usr/bin/python3 -I -X utf8 "$repo/installer/install.py" "$@" ;; esac

# The clone's owner, who must be the account that ran sudo where there is one.
owner="$(stat -c %U -- "$repo")"
[ "$owner" != root ] && [ "$owner" != UNKNOWN ] \
  || die "the clone is root's; clone it as your own account and run install.sh with sudo from there"
[ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = "$owner" ] \
  || die "the clone is $owner's, not $SUDO_USER's; run install.sh with sudo from $owner's account"

# As the owner, and without the lock, which only the installer holds. An administrator at a
# terminal may be asked for a password or a passphrase; anyone else, as the upgrade service, is
# not: git asks nothing (installer/upgrade.py GIT_ENVIRONMENT), with no terminal.
if [ -t 0 ] && [ -n "${SUDO_USER:-}" ]; then
  as_owner() { runuser -u "$owner" -- env "$@" 9>&-; }
else
  as_owner() {
    runuser -u "$owner" -- env GIT_TERMINAL_PROMPT=0 \
      GIT_SSH_COMMAND="ssh -oBatchMode=yes -oConnectTimeout=60" GCM_INTERACTIVE=never \
      GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60 "$@" </dev/null 9>&-
  }
fi

if [ -z "${RCMAN_INSTALL_UPDATED:-}" ]; then
  [ -z "$(as_owner git -C "$repo" status --porcelain)" ] || die "the clone has uncommitted changes; commit or discard them first"
  branch="$(as_owner git -C "$repo" rev-parse --abbrev-ref HEAD)"
  [ "$branch" = main ] || die "the clone is on $branch; switch to main first"
  before="$(sha256sum "$self")"
  as_owner git -C "$repo" pull --ff-only --quiet origin main || die "main cannot be fast-forwarded; resolve that in the clone first"
  if [ "$(sha256sum "$self")" != "$before" ]; then
    echo "install.sh changed; starting it again."
    RCMAN_INSTALL_UPDATED=1 exec "$self" "$@"
  fi
fi

exec /usr/bin/python3 -I -X utf8 "$repo/installer/install.py" "$@"
