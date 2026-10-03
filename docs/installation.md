# Installing rcman

rcman runs on one Ubuntu machine. Caddy, already running there, serves it on a
public host name with HTTPS; Google sign-in goes through oauth2-proxy, which
the installer sets up. The installer is `install.sh` in a clone of this
repository, and the same command upgrades an installation.

## Before you start

- **A Google OAuth client.** Creating one is free; the steps are below.
- **A host name** pointing at the machine, served by Caddy.
- **A clone** of this repository on `main`, owned by your own account, with
  git able to pull from GitHub as you (for a private repository, for example
  through `gh auth login`).
- **sudo** for your account.
- **git** from the system's packages, recent enough for what rcman runs. The
  installer checks it and names the version it needs when it is older
  (`GIT_MIN_VERSION` in `helpers/git_run.py`).

Everything else — the Node.js that builds the web interface, uv for the
Python environment, oauth2-proxy, and Claude Code for each person — the
installer downloads itself and checks against its published checksum.

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
| Their Linux user; you, unless given | `--admin-linux` | `RCMAN_INSTALL_ADMIN_LINUX` |
| The local port oauth2-proxy listens on | `--proxy-port` | `RCMAN_INSTALL_PROXY_PORT` |
| The Linux account Caddy runs as | `--caddy-user` | `RCMAN_INSTALL_CADDY_USER` |

An option wins over a variable, a variable over the saved answer, and the
saved answer over asking. The port and Caddy's account have defaults and are
not asked for; the account must exist. Anything else saved in
`/etc/rcman/install.env` is dropped from it.
Without a terminal the installer does not ask; it names what is missing and
stops. The secret oauth2-proxy signs its cookies with is generated on the first
run and kept.

## What the installer does

1. **Brings the clone up to date** with `main`, as you, so your git
   credentials are used. It refuses when the clone has uncommitted changes, is
   on another branch, or cannot be fast-forwarded. If the update changed
   `install.sh`, it starts again with the new version.
2. **Checks git** on the system path is new enough, and stops there when it
   is not.
3. **Creates the accounts it needs**: the `rcusers` group every person's Linux
   account belongs to, a system user `rcman` for rcman itself (its data lives
   in `/var/lib/rcman`), a system user `rcman-proxy` for oauth2-proxy, and a
   group `rcman-web` whose only member is Caddy's account; anyone else in it is
   taken out.
4. **Makes the directory of rcman's socket**, `/run/rcman`: it belongs to
   `rcman` and the group `rcman-web`, and no one else may enter it.
   `/etc/tmpfiles.d/rcman.conf` makes it again at every boot.
5. **Builds a release** of the clone's commit under `/opt/rcman/releases`: the
   files rcman runs, the built web interface, and its Python environment — and
   nothing used only for development. Everything in it belongs to root.
6. **Makes it current** — `/opt/rcman/current` — and keeps the release before
   it as `/opt/rcman/previous`; older ones are removed.
7. **Writes the configuration** in `/etc/rcman`, the sudoers rules in
   `/etc/sudoers.d/rcman` (checked with `visudo` first), and the two systemd
   services, `rcman` and `rcman-oauth2-proxy`.
8. **Starts the services**, restarting them when anything changed.
9. **Adds the first administrator**, as `rcman-user add --admin --adopt` would.
10. **Checks** that both services answer, and prints the Caddy configuration
   and what to do about Caddy, if anything.

Running it again for the same commit changes nothing.

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
at `127.0.0.1:2019` or `[::1]:2019`.

## Upgrading and rolling back

To upgrade, run `sudo ./install.sh` again: it pulls `main` and installs it.
Then do what it says about Caddy, as described under [Caddy](#caddy).

To go back to the release before the current one, use `--rollback`:

```
sudo ./install.sh --rollback
```

Rolling back again returns to the newer release.

## Afterwards

Add people with `sudo rcman-user add` or on the **People** page, under
**Admin** in rcman's navigation — see [Adding and removing people](users.md).
rcman's own settings, which administrators set, are described in
[Configuration](configuration.md).
