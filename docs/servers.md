# Servers

A **server** is one Claude Code Remote Control process running in one folder
under your home folder, on this machine, under your own Linux account and your
own Claude account. The conversations it hosts — its **sessions** — appear in
[claude.ai/code](https://claude.ai/code) and in the Claude app, where you work
with them.

## The Servers page

The Servers page has two sides. On the left are your **Folders**, and under
them **Your servers**, which lists every server you have; on the right is the
panel of the folder you have selected.

Select a folder by its name. The arrow beside a name opens the folder to show
the folders inside it, without selecting it. A folder whose server is running,
waiting for your answer, has a problem or is not running carries a coloured dot
saying which; a folder with no server carries none. While a folder is closed,
its dot also stands for the servers inside it, showing the most urgent of them.
Choosing a server in **Your servers** selects its folder, wherever it is in the
tree.

The panel starts with where the folder is, from **Home** down; any step of it
selects that folder, and **Home** selects none. Below that, its
**Claude server** section shows the folder's server, or **Start** when it has none.
**Start**, **Resume** and **Start again** are offered only while your Claude
account is connected; until then the section says to connect it. The
folder you have selected is part of the page's address, so reloading the page
keeps it, and the browser's back button returns to the folder selected before.

A server shows its state as a coloured badge, a sentence saying what to do
next, and only the actions that state allows. Its main button, the action to
take next, stands out; **Stop** and **Forget** never do. A server that is
ready or in use also has **Open in claude.ai**, which is its main button:

| State | Dot | What to do | Main button | Buttons |
|---|---|---|---|---|
| Starting | Running | Claude is starting. Wait a moment. | — | **Stop** |
| Waiting for your answer | Needs your answer | Answer whether you trust this folder. | **Yes, trust it** | **Yes, trust it**, **No**, **Stop** |
| Ready to accept sessions | Running | Open it in claude.ai to start a session. | **Open in claude.ai** | **Stop** |
| In use | Running | A session is attached; continue it in claude.ai. | **Open in claude.ai** | **Stop** |
| Stopped | Not running | Resume to pick up its sessions, or forget it. | **Resume** | **Resume**, **Forget** |
| Not started: folder not trusted | Not running | Start again to answer once more, or forget it. | **Start again** | **Start again**, **Forget** |
| Not started: Claude account not connected | Has a problem | Connect your Claude account above, then start again. | **Start again** | **Start again**, **Forget** |
| Stopped by itself | Has a problem | Show the screen to see why it stopped, then resume it. | **Resume** | **Resume**, **Forget**, **Show screen** |
| Unrecognised screen | Has a problem | Show the screen, stop it, and tell an administrator. | **Show screen** | **Show screen**, **Stop** |

The page refreshes the states by itself. When rcman refuses an action, the
reason it gave is shown on the server it was about.

## Rules

- A server always runs in a folder *inside* your home folder, never in the home
  folder itself.
- Each folder has at most one server. One server can host several sessions.
- Servers keep running when you close your browser.
- Your Claude account has to be connected before a server can start.
- Administrators can see your servers and their states, but cannot start,
  stop or otherwise act on them; only you can.
- The list shows only servers started from rcman. A `claude remote-control`
  you start yourself, in a terminal, is not shown, and rcman cannot stop or
  resume it. To have rcman manage that folder, quit the one you started and
  select the folder here and choose **Start**.

## Starting

The first time a server starts in a folder, Claude asks whether you trust the
folder. Sessions started there can read, edit and run the files in it, and the
folder's own Claude settings apply to them. Answer yes to start the server, or
no to cancel. Claude remembers the answer yes for that folder.

Once it has started, a server is **ready to accept sessions**, and **Open in
claude.ai** takes you to it on claude.ai/code, where you start a session; the
Claude app lists it too. While a session is attached the server is **in use**,
and shows the session's name beside the same link.

## Stopping and resuming

Stopping a server ends the process but keeps its sessions. Starting it again
in the same folder **resumes** those sessions, as long as that happens within a
few hours; after that, starting gives you a new session.

A server can also stop by itself — for example when the machine loses its
network connection for a long time, or when its process is ended outside
rcman. It is then shown as **Stopped by itself**, with its exit status, and
**Show screen** displays what Claude showed last. **Resume** starts it again
in the same folder.

A server that has stopped can be **forgotten**, which removes it from
your list. Archiving or deleting individual sessions is done in claude.ai/code.

## When something looks unfamiliar

If Claude shows a screen rcman does not recognise, the server is shown as
**Unrecognised screen** and **Show screen** displays exactly what Claude shows,
rather than rcman guessing what it means. This usually means Claude Code has
changed, or is asking something, in a way rcman does not handle yet.

When it happens:

1. Choose **Show screen** and read what Claude says. It may name something to
   do outside rcman, such as signing in again (see
   [Connecting your Claude account](claude-account.md)).
2. Choose **Stop**, then **Forget**.
3. Tell an administrator, with what the screen said. Administrators are warned
   about the server with your Linux user and the folder, but not what the
   screen says.

A server cannot be used in that folder while Claude keeps showing that screen;
rcman handles it once it has been taught to recognise it.

## For administrators

The **Everyone's servers** page, under **Admin** in the navigation, lists each
person in rcman with their servers:
the folder, the state and the session's name, as each person's own Servers page
shows them. It is for looking only; it has no buttons, because rcman lets only
the person a server belongs to act on it. A person whose servers could not be
listed — rcman's helper failed or took too long for them — is shown with the
reason, and everyone else is listed as usual.

While anyone's server is on a screen rcman does not recognise, administrators
are warned at the top of every page, with the person's Linux user and the
folder and a link to **Everyone's servers**. The warning never includes the screen itself; the
person whose server it is sees it with **Show screen**. Each action that ends
on such a screen is also recorded in the [audit trail](audit.md) with the
result `unrecognised_screen`. The warning is worked out afresh each time a page
is opened, so it lasts only while the server shows that screen.
