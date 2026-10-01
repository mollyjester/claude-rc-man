#!/bin/bash
# Install or upgrade rcman. Run from a clone, as yourself through sudo:
#
#   sudo ./install.sh
#
# First the clone is brought up to date with main, as the user who ran sudo
# (so their git credentials are used); if that changed this script, it starts
# again with the new version. Then installer/install.py does the rest.
# See docs/installation.md.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
self="$repo/install.sh"

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run install.sh with sudo"
case " $* " in *" --rollback "*) exec /usr/bin/python3 -I "$repo/installer/install.py" "$@" ;; esac
[ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] || die "run install.sh with sudo from the account that owns the clone"

as_owner() { runuser -u "$SUDO_USER" -- "$@"; }

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

exec /usr/bin/python3 -I "$repo/installer/install.py" "$@"
