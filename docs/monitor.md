# Monitor

Administrators can see how much CPU, memory, processes and disk rcman's host, each
person and each workspace container use now, and how much of the disk is rcman's own,
on the **Monitor** page, under **Administration**. rcman collects these figures in the
background, in two rounds, each on an interval of its own, and the page shows the
latest it has, reading them again every few seconds while it is open. There is no
history and no chart: each figure is the one the last round read.

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
  Folders on one filesystem share one line. Each line is the whole filesystem:
  what everything on it uses, the rest of the host's files among it, not only
  rcman's. rcman's own share is under **Storage**.
- **Storage**: what of the disk is rcman's own, by part, and their total:
  - **Images**: the images rcman's Podman holds, each layer counted once however
    many images share it. It counts layers that belong to no image too, such as
    those a build or a load that was killed leaves, which nothing removes.
  - **Workspace volumes**: each workspace's volume and the volume at its `/tmp`.
  - **Other volumes**: every other volume of rcman's Podman, such as the one that
    holds a person's Claude sign-in and git folder.
  - **Claude Code versions**: the Claude Code versions rcman keeps for workspaces
    to run (see [Claude Code versions](claude-version.md#the-store-and-the-approved-version)).

  A part rcman has not read yet is **Not known**, and so is the total then.
- **People**: a table of each person, by their address, with how many workspaces
  they have and what those use together: **CPU**, **Memory with swap**,
  **Processes** and **Disk**. A person's CPU, memory and processes are those of
  their slice (see [Limits](users.md#limits)), which holds their workspaces and any
  build of a repository's `devcontainer.json` they asked for.
- **Containers**: a table of each workspace, by its name, its owner and its state,
  with what its container uses, in the same columns. A workspace its person or an
  administrator stopped is shown as Stopped (see
  [Stopping and starting a workspace](workspaces.md#stopping-and-starting-a-workspace)).

On a phone each table is a list, a row for each person or workspace.

A part whose scope is switched off says **Not collected**, with a link to the
**Service settings** page, where it is switched on again.

A workspace's disk is its volume, the volume at its `/tmp` and its container's own
layer, together. A person's disk is the sum over their workspaces; it is not known
when any of their workspaces' is not.

A figure rcman could not read is shown as **Not known**, never as zero. CPU is the
average since the round before, so the first round after rcman starts, or after a
scope is switched on, knows no CPU yet; nor does a round after a workspace started
or stopped.

## The two rounds

- **Usage** reads CPU, memory and processes, of the host from its own files, and of
  people and containers with one request to rcman's user manager, and the host's
  filesystems. It costs little.
- **Disk** asks Podman once for the size of every container and volume, and once
  for which volume is at each workspace's `/tmp`. Podman walks every file of every
  volume to answer, so it costs more the more files there are, and runs less often.
  With **Storage** collected, it also asks Podman once for its summary of the disk
  it uses, the images' size among it, and adds up the files of the Claude Code
  versions; Podman walks every file of every volume for that summary too, so the
  volumes are walked twice a round. It runs only when people, containers or storage
  are collected. Its first run is one disk interval after rcman, as it starts, has
  brought its workspaces back. Each read that fails is tried once more at once, and
  one that worked is not read again; when one fails again, the last figures read
  stay, with the time they were read, and the failure is shown. One volume Podman
  cannot read fails the whole read, as files deleted while it runs can. Each of
  Podman's two reads is given a set time, `DISK_TIMEOUT` in
  `backend/rcman/runtime.py`, and is stopped when it outlasts it, so Podman's reads
  in a round end within twice `DISK_ATTEMPTS` (in `backend/rcman/monitor.py`) times
  that time. The walk of the Claude Code versions has no set time; it stops when
  rcman stops.

A round's figures are all of one run: when part of a usage round fails, such as the
request to the user manager, the host's figures stay as they were too. A failure
is also written to the [system journal](journal.md) when it starts, when its
reason changes and when it ends, not at every run.

## Refresh

**Refresh**, beside the page's title, collects every reading again at once: a
usage round and a disk round, whatever their intervals, each only when something
it collects is switched on. The button says **Refreshing…**, and cannot be pressed,
until both rounds are in, and the page then shows the new figures. It also says
**Refreshing…**, and cannot be pressed, while a timed round runs, since a press
would only join it. A round already
running, or already asked for, from this page or another administrator's, is
joined rather than started again, so there is never a second disk round at once;
an administrator who presses it as each round ends can still run the disk round
back to back, as costly each time as a timed one. A round that fails says so as a
timed round's failure does. Each press is recorded in the
[audit trail](audit.md) as `monitor.refresh`. The next timed round of each is then
due one interval after the refreshed one.

## Its settings

How often each round runs, and which of **Host**, **People**, **Containers** and
**Storage** are collected, are set on the **Service settings** page (see
[Monitoring](service-settings.md#monitoring)).

## What it does not cover

- containers that run inside a workspace, which count in their workspace's CPU,
  memory and processes but not in its disk;
- rcman's other containers: sign-in containers, builders and the check of a
  Claude Code version;
- images, and the volume that holds a person's Claude sign-in and git folder, in a
  person's disk; both are under **Storage**;
- in **Storage**, the workspace containers' own layers, what each container wrote
  outside its volumes, which are in each workspace's **Disk** in the **Containers**
  table instead;
- in **Storage**, rcman's database, its configuration and anything else of rcman's
  outside Podman's storage and the Claude Code versions, and which images, if any,
  could be removed;
- exact sizes: Podman gives sizes rounded, in decimal SI units, and the page rounds
  them further (`formatBytes` in [`frontend/src/format.ts`](../frontend/src/format.ts));
- disk figures newer than the last disk round.
