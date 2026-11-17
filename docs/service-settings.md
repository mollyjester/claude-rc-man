# Service settings

Administrators change the settings of rcman itself, which are the same for everyone, on
the **Service settings** page, under **Administration**. It is not a person's own
[Settings](settings.md) page.

The page has a card for each part, **Monitoring**, **Port forwarding**, **Files**
and **Microsoft sign-in**. Each card
saves on its own: **Save** in a card saves only what that card holds, the card says
**Saving…** while it does, and then that its settings are saved. A value rcman refuses
is shown under its field, with what to change, and nothing in that card is saved. The
settings are kept in rcman's database, not in its [configuration](configuration.md),
and apply at once, for every administrator.

## Monitoring

How often the two rounds of the [Monitor](monitor.md) page run, in **Usage interval
(seconds)** and **Disk interval (seconds)**, and which of **Host**, **People**,
**Containers** and **Storage** are collected. The disk round runs only while **People**,
**Containers** or **Storage** is collected. A save applies at once: a scope switched off is hidden
on the Monitor page at once, and a new interval counts from that round's last run, so a
round whose new interval has passed already runs at once. Switching a scope off and on
does not move a round. With every scope off, nothing is collected. The defaults, and the
shortest and longest interval each round takes, are in `backend/rcman/monitor.py`.
Saving them is recorded in the [audit trail](audit.md) as `monitor.settings`.

## Port forwarding

How the ports a workspace's `devcontainer.json` lists (see
[devcontainer.json support](devcontainer.md)) are reached: while forwarding is on, each such
port of a ready workspace is reached by its owner's browser, and no one else's, at an address
of its own, through rcman, which the workspace's page shows its owner (see
[Ports](workspaces.md#ports) and [Forwarded ports](installation.md#forwarded-ports)). Turning it
off ends every session on a forwarded port at once.

- **Forward ports**, whether ports are to be forwarded at all; off until an administrator
  turns it on;
- **Idle timeout (minutes)**, how long a session on a forwarded port is to last with
  nothing going on;
- **Absolute lifetime (hours)**, how long such a session is to last in all, busy or not.

Each timeout is a whole number in its label's unit, and the idle timeout must be shorter
than the absolute lifetime. The defaults, and the shortest and longest each takes, are in
`backend/rcman/forwarding_settings.py`. Saving them is recorded in the
[audit trail](audit.md) as `forwarding.settings`.

## Files

What may be downloaded from the files of a workspace (see [Files](workspaces.md#files)):
**Largest folder download (MB)**, the largest folder that downloads as a zip, in megabytes of a
million bytes, a whole number, where 0 means no limit. A single file's download is never
limited. A folder over the limit is refused before anything downloads, with its size and the
limit. The default, and the largest value it
takes, are in `backend/rcman/files_settings.py`. Saving it is recorded in the
[audit trail](audit.md) as `files.settings`.

## Microsoft sign-in

Whether people may sign in to rcman with their Microsoft work account as well as with Google.
Google sign-in is always on. Microsoft sign-in lets in only the members of one Microsoft Entra
tenant: an account of another tenant, and a guest account of this one, is refused. A person
signs in with the address rcman lists them by (see [People](users.md)): a Microsoft account is
matched by its user principal name (UPN), never by the email address set on it.

The card follows the setup of an app registration in Microsoft Entra:

- **Redirect URI**, rcman's own address for Microsoft to send people back to, which **Copy**
  copies. Give it to the app registration before anything else.
- **Application (client) ID** and **Directory (tenant) ID**, from the app registration's
  Overview, each as Microsoft Entra shows it.
- **Client secret**, a secret's Value, not its Secret ID. rcman keeps it encrypted, and it is
  never shown again: the card says only whether one is stored. Leave the field empty to keep
  the one stored; enter a new one to replace it.
- **Allow Microsoft sign-in**, last, since it is refused until both IDs and a client secret are
  there.

To set up the app registration, in Microsoft Entra:

1. Register an application for accounts in this organizational directory only (single
   tenant), with the card's **Redirect URI** as a Web redirect URI.
2. Under Token configuration, add the optional claim `acct` to the ID token. Without it,
   every Microsoft sign-in is refused, since rcman cannot tell a member from a guest.
3. Under API permissions, keep Microsoft Graph's delegated `User.Read`, with an administrator's
   consent.
4. Under Certificates & secrets, add a client secret and copy its Value into the card.
5. On the application's enterprise application, turn on Assignment required and assign the
   people who may use rcman, if only they are to get as far as rcman. rcman still lets in only
   the people it lists.

A client secret expires on the date Microsoft Entra gives it. Before then, add a new one in
Microsoft Entra, enter it in the card and save; Microsoft sign-in stops working once the stored
one has expired. Moving rcman's database to an installation with another secret key, or replacing the key
(see [Configuration](configuration.md)), makes the stored secret unreadable: Microsoft sign-in
is refused until the secret is entered again.

A Microsoft sign-in lasts as long as a Google one (`SESSION_SECONDS` in
`backend/rcman/microsoft.py`). Turning Microsoft sign-in off, or
changing either ID, signs out everyone signed in with Microsoft at once; removing a person
signs them out as it does a Google one. Without rcman's secret key, which the installer
generates, the card offers no form and says to run the installer again.

What the card holds an administrator to, and the rules a save is refused by, are in
`backend/rcman/microsoft_settings.py`. Saving them is recorded in the
[audit trail](audit.md) as `microsoft.settings`; the record says whether a client secret was
sent, never the secret.

An administrator who points rcman at a Microsoft Entra tenant they control, with a verified
domain, can sign in as anyone rcman lists at that domain's addresses. Changing the settings is
audited, and administrators can already add and remove people.
