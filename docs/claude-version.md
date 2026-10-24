# Claude Code versions

For administrators. Every workspace runs Claude Code from one store,
`/var/lib/rcman/claude`, which every workspace has, read-only. Which version
of it new servers start is the approved version, and an administrator
chooses it on the versions page.

## The store and the approved version

The store holds the versions rcman has installed, and `current` names the
approved one. rcman owns the store and writes it; the installer makes it and
fills it with the tested version, the one rcman's recorded tests were made
with, named in `helpers/claude-version`. The installer sets `current` only
when the store has none; on any later run, and when rcman is upgraded, the
approved version stays as it is. Changing the tested version in a new release
of rcman puts that version in the store, and an administrator decides whether
to approve it.

Approvals are kept in rcman's database. The latest is the approved version;
the latest before it that names another version and is not marked as failed
(see [Marking a version as failed](#marking-a-version-as-failed)) is the
previous version, the last good one; and going back to an earlier version is a
new approval, not an undo. When rcman starts for the first time with a store
the installer filled, it takes the version `current` names as the first
approval.

Claude Code's own self-update is turned off wherever Claude runs
(`DISABLE_AUTOUPDATER`), so a workspace runs only a version of the store.

## Servers keep the version they started with

A server starts on the approved version and keeps it until it is stopped and
started again: a person's own stop and start, a resume, or the restore after the
machine restarts. Approving another version restarts nothing and cuts no running
session: the server's process and the sessions it starts keep the version the
server started with. The one exception is a command `claude` typed by hand, in a
session's terminal or in a workspace's **Terminal** tab, which follows the store's
`current` and so runs the approved version. A person whose server runs a version
other than the approved one sees a quiet note in the server section of their
workspace, which says to stop the server and start it again to use the approved
one.

A version leaves the store only when it is neither the approved nor the
previous version, nor the tested one, nor the one `current` names, no running server
uses it, and it was not put in the store within `PRUNE_GRACE_SECONDS`
(`backend/rcman/claude_versions.py`); rcman removes such versions
after a roll-out, after a version is marked as failed, when servers stop and
when it starts. A server whose state rcman cannot read counts as running.
`rcman-admin` removes none. A version marked as failed is never the previous
version, so it leaves the store like any other once nothing else keeps it; its
mark stays. The previous version, too, can be one that is not in the store: one
that left it while it was neither approved nor previous.

## The versions page

In the **Administration** group of the sidebar, the **Claude Code versions**
page shows:

- the **Approved version**, the **Previous version** and the **Tested
  version**;
- the **Installed versions**, with how many running servers use each,
  **Failed** beside one marked as failed, and beside each one that is neither
  approved nor marked as failed **Use**, which approves it again, and **Mark as
  failed**;
- the **Failed versions**, each with why it was marked, by whom and when, and
  **Remove mark**; at the card's head, **Mark a version as failed**, for a
  version by its number, installed or not (see
  [Marking a version as failed](#marking-a-version-as-failed));
- **Servers on another version**: the running servers whose version is not the
  approved one, with their owners, who are asked nothing and told nothing but
  the note above, and **Failed** beside a version marked as failed;
- the **Newest release**, which rcman asks the release site for and offers as
  a suggestion with **Roll out**, or, where it is marked as failed, says so and
  offers nothing; nothing is installed or approved until an administrator
  chooses it;
- a page that follows a roll-out another administrator has under way, and reads
  the versions again after an answer that one is under way;
- a form to roll out any version by its number, with **Check and roll out**.

## Rolling out a version

A roll-out takes as long as it takes, often minutes, and the page waits for
it; only one runs at a time, and none while rcman itself is being upgraded from
its web interface (see
[Upgrading from the web interface](installation.md#upgrading-from-the-web-interface)),
whose restart would cut it short: the page then offers no roll-out and says why. An
upgrade of rcman is not asked for while a roll-out runs. rcman:

1. downloads the version from the release site into a staging folder outside
   the store, as `RCMAN_CLAUDE_DOWNLOAD` names it (see
   [Settings](configuration.md)), checks it against the checksum the release
   publishes and refuses one bigger than it takes; a version already in the
   store is copied from it instead;
2. runs the version through the canary checks in a throwaway container with no
   network, which has nothing but that version and rcman's own helpers: the
   checks are the commands rcman uses to read a server and to sign a person in
   and out, and each must answer as rcman expects (`helpers/claude_checks.py`);
3. only if every check passes, copies it into the store, approves it and makes
   it `current`.

A version that was approved before and is still in the store passed the checks
then, so going back to it, or approving it again, needs no download and no
canary and works even while rcman cannot reach its containers; a version never
approved, or one approved before that has left the store, always goes through
the download and the canary. A version number that is not one is refused at
once, and so is a version marked as failed, approved before or not, before
anything is downloaded or checked (see
[Marking a version as failed](#marking-a-version-as-failed)).

When a release site does not answer or a download stops part way, the request
fails with a gateway error that tells you to try again; the other refusals are
about the version itself. rcman switches `current` first and records the
approval only afterwards, and puts `current` back if the recording fails, so the
approved version is always the one `current` names. After a crash, rcman's start
points `current` at the approved version. A file in the store with nothing in it
is not counted as installed.

A version that cannot be downloaded, does not match its checksum, is not
there, or fails a check leaves the store, the approvals and every server
exactly as they were. The page says why in plain words, and for failed checks
shows what each ran and what it got back. If the checks themselves cannot be
run, for example because the container could not start, nothing is changed
either, and trying again is the remedy.

The checks do not look at what Claude makes of the instructions rcman gives it in every
workspace (see
[What Claude is told in a workspace](workspaces.md#what-claude-is-told-in-a-workspace)). The
version rcman is tested with has been checked by hand for that. After rolling out another
version, start a server in a workspace of the default image and, in a new session, check that
Claude knows what it was told, for instance that work in `/rcman` is kept without a push, and
that the text Remote Control adds to its system prompt still has a section headed "Your
current remote execution environment", the one rcman's instructions correct. If either is not
so, rcman's instructions do not reach Claude as they should: go back to the previous version
and mark the one you rolled out as failed, as
[Going back to the previous version](#going-back-to-the-previous-version) says.

The checksum comes from the same place as the program, the release site. It
protects against a download that is damaged, not against a release site that
has been compromised: rcman checks no signature. Point
`RCMAN_CLAUDE_DOWNLOAD` only at a source you trust.

## Going back to the previous version

When a version you rolled out does not work as it should, go back to the
**Previous version**, the last good one:

1. Where the previous version is among the **Installed versions**, choose
   **Use** beside it. It was approved before and is in the store, so it is
   approved again at once, with no download and no canary. Where it has left
   the store, it is not among the installed versions: roll it out by the
   number the **Previous version** shows, with **Check and roll out**, which downloads it and runs it through
   the canary again, as any roll-out does.
2. Then mark the version you left as failed, with **Mark as failed** beside it,
   so that nobody rolls it out again by mistake. The approved version cannot be
   marked, so go back first and mark it afterwards.

Going back restarts nothing: servers keep the version they started with until
they are stopped and started again, and those on the version you left are
listed under **Servers on another version**, with **Failed** beside it.

## Marking a version as failed

Any administrator can mark any version but the approved one as failed:
installed or not, the newest release and the tested version among them. Use
**Mark as failed** beside an installed version, or **Mark a version as
failed** in the **Failed versions** card, which asks for the version's number.
A reason is optional: one line, trimmed of spaces at either end, of at most
the length `helpers/rules.py` allows (`FAILURE_REASON`). rcman records who
marked the version and when, and the **Failed versions** card shows the
reason, who and when. Marking may be done at any time, while a roll-out runs
too.

While a version is marked as failed:

- a roll-out of it is refused before anything is downloaded or checked, even
  where it was approved before and is still in the store, and **Use** is not
  offered beside it;
- a roll-out of it already under way when it is marked is stopped before it is
  approved, and nothing is changed;
- the **Newest release** card offers no **Roll out** for it;
- it is never the **Previous version**, so pruning keeps it only while
  something else does, such as a running server; it then leaves the store as
  any version does, and its mark stays.

rcman marks no version by itself: a roll-out whose checks fail changes nothing
and marks nothing. Mark such a version yourself if you do not want it rolled
out again.

The only way to roll out a version marked as failed is to remove its mark
first: **Remove mark** beside it in the **Failed versions** card removes the
mark at once, without asking. The version can then be rolled out as any other,
through the download and the canary where it is not in the store. A version
marked already cannot be marked again; remove the mark first to give it
another reason.

## Audit and journal

A roll-out is the audit action `claude.upgrade`, with the version as its
target and `ok` or the error code as its result. Marking a version as failed is
`claude.mark_failed`, and removing a mark is `claude.clear_failed`; what each
records is in [Audit trail](audit.md).
What rcman logs about versions is in [The journal](journal.md).
