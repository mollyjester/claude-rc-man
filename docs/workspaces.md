# Workspaces

A **workspace** is a place of your own on this machine, kept apart from
everyone else's and from rcman itself, where a Claude Code Remote Control
process can run: the workspace's **server**. The conversations a server hosts
— its **sessions** — appear in [claude.ai/code](https://claude.ai/code) and in
the Claude app, where you work with them, and they work on the files in the
workspace.

## The Workspaces page

The Workspaces page has two sides. On the left are **Your workspaces**, and
under them **New workspace**; on the right is the panel of the workspace you
have selected.

Select a workspace by its name. A workspace whose server is running, waiting
for your answer, has a problem or is not running carries a dot whose shape and
colour say which, and pointing at a dot names it. A workspace with no server
carries none, except that one that failed to be made, or whose server rcman
could not read, carries the problem dot:

| Dot | Shape |
|---|---|
| Has a problem | triangle |
| Needs your answer | ring |
| Running | filled circle |
| Not running | hollow square |

A workspace that is not ready says so beside its name: **Being made**,
**Failed** or **Being deleted**.

To make a workspace, type its name under **New workspace** and choose
**Create**. The new workspace is selected, and is shown as being made until it
is ready; that takes a moment, and longest for your first workspace. Without
an address a workspace is empty. When rcman refuses a name, the form says why
and keeps what you typed; each of your workspaces has a name of its own.

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
gets the default image. If the file describes an image to build, with a
Dockerfile or features, rcman builds it first: the steps show the build's
progress, which can take a long time for a big image, and when other builds are
running, a line says that this one waits for them to finish. The build uses a
copy of the repository's files, not the workspace itself, and cannot sign in to
a private registry. What rcman supports and refuses is in
[devcontainer.json support](devcontainer.md).

The workspace you have selected is part of the page's address, so reloading
the page keeps it, and the browser's back button returns to the workspace
selected before.

The panel of a workspace that is ready has its **Claude server** section,
which shows the workspace's server, or **Start** when it has none. If rcman
could not read the server, the section says why instead. Below it is the
**Changes** section, for the git repository in the workspace (see
[Git](git.md)). A workspace that
failed to be made says what went wrong in plain words, with **Steps so far**
beneath, and offers only **Delete workspace**. When the repository's
`devcontainer.json` is the cause, the reason names the setting to change, or
the image rcman could not pull or use, or the command that failed or took too
long, with what it printed last in the steps. A build that failed says why in
plain words, with the end of what the builder said: a registry or address that
did not answer, an image or feature that is not there or needs a sign-in, a
Dockerfile step that failed, a feature that failed to install (named), no space
left, a repository or image too big, or a build that took too long. After a refused configuration
the workspace holds nothing but this entry; after a failed command it is kept
so that you can look at what happened, and **Delete workspace** removes it. A workspace being made or being deleted says
so, and offers nothing, except that one being made from a GitHub repository
offers **Delete workspace** too, to stop it.

A server shows its state as a coloured badge, a sentence saying what to do
next, and only the actions that state allows. Its main button, the action to
take next, stands out; **Stop** and **Forget** never do. A server that is
ready or in use also has **Open in claude.ai**, which is its main button:

| State | Dot | What to do | Main button | Buttons |
|---|---|---|---|---|
| Starting | Running | Claude is starting. Wait a moment. | — | **Stop** |
| Waiting for your answer | Needs your answer | Answer whether you trust this workspace. | **Yes, trust it** | **Yes, trust it**, **No**, **Stop** |
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

## Deleting a workspace

**Delete workspace** deletes the workspace, everything in it, and its server.
It asks first; confirming with **Delete** stops the server if it is running
and deletes it with the workspace. It cannot be undone. Afterwards no
workspace is selected, and the page says the workspace was deleted.

The sessions of its server stay in claude.ai/code, where they are archived or
deleted.

Deleting a workspace that is still being cloned, or that is pulling or
building its image or running a command of its `devcontainer.json`, stops that and
deletes the workspace. If rcman restarts while a workspace is being made, the workspace is
shown as failed, and says so; delete it and make it again.

## Rules

- Each workspace has at most one server. One server can host several
  sessions, and they all work in the same workspace.
- A workspace is yours alone: no one else can see it, reach it or act on its
  server. Administrators can see your workspaces and the states of their
  servers, but cannot start, stop or otherwise act on them.
- Servers keep running when you close your browser, and while rcman restarts or
  is upgraded. When the machine restarts, rcman brings your workspaces back, and
  starts again each server that was running.
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
in the same workspace.

A server runs the Claude Code version that was approved when it started, and
keeps it while it runs. Starting it again after you stopped it, resuming it, or
the restore after a restart of the machine takes the version approved then. When
the approved version differs from the one your server runs, its section says so
quietly; nothing restarts by itself and no session is cut (see
[Claude Code versions](claude-version.md)).

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
unrecognised screen. Claude shows only the lines of its log, the last saying
that Remote Control has reconnected, and not the status from which rcman
reads the session's name and the link to it, so the server has no **Open in
claude.ai**; **Show screen** displays what Claude says. If Claude shows its
status again, the server is shown as ready or in use once more. Nothing has
to be reported, and administrators are not warned about it. **Stop** stops
it as it stops any server.

## For administrators

The **Everyone's workspaces** page, under **Admin** in the navigation, lists
each person in rcman, by address, with their workspaces: the name, the state
— the workspace's while it is not ready, and otherwise its server's, or that
it has none — and the session's name, as each person's own Workspaces page
shows them. It is for looking only; it has no buttons, because rcman lets
only the person a workspace belongs to act on it. A workspace whose server
rcman could not read is shown with the reason, and everything else is listed
as usual.

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
refuses, is in [devcontainer.json support](devcontainer.md). The lifecycle
commands in it run with the person's GitHub token within reach; the note there
says what that means.
