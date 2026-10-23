# Monitor

Administrators can see how much CPU, memory, processes and disk rcman's host, each
person and each workspace container use now, on the **Monitor** page, under
**Administration**. rcman collects these figures in the background, in two rounds,
each on an interval of its own, and the page shows the latest it has, reading them
again every few seconds while it is open. There is no history and no chart: each
figure is the one the last round read.

## What is shown

Under the page's title, one line says when each round, **Usage** and **Disk**,
last collected, in UTC, and how often it runs; a screen reader announces it when a
round collects again. Under it, a round whose last run failed says so, with what it
failed with, and the figures stay those it last read. When the page cannot reach
rcman, it says so too, and keeps showing what it read before.

- **Host**: the CPU in use, as a percent of one core, beside how many cores there
  are; the memory in use (the total less what is available) and the total; the
  processes running, threads counting; and, under **Filesystems**, each filesystem
  by the path of a folder on it, with what is used, its size and what is free, and
  which of rcman's database, the host's root folder and Podman's storage it holds.
  Folders on one filesystem share one line.
- **People**: a table of each person, by their address, with how many workspaces
  they have and what those use together: **CPU**, **Memory with swap**,
  **Processes** and **Disk**. A person's CPU, memory and processes are those of
  their slice (see [Limits](users.md#limits)), which holds their workspaces and any
  build of a repository's `devcontainer.json` they asked for.
- **Containers**: a table of each workspace, by its name, its owner and its state,
  with what its container uses, in the same columns.

On a phone each table is a list, a row for each person or workspace.

A part whose scope is switched off says **Not collected**, with a link to the
**Monitoring settings**.

A workspace's disk is its volume, the volume at its `/tmp` and its container's own
layer, together. A person's disk is the sum over their workspaces; it is not known
when any of their workspaces' is not.

A figure rcman could not read is shown as **Not known**, never as zero. CPU is the
average since the round before, so the first round after rcman starts, or after a
scope is switched on, knows no CPU yet; nor does a round after a workspace started
or stopped.

## The two rounds

- **Usage** reads CPU, memory and processes, of the host from its own files, and of
  people and containers with one request to rcman's user manager. It costs little.
- **Disk** asks Podman once for the size of every container and volume, and once
  for which volume is at each workspace's `/tmp`. Podman walks every file of every
  volume to answer, so it costs more the more files there are, and runs less often.
  It runs only when people or containers are collected. Its first run is one disk
  interval after rcman, as it starts, has brought its workspaces back. Each of the
  two reads that fails is tried once more at once; when one fails again, the last
  figures read stay, with the time they were read, and the failure is shown. One
  volume Podman cannot read fails the whole read, as files deleted while it runs
  can.

A round's figures are all of one run: when part of a usage round fails, such as the
request to the user manager, the host's figures stay as they were too. A failure
is also written to the [system journal](journal.md) when it starts, when its
reason changes and when it ends, not at every run.

## Monitoring settings

The **Monitoring settings**, a card at the foot of the page, set how often each
round runs, in **Usage interval (seconds)** and **Disk interval (seconds)**, and
which of **Host**, **People** and **Containers** are collected; **Save** saves them
all, and the card says when they are saved. They are kept in rcman's database, not
in its [configuration](configuration.md), and apply at once: a scope switched off is
hidden at once, and a new interval counts from that round's last run, so a round
whose new interval has passed already runs at once. Switching a scope off and on
does not move a round. With every scope off, nothing is collected. The defaults, and
the shortest and longest interval each round takes, are in
`backend/rcman/monitor.py`; a value outside them is refused under its field. Saving
them is recorded in the [audit trail](audit.md) as `monitor.settings`.

## What it does not cover

- containers that run inside a workspace, which count in their workspace's CPU,
  memory and processes but not in its disk;
- rcman's other containers: sign-in containers, builders and the check of a
  Claude Code version;
- images, and the volume that holds a person's Claude sign-in and git folder, in a
  person's disk;
- exact sizes: Podman gives sizes rounded, in decimal SI units, and the page rounds
  them further (`formatBytes` in [`frontend/src/format.ts`](../frontend/src/format.ts));
- disk figures newer than the last disk round.
