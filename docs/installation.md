# Installing rcman

rcman runs on one Ubuntu 24.04 machine. Caddy, already running there, serves it
on a public host name with HTTPS; Google sign-in goes through oauth2-proxy,
which the installer sets up. People's workspaces are containers of rootless
Podman, run by rcman's own account; the installer sets that up too. The
installer is `install.sh` in a clone of this repository, and the same command
upgrades an installation.

## Before you start

- **A Google OAuth client.** Creating one is free; the steps are below.
- **A host name** pointing at the machine, served by Caddy.
- **A clone** of this repository on `main`, owned by your own account, with
  git able to pull from GitHub as you (for a private repository, for example
  through `gh auth login`).
- **sudo** for your account.
- **Access to the internet** while it runs, for the packages and the downloads
  below.

The installer installs the system packages Podman and the workspaces need
(`PACKAGES` in `installer/install.py`) when they are missing. Everything else —
the Node.js that builds the web interface, uv for the Python environment,
oauth2-proxy, tmux's source, and Claude Code — it downloads itself and checks
against its published checksum, or, for tmux, which publishes none, against
the one the installer names (`TMUX_SHA256`). The images workspaces are built
from come from Docker Hub. The builder image needs the network as well, for
the Ubuntu packages and the npm package it installs, so an installation cannot
be done offline. Building a repository's devcontainer.json later needs the
network too (see [devcontainer.json support](devcontainer.md#building-an-image)).

## Creating the Google OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project (for example `rcman`). No billing account is needed.
2. Open **Google Auth Platform** and choose **Get started**: an app name, a
   support email, and as **Audience** choose **External** — personal Gmail
   accounts can then sign in. Do not upload a logo; that starts Google's brand
   review.
3. Under **Data access**, add only the `openid`, `userinfo.email` and
   `userinfo.profile` scopes. These need no verification by Google.
4. Under **Branding**, Google needs two links before the app can be published:
   - **Application home page**: `https://<your host name>/`.
   - **Application privacy policy link**: the same address. rcman has no
     separate privacy page, and Google does not check the link for these
     scopes.
   - Under **Authorised domains**, add the domain your host name belongs to:
     `example.com` for `rcman.example.com`. It also covers the redirect URI
     below.

   Leave the logo and the Terms of Service link empty, and save.
5. Under **Audience**, choose **Publish app**. Who gets in is still decided by
   rcman's own list of people, not by Google.
6. Under **Clients**, create a client of type **Web application**, with the
   authorized redirect URI `https://<your host name>/oauth2/callback`, exactly.
7. Copy the client ID and the client secret straight away: Google shows the
   secret only once. If it is lost, add a new secret to the client and delete
   the old one.

## Installing

From the clone, as yourself:

```
sudo ./install.sh
```

The installer asks for what it needs the first time, and saves the answers in
`/etc/rcman/install.env`, readable only by root, so later runs ask nothing:

| Asked for | Option | Variable |
|---|---|---|
| The public host name, such as `rcman.example.com` | `--domain` | `RCMAN_INSTALL_DOMAIN` |
| The Google OAuth client ID | `--google-client-id` | `RCMAN_INSTALL_GOOGLE_CLIENT_ID` |
| The Google OAuth client secret | `--google-client-secret` | `RCMAN_INSTALL_GOOGLE_CLIENT_SECRET` |
| The first administrator's Google address | `--admin-email` | `RCMAN_INSTALL_ADMIN_EMAIL` |
| The local port oauth2-proxy listens on | `--proxy-port` | `RCMAN_INSTALL_PROXY_PORT` |
| The Linux account Caddy runs as | `--caddy-user` | `RCMAN_INSTALL_CADDY_USER` |
| Where Claude Code's releases are downloaded from | `--claude-download` | `RCMAN_INSTALL_CLAUDE_DOWNLOAD` |

An option wins over a variable, a variable over the saved answer, and the
saved answer over asking. The port, Caddy's account and the download address
have defaults (`FIELDS` in `installer/install.py`) and are not asked for;
the download address is also rcman's own setting `RCMAN_CLAUDE_DOWNLOAD`, which
rcman rolls out versions from (see [Configuration](configuration.md));
Caddy's account must exist. Anything else saved in
`/etc/rcman/install.env` is dropped from it.
Without a terminal the installer does not ask; it names what is missing and
stops. The secret oauth2-proxy signs its cookies with is generated on the first
run and kept.

## What the installer does

1. **Brings the clone up to date** with `main`, as you, so your git
   credentials are used. It refuses when the clone has uncommitted changes, is
   on another branch, or cannot be fast-forwarded. If the update changed
   `install.sh`, it starts again with the new version.
2. **Installs the packages** rootless Podman and the workspaces need, those
   that are missing.
3. **Creates the accounts it needs**: a system user `rcman` for rcman itself
   (its data, and its containers' storage, live in `/var/lib/rcman`), a system
   user `rcman-proxy` for oauth2-proxy, and a group `rcman-web` whose only
   member is Caddy's account; anyone else in it is taken out.
4. **Sets up rootless Podman for `rcman`**: a range of subordinate user and
   group ids of its own in `/etc/subuid` and `/etc/subgid`, big enough for rcman's
   own range, a range for each person rcman has room for, and one more for the
   builder of devcontainer images, which no person's workspace uses; a machine that
   has a range from an earlier installation gets another, full one; lingering, so its user manager
   runs from boot, without anyone signed in; and Podman's socket in that user
   manager. Workspaces run there, outside rcman's own service, so they keep
   running while rcman restarts or upgrades.
   The limits administrators put on memory, CPU and processes (see
   [Limits](users.md#limits)) need that user manager to have the `cpu`, `memory`
   and `pids` controllers; the installer checks, and prints a warning, and goes on, when
   a controller has not been delegated to the user managers (`Delegate=` in a
   drop-in of `user@.service`, which is for you to decide and does not change by
   itself), when the machine does not use only cgroup v2, which limits need, or
   when it cannot tell. Limits are off until an administrator sets one, and then
   setting one says it could not be applied. It also lifts the default ceiling on processes of rcman's user slice
   (`/etc/systemd/system/user-<uid>.slice.d/rcman.conf`) and of rcman's user manager
   (`.config/systemd/user.conf.d/rcman.conf` in rcman's home), so that the
   only limits on processes are the ones administrators set. Both drop-ins are written
   even after such a warning, and the installer asks rcman's user manager to read
   its configuration again on every run, which restarts nothing and ends nothing
   running. It refuses, as it does for no other file, when a folder it writes
   into for this is a link.
   **Warning:** adding the builder's range to a machine that has the earlier, smaller
   one makes the installer run `podman system migrate` as `rcman`, which stops its
   running containers, and so every workspace's server. rcman is in an alpha without
   upgrades: reinstall a machine rather than upgrade it.
5. **Makes the directory of rcman's socket**, `/run/rcman`: it belongs to
   `rcman` and the group `rcman-web`, and no one else may enter it.
   `/etc/tmpfiles.d/rcman.conf` makes it again at every boot.
6. **Builds a release** of the clone's commit under `/opt/rcman/releases`: the
   files rcman runs, the built web interface, and its Python environment — and
   nothing used only for development. Everything in it belongs to root.
7. **Makes it current** — `/opt/rcman/current` — and keeps the release before
   it as `/opt/rcman/previous`; older ones are removed.
8. **Makes what workspaces are built from**:
   - the tools every workspace has, whatever its image: tmux, built here into a
     program that needs nothing of any image, with busybox and the terminal
     descriptions, in `/var/lib/rcman/tools`;
   - the default workspace image, Ubuntu with git and what Claude Code needs,
     from `installer/workspace/Containerfile`, named after that file's
     content, so that a changed file makes a new image and workspaces made
     before keep theirs. A repository's devcontainer.json can name another
     image, which rcman pulls when a workspace is made (see
     [devcontainer.json support](devcontainer.md));
   - the builder image, from `installer/builder`: the Dev Containers CLI and a
     Podman of its own, which rcman runs for one build of a repository's
     devcontainer.json at a time and removes after it. The CLI is installed from a
     lock file that pins it, and the image is named after the content of the
     folder, so that a change makes a new image and older ones are removed when
     nothing runs from them. It is built as the default image is, with the network;
   - the Claude Code store, `/var/lib/rcman/claude`: the version rcman is
     tested with (`helpers/claude-version`), downloaded and checked against its
     release's checksum (see [Claude Code versions](claude-version.md)). The store
     belongs to rcman, which writes it when an administrator rolls out another
     version, and every workspace has it, read-only. The installer sets the
     version `current` names only when there is none, so running it again never
     changes the approved version.
9. **Writes the configuration** in `/etc/rcman` and the two systemd services,
   `rcman` and `rcman-oauth2-proxy`.
10. **Puts `rcman-admin` on root's path**: `/usr/local/sbin/rcman-admin` is a
   link to the current release's, so `sudo rcman-admin` finds it. If something
   else is already there, the installer stops rather than replace it.
11. **Adds the first administrator** with `rcman-admin`, unless they are there.
12. **Starts the services**, restarting them when anything changed.
13. **Checks** that both services answer, and prints the Caddy configuration
   and what to do about Caddy, if anything.

Running it again for the same commit changes nothing.

## What a workspace can reach

A workspace has the network, as Claude Code and git need: it reaches the
internet, and nothing reaches it, since it publishes no port. It cannot reach
another workspace, nor anything on this machine's loopback addresses, such as
oauth2-proxy. It can reach **anything this machine serves on its other
addresses**, and anything on the networks this machine is on, as rcman's own
account could. If that matters, keep such services on loopback, or limit what
rcman's account (`rcman`) may connect to with the machine's firewall. The
builder of a repository's devcontainer.json has the same network as a
workspace, and so the same reach, and it runs the repository's own Dockerfile
steps and feature scripts.

## Podman's volumes are rcman's

rcman keeps each workspace, and each person's Claude sign-in, in volumes of its
own account's Podman. A person's sign-in is a volume no container mounts by
name, so to Podman it looks unused: **never run `podman volume prune` as
`rcman`**, or everyone is signed out of Claude. rcman removes a person's volumes
itself when they are removed.

## Caddy

rcman listens on no network port. Its only listener is the unix socket
`/run/rcman/rcman.sock`, in a directory only the group `rcman-web` may enter,
and Caddy's account is that group's only member. rcman believes the address
Caddy passes it, so this is what keeps anyone else on the machine from
passing one.

The installer prints the site block for your Caddyfile and keeps it in
`/etc/rcman/Caddyfile.snippet`. Either `import` that file in your Caddyfile, so
it is always the current one, or copy the block in. It sends oauth2-proxy's
own pages to oauth2-proxy, asks oauth2-proxy whether each other request is
signed in, sends people who are not to the Google sign-in, and passes the
signed-in address to rcman through the socket. It also removes any address a
browser sends itself, so only oauth2-proxy can tell rcman who someone is.

The site block also has Caddy log every request, the polls included, to
`/var/log/caddy/rcman-access.log`: one JSON line each, with the address, the
browser, the path and the status, and with cookies left out. Caddy rotates the
file itself and deletes a rotated file once there are too many of them or it is
too old; the limits are the `roll_` settings in
[`installer/templates/Caddyfile`](../installer/templates/Caddyfile). Caddy's
account must be able to write to `/var/log/caddy`, as it can when Caddy comes
from its Ubuntu package. Do not check the Caddyfile with `sudo caddy validate`:
run as root, it creates the log file as root's, and Caddy then cannot open it
and does not start. Run it as Caddy's account (`sudo -u caddy caddy validate
--config /etc/caddy/Caddyfile`), or give the file back to that account with
`chown` if root already made it.

The installer says when Caddy needs something from you, at the end of its
output:

- **The site block changed** — on the first installation, and whenever an
  upgrade changes it. If your Caddyfile imports the snippet there is nothing
  to change in it; if it holds a copy, copy the block again. Then **restart**
  Caddy: it keeps the site block it started with until then.
- **Caddy's account joined `rcman-web`** — on the first installation, or when
  `--caddy-user` names another account. **Restart** Caddy. A reload is not
  enough, because only a Caddy that starts again belongs to the group, and
  until then it cannot reach rcman's socket.

Caddy's admin API must not be reachable by other local users. By default it
listens on `localhost:2019`, where any local user can rewrite Caddy's
configuration — and so send rcman any address they like. Turn it off, or move
it to a unix socket, in the global options at the top of your Caddyfile, then
restart Caddy:

```
{
	admin off
}
```

The installer warns, on every run, while something answers on that address,
at `127.0.0.1:2019`.

## A machine with an rcman from before workspaces

An rcman from before workspaces — where people had Linux accounts of their own —
cannot be upgraded. The installer refuses to run on a machine that has one: one
with `/etc/sudoers.d/rcman`, `/etc/rcman/users.toml`, or a database in
`/var/lib/rcman` without people in it. rcman itself refuses such a database.
Remove that installation first — stop and disable its services `rcman` and
`rcman-oauth2-proxy`, and remove `/etc/systemd/system/rcman*.service`,
`/opt/rcman`, `/etc/rcman`, `/var/lib/rcman`, `/etc/sudoers.d/rcman`,
`/usr/local/sbin/rcman-user`, and the Linux accounts it made for people — and
then install as above. `--rollback` refuses to go back to a release from before
workspaces for the same reason.

## Upgrading and rolling back

To upgrade, run `sudo ./install.sh` again: it pulls `main` and installs it.
Then do what it says about Caddy, as described under [Caddy](#caddy).

To go back to the release before the current one, use `--rollback`:

```
sudo ./install.sh --rollback
```

Rolling back again returns to the newer release.

Upgrading rcman, rolling back, and running the installer again never change
which Claude Code version is approved, and restart no running server; see
[Claude Code versions](claude-version.md).

## Afterwards

Add people with `sudo rcman-admin add` or on the **People** page, under
**Admin** in rcman's navigation — see [Adding and removing people](users.md).
rcman's own settings, which administrators set, are described in
[Configuration](configuration.md).
