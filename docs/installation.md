# Installing rcman

rcman runs on one Ubuntu 24.04 machine. Caddy, already running there, serves it
on a public host name with HTTPS; Google sign-in goes through oauth2-proxy,
which the installer sets up. People's workspaces are containers of rootless
Podman, run by rcman's own account; the installer sets that up too. The
installer is `install.sh` in a clone of this repository, and the same command
upgrades an installation; where you turn it on, administrators can also upgrade
rcman from its web interface (see
[Upgrading from the web interface](#upgrading-from-the-web-interface)).

## Before you start

- **A Google OAuth client.** Creating one is free; the steps are below.
- **A host name** pointing at the machine, served by Caddy. Caddy is installed and
  running already; the installer does not install it, and its account must exist.
- **A wildcard DNS record**, `*.ws.<DOMAIN>` for your host name `<DOMAIN>`, pointing at the
  same machine, set once: each workspace's [forwarded port](#forwarded-ports) is a host under it.
- **A dedicated clone** of this repository on `main`, owned by your own
  account and used for installing and nothing else, with git able to pull from
  GitHub as you (for a private repository, for example through `gh auth
  login`). `install.sh` runs `npm ci` in the clone, as you, and the installer
  runs as root and imports Python from it, so no one else may be able to write
  in it.
- **Administrator rights**: your account runs `sudo ./install.sh`.
- **A clean Ubuntu 24.04 machine** with systemd, on x86-64 or arm64; the
  installer refuses another processor. Podman and `crun` are in the `universe`
  component of Ubuntu's package archive, which must be enabled. Its kernel must
  mount overlay inside a user namespace, which the builder of devcontainer images
  stores its images with; the installer refuses a kernel older than
  `USERNS_OVERLAY_KERNEL` in `installer/install.py`, before it changes anything.
- **Access to the internet** while it runs, for the packages and the downloads
  below. Behind a firewall that limits outbound traffic, allow Ubuntu's package
  archive, `nodejs.org` and `github.com` (Node.js, uv, oauth2-proxy and tmux's
  source), the npm and Python package registries, Docker Hub, the Claude Code
  download address (`RCMAN_INSTALL_CLAUDE_DOWNLOAD`, below), and the hosts these
  redirect to.

The installer installs the system packages Podman and the workspaces need
(`PACKAGES` in `installer/install.py`) when they are missing; among them are
`podman`, `uidmap`, which rootless Podman needs to map user ids, and `crun`, the
OCI runtime workspaces and builds are tested with. Workspace containers run with a
seccomp profile derived from Podman's own, `/usr/share/containers/seccomp.json`, which
Ubuntu's Podman brings; the installer refuses a machine where it is not there. Everything else — the Node.js
that builds the web interface, uv for the Python environment, oauth2-proxy,
tmux's source, and Claude Code — it downloads itself and checks against a
checksum. Claude Code is checked against the manifest of its release.
Node.js, uv, oauth2-proxy and tmux are checked against checksums the repository
pins (`NODE_SHA256`, `UV_SHA256`, `OAUTH2_PROXY_SHA256` and `TMUX_SHA256` in
`installer/install.py`), each beside the version it belongs to, so a release page
that is not to be trusted cannot swap an archive together with its checksum. The
images the installer builds from are named by tag and by digest (`TMUX_BUILD_DIGEST`
for the one tmux is built in, and the `FROM` line of each Containerfile under
`installer/`), so that a tag moved at Docker Hub changes nothing here. The images
workspaces are built from come from Docker Hub. The builder image needs the
network as well, for the Ubuntu packages and the npm package it installs, so an
installation cannot be done offline. Building a repository's devcontainer.json
later needs the network too (see
[devcontainer.json support](devcontainer.md#building-an-image)).

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

From the clone made for installing, as yourself:

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
| The local port Caddy asks rcman on before it gets a certificate for a forwarded port; not oauth2-proxy's | `--ask-port` | `RCMAN_INSTALL_ASK_PORT` |
| The account Caddy runs as | `--caddy-user` | `RCMAN_INSTALL_CADDY_USER` |
| Where Claude Code's releases are downloaded from | `--claude-download` | `RCMAN_INSTALL_CLAUDE_DOWNLOAD` |
| Whether administrators may upgrade rcman from its web interface, `yes` or `no` | `--web-upgrade` | `RCMAN_INSTALL_WEB_UPGRADE` |

An option wins over a variable, a variable over the saved answer, and the
saved answer over asking. The ports, Caddy's account, the download address and
upgrading from the web interface have defaults (`FIELDS` in `installer/install.py`)
and are not asked for; upgrading from the web interface is off by default (see
[Upgrading from the web interface](#upgrading-from-the-web-interface));
the download address is also rcman's own setting `RCMAN_CLAUDE_DOWNLOAD`, which
rcman rolls out versions from (see [Configuration](configuration.md));
Caddy's account must exist. Anything else saved in
`/etc/rcman/install.env` is dropped from it.
Without a terminal the installer does not ask; it names what is missing and
stops. The secret oauth2-proxy signs its cookies with is generated on the first
run and kept.

## What the installer does

1. **Takes the installer's lock**, `/opt/rcman/.install.lock`, which it holds
   to the end, so that one run goes at a time: a second run, a rollback among
   them, says another is running and stops. Then it **brings the clone up to date** with `main`, as
   the account that owns the clone, which must be you, so your git credentials
   are used. It refuses when the clone is root's or has
   uncommitted changes, is on another branch, or cannot be fast-forwarded. If
   the update changed `install.sh`, it starts again with the new version,
   holding the same lock.
2. **Installs the packages** rootless Podman and the workspaces need, those
   that are missing.
3. **Creates the accounts it needs**: a system user `rcman` for rcman itself
   (its data, and its containers' storage, live in `/var/lib/rcman`), a system
   user `rcman-proxy` for oauth2-proxy, and a group `rcman-web` whose only
   member is Caddy's account; anyone else in it is taken out.
4. **Sets up rootless Podman for `rcman`**, in this order:
   - a range of subordinate user and group ids of its own in `/etc/subuid` and
     `/etc/subgid`, big enough for rcman's own range, a range for each person
     rcman has room for, and one more for the builder of devcontainer images,
     which no person's workspace uses; an account whose range is smaller than
     that gets another, full one. When the installer has to add a range, it
     first looks for containers running in rcman's Podman, and **refuses** to
     add it if there is one: Podman takes a new range only through
     `podman system migrate`, which stops every running container, and so every
     workspace's server. What it did before that, the settings, the packages and
     the accounts, stays, and it is safe to run it again. rcman's interface
     cannot stop a container, and rcman starts the workspaces again when it
     starts, so stop rcman first and then the containers, as root:

     ```
     systemctl stop rcman
     runuser -u rcman -- env -i PATH=/usr/bin:/bin HOME=/var/lib/rcman \
       XDG_RUNTIME_DIR=/run/user/$(id -u rcman) podman stop --all
     ```

     Then run `./install.sh` again: it adds the range, and starts rcman, which
     brings the workspaces back. With the range there, a second run asks
     nothing of Podman's containers and ends none;
   - lingering, so its user manager runs from boot, without anyone signed in;
     and Podman's socket in that user manager. Workspaces run there, outside
     rcman's own service, so they keep running while rcman restarts or
     upgrades;
   - a check, with `podman info` as `rcman`, that rcman's Podman uses `crun` as
     its OCI runtime, the one workspaces and the builder are tested with. The
     installer stops with a message if it reports another;
   - what the limits need. The limits administrators put on memory, CPU and
     processes (see [Limits](users.md#limits)) need that user manager to have
     the `cpu`, `memory` and `pids` controllers; the installer checks, and
     prints a warning, and goes on, when a controller has not been delegated to
     the user managers (`Delegate=` in a drop-in of `user@.service`, which is
     for you to decide and does not change by itself), when the machine does not
     use only cgroup v2, which limits need, or when it cannot tell. Limits are
     off until an administrator sets one, and then setting one says it could not
     be applied. It also lifts the default ceiling on processes of rcman's user
     slice (`/etc/systemd/system/user-<uid>.slice.d/rcman.conf`) and of rcman's
     user manager (`.config/systemd/user.conf.d/rcman.conf` in rcman's home), so
     that the only limits on processes are the ones administrators set. Both
     drop-ins are written even after such a warning, and the installer asks
     rcman's user manager to read its configuration again on every run, which
     restarts nothing and ends nothing running. The drop-in in rcman's home is
     written by `rcman` itself, as is everything else the installer makes in
     rcman's home: root never writes in a folder `rcman` could have put a link
     in.
5. **Makes the directory of rcman's socket**, `/run/rcman`: it belongs to
   `rcman` and the group `rcman-web`, and no one else may enter it.
   `/etc/tmpfiles.d/rcman.conf` makes it again at every boot.
6. **Builds a release** of the clone's commit under `/opt/rcman/releases`: the
   files rcman runs, the built web interface, and its Python environment — and
   nothing used only for development. The Node.js, uv and oauth2-proxy it builds
   and runs with are downloaded, checked, into `/opt/rcman/toolchain` first.
   Everything in a release belongs to root, and the built web interface is
   copied only if it holds no symbolic link.
7. **Makes it current** — `/opt/rcman/current` — and keeps the release before
   it as `/opt/rcman/previous`; older ones are removed.
8. **Makes what workspaces are built from**:
   - the tools every workspace has, whatever its image: tmux, built here into a
     program that needs nothing of any image, with busybox and the terminal
     descriptions; the `podman` and `docker` that run containers in a workspace,
     with their configuration (see [Containers](workspaces.md#containers)); the
     instructions rcman gives Claude in every workspace, made from the release's
     `backend/rcman/instructions` (see
     [What Claude is told in a workspace](workspaces.md#what-claude-is-told-in-a-workspace));
     and the seccomp profile of every workspace container, derived from Podman's own; in
     `/opt/rcman/tools`. They are root's, outside rcman's home, so that rcman cannot
     replace what every workspace mounts;
   - the default workspace image, Ubuntu with git, `gh`, Podman (with `crun`,
     `catatonit` and `uidmap`), what Claude Code needs and the tools a repository's
     own setup commonly runs (`make`, Python's venv, `tar` with `xz`, `zip` and
     `unzip`), from Ubuntu's packages as
     they are when it is built, from
     `installer/workspace/Containerfile`, named after that file's
     content, so that a changed file makes a new image and workspaces made
     before keep theirs (see [Git](git.md#images)), until they are rebuilt; rcman
     removes an older one once nothing is made from it (see
     [Restoring](devcontainer.md#restoring)). A repository's devcontainer.json can name another
     image, which rcman pulls when a workspace is made (see
     [devcontainer.json support](devcontainer.md));
   - the builder image, from `installer/builder`: the Dev Containers CLI and a
     Podman of its own, which rcman runs for one build of a repository's
     devcontainer.json at a time and removes after it. The CLI is installed from a
     lock file that pins it, and the image is named after the content of the
     folder, so that a change makes a new image. Older ones are removed at the
     end, once rcman has restarted with the new one's name, and only those
     nothing runs from. It is built as the default image is, with the network;
   - the Claude Code store, `/var/lib/rcman/claude`: the version rcman is
     tested with (`helpers/claude-version`), downloaded and checked against its
     release's checksum (see [Claude Code versions](claude-version.md)). The store
     belongs to rcman, which writes it when an administrator rolls out another
     version, and every workspace has it, read-only. The installer checks the
     download in a folder of its own, outside the store, and has `rcman` copy
     the checked file in and set the links. It sets the
     version `current` names only when there is none, so running it again never
     changes the approved version.
9. **Writes the configuration** in `/etc/rcman` and the two systemd services,
   `rcman` and `rcman-oauth2-proxy`; and, when upgrading from the web interface
   is on, what it needs (see
   [Upgrading from the web interface](#upgrading-from-the-web-interface)), or,
   when it is off, removes them.
10. **Puts `rcman-admin` on root's path**: `/usr/local/sbin/rcman-admin` is a
   link to the current release's, so `sudo rcman-admin` finds it. If something
   else is already there, the installer stops rather than replace it.
11. **Adds the first administrator** with `rcman-admin`, unless they are there.
12. **Starts the services**, restarting them when anything changed.
13. **Checks** that both services answer — rcman on its socket, asked as `rcman`,
    and oauth2-proxy on its local port — and waits for them to answer. It then
    removes the older builder images, and prints the Caddy configuration and what
    to do about Caddy, if anything.

When something it checks is not right, or a download fails, the installer says
what on standard error and stops with a failure. Every step leaves things as they
are when they are already right, so mending the cause and running it again
carries on, and running it again for the same commit changes nothing.

## What a workspace can reach

A workspace has the network, as Claude Code and git need: it reaches the
internet, and nothing reaches it, since it publishes no port, but the ports its
devcontainer.json forwards, which only its owner's browser reaches, through rcman, while an
administrator has turned forwarding on (see [Forwarded ports](#forwarded-ports)). It cannot reach
another workspace, nor anything on this machine's loopback addresses, such as
oauth2-proxy. It can reach **anything this machine serves on its other
addresses**, and anything on the networks this machine is on, as rcman's own
account could. If that matters, keep such services on loopback, or limit what
rcman's account (`rcman`) may connect to with the machine's firewall. The
builder of a repository's devcontainer.json has the same network as a
workspace, and so the same reach, and it runs the repository's own Dockerfile
steps and feature scripts. A container started in a workspace shares the
workspace's network, and so its reach.

So that a container started in a workspace can have a `/proc` of its own, the
workspace's `/proc` is not covered where Podman would otherwise cover it. The kernel
still keeps its own files from a workspace: it cannot read the machine's memory
(`/proc/kcore`) or its timers (`/proc/timer_list`), trigger a system request
(`/proc/sysrq-trigger`) or change the machine's settings (`/proc/sys/kernel`), only
those of its own namespaces. It can read what the kernel shows any user there: the
header of `/proc/latency_stats`, its own keys in `/proc/keys`, and the descriptions
of the machine's hardware in `/proc/acpi`, `/proc/bus`, `/proc/irq` and `/proc/scsi`.

## Podman's volumes are rcman's

rcman keeps each workspace, and each person's Claude sign-in, in volumes of its
own account's Podman. A person's sign-in, with their git and gh configuration,
is a volume, which rcman makes for everyone let in at the latest when it starts.
No container mounts it by name, so to Podman it looks unused: **never prune volumes as `rcman`** (`podman
volume prune`, or `podman system prune --volumes`), or everyone is signed out of
Claude. rcman removes a person's volumes itself when they are removed. When it
starts and finds a container of a workspace it has no record of, it removes the container
and keeps the volume, with whatever work is in it, for you to decide about; and
when the table of workspaces is empty while Podman holds some, it removes
nothing at all and writes an error to the [journal](journal.md), so that a
database that was lost or replaced does not cost anyone their files.

Each workspace container also has a volume of its own at `/tmp`, which Podman makes with the
container and names with a long hex string in `podman volume ls`, with no label. It goes when its
container is removed, and while the container exists, running or stopped, Podman counts it as
used, so a prune does not take it; it holds only what the workspace's programs left in `/tmp`.

To look at rcman's Podman, run it as `rcman` from a root shell, with the
environment the installer runs its Podman commands with:

```
runuser -u rcman -- env -i PATH=/usr/bin:/bin HOME=/var/lib/rcman \
  XDG_RUNTIME_DIR=/run/user/$(id -u rcman) podman ps --all
```

`podman volume ls`, `podman images` and `podman info` are asked the same way.

## Caddy

rcman listens on no network port. Its listeners are two unix sockets in
`/run/rcman`, a directory only the group `rcman-web` may enter, and Caddy's account is that
group's only member: `/run/rcman/rcman.sock`, rcman itself, and `/run/rcman/forward.sock`, its
forwarding server (see [Forwarded ports](#forwarded-ports)). rcman believes the address
Caddy passes it, so this is what keeps anyone else on the machine from
passing one. The health check at the end of every run asks both.

The installer prints the site block for your Caddyfile and keeps it in
`/etc/rcman/Caddyfile.snippet`. Either `import` that file in your Caddyfile, so
it is always the current one, or copy the block in. It sends oauth2-proxy's
own pages to oauth2-proxy, asks oauth2-proxy whether each other request is
signed in, sends people who are not to the Google sign-in, and passes the
signed-in address to rcman through the socket. It also removes any address a
browser sends itself, so only oauth2-proxy can tell rcman who someone is. Two more sites in it
are for forwarded ports: `*.ws.<DOMAIN>`, which passes every request for one to the
forwarding server, and `http://127.0.0.1:<ask port>`, where Caddy asks rcman whether it may get
a certificate (see [Forwarded ports](#forwarded-ports)).

oauth2-proxy's sign-in cookie is `__Host-rcman-auth`, and the cookies it names
after it, its CSRF cookie and the parts of a long session, begin with that name
too ([`installer/templates/oauth2-proxy.cfg`](../installer/templates/oauth2-proxy.cfg)).
A browser keeps a cookie with the `__Host-` prefix only when it is Secure, for
the whole site and for this host name alone, and lets no other host set or
replace it, so a page served on a name under your host name cannot sign a
person in to rcman as someone else.

The site block also has Caddy log every request, the polls included, to
`/var/log/caddy/rcman-access.log`: one JSON line each, with the address, the
browser, the path and the status, and with cookies left out. Caddy rotates the
file itself and deletes a rotated file once there are too many of them or it is
too old; the limits are the `roll_` settings in
[`installer/templates/Caddyfile`](../installer/templates/Caddyfile). Caddy's
account must be able to write to `/var/log/caddy`, as it can when Caddy comes
from its Ubuntu package. Do not check the Caddyfile by running `caddy validate`
as root: it creates the log file as root's, and Caddy then cannot open it
and does not start. Run it as Caddy's account (`sudo -u caddy caddy validate
--config /etc/caddy/Caddyfile`), or give the file back to that account with
`chown` if root already made it.

The installer says when Caddy needs something from you, at the end of its
output:

- **The site block changed** — on the first installation, and whenever an
  upgrade changes it. If your Caddyfile imports the snippet there is nothing
  to change in it; if it holds a copy, copy the block again. Then **restart**
  Caddy: it keeps the site block it started with until then.
- **The global options** — with the site block's notice: the block Caddy's global options,
  at the top of your Caddyfile, must hold once, `on_demand_tls` asking rcman at
  `http://127.0.0.1:<ask port>/.rcman/ask` (see [Forwarded ports](#forwarded-ports)). Paste it
  there once, beside `admin off`; change its port if you change `--ask-port`.
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

### Forwarded ports

A workspace whose devcontainer.json lists a port in `forwardPorts` has it reached by its owner's
browser, and no one else's, at `https://<port>-<workspace id>.ws.<DOMAIN>/`, while an
administrator has turned port forwarding on (see [Port
forwarding](service-settings.md#port-forwarding)), as a link on the workspace's page (see
[Ports](workspaces.md#ports)). Caddy passes every request for such a host to rcman's forwarding
server, which lets it in only with a session of the port's owner, made when they open the
address, and holds every request that is not a top-level navigation to having come from the
port's own pages.

- **Pasted once.** The two sites are in the site block; the global options are yours, and must
  hold the `on_demand_tls` block the installer prints, once, here beside `admin off`:

  ```
  {
  	admin off
  	on_demand_tls {
  		ask http://127.0.0.1:<ask port>/.rcman/ask
  	}
  }
  ```

  Without it, Caddy asks no one: it gets a certificate for any name under `*.ws.<DOMAIN>` that
  anyone's browser or script asks it for, each one published and counted against your
  authority's limits, though the forwarding server still lets no one in to a port that is not
  theirs. The installer reads your Caddyfile, `/etc/caddy/Caddyfile`, as Caddy does
  (`caddy adapt`, as Caddy's account), on every run, and warns while its global options do not
  ask rcman, or when it cannot read them. The global options are Caddy's, not the site's: the
  same `ask` decides for every other site of yours that gets certificates on demand, and rcman
  says no to every name that is not a forwarded port's.

- **Certificates.** Each forwarded port's host gets a certificate of its own, on demand, from
  the authority Caddy uses (Let's Encrypt by default), only once rcman says yes: for a ready
  workspace's listed port whose owner was shown its address, on their own workspace's page, or
  opened it lately; an administrator's views of everyone's workspaces mark nothing. Let's Encrypt
  gives a registered domain a limited number of new certificates a week, shared by every host
  under it, your other sites' among them, so rcman says yes to new hosts at a rate well under
  it (`NEW_HOSTS_BURST` and `NEW_HOSTS_PER_WEEK` in `backend/rcman/forward.py`), and a workspace
  forwards a limited number of ports (`MAX_FORWARDED_PORTS` in `backend/rcman/devcontainer.py`).
  Every certificate is published in the public certificate transparency logs, with its host.
- **The ask** listens on `127.0.0.1` alone and passes only its exact path on, but any local user
  of this machine can query it, and so learn which ports' hosts are wanted now.
- **The sign-in cookie stays rcman's.** A forwarded port's host is under your host name, so its
  app can set ordinary cookies for your domain, but not rcman's sign-in cookie, a `__Host-`
  cookie (see [Caddy](#caddy)), nor the forwarding server's own. It can still set so many
  cookies of its own that the browser drops older ones, rcman's sign-in among them, which
  signs its owner out of rcman.
- **A GET that changes something.** The forwarding server lets in a top-level navigation from
  any page, so an app whose `GET` requests change something can be made to change it by any page
  its owner visits while their session lasts, as with any site.

Its log is Caddy's, `/var/log/caddy/rcman-forward-access.log`, rotated as rcman's own, with
cookies, referrers, `Location`s and query strings left out.

## Upgrading and rolling back

To upgrade, run `sudo ./install.sh` again from the dedicated clone: it pulls
`main` and installs it; where you have turned it on, an administrator can do the
same from rcman's web interface (see
[Upgrading from the web interface](#upgrading-from-the-web-interface)). rcman restarts, which takes a moment; the workspaces and
their servers keep running, since they live in rcman's Podman and not in
`rcman.service`. Then do what the installer says about Caddy, as described under
[Caddy](#caddy). An upgrade that needs a range of subordinate ids rcman's account
does not have yet is the one thing that would stop servers, and the installer
refuses it while a container runs, with the commands to stop them (see step 4
under [What the installer does](#what-the-installer-does)).

An upgrade or a rollback that changes the name of oauth2-proxy's cookie (see
[Caddy](#caddy)) signs everyone out of rcman once: oauth2-proxy reads only the
cookie named in its configuration, so the one their browsers hold under the
other name is ignored, and each person is sent to the Google sign-in again at
their next request. Their workspaces and servers are not affected. After a
rollback to the other name, a browser may still hold a sign-in of that name from
before, which oauth2-proxy reads again, so people who signed out of rcman since
should sign out again.

Run the installer again after upgrading Podman, or Ubuntu's `containers-common`
that holds Podman's seccomp profile: the workspaces' profile is derived from it when
the installer runs, and an upgrade of it leaves the one in the tools as it was.
rcman says so in the [system journal](journal.md) each time it starts while the two
differ; workspaces are made with the profile there until the installer runs again. If
the profile is missing from the tools, no workspace can be made until the installer
runs again.

A workspace keeps the container and the image it was made with: where an upgrade changes how
rcman makes a workspace's container, or the default image, only the workspaces made or rebuilt
after it have the change. One made before keeps what it had; to give it the change, a colleague
rebuilds one cloned from a repository, which makes its container again on the same volume, from the current default
image where its repository has no devcontainer.json (see
[Rebuilding a workspace](devcontainer.md#rebuilding-a-workspace)), or makes a new workspace in
its place. A workspace whose container or image lacks what containers need
there says so when `podman` or `docker` runs in it (see [Containers](workspaces.md#containers)).

A server keeps the environment it was started with. Where an upgrade changes
what rcman starts Claude with, a server that ran across it gets the change once
it is stopped and resumed from its workspace's panel (**Stop**, then
**Resume**), with no restart of its container or of rcman; a terminal gets it at
its next start, or **Reconnect**. Each time rcman starts, once the workspaces
are back, it writes gh's files into the git folder of every person let in, with
the token of those who have one stored (see [Git](git.md#gh)).

To go back to the release before the current one, use `--rollback`:

```
sudo ./install.sh --rollback
```

It makes the previous release current and restarts the services; it neither
updates the clone nor changes any setting. Rolling back again returns to the
newer release. The installer **refuses** a rollback when the database is at a
schema version newer than the previous release reads, or when it cannot tell
what that release reads, since that release might not start; it says so and
changes nothing. Install a newer release instead.

Upgrading rcman, rolling back, and running the installer again never change
which Claude Code version is approved, and restart no running server; see
[Claude Code versions](claude-version.md).

## Upgrading from the web interface

Administrators can upgrade rcman from its web interface, when you let them. It
is off by default; to turn it on, run the installer with `--web-upgrade`:

```
sudo ./install.sh --web-upgrade yes
```

and `--web-upgrade no` turns it off again, which removes what it added. The
answer is saved like the others.

**What it risks.** An upgrade from the web interface runs the clone's
`install.sh` as root, with no one there to look at what it pulled. So, while it
is on:

- whoever can push to `main` of the clone's origin can run code as root on this
  machine, through `install.sh`, through `installer/` and through `uv sync`,
  which builds the project as root;
- a GitHub token stored in rcman that can push to that repository is a root
  credential, whoever it belongs to;
- the clone owner's account is effectively root without a password: whatever
  it changes in the clone runs as root at the next upgrade.

Turn it on only where you accept that.

**What is checked.** When it is turned on, and on every upgrade before anything
of the clone runs, the installer refuses:

- a clone owned by root, by an account below `UID_MIN` of `/etc/login.defs`, or
  by `rcman`, `rcman-proxy` or Caddy's account; the owner is the account that
  owns the clone's folder, and must be you;
- a clone without `.git` as a folder, `install.sh`, `installer/` or
  `helpers/rules.py`;
- anything that anyone other than root and the owner can write, through its
  owner, its mode or an ACL: every file and folder in the clone, `.git` and the
  project `uv sync` builds among them, and every folder above the clone. Only
  `frontend/node_modules` and `frontend/dist`, which the owner's npm makes and
  uses, are left out;
- a link in `.git`, `installer/` or `helpers/`, which root reads from the clone,
  a link where git tracks a file or a folder, which a release is built from, and
  a folder above the clone that is a link. Other links, such as those `make setup`
  leaves in `.venv`, are left alone: root never follows them;
- a clone whose path holds characters other than letters, digits and
  `. _ / + -`.

When it is turned on it also asks the clone's origin for `main`
(`git ls-remote origin main`) as the owner, with an empty environment, no
terminal and git asking nothing, as an upgrade will pull it, and refuses if that
fails: give the owner credentials that need no answer, such as a credential
helper or an SSH key without a passphrase. No token stored in rcman is used. Run
by you from a terminal, `install.sh` lets git ask you for a password or a
passphrase; an upgrade from the web interface has no one to ask. Turning it off
is done from a shell: an upgrade from the web interface refuses to remove the
service it runs in.

**Upgrading.** In rcman, in the **Administration** group of the sidebar, the
**rcman release** page, headed Upgrade rcman, shows the release rcman runs and how
the last upgrade went. With the option on, its button **Upgrade rcman**, beside the
heading, asks first, then upgrades rcman to
`main` of its clone, as `sudo ./install.sh` from the clone would. While it runs:

- workspaces and their Claude servers keep running;
- every open **Terminal** tab closes as rcman restarts; reconnect once it is back;
- a git action or a Claude sign-in in progress is cut off; do it again afterwards;
- no workspace is made, cloned or rebuilt and no Claude Code version is rolled out,
  and the pages that offer them say why.

rcman does not take the request while a workspace is being made or rebuilt (a workspace a
rebuild could not put back does not count: the upgrade's restart puts it back), a Claude Code
version is being rolled out or what one left is being cleared away, rcman is
clearing away images no workspace uses, or nothing watches the request file;
the page says which. The page follows the upgrade
through rcman's restart, and once it is done offers **Reload page**, which loads
the new release's page. If it fails, the page shows the last line of what went
wrong; nothing is rolled back for you: roll back from a shell, as
[Upgrading and rolling back](#upgrading-and-rolling-back) says. The audit trail
records who asked (`rcman.upgrade`, see [Audit trail](audit.md)).

**How it works.** rcman writes a request file in its home,
`/var/lib/rcman/upgrade.request`. `rcman-upgrade.path` watches it and starts
`rcman-upgrade.service`, which runs root's copy of `installer/upgrade.py`,
`/opt/rcman/bin/rcman-upgrade`. It checks the clone, runs `install.sh` with a
fixed environment and no terminal, and writes what happened to
`/opt/rcman/upgrade/status.json`, which rcman reads. What `install.sh` prints goes to
the system journal (`journalctl -u rcman-upgrade`). An upgrade that fails is
not rolled back: roll back from a shell, as
[Upgrading and rolling back](#upgrading-and-rolling-back) says.

## Removing rcman

The installer has no uninstall. To take rcman off a machine, do the following as
root, in this order. Caddy's own configuration and the packages other software
uses are yours to change; the steps say where. The only accounts removed are
`rcman` and `rcman-proxy` and the group `rcman-web`: none of them is yours, and
nothing here touches your own account, Docker, Caddy's other sites or Caddy's
account itself. The Google OAuth client is Google's: delete it in the Google
Cloud console when you have no use for it.

1. **Stop the services**: `systemctl disable --now rcman rcman-oauth2-proxy`,
   and, where upgrading from the web interface is on,
   `systemctl disable --now rcman-upgrade.path rcman-upgrade.service`.
   Workspaces run in rcman's own Podman, outside these services, so they keep
   running; step 3 ends them.
2. **Take rcman out of Caddy**, or keep Caddy's `import` from dangling. Remove
   the `import` of `/etc/rcman/Caddyfile.snippet` from your Caddyfile, or the
   copy of the site block, and leave Caddy running for now: restart Caddy after
   step 6, which removes the group, since Caddy keeps the groups it started
   with. If you will install rcman again, keep the `import` instead, and keep
   the snippet, or an empty file in its place, at that path from the wipe to the
   reinstall: Caddy does not load a Caddyfile whose `import` names a file that
   is not there, and step 7 removes `/etc/rcman`, so make the empty file again
   after it (`mkdir -p /etc/rcman`, then `touch /etc/rcman/Caddyfile.snippet`).
   Never check the Caddyfile with `caddy validate` as root (see
   [Caddy](#caddy)). Caddy's log of requests, `/var/log/caddy/rcman-access.log`
   and its rotated files, is Caddy's: delete them if you want them gone.
3. **Remove every workspace, image and volume.** Everything rcman's Podman holds
   is in rcman's home, `/var/lib/rcman`: the workspaces' containers, the volumes
   of their files and of each person's Claude sign-in, and the images (the
   default workspace image, the builder and those built from repositories). Ask
   Podman to remove them while rcman's user manager still runs:

   ```
   runuser -u rcman -- env -i PATH=/usr/bin:/bin HOME=/var/lib/rcman \
     XDG_RUNTIME_DIR=/run/user/$(id -u rcman) podman system reset --force
   ```

4. **End rcman's user manager**, which runs from boot because of lingering, with
   its `podman.socket`: `loginctl disable-linger rcman` removes the entry
   `/var/lib/systemd/linger/rcman`, and `loginctl terminate-user rcman` ends
   what runs for it.
5. **Remove the files systemd and the system read**:
   - the units `/etc/systemd/system/rcman.service`,
     `/etc/systemd/system/rcman-oauth2-proxy.service` and, where upgrading from
     the web interface is on, `/etc/systemd/system/rcman-upgrade.path` and
     `/etc/systemd/system/rcman-upgrade.service`, then
     `systemctl daemon-reload`;
   - the drop-in that lifts the ceiling on processes of rcman's user slice,
     `/etc/systemd/system/user-<uid>.slice.d/rcman.conf` (`<uid>` is rcman's
     user id) with its folder; the user manager's own,
     `.config/systemd/user.conf.d/rcman.conf` in rcman's home, goes with the
     home in step 7;
   - `/etc/tmpfiles.d/rcman.conf`, and the socket's folder `/run/rcman` with
     rcman's socket `/run/rcman/rcman.sock` and its forwarding server's
     `/run/rcman/forward.sock`;
   - the command `/usr/local/sbin/rcman-admin`, a link.
6. **Remove the accounts**: `userdel rcman-proxy`, then `userdel rcman` (the
   proxy's account is a member of rcman's group, so the group stays until the
   proxy's account is gone), then `groupdel rcman-web`, whose only member was
   Caddy's account. The accounts' ranges of subordinate ids in `/etc/subuid` and
   `/etc/subgid` go with `userdel`; step 9 checks. Then **restart** Caddy, and
   only now: a reload is not enough, and a Caddy restarted before `groupdel`
   still holds the group's old id.
7. **Remove rcman's folders**: `/opt/rcman` (the releases, the links
   `/opt/rcman/current` and `previous`, the toolchain of Node.js, uv and
   oauth2-proxy in `/opt/rcman/toolchain`, the tools every workspace mounts
   in `/opt/rcman/tools`, the installer's lock `/opt/rcman/.install.lock`, and,
   from upgrading from the web interface, the wrapper `/opt/rcman/bin/rcman-upgrade`
   and the folder `/opt/rcman/upgrade` with its `/opt/rcman/upgrade/status.json`),
   `/etc/rcman` and `/var/lib/rcman`.
   `/etc/rcman/install.env` holds the Google OAuth client and the other answers
   the installer saved: keep it aside to install again with the same answers.
   `/var/lib/rcman` holds the database, the list of allowed addresses, the
   Claude Code store `/var/lib/rcman/claude`, the request file of upgrading
   from the web interface `/var/lib/rcman/upgrade.request`, and rcman's home
   with the storage of its Podman.
8. **Leave the packages.** The installer installed the packages Podman and the
   workspaces need, those that were missing: `podman`, `crun`, `uidmap`,
   `catatonit`, `slirp4netns`, `dbus-user-session`, `busybox-static` and
   `ncurses-base`.
   Other software may use them; remove one with `apt` only when nothing does.
9. **Check that nothing is left**, with commands that only read:

   ```
   getent passwd rcman rcman-proxy
   getent group rcman rcman-web
   grep rcman /etc/subuid /etc/subgid
   ls -d /opt/rcman /etc/rcman /var/lib/rcman /run/rcman /usr/local/sbin/rcman-admin
   ls /etc/systemd/system | grep rcman
   ls /etc/systemd/system/user-*.slice.d/rcman.conf
   ls /etc/tmpfiles.d | grep rcman
   ls /var/lib/systemd/linger/rcman
   systemctl list-unit-files 'rcman*'
   ```

   Each prints nothing, or says there is no such file. Anything it prints is
   removed by hand.

## Afterwards

Once the installer has said what to do about Caddy and you have done it, check
that nothing failed to start — `systemctl --failed` lists nothing — and open
`https://<your host name>/`: Google asks the first administrator to sign in, with
the address given as `RCMAN_INSTALL_ADMIN_EMAIL`, and rcman opens on the
Workspaces page. What rcman writes to the system journal, and how to read it, is
in [The system journal](journal.md).

Add people with `sudo rcman-admin add` or on the **People** page, in the
**Administration** group of rcman's sidebar — see
[Adding and removing people](users.md). rcman's own settings, which
administrators set, are described in [Configuration](configuration.md).
