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
| The local port rcman listens on | `--app-port` | `RCMAN_INSTALL_APP_PORT` |
| The local port oauth2-proxy listens on | `--proxy-port` | `RCMAN_INSTALL_PROXY_PORT` |

An option wins over a variable, a variable over the saved answer, and the
saved answer over asking. The ports have defaults and are not asked for.
Without a terminal the installer does not ask; it names what is missing and
stops. The secret oauth2-proxy signs its cookies with is generated on the first
run and kept.

## What the installer does

1. **Brings the clone up to date** with `main`, as you, so your git
   credentials are used. It refuses when the clone has uncommitted changes, is
   on another branch, or cannot be fast-forwarded. If the update changed
   `install.sh`, it starts again with the new version.
2. **Creates the accounts it needs**: the `rcusers` group every person's Linux
   account belongs to, a system user `rcman` for rcman itself (its data lives
   in `/var/lib/rcman`), and a system user `rcman-proxy` for oauth2-proxy.
3. **Builds a release** of the clone's commit under `/opt/rcman/releases`: the
   files rcman runs, the built web interface, and its Python environment — and
   nothing used only for development. Everything in it belongs to root.
4. **Makes it current** — `/opt/rcman/current` — and keeps the release before
   it as `/opt/rcman/previous`; older ones are removed.
5. **Writes the configuration** in `/etc/rcman`, the sudoers rules in
   `/etc/sudoers.d/rcman` (checked with `visudo` first), and the two systemd
   services, `rcman` and `rcman-oauth2-proxy`.
6. **Starts the services**, restarting them when anything changed.
7. **Adds the first administrator**, as `rcman-user add --admin --adopt` would.
8. **Checks** that both services answer, and prints the Caddy configuration.

Running it again for the same commit changes nothing.

## Caddy

The installer prints the site block to add to your Caddyfile and keeps it in
`/etc/rcman/Caddyfile.snippet`. It sends oauth2-proxy's own pages to
oauth2-proxy, asks oauth2-proxy whether each other request is signed in, sends
people who are not to the Google sign-in, and passes the signed-in address to
rcman. It also removes any address a browser sends itself, so only
oauth2-proxy can tell rcman who someone is. Reload Caddy after adding it.

## Upgrading and rolling back

To upgrade, run `sudo ./install.sh` again: it pulls `main` and installs it.
To go back to the release before the current one, use `--rollback`:

```
sudo ./install.sh --rollback
```

Rolling back again returns to the newer release.

## Afterwards

Add people with `sudo rcman-user add` or on the **People** page — see
[Adding and removing people](users.md). Settings are described in
[Configuration](configuration.md).
