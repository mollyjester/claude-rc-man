# Workspaces

A **workspace** is a place of your own on this machine, kept apart from
everyone else's and from rcman itself, where a Claude Code Remote Control
process can run: the workspace's **server**. The conversations a server hosts
— its **sessions** — appear in [claude.ai/code](https://claude.ai/code) and in
the Claude app, where you work with them, and they work on the files in the
workspace.

## The Workspaces page

The Workspaces page lists your workspaces under its heading, and shows the panel
of the workspace you have selected beside the list. **New workspace**, beside
the heading, makes another; while you have none, the list says so and offers
**New workspace** in its place. Each workspace in the list has a trash button of
its own, which deletes that workspace whichever one is selected (see
[Deleting a workspace](#deleting-a-workspace)); with a mouse it shows on the row
you point at, the row you have selected and the row you move to with the
keyboard.

Select a workspace by its name; the selected row is marked with a bar at its
left. A workspace whose server is running, waiting for your answer, has a
problem or is not running carries a dot before its name, whose shape and colour
say which, and pointing at a dot names it. A workspace with no server carries
none, except that one that failed to be made, or whose server rcman could not
read, carries the problem dot; one whose container a rebuild is making again carries none:

| Dot | Shape |
|---|---|
| Has a problem | triangle |
| Needs your answer | ring |
| Running | filled circle |
| Not running | hollow square |

A workspace that is not ready says so beside its name: **Being made**,
**Failed** or **Being deleted**.

To make a workspace, choose **New workspace**, type its name in the dialog that
opens and choose **Create**; **Cancel**, the cross at its top, or Escape closes
the dialog. The new workspace is selected, and is shown as being made until it
is ready; that takes a moment, and longest for your first workspace. Without an
address a workspace is empty. When rcman refuses a name, the dialog stays open,
says why under the field and keeps what you typed; each of your workspaces has a
name of its own.

While an administrator has rcman upgraded from its web interface (see
[Upgrading from the web interface](installation.md#upgrading-from-the-web-interface)),
neither **New workspace** nor **Rebuild** is offered, and the page says why: no workspace is
made, cloned or rebuilt until the upgrade is done, since the restart it makes would cut one
short.
Your workspaces and their servers keep running through it.

To start from a repository on GitHub, give its address in **GitHub address
(optional)**, such as `https://github.com/owner/repository`, and choose
**Create**. rcman clones the repository into the new workspace: all its
branches, with the default one checked out. If you leave the name empty, the
workspace is named after the repository, and numbered (`repository-2`,
`repository-3`) when you already have one of that name. A private repository
needs a GitHub token in [Settings](settings.md); see [Git](git.md). Until the
clone is done the workspace is shown as being made, and its panel lists
**Steps so far**. A clone of a large repository can take a while, and the panel
does not show git's progress while it runs.

If the repository has a `devcontainer.json`, rcman reads it after the clone and
makes the workspace from the image it names, running the commands it lists
before the workspace is ready; the steps say what rcman is doing, and the
workspace is shown as being made until it is done. A repository without one
gets the default image, whose tools are listed in
[the installation guide](installation.md#what-the-installer-does). If the file describes an image to build, with a
Dockerfile or features, rcman builds it first: the steps show the build's
progress, which can take a long time for a big image, and when other builds are
running, a line says that this one waits for them to finish. The build uses a
copy of the repository's files, not the workspace itself, and cannot sign in to
a private registry. What rcman supports and refuses is in
[devcontainer.json support](devcontainer.md). A change to the file afterwards, made in the
workspace or pulled into it, changes nothing by itself: the workspace's **Container** card says
the file has changed, and **Rebuild** applies it (see
[The Container card and rebuilding](#the-container-card-and-rebuilding)).

The workspace you have selected is part of the page's address, so reloading
the page keeps it, and the browser's back button returns to the workspace
selected before.

The panel's head shows the workspace's name and its state, and, for a workspace
cloned from GitHub, the repository, owner first, as a link that opens its page
on GitHub in a new tab, whatever state the workspace is in. The panel of a
workspace that is ready has two tabs, **Overview**, shown first, and
**Terminal** (see [The terminal](#the-terminal)); the arrow keys move between
them. The tab you chose is part of the page's address, as the workspace is.
**Overview** has three cards: **Claude server**, which shows the workspace's
server, or **Start** when it has none, and says why instead if rcman could not
read the server, or that its container is being made again during a rebuild;
below it **Container**, which says what the workspace's container is made from and
offers **Rebuild** (see [The Container card and rebuilding](#the-container-card-and-rebuilding));
and below that **Changes**, for the git repository in the
workspace (see [Git](git.md)). A workspace that failed to be made says what went
wrong in a sentence, with the words rcman got behind **Details**, and **Steps so
far** beneath; besides its trash button in the list, it offers **Rebuild** beside the
reason where it can be rebuilt, as the next part says. When the
repository's `devcontainer.json` is the cause, the reason names the setting to
change, or the image rcman could not pull or use, or the command that failed or
took too long, with what it printed last in the steps. A build that failed says
why in plain words, with the end of what the builder said: a registry or address
that did not answer, an image or feature that is not there or needs a sign-in, a
Dockerfile step that failed, a feature that failed to install (named), no space
left, a repository or image too big, or a build that took too long. After a
refused configuration the workspace holds nothing but this entry; after a failed
command, or an image that could not be had, it keeps its files, so that you can look at
what happened, rebuild it once the cause is fixed, or remove it with its trash button. A
workspace being made or being deleted says so, and offers nothing;
one being made from a GitHub repository can be deleted with its trash button, to
stop it.

A server shows its state as a coloured pill, a sentence saying what to do
next, and only the actions that state allows. Its main button, the action to
take next, stands out; **Stop** and **Forget** never do. A server that is
ready or in use also has **Open in claude.ai**, which is its main button:

| State | Dot | What to do | Main button | Buttons |
|---|---|---|---|---|
| Starting | Running | Claude is starting. Wait a moment. | — | **Stop** |
| Waiting for your answer | Needs your answer | Answer whether you trust this workspace. | **Yes, trust it** | **Yes, trust it**, **No**, **Stop** |
| Waiting for your answer about Remote Control | Needs your answer | Answer whether to enable Remote Control. Not now ends the server, and Claude asks again next time. | **Enable** | **Enable**, **Not now**, **Stop** |
| Ready to accept sessions | Running | Open it in claude.ai to start a session. | **Open in claude.ai** | **Open in claude.ai**, **Stop** |
| In use | Running | A session is attached; continue it in claude.ai. | **Open in claude.ai** | **Open in claude.ai**, **Stop** |
| Stopped | Not running | Resume to pick up its sessions, or forget it. | **Resume** | **Resume**, **Forget** |
| Not started: workspace not trusted | Not running | Start again to answer once more, or forget it. | **Start again** | **Start again**, **Forget** |
| Not started: Claude not signed in | Has a problem | Claude is not signed in in this workspace. Start again once it is, or forget it. | **Start again** | **Start again**, **Forget** |
| Stopped by itself | Has a problem | Show the screen to see why it stopped, then resume it. | **Resume** | **Resume**, **Forget**, **Show screen** |
| Reconnected, status not shown | Running | Claude says it reconnected but is not showing its status. Show the screen to see what it says. | **Show screen** | **Show screen**, **Stop** |
| Unrecognised screen | Has a problem | Show the screen, stop it, and tell an administrator. | **Show screen** | **Show screen**, **Stop** |

**Show screen** opens what Claude shows below the buttons, and choosing it
again closes it; its arrow turns to say which.

The page refreshes the workspaces and their servers by itself. When rcman
refuses an action, the reason it gave is shown where the action was asked
for.

When a refresh cannot reach rcman — the connection has dropped, or rcman is
restarting — the page says it could not reach rcman, and when your sign-in has
ended it says to reload the page to sign in again. Either stays until a later
refresh works, and until then the page shows the workspaces as they last were.
The page reports these failures to rcman once it reaches it again, so that
administrators can see them in the [system journal](journal.md).

### On a phone

On a phone rcman's pages, which a wider screen lists in the sidebar at the left,
are behind the menu button at the left of the top bar; choosing one, or pressing
beside the menu, closes it again. Over the list, a line says how many workspaces
you have, and **New**, beside it, is **New workspace**. The page shows the list
or a workspace's panel, not both. Selecting a workspace shows its panel in the
list's place, under a bar that stays at the top as the panel scrolls: the back
arrow (**Back to workspaces**) returns to the list, and the workspace's name
beside it opens your workspaces as a sheet from the foot of the screen, to go
straight to another; Escape, or pressing above the sheet, closes it. Dialogs
rise from the foot of the screen as a sheet, their buttons kept at its foot
above the on-screen keyboard, and on any touch screen every button and link is
at least a fingertip wide, but for a link inside a sentence.

### The terminal

**Terminal** is a shell in the workspace, in the page. It connects when you
first choose the tab, and stays connected while you look at **Overview**. Above
it, a slim bar says whether it is connecting, connected or closed, with a dot
of the shapes above.

- The shell runs as the workspace container's own user. rcman adds no
  restriction to what it may do and takes none away: whatever that user may do
  in the workspace, the shell may. rcman neither records nor logs what you type
  or what the shell shows.
- A workspace has one terminal. Choosing **Terminal** for the same workspace in
  another browser tab or on another device takes it over: the shell here is
  ended, and this tab says the terminal was opened somewhere else.
- A terminal has no time limit: it stays open, idle or not, until something ends
  it.
- It ends when you close or reload the page, select another workspace or go to
  another page; when the shell exits, as `exit` does, and the tab says with what
  exit code; when the workspace is deleted; when a rebuild makes its container
  again, after which **Reconnect** opens a shell in the new one once the rebuild is
  done; when you sign out of rcman, which
  ends your terminals in every browser; when an administrator removes you from
  rcman; and when rcman restarts or is upgraded, from a shell or from its web
  interface. Ending a terminal ends its
  shell and every process in the shell's session; a process that has left the
  session, as one started with `setsid` or a daemon does, keeps running. The
  workspace's server is not touched.
- Once a terminal is closed, the tab says why, and **Reconnect** starts a new
  shell; nothing reconnects by itself. **Reconnect** is not offered where rcman
  would refuse a new terminal: once the workspace is gone or is not ready, you
  have signed out, or an administrator has removed you from rcman; the page then
  asks rcman for your workspaces again. If the terminal could not be opened at
  all, the page asks for them again too, and says, as a refresh does, when it
  cannot reach rcman or your sign-in has ended. If the terminal itself could not
  be loaded, as can happen to a page left open while rcman was upgraded, the tab
  says so and offers **Reload the page** instead.

Inside the terminal every key goes to the shell, Tab among them, so that the
shell can complete what you type; no key moves out of it: click outside the
terminal to move out of it. Its colours follow the light or dark setting of your
system, as the rest of the page does.

### The Container card and rebuilding

A workspace's container is made from its repository's `devcontainer.json` when the
workspace is made, and keeps what the file asked for then. The **Container** card of
**Overview** says what the container is made from. When the file in the workspace, or the
Dockerfile it builds from, differs from the one the container was made from, the card says
the file has changed and **Rebuild** stands out; otherwise **Rebuild** is offered plainly, to
make the container again anyway, as from a newer image of the same name. **Rebuild** is not
offered for an empty workspace, which has no repository to rebuild from, while rcman is being
upgraded, nor at other times rcman would refuse it, such as while rcman is starting or while the
workspace's container is not there.

**Rebuild** asks first, and says what happens. The image is pulled or built while everything
in the workspace keeps running; only then is its server stopped, its container made again and
its server started again. A rebuild ends the terminal, every process in the workspace, those
started with `nohup` or `setsid` too, the containers running inside it, what is in `/tmp`, the
container's own files, and what the repository's create commands installed outside the
workspace's files. It keeps the files in the workspace — the work folder, your home, Claude's
settings and what Podman stored inside it — your Claude sign-in and git. The repository's
lifecycle commands run again, so they must be safe to run again. The build shares your memory
limit with what already runs: going over it ends the largest process, which may be a live Claude
session (see [Limits](users.md#limits)). **Cancel** changes nothing.

While it is rebuilt, the button says **Rebuilding…** and waits, and the card lists **Steps so
far**. While the container is replaced, the **Claude server** card says so, and the terminal
closes. A rebuild that did not finish says, in the card, what became of the workspace and what
to do next: put back as it was, or left as it was after rcman restarted; **Rebuild** tries
again. One that could not put the workspace back at once stays **Rebuilding…**, its last step
saying why and that the workspace is put back as soon as rcman can; until then its server,
git and terminal are not available. What happens at each step, and when the workspace is put
back, is in [Rebuilding a workspace](devcontainer.md#rebuilding-a-workspace).

A workspace that failed because a create command failed or took too long, or because its image
could not be had, offers **Rebuild** beside the reason. The repository's commands run again on
the existing files; if the rebuild fails again, the workspace stays failed with the new reason,
since there is no earlier state to return to, and can be rebuilt again.

## Containers

A workspace made from the default image has Podman: `podman` and `docker`, both the same Podman,
are on the PATH of the workspace's terminal, of Claude and of what Claude runs, right after
Claude's own folder and before the image's own programs; `docker compose` is not offered. In a
workspace whose image has no Podman, `podman` and `docker` run the image's own program of that
name, the next one on the PATH, where it has one. As root, the workspace's own user in the default image, Podman runs in a user
and mount namespace the workspace makes for itself, so its mounts never reach what rcman runs
there; a user who is not root runs it rootless. What it stores is in the workspace's volume.
In an image of a repository's own, a user who is not root runs Podman when the image gives them
subordinate ids among the workspace's own ids (`/etc/subuid` and `/etc/subgid`) and gives
`newuidmap` and `newgidmap` the one capability each needs in place of setuid
(`setcap cap_setuid+ep` and `cap_setgid+ep`), as the default image does; a capability set
while rcman builds the image holds in the workspace.

- A container started in a workspace is no boundary inside it: it shares the workspace's
  network, and the workspace's user can reach whatever it holds.
- Publishing a port, with `-p`, `--publish` or `-P`, publishes nothing: a container that shares
  the workspace's network listens on the workspace's own ports, so one listening on port 8000 is
  at `localhost:8000`, and two cannot listen on the same port. `podman` and `docker` say so when
  asked to publish, unless `--network` or `--net` names a network other than `host`, or
  `--pod` or `--pod-id-file` names a pod, and run the container as asked.
- What containers store counts against no limit: the disk a workspace uses cannot be limited.
- `/tmp` is the workspace container's own, on disk and not on the container's own files, so a
  Podman a program starts with its storage under `/tmp`, as rcman's own tests do, works there;
  anyone in the workspace may write in it, and only what they wrote is theirs to remove. An
  image whose `/tmp` is a link or not a folder is refused. What it
  holds is kept when the workspace is stopped and started, goes with the workspace's container,
  when the workspace is deleted or rebuilt or rcman makes its container again, and counts against
  no limit either. A workspace keeps the container it was made with until it is rebuilt: where
  `stat -f -c %T /tmp` in its terminal prints `overlayfs`, its `/tmp` is on the container's own
  files, and a rebuild gives it one of its own (see
  [The Container card and rebuilding](#the-container-card-and-rebuilding)); an empty workspace,
  which cannot be rebuilt, gets one by a new workspace made in its place.
- `podman` and `docker` say so where they cannot run: in a workspace whose image has no Podman
  and no `podman` or `docker` of its own, and in one whose container was made without what
  Podman needs there. A workspace keeps the image and the container it was made with until it is
  rebuilt: rebuild it, with its `devcontainer.json` naming the default image or another image with
  Podman, or, for an empty workspace, make a new workspace in its place.

## What Claude is told in a workspace

rcman gives Claude instructions of its own in every workspace: a file at
`/etc/claude-code/CLAUDE.md`, which Claude Code reads into every session and which a session
cannot leave out or change. It is read-only in the workspace, takes the place of any file the
workspace's image has there. Your own instructions, in the
repository's `CLAUDE.md` or in `~/.claude`, are read beside it as usual.

Every workspace's sessions are told:

- where the work folder and `HOME` are, that both last as long as the workspace, and that your
  other sessions in the workspace share the work folder;
- that what is installed or changed outside `/rcman` lasts only until the container is made
  again, and that what must last belongs in the repository or its `devcontainer.json`;
- that git takes your commit author and GitHub token from Settings (see
  [Git](git.md#your-commit-author-and-github-token)), and that the
  global git configuration is not to be changed;
- that no port of the workspace can be reached from outside it, and that containers share the
  workspace's network, so publishing a port publishes nothing (see [Containers](#containers));
- that memory, CPU and processes may be limited.

They are also told where the description of the environment that Claude Code's Remote Control
adds to every session is wrong here. That description is of a cloud environment: Claude is told
that this one runs on this machine, that the work in `/rcman` is kept without a commit or a push,
that it has no environments or network policies of the cloud's, that `df` tells the truth about
the disk, and that no browsers for tests are installed in advance.

What else a session is told depends on the workspace's image:

- **An image that carries the default image's label**, `org.claude-rc-man.tools`: the default
  image, whether a workspace gets it by having no `devcontainer.json` or by one that names it,
  or an image built from it and pushed to a registry (see
  [The image](devcontainer.md#the-image)). Sessions are told that `gh` is there with your
  GitHub token, to be used for GitHub work rather than GitHub's tools for Claude (see
  [gh](git.md#gh)), which tools the image has, and that anything else can be installed with
  `apt-get`.
- **An older default image without that label**, which a workspace made from it keeps until it
  is rebuilt: that `gh` is there, but not which tools the image has.
- **Any other image**: nothing more.

When rcman is upgraded with different instructions, a workspace's sessions are told them from
the next time its container starts, as after the machine restarts or a rebuild. Until then they
keep what they were told before.

Where the instructions are missing from rcman's installation, no workspace is made or rebuilt:
a new workspace fails, and a Rebuild leaves the container as it was, each saying that the
installer is to be run again, which an administrator does (see [the journal](journal.md)).

## Deleting a workspace

The trash button beside a workspace's name in the list deletes that workspace,
everything in it, and its server, whichever workspace is selected. It asks
first. For a workspace that is ready, the question asks git what would be lost
and says so: files with changes that are not committed, and commits that are not
pushed; **Delete** waits until git has answered, and if rcman could not ask, the
question says so rather than that nothing would be lost. Confirming with
**Delete** stops the server if it is running and deletes it with the workspace;
**Cancel** deletes nothing. It cannot be undone. Afterwards the page says the
workspace was deleted; when it was the one selected, no workspace is selected,
and otherwise the selection stays as it was.

A workspace that is ready or failed can be deleted, a workspace being rebuilt among them, and so
can one being made from a GitHub repository. For any other, such as one being made empty or one
already being deleted, the trash button says why it cannot be deleted now, and
deletes nothing.

The sessions of its server stay in claude.ai/code, where they are archived or
deleted.

Deleting a workspace that is still being cloned, or that is pulling or
building its image or running a command of its `devcontainer.json`, while it is made or
rebuilt, stops that and deletes the workspace. If rcman restarts while a workspace cloned from a
repository is being made, the workspace is shown as failed, and says so; delete it and make it
again. An empty workspace that was being made is made again when rcman starts. If rcman restarts
while a workspace is being rebuilt, the rebuild is settled when it starts, and the workspace's
**Container** card says how (see [Rebuilding a workspace](devcontainer.md#rebuilding-a-workspace)).

## Rules

- Each workspace has at most one server. One server can host several
  sessions, and they all work in the same workspace.
- A workspace is yours alone: no one else can see it, reach it or act on its
  server. Administrators can see your workspaces and the states of their
  servers, but cannot start, stop or otherwise act on them.
- Servers keep running when you close your browser, and while rcman restarts or
  is upgraded. When the machine restarts, rcman brings your workspaces back, and
  starts again each server that was running. A rebuild you ask for stops the workspace's
  server while its container is made again, and starts it again afterwards.
- An administrator can limit the memory, CPU and processes of a workspace, and of all
  your workspaces together. A workspace is also held to your limits, and a
  memory limit is hard: a group that goes over it has its largest process ended,
  which can be the session of another of your workspaces (see
  [Limits](users.md#limits)).
- A server starts only once you have connected your Claude account, which every
  workspace of yours shares: see
  [Connecting your Claude account](claude-account.md).
- The names of a server's sessions begin with your session name prefix, or,
  while you have set none, with the workspace's name: see
  [Settings](settings.md).

## Starting

The first time a server starts in a workspace, Claude asks whether you trust
it. Sessions started there can read, edit and run the files in it, and its own
Claude settings apply to them. Answer yes to start the server, or no to
cancel. Claude remembers the answer yes for that workspace.

Until Remote Control is enabled for your Claude account, Claude then asks one
more question before the server starts: whether to enable Remote Control.
Choose **Enable** to start the server, or **Not now** to end it without
starting it; Claude asks again the next time a server starts. Choose **Stop**
to end the server there without answering.

Once it has started, a server is **ready to accept sessions**, and **Open in
claude.ai** takes you to it on claude.ai/code, where you start a session; the
Claude app lists it too. While a session is attached the server is **in use**,
and shows the session's name beside the same link.

## Stopping and resuming

Stopping a server ends the process but keeps its sessions. If Claude does
not stop when asked — while it shows a question rcman does not recognise, for
example — rcman ends it after a short wait; it is then shown as **Stopped by
itself**, and can be resumed or forgotten. Starting it again in the same
workspace **resumes** those sessions, as long as that happens within a few
hours; after that, starting gives you a new session.

A server can also stop by itself — for example when the machine loses its
network connection for a long time, or when its process is ended outside
rcman. It is then shown as **Stopped by itself**, with its exit status, and
**Show screen** displays what Claude showed last. **Resume** starts it again
in the same workspace. A server ended with **Not now** is shown as the screen
Claude leaves and its exit status say, most likely **Stopped by itself**, and
Claude asks the question again when it is resumed.

A server runs the Claude Code version that was approved when it started, and
keeps it while it runs. Starting it again after you stopped it, resuming it, or
the restore after a restart of the machine takes the version approved then. When
the approved version differs from the one your server runs, its **Claude
server** card says so quietly; nothing restarts by itself and no session is cut
(see [Claude Code versions](claude-version.md)).

A server that has stopped can be **forgotten**, which removes it from its
workspace. Archiving or deleting individual sessions is done in
claude.ai/code.

## When something looks unfamiliar

If Claude shows a screen rcman does not recognise, the server is shown as
**Unrecognised screen** and **Show screen** displays exactly what Claude shows,
rather than rcman guessing what it means. This usually means Claude Code has
changed, or is asking something, in a way rcman does not handle yet.

When it happens:

1. Choose **Show screen** and read what Claude says.
2. Choose **Stop**, then **Forget**.
3. Tell an administrator, with what the screen said. Administrators are warned
   about the server with your address and the workspace's name, but not what
   the screen says.

A server cannot be used in that workspace while Claude keeps showing that
screen; rcman handles it once it has been taught to recognise it.

A server shown as **Reconnected, status not shown** is not on an
unrecognised screen. Claude shows its log, the last line saying that Remote
Control has reconnected, with at most the text it showed at its start above
it and the answers that were given to its questions, and not the status from
which rcman reads the session's name and the link to it, so the server has no
**Open in claude.ai**; **Show screen** displays what Claude says. If Claude
shows its status again, the server is shown as ready or in use once more. Nothing has
to be reported, and administrators are not warned about it. **Stop** stops
it as it stops any server.

## For administrators

The **Everyone's workspaces** page, in the **Administration** group of the
sidebar, lists each person in rcman, by address, in a card of their own, with
their workspaces: the name, the state — the workspace's while it is not ready,
and otherwise its server's, or that it has none — the session's name, as each
person's own Workspaces page shows them, and the Claude Code version a running
server runs. It is for looking only; it has no buttons, because rcman lets only
the person a workspace belongs to act on it. A workspace whose server rcman
could not read is shown with the reason, and everything else is listed as usual.

While anyone's server is on a screen rcman does not recognise, administrators
are warned at the top of every page, with the person's address and the
workspace's name and a link to **Everyone's workspaces**. The warning never
includes the screen itself; the person whose server it is sees it with
**Show screen**. Each action that ends on such a screen is also recorded in
the [audit trail](audit.md) with the result `unrecognised_screen`. The warning
is worked out afresh each time a page is opened, so it lasts only while the
server shows that screen. When a server comes onto such a screen, rcman also
writes it once to the [system journal](journal.md), with the person and the
workspace, and never the screen itself.

Administrators can put limits on memory, CPU and processes on a person and on
each of their workspaces, on the **People** page: see [Limits](users.md#limits).

What a repository's `devcontainer.json` may ask of a workspace, and what rcman
refuses, is in [devcontainer.json support](devcontainer.md), and so is what a
Rebuild does. The lifecycle commands in it run with the person's GitHub token within
reach, when the workspace is made and again at each Rebuild the person asks for, with the
file that is in the workspace then; the note there says what that means. A Rebuild is refused
while rcman is upgraded, and an upgrade while a workspace is rebuilt (see
[Installation](installation.md)). On **Everyone's workspaces**, a workspace whose container a
rebuild replaces just then is shown with the reason its server is not read.
