# Service settings

Administrators change the settings of rcman itself, which are the same for everyone, on
the **Service settings** page, under **Administration**. It is not a person's own
[Settings](settings.md) page.

The page has a card for each part, **Monitoring** and **Port forwarding**. Each card
saves on its own: **Save** in a card saves only what that card holds, the card says
**Saving…** while it does, and then that its settings are saved. A value rcman refuses
is shown under its field, with what to change, and nothing in that card is saved. The
settings are kept in rcman's database, not in its [configuration](configuration.md),
and apply at once, for every administrator.

## Monitoring

How often the two rounds of the [Monitor](monitor.md) page run, in **Usage interval
(seconds)** and **Disk interval (seconds)**, and which of **Host**, **People** and
**Containers** are collected. A save applies at once: a scope switched off is hidden
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
