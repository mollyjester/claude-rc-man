# Servers

A **server** is one Claude Code Remote Control process running in one folder
under your home folder, on this machine, under your own Linux account and your
own Claude account. The conversations it hosts — its **sessions** — appear in
[claude.ai/code](https://claude.ai/code) and in the Claude app, where you work
with them.

## The Servers page

The Servers page shows your folders on one side and your servers on the other.
Open a folder with the arrow beside it, and choose **Start** beside any folder
that has no server yet. Each server shows its state, and only the actions that
state allows:

| State | What you can do |
|---|---|
| Starting, Connecting, Ready | Stop |
| Waiting for your answer | Trust the folder, decline, or stop |
| Stopped | Resume, or forget it |
| Not started (folder not trusted, or Claude account not connected) | Start again, or forget it |
| Ended | Start again, forget it, or show its last screen |
| Unrecognised screen | Show the screen, or stop |

The page refreshes the states by itself. When rcman refuses an action, the
reason it gave is shown on the server it was about.

## Rules

- A server always runs in a folder *inside* your home folder, never in the home
  folder itself.
- Each folder has at most one server. One server can host several sessions.
- Servers keep running when you close your browser.
- Your Claude account has to be connected before a server can start.
- The list shows only servers started from rcman. A `claude remote-control`
  you start yourself, in a terminal, is not shown, and rcman cannot stop or
  resume it. To have rcman manage that folder, quit the one you started and
  choose **Start** beside the folder here.

## Starting

The first time a server starts in a folder, Claude asks whether you trust the
folder. Sessions started there can read, edit and run the files in it, and the
folder's own Claude settings apply to them. Answer yes to start the server, or
no to cancel. Claude remembers the answer yes for that folder.

A server first **connects**, then becomes **ready** once a session is attached
to it. A ready server shows the session's name and a link to it on
claude.ai/code.

## Stopping and resuming

Stopping a server ends the process but keeps its sessions. Starting it again
in the same folder **resumes** those sessions, as long as that happens within a
few hours; after that, starting gives you a new session.

A server also stops by itself if the machine loses its network connection for
a long time.

A server that has stopped can be **forgotten**, which removes it from
your list. Archiving or deleting individual sessions is done in claude.ai/code.

## When something looks unfamiliar

If Claude shows a screen rcman does not recognise, the server is shown as
**Unrecognised screen** and **Show screen** displays exactly what Claude shows,
rather than rcman guessing what it means. This usually means Claude Code has
changed in a way rcman does not handle yet.
