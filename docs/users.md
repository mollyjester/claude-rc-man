# Adding and removing people

Each person who uses rcman signs in with their Google address, and works in
workspaces of their own (see [Workspaces](workspaces.md)). rcman keeps the list
of people itself; only the addresses on it may sign in, and the list of
addresses oauth2-proxy lets through is written from it on every change.

Administrators add and remove people on the **People** page in rcman, under
**Admin** in the navigation, or with `rcman-admin` as root over SSH. Both do the
same thing; `rcman-admin` runs as rcman's own account whoever starts it.

## Adding someone

On the **People** page, give their Google address, and tick **Administrator**
to make them one. Or:

```
sudo rcman-admin add <google-email> [--admin]
```

They can sign in at once. Each person gets a range of user ids of their own,
which every workspace of theirs runs in, so no two people's workspaces share
one. rcman has room for as many people as it has ranges; past that, adding
someone is refused.

## Removing someone

On the **People** page, **Remove** asks first, then removes them. Or:

```
sudo rcman-admin delete <google-email>
```

From the moment the removal starts, they cannot sign in. Every workspace of
theirs is deleted, with everything in it and its server, and their settings,
their GitHub token and their Claude sign-in with them, and so are the limits on
them. Anyone may be removed — yourself and the last administrator too: root can
always add an administrator again with `rcman-admin`.

## Seeing who is there

The **People** page lists everyone, with their role, how many workspaces they
have and their limits (**Memory**, **CPU** and **Processes**, **No limit**
where there is none). So does:

```
sudo rcman-admin list
```

## Limits

Administrators can cap how much memory, CPU and how many processes a person's
workspaces may use together, and each workspace on its own. There are no limits
by default, and nobody sees their own limits.

On the **People** page, **Limits** on a person's row opens a dialog with a
form for the person and one for each of their workspaces, with what each uses
now beside it. Fill in **Memory**, **CPU** and **Processes** and press
**Save**: each form is saved on its own, a field left empty is **No limit**, and
a line under each field says what it takes. A refusal is shown under the field
it is about. The dialog's **How limits work** holds the rest of what this
section says, folded away. Or, as root over SSH:

```
sudo rcman-admin limits <google-email>
sudo rcman-admin limits <google-email> [--workspace NAME] [--memory 4G] [--cpu 1.5] [--pids 500]
```

With no option it prints the limits and what is in use. Each option takes a
value, or `none` for no limit; an option left out leaves that limit as it is.
`rcman-admin limits --help` describes the options.

- Units. Memory is a whole number followed by `M` (mebibytes) or `G`
  (gibibytes), such as `512M` or `4G`. CPU is a number of cores, with up to two
  decimals, such as `1.5`. Processes is a whole number, in which threads count
  too. A value too small to be useful is refused with the smallest allowed.
- Together. A workspace is also held to its person's limits: all of a
  person's workspaces together stay within them, and the kernel enforces
  whichever is stricter. The builder of a repository's `devcontainer.json`
  counts against the limits of the person who asked for the build.
- Memory is a hard limit. When a group goes over its memory limit, the
  kernel ends the largest process in it, and that can be a process of a sibling
  workspace, a Claude session included. Give each workspace a smaller memory
  limit than its person's, so that the one that grows is the one that is ended.
- Lowering a limit never ends running work. A memory or process limit
  that is lower than the one the workspace or person has now, and lower than what
  is in use now, is refused, and the refusal names what is in use. A limit equal
  to the one there is, or higher, is never refused, nor is removing one, nor a
  change to CPU. The memory in use is memory plus swap and includes the kernel's
  cache, so a limit just above what the processes really use may be refused too.
  When rcman cannot tell what is in use, lowering a limit is refused with a
  plain message. Every save sends all three fields, so a save that changes only
  CPU is refused if the memory or process limit it carries is lower than what the
  workspace has now and below what is in use, which can follow a restart where a
  limit could not be applied. There is no way to force it: to relieve a runaway,
  stop the servers first.
- What is limited. Workspaces and the builder. Signing in, signing out and
  the housekeeping of rcman, and the check of a Claude Code version, are not, so
  a full limit never stops anyone signing out. Disk and I/O cannot be limited.
  The network helper of each workspace is outside every limit. Where no limit
  is set, a runaway program is bounded by the kernel alone.
- They last. The limits are kept in rcman's database and survive a restart
  of rcman or of the machine: rcman applies them again before it starts the
  workspaces, and judges them as it judges a change: a memory or process limit
  that is lower than what a group has now and than what it uses is not applied,
  so as not to end running work; the group keeps what it has, rcman writes that to
  the [journal](journal.md), and the limit is applied the next time rcman starts
  when it is not below the use. Deleting a workspace or a person removes theirs.

Every change is in the [audit trail](audit.md), with the values set.

`rcman-admin --help` describes every command.
