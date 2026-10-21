"""Workspaces: each a container of a person's, with at most one Claude server.

The ``workspaces`` table is what should exist and what should run; the runtime
(runtime.py) is what does. rcman is the only one who starts anything, so when it
starts it brings the runtime back to the table (``restore``), in the background,
while it already answers requests. A workspace is made in the background too:
the first of a person's waits while Podman copies the image into their range.

Every command rcman runs in a workspace goes through its channel (channel.py), but
for one that needs a stream of its own (Runtime.exec, and the Terminal tab's shell);
the channel is opened when it is first needed and closed once it has been idle for
CHANNEL_IDLE_SECONDS. A workspace whose table row records no server is never reached
at all to list it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from helpers import git_run, instructions, rules
from rcman import builder, db, devcontainer, limits, people
from rcman.channel import Channel, ChannelBroken, ChannelTimeout, Result
from rcman.claude_server import Server, TmuxFailed
from rcman.claude_versions import ClaudeVersions
from rcman.filelock import FileLock
from rcman.limits import Limits
from rcman.problems import (
    CLONE_MESSAGES,
    DEVCONTAINER_MESSAGES,
    MESSAGES,
    PERSON_DELETION_LEFT,
    REBUILD_MESSAGES,
    Problem,
    rebuild_advice,
)
from rcman.repository import ANSWER_MARGIN_SECONDS, KILL_GRACE_SECONDS, CloneFailed, Repository
from rcman.runtime import (
    BUILT_TAG,
    DIGEST,
    GH_CONFIG_DIR,
    LABEL_PERSON,
    LABEL_WORKSPACE,
    NEXT_TAG,
    SYSTEMCTL_TIMEOUT,
    TIMED_OUT,
    BuildFailed,
    BuildRequest,
    InstructionsMissing,
    Layout,
    Runtime,
    RuntimeFailed,
    RuntimeUnavailable,
    Spec,
    built_name,
    container_name,
    gh_config_dir,
    repository_of,
    workspace_of_built_name,
)
from rcman.settings import Settings

log = logging.getLogger("rcman.workspaces")
log.setLevel(logging.INFO)

CREATING, READY, FAILED, DELETING = "creating", "ready", "failed", "deleting"
# The phases of a rebuild (``Workspaces.rebuild``), kept in the row so that a restart of rcman
# knows what it cut short: the new image is being made ready while the old container runs; the
# old container is being replaced by the new one; the new plan is stored and the built image's
# name is being moved onto it.
REBUILD_PREPARING, REBUILD_SWITCHING, REBUILD_PROMOTING = "preparing", "switching", "promoting"
# The kinds of failure (the row's ``failure``) that leave a workspace's volume and its
# repository, so that a rebuild may make it again on them: a lifecycle command that failed or
# took too long, and an image that is no longer to be had.
RECOVERABLE = frozenset(
    ("hook_failed", "hook_timeout", "image_gone", "built_image_gone", "default_image_gone")
)
# A channel unused this long is closed; it is opened again when next needed.
CHANNEL_IDLE_SECONDS = 300.0
# How often idle channels, and sign-outs that wait for servers that have ended, are
# looked for.
IDLE_CHECK_SECONDS = 30.0
# How long the limits report may take in all: each reading asks the unit manager, one after the
# other, and one that does not answer takes up to rcman's timeout for it. The readings left
# when the time is used are not made, and are shown as not read.
LIMITS_REPORT_SECONDS = 2 * SYSTEMCTL_TIMEOUT
# The file, in the database's folder, that the service and rcman-admin lock to change limits.
LOCK_FILE = "rcman.lock"
# How long one wait for the runtime lasts before restore tries again, or stops when
# rcman does; and how long rcman's shutdown waits for restore to stop.
RUNTIME_WAIT_SECONDS = 5.0
# How many workspaces' servers are read at once for a listing.
LISTING_WORKERS = 8
# How long deleting a person waits for a workspace still being made.
MADE_WITHIN_SECONDS = 600.0
# How often work a deletion has to stop (``_working_on``) looks whether another process,
# rcman-admin, deleted its workspace, and how often deleting a person looks whether the
# workspaces being made are gone; and the name of the thread that looks, before the
# workspace's id.
WORKING_POLL_SECONDS = 1.0
WATCHER_PREFIX = "watching-workspace-"
# Most of what the runtime said that a failed workspace keeps.
MAX_ERROR_CHARS = 2000
# Most of the steps rcman wrote while making a workspace that are kept: the last ones.
LOG_MAX_CHARS = 4000
# The name of a workspace cloned from an address that has no name to take from it, and how
# many numbered names are tried for one made from a repository before giving up.
DEFAULT_NAME = "workspace"
NAME_TRIES = 1000
# What a token is shown as where git's words are kept.
REDACTED = "***"
# How long one lifecycle command of a repository's devcontainer.json may run before it is
# stopped, and how long after TERM it is left before it is killed.
HOOK_TIMEOUT_SECONDS = 1800
HOOK_KILL_GRACE_SECONDS = KILL_GRACE_SECONDS
# The most of a command's output the log keeps, and the most of what podman said that a failed
# pull's message holds.
HOOK_LOG_TAIL_CHARS = 1000
MAX_SAID_CHARS = 500
# The most of each of a command's output streams that is ever held, however much it prints:
# its last bytes.
HOOK_KEEP_BYTES = 64 * 1024
# How many images are built at once, how often a build that waits for a place looks whether it is
# stopped, and the line the log gets when it has to wait.
MAX_BUILDS = 2
BUILD_QUEUE_POLL_SECONDS = 0.5
WAITING_FOR_BUILD = "Waiting for another build to finish"
# How many workspaces a restore brings back at once; a slow one holds one of these places until
# it is done, and what restore does after all of them (removing what no row is for, finishing
# deletions, settling sign-outs) waits for the last.
RESTORE_WORKERS = 4
# What a registry says, in these words, when an image is not there or may not be had; any
# other failure of a pull - the network, a timeout, a registry that is down - says nothing
# of the image.
IMAGE_GONE_WORDS = (
    "manifest unknown",
    "name unknown",
    "not found",
    "unauthorized",
    "denied",
    "repository does not exist",
)
# How often a restore looks whether its workspaces are done, and whether rcman is closing.
RESTORE_POLL_SECONDS = 0.2
# Prints musl when the image has the musl loader and no glibc one, as Alpine images do, and ok
# otherwise. Shell builtins and globs only, so that it needs nothing of the image.
LIBC_PROBE = (
    "glibc=; musl=; for f in /lib/ld-linux* /lib64/ld-linux* /usr/lib/ld-linux* "
    "/usr/lib64/ld-linux* /lib/*-linux-gnu/ld-linux* /usr/lib/*-linux-gnu/ld-linux*; "
    'do [ -e "$f" ] && glibc=1; done; '
    'for f in /lib/ld-musl-* /usr/lib/ld-musl-*; do [ -e "$f" ] && musl=1; done; '
    'if [ -n "$musl" ] && [ -z "$glibc" ]; then echo musl; else echo ok; fi'
)
# Tells whether the image has a /bin/sh to run a command written as a string with.
SHELL_PROBE = "[ -x /bin/sh ] && echo image || echo mounted"
IMAGE_SHELL = "/bin/sh"
# The words of git's by which a failed clone's cause is recognised, in the order they are
# looked for: a sign-in asked for or refused, a repository GitHub does not have, no network.
CLONE_CAUSES = (
    (
        "sign_in",
        (
            "could not read username",
            "terminal prompts disabled",
            "authentication failed",
            "invalid username or password",
            "returned error: 401",
            "returned error: 403",
        ),
    ),
    # git's own "fatal: repository '<address>' not found", and GitHub's "Repository not found."
    ("not_found", ("repository not found", "' not found", "does not exist", "returned error: 404")),
    (
        "network",
        (
            "could not resolve host",
            "unable to access",
            "failed to connect",
            "connection refused",
            "connection timed out",
            "network is unreachable",
            "could not connect",
        ),
    ),
)


@dataclass(frozen=True)
class Workspace:
    id: int
    person: int
    name: str
    state: str
    error: str | None
    server: bool  # a server is recorded: from its start until it is forgotten
    wanted: bool  # it should be running
    prefix: str | None  # the prefix it was last started with
    source: str | None  # the address it was cloned from, as given; None for an empty one
    log: str | None  # the steps rcman wrote while making it; cleared once it is ready
    environment: str | None = None  # the devcontainer.Plan it is made from, as JSON, if any
    # The Claude Code version its server started with, recorded from the start until the
    # server is stopped or forgotten; None while none is.
    claude_version: str | None = None
    # What the workspace may use (limits.py), beyond what its person's limits hold it to.
    limits: Limits = Limits()
    # The id of rcman's default image its container was made from, so that one made again is
    # made from it and not from a newer default; None for a workspace with a plan, whose plan
    # holds the id, and until the first container of an older row is made.
    image_id: str | None = None
    # The digest of the devcontainer.json it was made or last rebuilt from (devcontainer.digest);
    # None where it is not known.
    devcontainer_digest: str | None = None
    # The phase of a rebuild under way (REBUILD_PREPARING and the others), or None; the steps of
    # the last rebuild, and why it did not finish.
    rebuild_state: str | None = None
    rebuild_log: str | None = None
    rebuild_error: str | None = None
    # The kind of failure of a failed workspace, one of RECOVERABLE, or None for any other.
    failure: str | None = None

    @property
    def container(self) -> str:
        return container_name(self.id)


def _workspace(row: sqlite3.Row | None) -> Workspace | None:
    if row is None:
        return None
    return Workspace(
        row["id"],
        row["person"],
        row["name"],
        row["state"],
        row["error"],
        bool(row["server"]),
        bool(row["wanted"]),
        row["prefix"],
        row["source"],
        row["log"],
        row["environment"],
        row["claude_version"],
        Limits(row["memory_mib"], row["cpu_percent"], row["pids"]),
        row["image_id"],
        row["devcontainer_digest"],
        row["rebuild_state"],
        row["rebuild_log"],
        row["rebuild_error"],
        row["failure"],
    )


class HookFailed(RuntimeFailed):
    """A lifecycle command that failed (``kind`` hook_failed) or was stopped for taking too long
    (hook_timeout): its name, its exit code and the tail of what it printed, without the
    person's token."""

    def __init__(self, message: str, kind: str, command: str, code: int, said: str):
        super().__init__(message)
        self.kind, self.command, self.code, self.said = kind, command, code, said


class NotRebuilt(Exception):
    """A rebuild that cannot go on, with what the person is told."""


@dataclass(frozen=True)
class _Read:
    """What reading a workspace's devcontainer.json gave: the plan, None where there is no
    file, and the digest of the file (devcontainer.digest)."""

    plan: devcontainer.Plan | None
    digest: str


@dataclass(frozen=True)
class _Prepared:
    """What a rebuild makes the workspace from: ``plan``, as its container is made from it (a
    built image by its NEXT_TAG name), and ``stored``, as the row keeps it (by its BUILT_TAG
    name); or neither, and ``image_id``, the default image's; with the digest of the file and
    whether the image was built."""

    plan: devcontainer.Plan | None
    stored: devcontainer.Plan | None
    image_id: str | None
    digest: str
    built: bool


def image_is_gone(said: str) -> bool:
    """Whether what a failed pull said is the registry telling that the image is not there, or
    that it may not be had, and not a failure that may pass."""
    text = said.lower()
    return any(words in text for words in IMAGE_GONE_WORDS)


def name_from(source: str) -> str:
    """The name a workspace cloned from ``source`` is given when it is not named: the
    repository's, without ``.git``, anything but letters, digits and hyphens made a hyphen,
    what is not a letter or digit trimmed from its start, and cut to the length a name may
    be; ``DEFAULT_NAME`` for one nothing is left of."""
    repository = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    name = re.sub(r"^[^A-Za-z0-9]+", "", re.sub(r"[^A-Za-z0-9-]", "-", repository))
    return name[: rules.MAX_WORKSPACE_NAME_LENGTH] or DEFAULT_NAME


def source_page(source: str | None) -> str | None:
    """The GitHub page of the repository a workspace was cloned from: ``source`` without a
    trailing slash or ``.git``; None for an empty workspace, or an address that is not a GitHub
    repository's as ``rules.GITHUB_URL`` takes one."""
    if source is None or not rules.GITHUB_URL.fullmatch(source):
        return None
    return source.rstrip("/").removesuffix(".git")


def _numbered(name: str, number: int) -> str:
    """``name``, or for the second and later ``name-2``, ``name-3``, cut to leave room for
    the number within the length a name may be."""
    if number == 1:
        return name
    suffix = f"-{number}"
    return name[: rules.MAX_WORKSPACE_NAME_LENGTH - len(suffix)] + suffix


@contextlib.contextmanager
def answered():
    """The runtime's and the channel's failures as problems."""
    try:
        yield
    except RuntimeUnavailable:
        raise Problem("runtime_unavailable") from None
    except ChannelTimeout:
        raise Problem("timeout") from None
    except (RuntimeFailed, ChannelBroken, TmuxFailed) as error:
        log.warning("a workspace failed: %s", error)
        raise Problem("workspace_failed") from None


@contextlib.contextmanager
def limits_answered():
    """The runtime's failures to set or read limits as problems."""
    try:
        yield
    except RuntimeUnavailable:
        raise Problem("runtime_unavailable") from None
    except RuntimeFailed as error:
        log.warning("limits could not be applied: %s", error)
        raise Problem("limits_not_applied") from None


class Workspaces:
    def __init__(
        self,
        settings: Settings,
        runtime: Runtime,
        prunes: bool = True,
        *,
        builder_slot: int = people.BUILDER_SLOT,
    ):
        """``prunes`` is False in a process that is not the service (``rcman-admin``): the
        locks that keep a prune from removing a version a start pins are the service's own
        process's, so the service prunes, when a server stops, a version is rolled out and rcman
        starts. ``builder_slot`` is the slot of the uid range builds and the canary run in, the
        product's unless a test engine gives its own; people are given the slots below it."""
        self.settings = settings
        self.runtime = runtime
        self.builder_slot = builder_slot
        self.prunes = prunes
        # Held while images are collected and while a workspace is recorded, so that a workspace
        # is either seen by the collection or recorded after it: its image, pulled or in the
        # storage, is never removed between the two.
        self._images_lock = threading.RLock()
        self.restored = threading.Event()
        self._lock = threading.Lock()
        self._channels: dict[str, Channel] = {}
        self._locks: dict[int, threading.RLock] = {}
        self._person_locks: dict[int, threading.RLock] = {}
        # What reads or changes the limits on the slices, so that a check of the caps and the
        # use, the apply that follows and the write to the table are one step: a request
        # between them would judge, or overwrite, what is no longer so. It is held across
        # calls to the unit manager, each of which has a timeout, because the check is only
        # worth anything if nothing else sets a limit before the apply; the price is that
        # another request for limits waits, and nothing else does. Taken after a person's lock
        # and a workspace's, and nothing else is taken while it is held.
        # It is also held across processes, by a lock file beside the database: rcman-admin
        # changes limits in a process of its own, beside the service.
        self._limits_lock = FileLock(settings.database.parent / LOCK_FILE)
        self._making: dict[int, threading.Thread] = {}  # by workspace
        # The workspaces being made that a deletion has to stop: while git clones, an image is
        # pulled or a command runs. Each has the event the deletion sets; under _lock.
        self._working: dict[int, threading.Event] = {}
        # The threads of ``settle_left_over`` putting back a workspace left in the switch, by
        # workspace; under _lock.
        self._putting_back: dict[int, threading.Thread] = {}
        # The workspaces whose put-back failed and was said so, until it is done; under _lock.
        self._put_back_failing: set[int] = set()
        # The places for builds: a build holds one from the moment it starts until it is done.
        self._builds = threading.BoundedSemaphore(MAX_BUILDS)
        self._closing = threading.Event()
        self._restoring: threading.Thread | None = None
        # The workspaces whose server is being started, with the version recorded for it, which
        # is in use before any server runs it; under _lock.
        self._starting: set[int] = set()
        # Called with a person's id once a server of theirs may have stopped: accounts.py
        # finishes a sign-out that waits for their last running server.
        self.on_stopped: Callable[[int], None] = lambda person: None
        # Called with a person's id once they are being deleted: accounts.py ends a sign-in of
        # theirs that waits for its code.
        self.on_person_deleting: Callable[[int], None] = lambda person: None
        # Called with a workspace just before its container is removed: terminals.py ends its
        # terminal, through its channel, while the workspace is still there to end it in.
        self.on_removing: Callable[[Workspace], None] = lambda workspace: None
        # Called with a workspace whose container a rebuild is about to replace: terminals.py
        # ends its terminal, through its channel, while the old container is still there.
        self.on_rebuilding: Callable[[Workspace], None] = lambda workspace: None
        # Called with a workspace whose container kept running while rcman was not: terminals.py
        # ends what the terminals of an earlier rcman left in it.
        self.on_kept_running: Callable[[Workspace], None] = lambda workspace: None
        # Called, each in a step of its own, at every restore once the workspaces are back and
        # before what no row is for is removed: what a module that needs this one (and so
        # cannot be called from it) brings back at a start. gitaccount.py writes gh's files.
        self.restore_hooks: list[Callable[[], None]] = []
        # Whether an upgrade of rcman is requested or runs (rcman_upgrade.py), which the service
        # sets: no workspace is recorded meanwhile, nor a Claude Code version rolled out, as the
        # restart the upgrade makes would cut either short. Asked under the images' lock, which
        # the admission of an upgrade holds.
        self.upgrading: Callable[[], bool] = lambda: False
        self.versions = ClaudeVersions(settings, runtime, self)
        self.versions.prunes = prunes

    # --- the table ---------------------------------------------------------------------

    def _con(self) -> sqlite3.Connection:
        return db.connect(self.settings.database)

    def _set(self, workspace_id: int, **columns) -> None:
        names = list(columns)
        con = self._con()
        try:
            con.execute(
                f"update workspaces set {', '.join(f'{n} = ?' for n in names)} where id = ?",
                (*columns.values(), workspace_id),
            )
            con.commit()
        finally:
            con.close()

    def get(self, workspace_id: int) -> Workspace | None:
        con = self._con()
        try:
            row = con.execute("select * from workspaces where id = ?", (workspace_id,)).fetchone()
        finally:
            con.close()
        return _workspace(row)

    def everything(self) -> list[Workspace]:
        con = self._con()
        try:
            rows = con.execute("select * from workspaces order by id").fetchall()
        finally:
            con.close()
        return [_workspace(row) for row in rows]

    @contextlib.contextmanager
    def recording_held(self, wait: float):
        """The images' lock, which every workspace is recorded under, held for what is in the
        block, waiting at most ``wait`` seconds for it: whether it was had. The admission of an
        upgrade of rcman holds it, so that a workspace is recorded before it or refused after."""
        held = self._images_lock.acquire(timeout=wait)
        try:
            yield held
        finally:
            if held:
                self._images_lock.release()

    def any_creating(self) -> bool:
        """Whether a workspace is being made, which an upgrade of rcman would cut short."""
        return any(w.state == CREATING for w in self.everything())

    def any_rebuilding(self) -> bool:
        """Whether a workspace is being rebuilt, by its row: an upgrade of rcman would cut it
        short too. Not a switch that could not be put back and that no thread of this process
        works on, once the restore is done: the upgrade's restart puts it back as the periodic
        look (``settle_left_over``) would, and refusing the upgrade for it would wait for what
        the upgrade does itself."""
        restored = self.restored.is_set()
        return any(
            w.rebuild_state is not None and not (restored and self._left_in_switch(w))
            for w in self.everything()
        )

    def _left_in_switch(self, workspace: Workspace) -> bool:
        """Whether the workspace is a ready one in the switch that no thread of this process
        works on: one a rebuild could not put back, to be put back by ``settle_left_over`` or
        the restore."""
        return (
            workspace.state == READY
            and workspace.rebuild_state == REBUILD_SWITCHING
            and not self._worked_on(workspace.id)
        )

    def of_person(self, person: int) -> list[Workspace]:
        return [w for w in self.everything() if w.person == person]

    def owned(self, person: int, workspace_id: int) -> Workspace:
        """The person's workspace; another's is as if it were not there."""
        found = self.get(workspace_id)
        if found is None or found.person != person:
            raise Problem("workspace_not_found")
        return found

    def ready(self, person: int, workspace_id: int) -> Workspace:
        """The person's workspace, which must be ready, and not have its container replaced
        by a rebuild just now (``workspace_rebuilding``)."""
        found = self.owned(person, workspace_id)
        if found.state != READY:
            raise Problem("workspace_not_ready")
        if found.rebuild_state == REBUILD_SWITCHING:
            raise Problem("workspace_rebuilding")
        return found

    def lock(self, workspace_id: int) -> threading.RLock:
        with self._lock:
            return self._locks.setdefault(workspace_id, threading.RLock())

    def person_lock(self, person: int) -> threading.RLock:
        """The person's lock: a start, from its account check until its server runs,
        and everything that signs them in or out (accounts.py) take turns. Always taken
        before a workspace's lock, never while one is held."""
        with self._lock:
            return self._person_locks.setdefault(person, threading.RLock())

    def _stopped(self, person: int) -> None:
        """``on_stopped``, which never makes what called it fail: what it would finish
        is finished later by the periodic check, the Claude page or restore. A server that
        stopped may have been the last to use a Claude Code version, so the store is pruned."""
        try:
            self.on_stopped(person)
        except Exception:  # noqa: BLE001 - see above
            log.exception("finishing a sign-out of person %s failed; it is tried again", person)
        self.versions.prune_quietly()

    def _being_made(self, workspace_id: int) -> bool:
        with self._lock:
            thread = self._making.get(workspace_id)
        return thread is not None and thread.is_alive()

    # --- making and deleting ---------------------------------------------------------

    def create(
        self, person: people.Person, name: str | None, source: str | None = None
    ) -> Workspace:
        """Record the workspace and make it in the background; with ``source``, an address,
        by cloning it. Without a ``name`` it is named after the repository, numbered when
        the person has one of that name already; a name given that is taken is refused, and so
        is every one while an upgrade of rcman is under way (``rcman_upgrading``)."""
        base = name if name is not None else name_from(source)
        con = self._con()
        recording = self._images_lock  # released once the row is recorded
        recording.acquire()
        try:
            if self.upgrading():
                raise Problem("rcman_upgrading")
            for number in range(1, NAME_TRIES + 1):
                chosen = base if name is not None else _numbered(base, number)
                try:
                    # Only for a person still let in: none for one being deleted meanwhile.
                    cursor = con.execute(
                        "insert into workspaces (person, name, state, source) "
                        "select ?, ?, ?, ? "
                        "where exists (select 1 from people where id = ? and state = ?)",
                        (person.id, chosen, CREATING, source, person.id, people.ACTIVE),
                    )
                    con.commit()
                except sqlite3.IntegrityError:
                    if name is None:
                        continue  # the next number
                    raise Problem("workspace_name_taken") from None
                if cursor.rowcount == 0:
                    raise Problem("not_allowed")
                break
            else:
                raise Problem("workspace_name_taken")
        finally:
            con.close()
            recording.release()
        made = self.get(cursor.lastrowid)
        thread = threading.Thread(target=self._make, args=(made, person.slot), daemon=True)
        with self._lock:
            self._making = {k: t for k, t in self._making.items() if t.is_alive()}
            self._making[made.id] = thread
        thread.start()
        return made

    def _log(self, workspace_id: int, text: str, column: str = "log") -> None:
        """Add a step to the workspace's log (``column``: ``log`` for its making,
        ``rebuild_log`` for a rebuild), keeping at most the last LOG_MAX_CHARS characters, from
        the start of a line: the line the cut falls in is dropped whole, unless it is the only
        one."""
        assert column in ("log", "rebuild_log")
        con = self._con()
        try:
            cut = f"substr(whole, {-LOG_MAX_CHARS})"
            con.execute(
                f"update workspaces set {column} = (select case "
                f"when length(whole) <= {LOG_MAX_CHARS} then whole "
                f"when substr(whole, {-LOG_MAX_CHARS - 1}, 1) = char(10) then {cut} "
                f"when instr({cut}, char(10)) = 0 then {cut} "
                f"else substr({cut}, instr({cut}, char(10)) + 1) end "
                f"from (select coalesce({column} || char(10), '') || ? as whole)) where id = ?",
                (text, workspace_id),
            )
            con.commit()
        finally:
            con.close()

    def _make(self, workspace: Workspace, slot: int) -> None:
        """Make the workspace, and clone its source if it has one; one deleted meanwhile is
        removed once it is made."""
        refused, failure = False, None
        try:
            self._log(workspace.id, "Making the workspace")
            self._apply_stored(workspace.id)  # before its container is in the slice
            workspace = self._pinned(workspace)
            self.runtime.create(self._spec(workspace, slot))
            self._log(workspace.id, "Checking git")
            self._check_git(workspace)
            if workspace.source:
                self._clone(workspace)
                self._configure(workspace, slot)
            error = None
        except Exception as failed:  # noqa: BLE001 - whatever it is, the workspace failed
            # Redacted before it is cut, so that a token the cut would split is still found.
            error = self._redacted(workspace.person, str(failed) or type(failed).__name__)
            error = error[:MAX_ERROR_CHARS]
            log.warning("workspace %s could not be made: %s", workspace.id, error)
            if isinstance(failed, RuntimeUnavailable):  # what podman said holds paths
                error = DEVCONTAINER_MESSAGES["runtime_gone"]
            refused = isinstance(failed, devcontainer.Refused)
            failure = failed.kind if isinstance(failed, HookFailed) else None
        if refused:
            self._clear(workspace)
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is not None and now.state == DELETING:
                try:
                    self._remove(now)
                except Problem as refused:  # left `deleting`, for restore to finish
                    log.warning("workspace %s could not be removed: %s", now.id, refused.code)
                    return
                self._finish_deleting(now.person)
            elif error is None:
                self._set(workspace.id, state=READY, error=None, log=None)
            else:
                self._set(workspace.id, state=FAILED, error=error, failure=failure)

    def _clear(self, workspace: Workspace) -> None:
        """Remove the container and the volume of a workspace whose configuration or image was
        refused, and the image built for it, so that only its failed row is left; one that cannot
        be removed now is removed by its deletion."""
        self.close_channel(workspace.container)
        try:
            self.runtime.remove(workspace.container)
            self.runtime.remove_build(workspace.id, image=True)  # once nothing is made from it
            self.runtime.remove_build(workspace.id, image=True, image_tag=NEXT_TAG)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning("the refused workspace %s could not be cleared: %s", workspace.id, failed)

    @contextlib.contextmanager
    def _working_on(self, workspace_id: int):
        """While the workspace is being worked on in a way a deletion has to stop - git clones
        into it, an image is pulled or built, a command runs - ``delete`` sets the event this
        yields and removes its builder and its container by force. A deletion from another
        process, rcman-admin, which has none of this process's events, is found in the row by a
        thread that watches it meanwhile (``_watch``), and does the same. The work notices the
        event, or that the container is gone, and stops; the caller then finds the workspace
        deleting, or gone. The watching thread ends before this does."""
        cancel, done = threading.Event(), threading.Event()
        with self._lock:
            self._working[workspace_id] = cancel
        watcher = threading.Thread(
            target=self._watch,
            args=(workspace_id, cancel, done),
            name=f"{WATCHER_PREFIX}{workspace_id}",
            daemon=True,
        )
        watcher.start()
        try:
            yield cancel
        finally:
            done.set()
            watcher.join()
            with self._lock:
                self._working.pop(workspace_id, None)

    def _watch(self, workspace_id: int, cancel: threading.Event, done: threading.Event) -> None:
        """Every WORKING_POLL_SECONDS until ``done``: once the workspace's row is deleting or
        gone and no deletion in this process has stopped the work already, stop it
        (``_stop_working``), as a deletion here does. It looks under the workspace's lock, which
        a deletion here holds from marking the row until it has set the event, so that it never
        stops what that deletion stops; a lock that is held is tried again at the next look,
        never waited for, so that the work's end never waits for it."""
        lock = self.lock(workspace_id)
        while not done.wait(WORKING_POLL_SECONDS):
            if cancel.is_set():
                return  # a deletion in this process stopped it
            if not lock.acquire(blocking=False):
                continue
            try:
                if cancel.is_set():
                    return
                try:
                    now = self.get(workspace_id)
                except sqlite3.Error as unread:
                    log.warning("workspace %s could not be looked at: %s", workspace_id, unread)
                    continue
                if now is not None and now.state != DELETING:
                    continue
                self._stop_working(workspace_id)  # deleted by another process
                return
            finally:
                lock.release()

    def _cancelled(self, workspace_id: int, cancel: threading.Event) -> bool:
        if cancel.is_set():
            return True
        now = self.get(workspace_id)
        return now is None or now.state == DELETING

    def _clone(self, workspace: Workspace) -> None:
        """Clone the workspace's source into its work folder, under no lock at all, so that
        nothing waits for it - a sign-out, a start, the deletion of another workspace - and
        so that nothing running is touched; a failure is a RuntimeFailed saying why. A
        workspace deleted meanwhile has its container removed by the deletion (``delete``),
        which ends git; the caller then finds it deleting."""
        with self._working_on(workspace.id) as cancel:
            if self._cancelled(workspace.id, cancel):  # deleted before git could start
                return
            self._log(workspace.id, f"Cloning {workspace.source}")
            run, run_stdin = self._execs(workspace.container)
            layout = self.runtime.layout(workspace.container)
            try:
                Repository(layout, run, run_stdin).clone(workspace.source)
            except Exception as failed:  # noqa: BLE001 - whatever it is, the clone failed
                self._log(workspace.id, "The clone failed")
                raise RuntimeFailed(self._clone_failure(workspace, failed)) from None
            self._log(workspace.id, "Cloned")

    # --- a repository's devcontainer.json ----------------------------------------------

    def _plan(self, workspace: Workspace) -> devcontainer.Plan | None:
        """The plan the workspace is made from, or None for one made from rcman's own image;
        a stored plan that cannot be read is a RuntimeFailed, not a guess."""
        if not workspace.environment:
            return None
        try:
            return devcontainer.Plan.from_json(workspace.environment)
        except ValueError:
            raise RuntimeFailed(
                f"the plan workspace {workspace.id} is made from cannot be read"
            ) from None

    @staticmethod
    def _plan_layout(base: Layout, plan: devcontainer.Plan) -> Layout:
        """``base``, the runtime's layout, for a workspace made from ``plan``: its programs
        run as the plan's user, with its environment, and with its PATH after rcman's own
        folder. HOME stays the workspace's home."""
        env = {k: v for k, v in plan.env.items() if k != "PATH"}
        return base.for_plan(plan.user_name, env, plan.path)

    def _layout(self, workspace: Workspace) -> Layout:
        """Where the workspace's things are, as the programs run in it see them, with its
        plan applied."""
        base = self.runtime.layout(workspace.container)
        plan = self._plan(workspace)
        return base if plan is None else self._plan_layout(base, plan)

    def _spec(self, workspace: Workspace, slot: int, plan: devcontainer.Plan | None = None) -> Spec:
        """The container to make for the workspace: from the image of its plan, running as the
        plan's user, or rcman's own image."""
        plan = plan or self._plan(workspace)
        base = Spec(
            workspace.container, slot, workspace.id, workspace.person, install=self._install()
        )
        if plan is None:
            return dataclasses.replace(base, image_id=workspace.image_id or "")
        return dataclasses.replace(
            base,
            image=plan.image,
            image_id=plan.image_id,
            user=plan.run_as,
            uid=plan.uid,
            gid=plan.gid,
            groups=plan.groups,
            env=plan.container_env,
        )

    def _install(self) -> str:
        """The id of this database (``db.install_id``), put in the labels of what is made."""
        con = self._con()
        try:
            return db.install_id(con)
        finally:
            con.close()

    def _pinned(self, workspace: Workspace) -> Workspace:
        """The workspace with the id of the default image its container is made from: the one
        it has, or the default image's now, which is recorded, since a container lost later is
        made again from this image and not from whichever default is newest by then. A runtime
        with no default image leaves it as it is."""
        if workspace.image_id:
            return workspace
        found = self.runtime.default_image_id()
        if not found:
            return workspace
        self._set(workspace.id, image_id=found)
        return dataclasses.replace(workspace, image_id=found)

    def _configure(self, workspace: Workspace, slot: int) -> None:
        """Make the cloned workspace what its devcontainer.json says, if it has one: read and
        check the file, pull its image, make the container again from the image on the same
        volume, check git and libc in it, and run the create commands, which run once here and
        again only at a rebuild (``rebuild``). The digest of the file, or of there being none, is
        recorded. A file or image rcman refuses is a devcontainer.Refused, which fails the
        workspace and removes everything but its row; a failing command is a RuntimeFailed (a
        HookFailed), which keeps the container and volume; so are the instructions for Claude
        missing from the tools (InstructionsMissing), which are looked for before the container
        is discarded. A workspace deleted meanwhile is left to its deletion."""
        with self._working_on(workspace.id) as cancel:
            if self._cancelled(workspace.id, cancel):
                return
            name = workspace.container
            read = self._read_plan(workspace, cancel, lambda t: self._log(workspace.id, t))
            if read is None:
                return
            if read.plan is None:
                self._set(workspace.id, devcontainer_digest=read.digest)
                return
            plan = read.plan
            if self._cancelled(workspace.id, cancel):  # nothing is discarded for a deleted one
                return
            self._set(workspace.id, environment=plan.to_json(), devcontainer_digest=read.digest)
            missing = self.runtime.instructions_ready()  # before what cannot be made again goes
            if missing is not None:
                raise InstructionsMissing(missing)
            self._log(workspace.id, "Making the workspace from the image")
            self.close_channel(name)
            self.runtime.discard(name)
            if self._cancelled(workspace.id, cancel):
                return
            try:
                self.runtime.create(self._spec(workspace, slot, plan))
            except InstructionsMissing:
                raise  # the installation's, not the image's
            except RuntimeFailed as failed:
                if self._cancelled(workspace.id, cancel):
                    return
                said = str(failed).strip()[-MAX_SAID_CHARS:]
                raise devcontainer.Refused(
                    "image_unrunnable", image=plan.image, said=said
                ) from None
            if self._cancelled(workspace.id, cancel):
                return
            layout = self._plan_layout(self.runtime.layout(name), plan)
            self._check_libc(workspace, plan)
            self._log(workspace.id, "Checking git")
            try:
                self._check_git(workspace, layout)
            except RuntimeFailed as unusable:
                raise devcontainer.Refused(
                    "git_unusable", image=plan.image, said=str(unusable)
                ) from None
            self._run_commands(
                workspace,
                layout,
                plan.create_commands(),
                cancel,
                lambda t: self._log(workspace.id, t),
            )
            self._log(workspace.id, "Created")

    def _read_plan(
        self,
        workspace: Workspace,
        cancel: threading.Event,
        report: Callable[[str], None],
        tag: str = BUILT_TAG,
    ) -> _Read | None:
        """Read the workspace's devcontainer.json in its container, and the Dockerfile it builds
        from if it builds one, then pull the image it names or build the one it asks for, as
        ``tag`` (a rebuild's is NEXT_TAG), and make the plan; what is done is said to ``report``.
        The plan is None where there is no file; the digest is the file's, or NO_FILE_DIGEST.
        None when the workspace was deleted meanwhile."""
        name = workspace.container
        run, _ = self._execs(name)
        report(devcontainer.LOOKED_IN)
        layout = self.runtime.layout(name)
        found = devcontainer.read(run, layout)
        if found is None:
            report("There is no devcontainer.json")
            return _Read(None, devcontainer.NO_FILE_DIGEST)
        config = devcontainer.parse(found.text)
        checked = devcontainer.check(config, workspace.id, layout.work, found.path)
        dockerfile = devcontainer.dockerfile_of(checked, found.path)
        contents = None
        if dockerfile is not None:
            contents = devcontainer.read_path(
                run, layout, dockerfile, **devcontainer.DOCKERFILE_REFUSALS
            )
        digest = devcontainer.digest(found, contents)
        if checked.ignored:
            report(f"Ignored in devcontainer.json: {', '.join(checked.ignored)}")
        if checked.build is None:
            image = self._pulled(workspace, checked.image, cancel, report)
        else:
            image = self._built(workspace, checked.build, found.path, cancel, report, tag)
        if image is None or self._cancelled(workspace.id, cancel):
            return None
        try:
            facts = self.runtime.image_facts(image, cancel)
        except RuntimeFailed as failed:
            if self._cancelled(workspace.id, cancel):
                return None
            said = str(failed).strip()[-MAX_SAID_CHARS:]
            raise devcontainer.Refused("image_unreadable", image=image, said=said) from None
        if self._cancelled(workspace.id, cancel):
            return None
        for label in facts.labels:
            if label.startswith("rcman."):
                raise devcontainer.Refused(
                    "image_labels", image=image, label=devcontainer.shown(label)
                )
        refused = instructions.refused_at(facts.managed)
        if refused is not None:  # what rcman's instructions for Claude cannot be bound over
            raise devcontainer.Refused("image_managed_folder", image=image, path=refused)
        made = devcontainer.plan(
            config, facts, workspace.id, layout.work, image=image, size=self.runtime.id_range
        )
        return None if self._cancelled(workspace.id, cancel) else _Read(made, digest)

    def _pulled(
        self,
        workspace: Workspace,
        image: str,
        cancel: threading.Event,
        report: Callable[[str], None],
    ) -> str | None:
        """Pull the image the file names; None when the workspace was deleted meanwhile."""
        report(f"Pulling {image}")
        try:
            self.runtime.pull(image, report, cancel)
        except RuntimeFailed as failed:
            if self._cancelled(workspace.id, cancel):
                return None
            said = str(failed).strip()[-MAX_SAID_CHARS:]
            raise devcontainer.Refused(
                "pull_failed", image=image, said=said.rstrip(".") + "."
            ) from None
        return image

    def _built(
        self,
        workspace: Workspace,
        spec: devcontainer.BuildSpec,
        path: str,
        cancel,
        report: Callable[[str], None],
        tag: str = BUILT_TAG,
    ) -> str | None:
        """Build the image the file asks for, in a builder of its own, once there is a place for
        one more build, named with ``tag``; the name of the image that is kept, or None when the
        workspace was deleted meanwhile. The label of the image is held to the policy before it
        is copied out (``devcontainer.check_metadata``), a refusal naming the feature; any
        failure is a :class:`devcontainer.Refused` in plain words, and leaves nothing."""
        made = devcontainer.build_config(spec, path)
        request = BuildRequest(
            workspace.id,
            workspace.person,
            self.builder_slot,
            workspace.container,
            made.path,
            made.text,
            tag,
        )
        if not self._wait_for_build(workspace, cancel, report):
            return None
        try:
            report("Building the image")
            try:
                return self.runtime.build_image(
                    request,
                    lambda line: report(self._shown(workspace.person, line)),
                    cancel,
                    devcontainer.check_metadata,
                )
            except RuntimeFailed as failed:
                if self._cancelled(workspace.id, cancel):
                    return None
                raise self._build_refusal(workspace, failed) from None
        finally:
            self._builds.release()

    def _wait_for_build(
        self, workspace: Workspace, cancel: threading.Event, report: Callable[[str], None]
    ) -> bool:
        """Take a place for a build, saying to ``report`` that it waits when there is none free;
        False, with no place taken, when the workspace was deleted meanwhile."""
        if not self._builds.acquire(blocking=False):
            report(WAITING_FOR_BUILD)
            while not self._builds.acquire(timeout=BUILD_QUEUE_POLL_SECONDS):
                if self._cancelled(workspace.id, cancel):
                    return False
        if self._cancelled(workspace.id, cancel):
            self._builds.release()
            return False
        return True

    def _shown(self, person: int, text: str) -> str:
        """``text`` as the person is shown what a build said: without the control characters of
        a terminal, and without their GitHub token."""
        printable = "".join(c if c.isprintable() or c == "\n" else "?" for c in text)
        return self._redacted(person, printable)

    def _build_refusal(self, workspace: Workspace, failed: RuntimeFailed) -> devcontainer.Refused:
        """A failed build as the refusal a person is shown: the cause in plain words, the
        runtime's where it knows it and the one the end of what the builder said shows else, and
        that end, as much as fits."""
        tail = self._shown(workspace.person, getattr(failed, "tail", None) or str(failed))
        kind, fields = getattr(failed, "kind", None), dict(getattr(failed, "fields", {}))
        if kind is None:
            cause = builder.explain(tail) if isinstance(failed, BuildFailed) else None
            kind, fields = (cause.kind, dict(cause.fields)) if cause else ("build_failed", {})
        if "feature" in fields:
            fields["feature"] = devcontainer.shown(fields["feature"])
        head = DEVCONTAINER_MESSAGES[kind].format(tail="", **fields)
        room = MAX_ERROR_CHARS - len(head) - 1
        return devcontainer.Refused(kind, tail=self._fit(tail, room), **fields)

    def _check_libc(self, workspace: Workspace, plan: devcontainer.Plan) -> None:
        """Claude Code needs glibc: an image that has the musl loader and no glibc one, as
        Alpine's, is refused."""
        layout = self.runtime.layout(workspace.container)
        found = self.runtime.exec(workspace.container, [layout.shell, "-c", LIBC_PROBE], again=True)
        if found.out.decode("utf-8", "replace").strip() == "musl":
            raise devcontainer.Refused("musl", image=plan.image)

    @staticmethod
    def _hook_env(layout: Layout) -> dict[str, str]:
        """The only environment a lifecycle command is run with: the workspace's home and PATH
        and the plan's variables; and gh pointed at the person's git folder, as Claude's is
        (runtime.child_env), after them, so that no plan moves it."""
        return {
            "HOME": layout.home,
            "USER": layout.user,
            "LOGNAME": layout.user,
            "PATH": layout.path,
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            **layout.env,
            GH_CONFIG_DIR: gh_config_dir(layout),
        }

    def _command_shell(self, name: str, layout: Layout) -> str:
        """The shell that runs a command written as a string: the image's /bin/sh, else the
        mounted busybox's, since a slim image may have none."""
        found = self.runtime.exec(name, [layout.shell, "-c", SHELL_PROBE], again=True)
        return IMAGE_SHELL if found.out.strip() == b"image" else layout.shell

    def _run_commands(
        self,
        workspace: Workspace,
        layout: Layout,
        commands: tuple[devcontainer.Command, ...],
        cancel: threading.Event | None,
        report: Callable[[str], None],
    ) -> None:
        """Run the lifecycle commands one after another in the work folder, as the container's
        user, each under the ``timeout`` tool with HOOK_TIMEOUT_SECONDS, with exactly the
        environment ``_hook_env`` gives; what each printed, its tail and without the person's
        token, goes to ``report``. A command that fails or is stopped is a HookFailed with the
        exit code and the tail of its output; the commands after it do not run. A cancelled
        workspace is left alone."""
        if not commands:
            return
        name = workspace.container
        shell = None
        for command in commands:
            if cancel is not None and self._cancelled(workspace.id, cancel):
                return
            report(f"Running {command.name}")
            if command.argv is not None:
                program = list(command.argv)
            else:
                shell = shell or self._command_shell(name, layout)
                program = [shell, "-c", command.shell]
            argv = layout.with_env(
                self._hook_env(layout),
                [
                    *layout.tool("timeout"),
                    "-s",
                    "TERM",
                    "-k",
                    str(HOOK_KILL_GRACE_SECONDS),
                    str(HOOK_TIMEOUT_SECONDS),
                    *program,
                ],
            )
            wait = HOOK_TIMEOUT_SECONDS + HOOK_KILL_GRACE_SECONDS + ANSWER_MARGIN_SECONDS
            try:
                result = self.runtime.exec(
                    name, argv, timeout=wait, cwd=layout.work, keep=HOOK_KEEP_BYTES
                )
            except RuntimeFailed:
                if cancel is not None and self._cancelled(workspace.id, cancel):
                    return
                raise
            said = self._redacted(
                workspace.person, (result.out + result.err).decode("utf-8", "replace")
            )
            if cancel is not None and self._cancelled(workspace.id, cancel):
                return
            if result.code == 0:
                if said.strip():
                    report(said.strip()[-HOOK_LOG_TAIL_CHARS:])
                continue
            kind = "hook_timeout" if result.code in TIMED_OUT else "hook_failed"
            head = DEVCONTAINER_MESSAGES[kind].format(command=command.name, code=result.code)
            raise HookFailed(self._with_tail(head, said), kind, command.name, result.code, said)

    def _start_commands(self, workspace: Workspace, cancel: threading.Event | None = None) -> None:
        """Run the workspace's postStartCommand, if its plan has one, after its container was
        started or made again and before its server is: a failure is logged and nothing more."""
        try:
            plan = self._plan(workspace)
            if plan is None or not plan.start_commands():
                return
            layout = self._plan_layout(self.runtime.layout(workspace.container), plan)
            self._run_commands(
                workspace,
                layout,
                plan.start_commands(),
                cancel,
                lambda text: log.info("workspace %s: %s", workspace.id, text),
            )
        except Exception as failed:  # noqa: BLE001 - a start command never stops a restore
            log.warning("workspace %s: its start command failed: %s", workspace.id, failed)

    def _token(self, person: int) -> str | None:
        con = self._con()
        try:
            found = con.execute("select token from github_tokens where person = ?", (person,))
            row = found.fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def _redacted(self, person: int, text: str) -> str:
        """``text`` without the person's GitHub token, in case what said it echoed it."""
        token = self._token(person)
        return text.replace(token, REDACTED) if token else text

    def _clone_failure(self, workspace: Workspace, failed: Exception) -> str:
        """Why a clone failed, for the workspace's error, at most MAX_ERROR_CHARS: the
        cause in plain words when it is recognised, then the exit code and the tail of what
        git said, which is where it says why."""
        if isinstance(failed, Problem) and failed.code == "git_timeout":
            return CLONE_MESSAGES["timeout"]
        if not isinstance(failed, CloneFailed):
            said = failed.title if isinstance(failed, Problem) else str(failed)
            return self._redacted(workspace.person, said or type(failed).__name__)[:MAX_ERROR_CHARS]
        token = self._token(workspace.person)
        said = (failed.err + failed.out).decode("utf-8", "replace")
        if token:
            said = said.replace(token, REDACTED)
        cause = self._cause(said, token is not None)
        head = "\n".join(
            [
                *([CLONE_MESSAGES[cause]] if cause else []),
                f"git clone failed, exit code {failed.code}:",
            ]
        )
        return self._with_tail(head, said)

    @staticmethod
    def _with_tail(head: str, said: str) -> str:
        """``head`` and the tail of ``said``, the end of it where it says why, from the start
        of a line, within MAX_ERROR_CHARS."""
        return f"{head}\n{Workspaces._fit(said, MAX_ERROR_CHARS - len(head) - 1)}"

    @staticmethod
    def _fit(said: str, room: int) -> str:
        """The end of ``said`` within ``room`` characters, from the start of a line."""
        tail = said.strip()
        if len(tail) > room:
            tail = tail[-room:]
            if "\n" in tail:  # not from the middle of a line
                tail = tail.split("\n", 1)[1]
        return tail

    @staticmethod
    def _cause(said: str, token: bool) -> str | None:
        """The key in CLONE_MESSAGES of the cause git's words show, or None."""
        text = said.lower()
        for cause, patterns in CLONE_CAUSES:
            if any(pattern in text for pattern in patterns):
                if cause == "sign_in":
                    return "token_refused" if token else "private_no_token"
                return cause
        return None

    def _check_git(self, workspace: Workspace, layout: Layout | None = None) -> None:
        """The image must hold a git that rcman can use: one new enough, which the workspace
        is failed without otherwise."""
        run, run_stdin = self._execs(workspace.container)
        repository = Repository(layout or self.runtime.layout(workspace.container), run, run_stdin)
        try:
            found = repository.version()
        except Problem as refused:
            if refused.code == "git_not_installed":
                raise RuntimeFailed("git is not installed in the image") from None
            said = " ".join(refused.extra.get("git") or []) or refused.title
            raise RuntimeFailed(
                f"git in the image does not answer as rcman needs: {said}"
            ) from None
        if found < git_run.GIT_MIN_VERSION:
            raise RuntimeFailed(
                f"git {found[0]}.{found[1]} in the image is older than the "
                f"{git_run.GIT_MIN_VERSION[0]}.{git_run.GIT_MIN_VERSION[1]} rcman needs"
            )

    # --- rebuilding a workspace on its volume ------------------------------------------

    def rebuild_refusal(
        self, workspace: Workspace, containers: set[str] | None = None, asked: bool = False
    ) -> str | None:
        """Why the workspace may not be rebuilt now, as the code of the problem a request for it
        is answered with, or None where it may: the one test the route and the listing's
        ``rebuildable`` share. A ready workspace cloned from a repository, with its container,
        may be; so may a failed one whose failure is RECOVERABLE and whose volume is there. None
        may while rcman is upgraded, before its restore is done, or while anything else works
        on the workspace. What the row says is looked at first, and the runtime is asked only
        about a workspace that passes it: ``containers`` are the names of the containers there
        are, where the caller asked the runtime for them all at once; ``asked`` says that the
        runtime's part was found good already, and is not asked again."""
        why = self._rebuild_refusal_by_row(workspace)
        if why is not None or asked:
            return why
        if workspace.state == FAILED:
            there = self._there(self.runtime.has_volume, workspace.id)
            return None if there else "workspace_not_ready"
        if containers is not None:
            there = workspace.container in containers
        else:
            there = self._there(self.runtime.exists, workspace.container)
        return None if there else "rebuild_no_container"

    def _rebuild_refusal_by_row(self, workspace: Workspace) -> str | None:
        """``rebuild_refusal`` as far as the workspace's row and this process tell it, asking
        the runtime nothing."""
        if self.upgrading():
            return "rcman_upgrading"
        if not self.restored.is_set():
            return "rcman_starting"
        if workspace.state in (CREATING, DELETING):
            return "workspace_not_ready"
        with self._lock:
            working = workspace.id in self._working
        if workspace.rebuild_state is not None or working or self._being_made(workspace.id):
            return "workspace_rebuilding"
        if workspace.state == FAILED:
            if workspace.failure not in RECOVERABLE or not workspace.source:
                return "workspace_not_ready"
            return None
        if not workspace.source:
            return "rebuild_empty"
        return None

    def _containers(self) -> set[str]:
        """The names of the workspaces' containers there are, asked of the runtime at once; none
        where it cannot say."""
        try:
            return set(self.runtime.labelled())
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning("which containers are there could not be told: %s", failed)
            return set()

    @staticmethod
    def _there(ask: Callable, what) -> bool:
        """``ask(what)``, the runtime's answer whether something is there; one it cannot give is
        no."""
        try:
            return bool(ask(what))
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning("whether %s is there could not be told: %s", what, failed)
            return False

    def rebuild(self, person: people.Person, workspace_id: int, confirm: bool) -> Workspace:
        """Rebuild the person's workspace in the background: make its container again on the
        same volume from what its devcontainer.json in its work folder asks for now, or from
        rcman's current default image where it has none (``_replace``), or make a failed one that
        kept its volume ready again so (``_recover``). ``confirm`` says the person accepts that
        its server and terminal stop meanwhile. Refused as ``rebuild_refusal`` says; recorded
        under the images' lock, which the admission of an upgrade holds, so that an upgrade and
        a rebuild each see the other. The runtime is asked whether the container or the volume is
        there before either lock is taken, and the row looked at again under them."""
        found = self.owned(person.id, workspace_id)
        if not confirm:
            raise Problem("rebuild_confirm_required")
        why = self.rebuild_refusal(found)
        if why is not None:
            raise Problem(why)
        with self._images_lock, self.lock(found.id):
            now = self.owned(person.id, workspace_id)
            why = self.rebuild_refusal(now, asked=now.state == found.state)
            if why is not None:
                raise Problem(why)
            self._set(now.id, rebuild_state=REBUILD_PREPARING, rebuild_log=None, rebuild_error=None)
            now = self.get(now.id)
            thread = threading.Thread(target=self._rebuild, args=(now, person.slot), daemon=True)
            with self._lock:
                self._making = {k: t for k, t in self._making.items() if t.is_alive()}
                self._making[now.id] = thread
            thread.start()
        return now

    def _rebuild(self, workspace: Workspace, slot: int) -> None:
        """The rebuild's thread: what it ended in is one line of the journal. A workspace deleted
        meanwhile is removed once it ends, as one being made is."""
        try:
            outcome = "cancelled"
            try:
                if workspace.state == FAILED:
                    outcome = self._recover(workspace, slot)
                else:
                    outcome = self._replace(workspace, slot)
            except Exception as failed:  # noqa: BLE001 - whatever it is, the rebuild ends
                log.exception("the rebuild of workspace %s failed", workspace.id)
                outcome = f"failed: {type(failed).__name__}"
                self._rebuild_failed(workspace)
            log.info("workspace %s: %s", workspace.id, outcome)
        finally:
            self._rebuild_ended(workspace)

    def _rebuild_failed(self, workspace: Workspace) -> None:
        """Say where the person sees it that the rebuild did not finish, for a reason nothing
        foresaw: a failed workspace keeps its error and the kind of its failure, and its rebuild's
        steps say so; a ready one whose container was being replaced is left in the switch, to
        be put back by the service's periodic look or the next restore (``_left_to_put_back``),
        since what its container is now is not known; another has it as its rebuild's error."""
        words = MESSAGES["workspace_failed"]
        with contextlib.suppress(sqlite3.Error):
            now = self.get(workspace.id)
            if now is None or now.state == DELETING:
                return
            if now.state == FAILED:
                self._log(now.id, words, "rebuild_log")
            elif now.rebuild_state == REBUILD_SWITCHING:
                self._left_to_put_back(now.id, words)
            else:
                self._set(now.id, rebuild_error=words)

    def _left_to_put_back(self, workspace_id: int, cause: str) -> None:
        """Leave a ready workspace whose container could not be put back in the switch, which
        refuses its server, git and terminal and is what the service's periodic look
        (``settle_left_over``), or the next restore, puts back (``_restore_rebuild``): no server
        runs in it meanwhile, though it should run again once it is put back; its rebuild's
        error, and the last of its steps, which the person sees while it is in the switch, say
        why and that it is put back as soon as rcman can."""
        head = REBUILD_MESSAGES["rollback_failed"].split("{cause}")[0]
        error = head + self._fit(cause, MAX_ERROR_CHARS - len(head))
        self._set(workspace_id, rebuild_error=error, server=0)
        self._log(workspace_id, error, "rebuild_log")

    def _rebuild_ended(self, workspace: Workspace) -> None:
        """Clear the phase of a rebuild that ended - not the last one, which is the restore's or
        the service's to finish (``settle_left_over``), nor the switch of a ready workspace that
        was not put back, which is the restore's -; finish deleting a workspace deleted
        meanwhile; and remove the images nothing refers to any more, the old one among them. A
        deletion while the images are removed finds the thread still at work and leaves the
        workspace to it: so the thread, as it ends, stops counting as making it and looks again,
        under the workspace's lock, after which a deletion removes it itself."""
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is not None and now.state == DELETING:
                self._remove_deleted(now)
            elif now is not None and (
                now.rebuild_state == REBUILD_PREPARING
                or (now.rebuild_state == REBUILD_SWITCHING and now.state == FAILED)
            ):
                self._set(now.id, rebuild_state=None)
        self._stopped(workspace.person)
        self._collect_images_quietly()
        with self.lock(workspace.id):
            with self._lock:
                if self._making.get(workspace.id) is threading.current_thread():
                    del self._making[workspace.id]
            now = self.get(workspace.id)
            removed = now is not None and now.state == DELETING and self._remove_deleted(now)
        if removed:
            self._stopped(workspace.person)
            self._collect_images_quietly()

    def _remove_deleted(self, workspace: Workspace) -> bool:
        """Remove a workspace whose deletion was left to what worked on it, under its lock, and
        the person with it if they are being deleted and it was their last: whether it was. One
        that cannot be removed now is left deleting, for the next look or restore to finish."""
        try:
            self._remove(workspace)
        except Problem as refused:
            log.warning("workspace %s could not be removed: %s", workspace.id, refused.code)
            return False
        self._finish_deleting(workspace.person)
        return True

    def _reporter(self, workspace_id: int) -> Callable[[str], None]:
        return lambda text: self._log(workspace_id, text, "rebuild_log")

    def _replace(self, workspace: Workspace, slot: int) -> str:
        """Rebuild a ready workspace: (preparing) read its devcontainer.json in its container,
        pull or build its image as NEXT_TAG and make the new plan, while the old container runs;
        a refusal or a failure here, the instructions for Claude missing from the tools among
        them, changes nothing. (switching) Stop its server, end its
        terminal and discard the container, then make the new one on the same volume, check it
        and run every create command and then postStartCommand; a failure from the discard on,
        whatever it is, puts the old one back (``_roll_back``). (promoting) Store the new plan,
        then move the built image's name. The server, if it ran, is started again. No lock is
        held through anything slow."""
        report = self._reporter(workspace.id)
        with self._working_on(workspace.id) as cancel:
            if self._cancelled(workspace.id, cancel):
                return "cancelled"
            try:
                prepared = self._prepare(workspace, cancel, report, advise=rebuild_advice)
                missing = self.runtime.instructions_ready()  # the old one stays running
                if prepared is not None and missing is not None:
                    raise NotRebuilt(missing)
            except NotRebuilt as refused:
                self._discard_next(workspace.id)
                self._set(workspace.id, rebuild_error=str(refused), rebuild_state=None)
                return f"refused: {str(refused).splitlines()[0]}"
            if prepared is None:
                return "cancelled"
            switched = self._switch(workspace, cancel)
            if switched is None:
                return "cancelled"
            was_running, prefix = switched
            try:
                self.runtime.discard(workspace.container)
                made = self._make_new(workspace, slot, prepared, cancel, report)
            except Exception as failed:  # noqa: BLE001 - whatever it is, the old one is put back
                if self._cancelled(workspace.id, cancel):
                    return "cancelled"
                cause = self._cause_of(workspace, failed, rebuild_advice)
                return self._roll_back(workspace, slot, cancel, report, cause, was_running, prefix)
            if not made or not self._commit(workspace, prepared, cancel):
                return "cancelled"
        if was_running:
            self._start_again(workspace.id, prefix)
        return "rebuilt"

    def _prepare(
        self,
        workspace: Workspace,
        cancel: threading.Event,
        report: Callable[[str], None],
        advise: Callable[[str], str] = lambda words: words,
    ) -> _Prepared | None:
        """What the workspace is to be made from now: its file read in its container, and the
        image it names pulled, or built as NEXT_TAG; rcman's current default image where it has
        no file. None when the workspace was deleted meanwhile; a refusal or a failure is a
        NotRebuilt in the words a person is shown, ``advise`` applied to a refusal's."""
        try:
            read = self._read_plan(workspace, cancel, report, NEXT_TAG)
        except Exception as failed:  # noqa: BLE001 - whatever it is, nothing is rebuilt
            if self._cancelled(workspace.id, cancel):
                return None
            raise NotRebuilt(self._cause_of(workspace, failed, advise)) from None
        if read is None:
            return None
        if read.plan is None:
            default = self.runtime.default_image_id()
            if not default:
                raise NotRebuilt(DEVCONTAINER_MESSAGES["default_image_gone"])
            report("Making the workspace from rcman's own image")
            return _Prepared(None, None, default, read.digest, False)
        plan = read.plan
        built = workspace_of_built_name(plan.image) is not None
        stored = dataclasses.replace(plan, image=built_name(workspace.id)) if built else plan
        return _Prepared(plan, stored, None, read.digest, built)

    def _cause_of(
        self, workspace: Workspace, failed: Exception, advise: Callable[[str], str]
    ) -> str:
        """Why a step of a rebuild failed, in the words a person is shown, at most
        MAX_ERROR_CHARS: a refusal's (with ``advise`` applied), a command's name and exit code
        and the end of what it printed, and nothing of the repository's files else."""
        if isinstance(failed, NotRebuilt):
            return str(failed)
        if isinstance(failed, devcontainer.Refused):
            return advise(str(failed))[:MAX_ERROR_CHARS]
        if isinstance(failed, HookFailed):
            head = REBUILD_MESSAGES[failed.kind].format(command=failed.command, code=failed.code)
            return self._with_tail(head, failed.said)
        if isinstance(failed, RuntimeUnavailable):
            return MESSAGES["runtime_unavailable"]
        said = self._redacted(workspace.person, str(failed) or type(failed).__name__)
        return said[:MAX_ERROR_CHARS]

    def _switch(self, workspace: Workspace, cancel: threading.Event) -> tuple[bool, str] | None:
        """The start of the switch, short and under the workspace's lock: the phase is written,
        from which its server and git are refused; its server, if it runs, is stopped, with no
        change to whether it should run, and the version it ran is no longer recorded. Then,
        without the lock, its terminal is ended and its channel closed; the caller discards the
        container. Whether its server ran, and the prefix it is started again with; None when
        the workspace was deleted meanwhile."""
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is None or now.state == DELETING or cancel.is_set():
                return None
            self._set(now.id, rebuild_state=REBUILD_SWITCHING)
            was_running = False
            try:
                server = self.server(now)
                if server.tmux.exists() and server.tmux.exit_status() is None:
                    was_running = True
                    server.stop()
            except Exception as failed:  # noqa: BLE001 - discarding the container ends it
                log.warning("the server of workspace %s did not stop: %s", now.id, failed)
            self._set(now.id, claude_version=None)
        self._stopped(now.person)
        try:
            self.on_rebuilding(now)
        except Exception:  # noqa: BLE001 - discarding the container ends whatever is left
            log.exception("the terminal of workspace %s did not end", now.id)
        self.close_channel(now.container)
        return was_running, now.prefix or now.name

    def _made_for(self, workspace_id: int, cancel: threading.Event) -> bool:
        """After a container was made: whether its workspace is still there to go on with. One
        deleted meanwhile has its container and volume removed here, under its lock, as its
        deletion does, since the deletion may have looked for them before they were made."""
        with self.lock(workspace_id):
            now = self.get(workspace_id)
            if now is not None and now.state != DELETING and not cancel.is_set():
                return True
            try:
                self.runtime.remove(container_name(workspace_id))
            except (RuntimeFailed, RuntimeUnavailable) as failed:
                log.warning("workspace %s could not be removed: %s", workspace_id, failed)
            return False

    def _make_new(
        self,
        workspace: Workspace,
        slot: int,
        prepared: _Prepared,
        cancel: threading.Event,
        report: Callable[[str], None],
    ) -> bool:
        """Make the workspace's container from what it was prepared to be made from, on its
        volume, and check and set it up as making a workspace does: libc and git, then every
        create command and postStartCommand. A refusal or a failure is raised; False when the
        workspace was deleted meanwhile."""
        plan = prepared.plan
        target = dataclasses.replace(
            workspace,
            environment=plan.to_json() if plan is not None else None,
            image_id=prepared.image_id,
        )
        if self._cancelled(workspace.id, cancel):
            return False
        self._apply_stored(workspace.id)  # before its container is in the slice
        report("Making the workspace from the image")
        try:
            self.runtime.create(self._spec(target, slot, plan))
        except InstructionsMissing:
            raise  # the installation's, not the image's
        except RuntimeFailed as failed:
            if self._cancelled(workspace.id, cancel):
                return False
            said = str(failed).strip()[-MAX_SAID_CHARS:]
            image = plan.image if plan is not None else "rcman's own image"
            raise devcontainer.Refused("image_unrunnable", image=image, said=said) from None
        if not self._made_for(workspace.id, cancel):
            return False
        base = self.runtime.layout(workspace.container)
        layout = base if plan is None else self._plan_layout(base, plan)
        if plan is not None:
            self._check_libc(target, plan)
        report("Checking git")
        try:
            self._check_git(target, layout)
        except RuntimeFailed as unusable:
            if plan is None:
                raise
            raise devcontainer.Refused(
                "git_unusable", image=plan.image, said=str(unusable)
            ) from None
        if plan is not None:
            self._run_commands(target, layout, plan.create_commands(), cancel, report)
        return not self._cancelled(workspace.id, cancel)

    def _commit(
        self,
        workspace: Workspace,
        prepared: _Prepared,
        cancel: threading.Event,
        recovering: bool = False,
    ) -> bool:
        """The new container is good: store, in one write under the workspace's lock, its plan
        (by the built image's BUILT_TAG name), the id of the default image it is made from where
        it has no plan, the file's digest and the last phase; a recovered workspace is ready
        again. Then move the built image's name, and end the rebuild. False when the workspace
        was deleted meanwhile."""
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is None or now.state == DELETING or cancel.is_set():
                return False
            columns = {
                "environment": prepared.stored.to_json() if prepared.stored else None,
                "image_id": prepared.image_id,
                "devcontainer_digest": prepared.digest,
                "rebuild_state": REBUILD_PROMOTING,
            }
            if recovering:
                columns.update(state=READY, error=None, failure=None, log=None)
            self._set(workspace.id, **columns)
        if self._promote(self.get(workspace.id) or workspace):
            self._set(workspace.id, rebuild_state=None, rebuild_log=None, rebuild_error=None)
        self.close_channel(workspace.container)
        return True

    def _promote(self, workspace: Workspace) -> bool:
        """Move the name of the image built for the workspace onto the one a rebuild built, where
        its stored plan is of a built image; else remove the image built for it before, which
        nothing is made from any more. Again after a promotion that ended part way, it finishes
        it. Whether it was done: a failure is logged, and the caller leaves the row in the last
        phase, for the next restore to finish."""
        try:
            plan = self._plan(workspace)
            if plan is not None and workspace_of_built_name(plan.image) is not None:
                self.runtime.promote_build(workspace.id)
            else:
                self.runtime.remove_build(workspace.id, image=True)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning("the image of workspace %s could not be renamed: %s", workspace.id, failed)
            return False
        return True

    def _discard_next(self, workspace_id: int) -> None:
        """Remove the image a rebuild built for the workspace, by its NEXT_TAG name only."""
        try:
            self.runtime.remove_build(workspace_id, image=True, image_tag=NEXT_TAG)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning(
                "the image rebuilt for workspace %s was not removed: %s", workspace_id, failed
            )

    def _start_again(self, workspace_id: int, prefix: str) -> None:
        """Start the server that ran before the rebuild in the new container, with the prefix it
        had, holding no lock: one that does not start is logged, and the restore at rcman's next
        start starts it, as it should run."""
        now = self.get(workspace_id)
        if now is None or now.state != READY:
            return
        try:
            self.start_server(now, prefix)
        except Problem as refused:
            log.warning("the server of workspace %s did not start again: %s", now.id, refused.code)

    def _roll_back(
        self,
        old: Workspace,
        slot: int,
        cancel: threading.Event,
        report: Callable[[str], None],
        cause: str,
        was_running: bool,
        prefix: str,
    ) -> str:
        """Put the workspace back as it was before a rebuild whose new container failed: discard
        that container, make one again from the stored plan or the default image the workspace
        was made from, on the same volume, run the old create commands and postStartCommand
        again, and start the server again if it ran. What the new commands wrote to the volume
        stays. One that cannot be put back is left in the switch, with no container
        (``_left_to_put_back``), which the next restore makes again from the stored plan, with
        its create commands. Why goes to the rebuild's error. The instructions for Claude were
        there when the rebuild looked, before the switch; were they to go since, the old
        container could not be made again here either (InstructionsMissing), and it is left in
        the switch so."""
        report("Putting the workspace back as it was")
        name = old.container
        self.close_channel(name)
        try:
            self.runtime.discard(name)
            if self._cancelled(old.id, cancel):
                return "cancelled"
            self._apply_stored(old.id)
            self.runtime.create(self._spec(old, slot))
            if not self._made_for(old.id, cancel):
                return "cancelled"
            error = REBUILD_MESSAGES["rolled_back"]
            plan = self._plan(old)
            if plan is not None:
                layout = self._plan_layout(self.runtime.layout(name), plan)
                try:
                    self._run_commands(old, layout, plan.create_commands(), cancel, report)
                except RuntimeFailed as failed:
                    log.warning("workspace %s: an old create command failed: %s", old.id, failed)
        except Exception as failed:  # noqa: BLE001 - whatever it is, it is put back later
            if self._cancelled(old.id, cancel):
                return "cancelled"
            log.warning("workspace %s could not be put back: %s", old.id, failed)
            with contextlib.suppress(RuntimeFailed, RuntimeUnavailable):
                self.runtime.discard(name)
            if self._cancelled(old.id, cancel):
                return "cancelled"
            self._discard_next(old.id)
            self.close_channel(name)
            self._left_to_put_back(old.id, cause)
            return f"rollback failed: {cause.splitlines()[0] if cause else ''}"
        if self._cancelled(old.id, cancel):
            return "cancelled"
        head = error.split("{cause}")[0]
        self._discard_next(old.id)
        self.close_channel(name)
        self._set(
            old.id,
            rebuild_error=head + self._fit(cause, MAX_ERROR_CHARS - len(head)),
            rebuild_state=None,
        )
        if was_running:
            self._start_again(old.id, prefix)
        return f"rolled back: {cause.splitlines()[0] if cause else ''}"

    def _recover(self, workspace: Workspace, slot: int) -> str:
        """Rebuild a failed workspace that kept its volume: read its file through its container
        - the one there is, started if it stopped, or one made from rcman's current default
        image on the volume where there is none - then make the container as ``_replace`` does,
        and make the workspace ready. A failure keeps it failed, with why and what kind of
        failure it was, and keeps its volume, so that it can be rebuilt again; its container is
        kept only after a create command failed, for a look, as at its making, or where the
        instructions for Claude are missing from the tools, which is looked at before it is
        discarded: its error is then INSTRUCTIONS_MISSING in place of the one it had, and the
        kind of its failure is kept."""
        report = self._reporter(workspace.id)
        name = workspace.container
        with self._working_on(workspace.id) as cancel:
            if self._cancelled(workspace.id, cancel):
                return "cancelled"
            try:
                if not self._reading_container(workspace, slot, cancel):
                    return "cancelled"
                prepared = self._prepare(workspace, cancel, report)
                if prepared is None:
                    return "cancelled"
                missing = self.runtime.instructions_ready()  # the container it has stays
                if missing is not None:
                    raise InstructionsMissing(missing)
                with self.lock(workspace.id):
                    now = self.get(workspace.id)
                    if now is None or now.state == DELETING or cancel.is_set():
                        return "cancelled"
                    self._set(workspace.id, rebuild_state=REBUILD_SWITCHING)
                self.close_channel(name)
                self.runtime.discard(name)
                if not self._make_new(workspace, slot, prepared, cancel, report):
                    return "cancelled"
            except Exception as failed:  # noqa: BLE001 - whatever it is, it stays failed
                if self._cancelled(workspace.id, cancel):
                    return "cancelled"
                if not isinstance(failed, (HookFailed, InstructionsMissing)):
                    self.close_channel(name)
                    with contextlib.suppress(RuntimeFailed, RuntimeUnavailable):
                        self.runtime.discard(name)
                self._discard_next(workspace.id)
                cause = self._cause_of(workspace, failed, lambda words: words)
                kind = failed.kind if isinstance(failed, HookFailed) else workspace.failure
                with self.lock(workspace.id):
                    now = self.get(workspace.id)
                    if now is not None and now.state != DELETING:
                        self._set(workspace.id, error=cause, failure=kind, rebuild_state=None)
                return f"not recovered: {cause.splitlines()[0]}"
            if not self._commit(workspace, prepared, cancel, recovering=True):
                return "cancelled"
        return "rebuilt"

    def _reading_container(self, workspace: Workspace, slot: int, cancel: threading.Event) -> bool:
        """A container of the failed workspace on its volume to read its file through: the one it
        has, started if it stopped, or one made from rcman's current default image, which the
        row does not record. False when the workspace was deleted meanwhile."""
        name = workspace.container
        if self.runtime.exists(name):
            if not self.runtime.running(name):
                self.runtime.start(name)
            return True
        default = self.runtime.default_image_id()
        if not default:
            raise NotRebuilt(DEVCONTAINER_MESSAGES["default_image_gone"])
        self._apply_stored(workspace.id)
        reader = dataclasses.replace(workspace, environment=None, image_id=default)
        self.runtime.create(self._spec(reader, slot))
        return self._made_for(workspace.id, cancel)

    def devcontainer_changed(self, person: int, workspace_id: int) -> bool | None:
        """Whether the devcontainer.json in the person's workspace, or the Dockerfile it builds
        from, is not what the workspace was made or last rebuilt from: read through its channel,
        as making it reads them. None where that is not known: a workspace that is not ready,
        is being rebuilt or worked on, has no container, or has no digest recorded, none of
        which is asked anything; and a file that cannot be read, or that rcman refuses."""
        found = self.owned(person, workspace_id)
        if found.state != READY or found.rebuild_state is not None or not found.source:
            return None
        if not found.devcontainer_digest or self._being_made(found.id):
            return None
        with self._lock:
            if found.id in self._working:
                return None
        try:
            if not self.runtime.exists(found.container):
                return None
            run = self._channel_runner(found.container)
            now = devcontainer.current_digest(run, self.runtime.layout(found.container), found.id)
        except (
            devcontainer.Refused,
            RuntimeFailed,
            RuntimeUnavailable,
            ChannelBroken,
            ChannelTimeout,
        ) as unread:
            log.info("the devcontainer.json of workspace %s was not read: %s", found.id, unread)
            return None
        return now != found.devcontainer_digest

    def _channel_runner(self, name: str) -> Callable[..., Result]:
        """The workspace's channel as devcontainer.read's runner, which may name the folder a
        program runs in: a fixed script that changes to it first, given the folder and the argv
        as its arguments."""
        channel = self._channel(name)
        layout = self.runtime.layout(name)

        def run(argv: list[str], timeout: float, cwd: str | None = None) -> Result:
            if cwd is None:
                return channel.run(argv, timeout)
            return channel.run(
                [layout.shell, "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *argv], timeout
            )

        return run

    def delete(self, workspace: Workspace) -> None:
        """Delete the workspace: its container and volume, its server with them, and its
        row. One still being made, or rebuilt, goes once what makes it stops: here, by a thread
        of this process; in rcman-admin, which does not prune and has none, by the service's,
        which a rebuild in the row stands for - its thread, or, where none works on it any more,
        the service's periodic look (``settle_left_over``)."""
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is None:
                return
            self._set(workspace.id, state=DELETING)
            self._stop_working(now.id)  # what is being done to it stops: making, restoring
            elsewhere = now.rebuild_state is not None and not self.prunes
            if now.state == CREATING or self._being_made(now.id) or elsewhere:
                return
            self._remove(now)
        self._stopped(workspace.person)
        self._collect_images_quietly()

    def _stop_working(self, workspace_id: int) -> None:
        """End what is being done to the workspace that a deletion has to stop - git cloning
        into it, an image being pulled, a command running - by telling it to stop and removing
        the container by force, which kills what runs in it: cancelling is deleting the
        workspace. The workspace's thread then finds it deleting and finishes removing it. One
        whose container cannot be removed now is removed when the work ends, at the latest at
        its timeout."""
        with self._lock:
            cancel = self._working.get(workspace_id)
        if cancel is None:
            return
        cancel.set()
        for remove in (
            lambda: self.runtime.remove_build(workspace_id),  # the builder: it ends a build
            lambda: self.runtime.remove(container_name(workspace_id)),
        ):
            try:
                remove()
            except (RuntimeFailed, RuntimeUnavailable) as failed:
                log.warning(
                    "the work on workspace %s could not be stopped: %s", workspace_id, failed
                )

    def _remove(self, workspace: Workspace) -> None:
        try:
            self.on_removing(workspace)
        except Exception:  # noqa: BLE001 - the container's removal ends whatever is left
            log.exception("the terminal of workspace %s did not end", workspace.id)
        self.close_channel(workspace.container)
        with answered():
            self.runtime.remove(workspace.container)
            self.runtime.remove_build(workspace.id, image=True)
            self.runtime.remove_build(workspace.id, image=True, image_tag=NEXT_TAG)
        self._drop_limits(workspace.person, workspace.id)
        con = self._con()
        try:
            con.execute("delete from workspaces where id = ?", (workspace.id,))
            con.commit()
        finally:
            con.close()

    def delete_person(self, person: people.Person) -> None:
        """Delete a person: let them in nowhere, delete every workspace of theirs, then
        their row and settings, and write the allowed list again."""
        con = self._con()
        try:
            people.mark_deleting(con, person)
            people.write_allowed(con, self.settings.allowed_emails)
        finally:
            con.close()
        self._person_deleting(person.id)  # a sign-in that waits for its code ends
        theirs = self.of_person(person.id)
        for workspace in theirs:
            self.delete(workspace)
        self._wait_while_made(
            {w.id for w in theirs if w.state == CREATING or w.rebuild_state is not None}
        )
        if self.of_person(person.id):  # left: the work ends, or rcman's next start, finishes it
            raise Problem("workspace_failed", title=PERSON_DELETION_LEFT)
        with self.person_lock(person.id):  # not while a sign-in or a start of theirs is under way
            self._person_deleting(person.id)
            self._drop_limits(person.id, None)  # slots are given again: not to the next person
            self._remove_account(person.id)
            con = self._con()
            try:
                people.remove(con, person)
                people.write_allowed(con, self.settings.allowed_emails)
            finally:
                con.close()
        self._collect_images_quietly()  # what only their workspaces referred to

    def _wait_while_made(self, workspace_ids: set[int]) -> None:
        """Wait, for up to MADE_WITHIN_SECONDS, until the workspaces of ``workspace_ids``, which
        were being made when they were deleted, are gone: their deletion is finished by what
        makes them, once the step it is on ends - a thread of this process, or of the service's
        when this is rcman-admin, whose threads this process cannot see, and so is found by the
        rows. One whose making in this process is over is not waited for: its removal failed,
        and rcman's next start finishes it."""
        deadline = time.monotonic() + MADE_WITHIN_SECONDS
        waiting = set(workspace_ids)
        while True:
            with self._lock:
                making = dict(self._making)
            waiting = {
                i
                for i in waiting
                if self.get(i) is not None and (i not in making or making[i].is_alive())
            }
            if not waiting or time.monotonic() >= deadline:
                return
            if self._closing.wait(WORKING_POLL_SECONDS):
                return

    def _person_deleting(self, person_id: int) -> None:
        """``on_person_deleting``, which never makes the deletion fail."""
        try:
            self.on_person_deleting(person_id)
        except Exception:  # noqa: BLE001 - the deletion goes on whatever this does
            log.warning("a sign-in of person %s could not be ended", person_id, exc_info=True)

    def _remove_account(self, person_id: int) -> None:
        """Remove the person's sign-in before their row: slots are given again, so
        a file left behind would sign the next person in. One that cannot be removed
        leaves the row, for rcman's next start."""
        try:
            with answered():
                self.runtime.remove_account(person_id)
        except Problem:
            log.warning("the sign-in of person %s could not be removed", person_id)
            raise

    # --- limits ------------------------------------------------------------------------

    def set_limits(self, person: people.Person, workspace_id: int | None, held: Limits) -> None:
        """Set the limits on the person's workspaces together, or on the person's workspace
        ``workspace_id``: the whole set, a limit left out taken away. They are applied to the
        slice first and recorded only once applied, so that a set that could not be applied
        changes nothing. A memory or process limit lower than the one there is, and lower than
        what is in use, is refused (``limit_below_use``): the kernel would end work that is
        running, or a process limit would stop new commands in the workspace, rcman's own among
        them. Raising or taking away a limit, and any change of the CPU, are always allowed."""
        with self.person_lock(person.id):
            if workspace_id is None:
                self._set_limits(person, None, held)
                return
            found = self.owned(person.id, workspace_id)
            with self.lock(found.id):
                self._set_limits(person, found.id, held)

    def _set_limits(self, person: people.Person, workspace_id: int | None, held: Limits) -> None:
        with self._limits_lock:
            if workspace_id is None:
                con = self._con()
                try:
                    now = people.by_id(con, person.id)
                finally:
                    con.close()
                if now is None or now.state != people.ACTIVE:
                    raise Problem("person_not_found")
            else:
                now = self.get(workspace_id)
                if now is None or now.person != person.id or now.state == DELETING:
                    raise Problem("workspace_not_found")
            lowers = self._refuse_below_use(person.id, workspace_id, held)
            if lowers:
                # Applied first and recorded after: a crash between leaves the table above
                # the slice, which the restore raises the slice to.
                with limits_answered():
                    self.runtime.apply_limits(person.id, workspace_id, held)
                self._record_limits(person.id, workspace_id, held)
                return
            # A raise is recorded first and applied after: a crash between leaves the slice
            # below the table, never the table below the slice, which the restore would apply
            # to the slice as a lowering. One that cannot be applied is taken back.
            before = now.limits
            self._record_limits(person.id, workspace_id, held)
            try:
                with limits_answered():
                    self.runtime.apply_limits(person.id, workspace_id, held)
            except Exception:
                self._record_limits(person.id, workspace_id, before)
                raise

    def _record_limits(self, person_id: int, workspace_id: int | None, held: Limits) -> None:
        if workspace_id is None:
            con = self._con()
            try:
                people.set_limits(con, person_id, held)
            finally:
                con.close()
        else:
            self._set(
                workspace_id,
                memory_mib=held.memory_mib,
                cpu_percent=held.cpu_percent,
                pids=held.pids,
            )

    def _refuse_below_use(self, person_id: int, workspace_id: int | None, held: Limits) -> bool:
        """Refuse a memory or process limit that is lower than the cap the slice has now and
        lower than what is in use. The cap is the slice's own, read from the unit manager, not
        what the table holds: they differ after a restore that could not apply a limit, or a
        set that was applied and not recorded, and a table's value sent again with a change
        of the CPU would then be a cap below the use. A limit equal to the cap, or higher,
        lowers nothing. The caps and the use are read only for a limit that is given; the
        use is read only for one that would lower a cap, and one the kernel does not count is
        refused (``usage_unknown``), as nothing says it is safe. Whether the set lowers a cap
        of the slice's (memory or processes) is returned; one that does not only raises."""
        if held.memory_mib is None and held.pids is None:
            return False
        with limits_answered():
            caps = self.runtime.caps(person_id, workspace_id)
        memory = held.memory_mib is not None and limits.lower_than(
            held.memory_mib * limits.MEBIBYTE, caps.memory_bytes
        )
        pids = held.pids is not None and limits.lower_than(held.pids, caps.tasks)
        if not (memory or pids):
            return False
        with limits_answered():
            used = self.runtime.usage(person_id, workspace_id)
        if (memory and not used.memory_known) or (pids and not used.tasks_known):
            raise Problem("usage_unknown")
        if memory and limits.memory_below(held.memory_mib, used):
            raise Problem(
                "limit_below_use",
                title=(
                    f"{used.memory_text()} of memory is in use now, so the limit cannot be set "
                    "below it: that would end work that is running. Set a higher limit, or "
                    "stop what uses it first."
                ),
                limit="memory",
                used=used.memory_text(),
            )
        if pids and limits.pids_below(held.pids, used):
            raise Problem(
                "limit_below_use",
                title=(
                    f"{used.tasks} processes are in use now, so the limit cannot be set below "
                    "that: new commands in the workspace would fail. Set a higher limit, or "
                    "stop what uses them first."
                ),
                limit="pids",
                used=str(used.tasks),
            )
        return True

    def limits_report(self, person: people.Person) -> dict:
        """The person's limits and use, and each workspace's, as the API shows them: the use
        is None where it could not be read, or where the report ran out of time (at most
        LIMITS_REPORT_SECONDS in all), and a figure the kernel does not count is None. The
        person is read again: their limits are the
        table's."""
        con = self._con()
        try:
            person = people.by_id(con, person.id) or person
        finally:
            con.close()

        deadline = time.monotonic() + LIMITS_REPORT_SECONDS

        def used(workspace_id: int | None) -> dict | None:
            if time.monotonic() > deadline:
                return None  # no time left for this one: not read
            try:
                now = self.runtime.usage(person.id, workspace_id)
            except (RuntimeFailed, RuntimeUnavailable) as failed:
                log.warning("what is in use could not be read: %s", failed)
                return None
            return {
                "memory": now.memory_text() if now.memory_known else None,
                "memory_bytes": now.memory_bytes if now.memory_known else None,
                "tasks": now.tasks if now.tasks_known else None,
            }

        return {
            "person": {"limits": person.limits.text(), "usage": used(None)},
            "workspaces": [
                {
                    "id": w.id,
                    "name": w.name,
                    "state": w.state,
                    "limits": w.limits.text(),
                    "usage": used(w.id),
                }
                for w in self.of_person(person.id)
            ],
        }

    def _apply_stored(self, workspace_id: int | None = None, person_id: int | None = None) -> None:
        """Apply the set the table holds for the workspace, or for the person, to the slice: an
        empty set resets it. The unit manager holds limits only until the machine restarts, so the
        slice is set before a container is made in it and when rcman starts, whether or not
        there are limits: otherwise a slice a failed drop left would keep its old caps. The
        caps of a slice that is not lowered below what is in use are ``_not_below_use``'s.
        A set with limits that cannot be applied is the caller's failure; a reset that cannot
        be is logged, and is tried again when rcman next starts."""
        with self._limits_lock:
            if workspace_id is not None:
                now = self.get(workspace_id)
                if now is None:
                    return
                owner, workspace, held = now.person, now.id, now.limits
            else:
                con = self._con()
                try:
                    found = people.by_id(con, person_id)
                finally:
                    con.close()
                if found is None:
                    return
                owner, workspace, held = found.id, None, found.limits
            try:
                self.runtime.apply_limits(
                    owner, workspace, self._not_below_use(owner, workspace, held)
                )
            except (RuntimeFailed, RuntimeUnavailable) as failed:
                if held.limited:
                    raise
                log.warning(
                    "the slice of person %s workspace %s could not be reset: %s",
                    owner,
                    workspace,
                    failed,
                )

    def _not_below_use(self, person_id: int, workspace_id: int | None, held: Limits) -> Limits:
        """``held``, but for a memory or process limit that would lower the cap the slice has
        now below what is in use, which stays at the slice's cap: the judgement of an edit
        (``_refuse_below_use``), by the slice's own caps and not the table's, which the slice
        can be above (a raise that was applied and never recorded) and a restart of rcman must
        not end work that is running. A use the kernel does not count is no safe use, as it is
        for an edit. A slice whose caps or use cannot be read is given ``held``, and the apply
        says what is wrong. The table is left as it is: a later edit starts from the slice."""
        if held.memory_mib is None and held.pids is None:
            return held
        try:
            caps = self.runtime.caps(person_id, workspace_id)
            memory = held.memory_mib is not None and limits.lower_than(
                held.memory_mib * limits.MEBIBYTE, caps.memory_bytes
            )
            pids = held.pids is not None and limits.lower_than(held.pids, caps.tasks)
            if not (memory or pids):
                return held
            used = self.runtime.usage(person_id, workspace_id)
        except (RuntimeFailed, RuntimeUnavailable):
            return held
        memory_kept = memory and (
            not used.memory_known or limits.memory_below(held.memory_mib, used)
        )
        pids_kept = pids and (not used.tasks_known or limits.pids_below(held.pids, used))
        if not (memory_kept or pids_kept):
            return held
        log.warning(
            "the %s of person %s workspace %s is held at what its slice has, as what is in use "
            "is more than the stored limit",
            " and ".join(
                n for n, kept in (("memory", memory_kept), ("processes", pids_kept)) if kept
            ),
            person_id,
            workspace_id,
        )
        return dataclasses.replace(
            held,
            memory_mib=(
                None if caps.memory_bytes is None else -(-caps.memory_bytes // limits.MEBIBYTE)
            )
            if memory_kept
            else held.memory_mib,
            pids=caps.tasks if pids_kept else held.pids,
        )

    def _restore_limits(self) -> None:
        """Set the table's limits on every slice there is a row for, before any container is
        started; a row with none resets its slice. One that cannot be set is logged, and the
        rest go on."""
        con = self._con()
        try:
            everyone = people.everyone(con)
        finally:
            con.close()
        for person in everyone:
            self._step(self._apply_stored, None, person.id)
        for workspace in self.everything():
            if workspace.state != DELETING:
                self._step(self._apply_stored, workspace.id)

    def _drop_limits(self, person_id: int, workspace_id: int | None) -> None:
        """Take the limits off a slice whose person or workspace is gone, whether or not it
        had any, so that none lingers. Never fails the deletion; the unit manager forgets them at
        the next restart in any case."""
        try:
            with self._limits_lock:
                self.runtime.drop_limits(person_id, workspace_id)
        except (RuntimeFailed, RuntimeUnavailable) as failed:
            log.warning(
                "the limits of person %s workspace %s could not be dropped: %s",
                person_id,
                workspace_id,
                failed,
            )

    # --- channels and servers ------------------------------------------------------------

    def _channel(self, name: str) -> Channel:
        with self._lock:
            channel = self._channels.get(name)
            if channel is None:
                channel = self._channels[name] = self.runtime.channel(name)
            return channel

    def close_channel(self, name: str) -> None:
        with self._lock:
            channel = self._channels.pop(name, None)
        if channel is not None:
            channel.close()

    def close_channels(self) -> None:
        for name in self.open_channels():
            self.close_channel(name)

    def open_channels(self) -> list[str]:
        with self._lock:
            return sorted(self._channels)

    def close_idle(self) -> None:
        with self._lock:
            idle = [n for n, c in self._channels.items() if c.idle_seconds() > CHANNEL_IDLE_SECONDS]
        for name in idle:
            self.close_channel(name)

    def server(self, workspace: Workspace, version: str | None = None) -> Server:
        """The workspace's server; with ``version``, one that runs that exact version of
        Claude Code. Without one it reads, and runs nothing of Claude's but what a read needs,
        through the store's `current`."""
        name = workspace.container
        channel = self._channel(name)
        layout = self._layout(workspace)
        return Server(
            layout if version is None else layout.pinned(version), channel.run, workspace.id
        )

    def layout_of(self, workspace: Workspace) -> Layout:
        """Where the workspace's things are, as the programs run in it see them, with its plan
        applied."""
        return self._layout(workspace)

    def run_in(self, workspace: Workspace) -> Callable[..., Result]:
        """The ``run`` of the workspace's channel, opened as it is first used."""
        return self._channel(workspace.container).run

    def _execs(self, name: str):
        """The runtime's exec as a repository's runners: without and with stdin."""

        def run(argv: list[str], timeout: float, cwd: str | None = None):
            if cwd is None:
                return self.runtime.exec(name, argv, timeout=timeout)
            return self.runtime.exec(name, argv, timeout=timeout, cwd=cwd)

        def run_stdin(argv: list[str], stdin: bytes, timeout: float):
            return self.runtime.exec(name, argv, stdin, timeout=timeout)

        return run, run_stdin

    def repository(self, workspace: Workspace) -> Repository:
        """The git repository at the root of the workspace's work folder, run through its
        channel, and through ``exec`` for what carries stdin. Nothing is started for it."""
        name = workspace.container
        return Repository(self._layout(workspace), self._channel(name).run, self._execs(name)[1])

    def server_status(self, workspace: Workspace, probe: bool = False) -> dict | None:
        """The workspace's server, or None; a workspace that records none is not reached,
        unless ``probe``: then tmux in the workspace is asked, whatever the row says."""
        if not workspace.server and not probe:
            return None
        with answered():
            return self.server(workspace).status()

    def listing(self, workspaces: list[Workspace]) -> list[dict]:
        """Each workspace as the API shows it, its server read side by side with the others',
        but for one whose container a rebuild replaces just now, which is not reached; with its
        rebuild, and whether one may be asked for now (``rebuild_refusal``, as the route asks)."""

        def shown(workspace: Workspace) -> dict:
            entry = {
                "id": workspace.id,
                "name": workspace.name,
                "state": workspace.state,
                "error": workspace.error,
                "source": workspace.source,
                "source_page": source_page(workspace.source),
                "log": workspace.log if workspace.state != READY else None,
                "server": None,
                "server_error": None,
                "claude_version": None,
                "rebuild": {
                    "state": workspace.rebuild_state,
                    "log": workspace.rebuild_log,
                    "error": workspace.rebuild_error,
                },
                "rebuildable": self.rebuild_refusal(workspace, containers) is None,
            }
            if workspace.state == READY and workspace.rebuild_state == REBUILD_SWITCHING:
                code = "workspace_rebuilding"
                entry["server_error"] = {"code": code, "title": MESSAGES[code]}
            elif workspace.state == READY:
                try:
                    entry["server"] = self.server_status(workspace)
                except Problem as problem:
                    entry["server_error"] = {"code": problem.code, "title": problem.title}
                if entry["server"] is not None or entry["server_error"] is not None:
                    entry["claude_version"] = workspace.claude_version
            return entry

        if not workspaces:
            return []
        # The runtime is asked once for every container, and only where a ready workspace may be
        # rebuilt as far as its row says.
        asking = any(
            w.state == READY and self._rebuild_refusal_by_row(w) is None for w in workspaces
        )
        containers = self._containers() if asking else set()
        with ThreadPoolExecutor(max_workers=min(len(workspaces), LISTING_WORKERS)) as pool:
            return list(pool.map(shown, workspaces))

    def start_server(self, workspace: Workspace, prefix: str, resume: bool = False) -> dict:
        """Start the workspace's server, or resume it. It runs the approved Claude Code
        version, which is recorded on the workspace's row before it runs, and stays recorded
        until the server is stopped or forgotten (or the start leaves it not running); a
        server that runs already is refused and its record is left as it is. A workspace that is
        no longer ready by the time the lock is had, one deleted meanwhile among them, is refused
        (``workspace_not_ready``), and so is one whose container a rebuild has begun to replace
        (``_not_switching``): nothing is started in it."""
        with self.person_lock(workspace.person), self.lock(workspace.id), answered():
            now = self.get(workspace.id)
            if now is None or now.state != READY:
                raise Problem("workspace_not_ready")
            self._not_switching(now.id)
            con = self._con()
            try:
                account, signing_out = people.claude_account(con, workspace.person)
            finally:
                con.close()
            if account is None:
                raise Problem("claude_not_logged_in")
            if signing_out:
                raise Problem("claude_signing_out")
            server = self.server(workspace)
            live = server.tmux.exists() and server.tmux.exit_status() is None
            if not live:
                self._seed(workspace, account)
            pinned = False
            try:
                if not live:
                    pinned = True  # before the pin, which may fail having recorded it
                    server = self.server(workspace, self._pin(workspace))
                answer = server.resume(prefix) if resume else server.start(prefix)
            finally:
                self._started(workspace, server, pinned)
            self._set(workspace.id, wanted=1, prefix=prefix)
            return answer

    def _pin(self, workspace: Workspace) -> str:
        """The approved version, which is in the store, recorded as the one the workspace's
        server starts with: under the lock a prune holds, so it is not removed meanwhile, and
        in use from now until the start is over (``_started``)."""
        with self.versions.lock:
            version = self.versions.resolve()
            with self._lock:
                self._starting.add(workspace.id)
            self._set(workspace.id, claude_version=version)
        return version

    def _started(self, workspace: Workspace, server: Server, pinned: bool) -> None:
        """What a start leaves in the row: whether a server is recorded, and, for a start that
        pinned a version, that version only if the server runs. A server that is not
        running, or whose state cannot be read, is never recorded as running a version wrongly:
        one that cannot be read keeps the record, since it may run."""
        try:
            exists = server.tmux.exists()
            columns: dict = {"server": int(exists)}
            if pinned:
                try:
                    running = exists and server.tmux.exit_status() is None
                except Exception:  # noqa: BLE001 - unreadable: it may run, so it keeps its record
                    running = True
                if not running:
                    columns["claude_version"] = None
            self._set(workspace.id, **columns)
        finally:
            with self._lock:
                self._starting.discard(workspace.id)

    def _seed(self, workspace: Workspace, account: dict) -> None:
        """Put the account the sign-in named into the workspace's ``.claude.json``, owner-
        only, keeping every other key, before its server starts: Claude's Remote Control
        needs it, and the credentials file alone does not hold it."""
        name = workspace.container
        layout = self.runtime.layout(name)
        path = f"{layout.config}/.claude.json"
        found = self.runtime.exec(name, [*layout.tool("cat"), path], again=True)
        config: dict = {}
        if found.code == 0 and found.out.strip():
            try:
                config = json.loads(found.out)
            except ValueError:
                config = None
            if not isinstance(config, dict):
                log.warning("workspace %s: its .claude.json is not an object", workspace.id)
                raise Problem("workspace_failed")
        elif found.code != 0 and b"No such file" not in found.err:
            raise RuntimeFailed(found.err.decode("utf-8", "replace").strip())
        text = json.dumps({**config, "oauthAccount": account.get("oauthAccount")})
        cat, mv = " ".join(layout.tool("cat")), " ".join(layout.tool("mv"))
        script = f'umask 077 && {cat} > "$1.rcman" && {mv} -f "$1.rcman" "$1"'
        written = self.runtime.exec(
            name, [layout.shell, "-c", script, "sh", path], stdin=text.encode()
        )
        if written.code != 0:
            raise RuntimeFailed(written.err.decode("utf-8", "replace").strip())

    def _not_switching(self, workspace_id: int) -> None:
        """Refuse what reaches the workspace's server once a rebuild has begun to replace its
        container (``workspace_rebuilding``): called under the workspace's lock, which the
        rebuild writes the switch under, by a request that found the workspace ready before."""
        now = self.get(workspace_id)
        if now is not None and now.rebuild_state == REBUILD_SWITCHING:
            raise Problem("workspace_rebuilding")

    def stop_server(self, workspace: Workspace) -> dict:
        with self.lock(workspace.id), answered():
            self._not_switching(workspace.id)
            self._set(workspace.id, wanted=0)
            answer = self.server(workspace).stop()
            self._set(workspace.id, claude_version=None)
        self._stopped(workspace.person)
        return answer

    def forget_server(self, workspace: Workspace) -> dict:
        with self.lock(workspace.id), answered():
            self._not_switching(workspace.id)
            answer = self.server(workspace).forget()
            self._set(workspace.id, server=0, wanted=0, claude_version=None)
        self._stopped(workspace.person)
        return answer

    def pinned_servers(self, versions: set[str] | None = None) -> list[tuple[Workspace, str]]:
        """The workspaces with a Claude Code version recorded whose server runs, cannot be read
        or is being started, each with that version; with ``versions``, only those among them.
        A server that cannot be read counts as running, so no version it may run is lost, and
        the row's ``server`` flag is not believed: the record of a version and the flag are two
        writes, and only tmux saying there is no server frees the version."""
        with self._lock:
            starting = set(self._starting)
        found = []
        for workspace in self.everything():
            version = workspace.claude_version
            if version is None or (versions is not None and version not in versions):
                continue
            if workspace.id in starting or workspace.rebuild_state == REBUILD_SWITCHING:
                found.append((workspace, version))  # not reached while its container is replaced
                continue
            if workspace.state != READY:
                continue
            try:
                status = self.server_status(workspace, probe=True)
            except Problem:
                found.append((workspace, version))
                continue
            if status is not None and status.get("exit_code") is None:
                found.append((workspace, version))
        return found

    def recorded_versions(self) -> dict[int, str]:
        """The Claude Code version recorded on each workspace's row that has one, by id: read
        from the table alone, so it is quick."""
        return {w.id: w.claude_version for w in self.everything() if w.claude_version}

    def versions_in_use(self, versions: set[str]) -> set[str]:
        """Those of ``versions`` that a server runs, may run, or is being started with."""
        return {version for _, version in self.pinned_servers(versions)}

    def emails(self) -> dict[int, str]:
        """Each person's address, by their id."""
        con = self._con()
        try:
            return {p.id: p.email for p in people.everyone(con)}
        finally:
            con.close()

    def running(self, person: int) -> list[Workspace]:
        """The person's workspaces whose server runs; one that cannot be read counts as
        running, so nothing waiting for them all to stop goes on too soon."""
        found = []
        for workspace in self.of_person(person):
            if workspace.state != READY or not workspace.server:
                continue
            if workspace.rebuild_state == REBUILD_SWITCHING:
                found.append(workspace)  # not reached while its container is replaced
                continue
            try:
                status = self.server_status(workspace)
            except Problem:
                found.append(workspace)
                continue
            if status is not None and status.get("exit_code") is None:
                found.append(workspace)
        return found

    def stop_running(self, person: int) -> None:
        """Stop every running server of the person's, under their lock, so no start slips
        in meanwhile; one that does not stop is logged."""
        with self.person_lock(person):
            for workspace in self.running(person):
                if workspace.rebuild_state == REBUILD_SWITCHING:
                    # The rebuild has stopped it, and its start afterwards is refused.
                    log.warning("the server of workspace %s is being rebuilt", workspace.id)
                    continue
                try:
                    self.stop_server(workspace)
                except Problem as refused:
                    log.warning(
                        "the server of workspace %s did not stop: %s", workspace.id, refused.code
                    )

    def confirm(self, workspace: Workspace, answer: str) -> dict:
        with self.lock(workspace.id), answered():
            self._not_switching(workspace.id)
            return self.server(workspace).confirm(answer)

    def output(self, workspace: Workspace) -> dict:
        with answered():
            return self.server(workspace).output()

    # --- bringing the runtime back to the table ----------------------------------------

    def restore(self) -> None:
        """Wait for the runtime, then: finish what was being made or deleted, make again
        or start what should exist, start the servers that should run, run the restore
        hooks, remove the workspaces no row is for, and finish deleting the people whose
        deletion an earlier rcman began. A step that fails is logged, and the rest go on."""
        try:
            said = False
            while not self._closing.is_set():
                try:
                    self.runtime.wait_ready(RUNTIME_WAIT_SECONDS)
                    break
                except RuntimeUnavailable as error:
                    if not said:  # once, not on every try
                        log.warning("rcman is waiting for its runtime: %s", error)
                        said = True
            if self._closing.is_set():
                return
            self._step(self.runtime.clean_probes)
            self._step(self.versions.sweep)
            self._step(self._restore_limits)  # the unit manager forgot them; before any start
            self._restore_all()
            if self._closing.is_set():
                return
            for hook in self.restore_hooks:
                self._step(hook)
            if self._table_is_empty_of_workspaces_there_are():
                pass  # nothing is removed, or pruned, on the word of a table that is empty
            else:
                for name, labels in self.runtime.labelled().items():
                    self._step(self._remove_orphan, name, labels)
                self._step(self._remove_orphan_builds)
                self._step(self._collect_images)
                self._step(self.versions.prune)
            self._step(self._finish_deleting_people)
            self._step(self.settle_signing_out)
        except Exception:  # noqa: BLE001 - restore never stops rcman from answering
            log.exception("restoring the workspaces failed")
        finally:
            self.restored.set()

    def _restore_all(self) -> None:
        """Bring each workspace back, up to RESTORE_WORKERS at once, each in a thread of its own
        with its server after it, so that one that is slow - an image to make a container from,
        a start command that hangs - does not keep another from being brought back, though it
        holds one of the places until it is done, and what comes after (the removal of what no
        row is for, and the rest) waits for the last. The threads are daemons and look at
        ``_closing`` between steps; this returns once they are done, or as soon as rcman is
        closing. Nothing is brought back, and nothing discarded or made again, while the
        instructions for Claude are missing from the tools: the journal says so."""
        missing = self.runtime.instructions_ready()
        if missing is not None:
            log.error("no workspace was brought back: %s", missing)
            return
        free = threading.BoundedSemaphore(RESTORE_WORKERS)
        threads = []
        for workspace in self.everything():
            while not free.acquire(timeout=RESTORE_POLL_SECONDS):
                if self._closing.is_set():
                    return
            if self._closing.is_set():
                return
            thread = threading.Thread(
                target=self._restore_task, args=(workspace, free), daemon=True
            )
            thread.start()
            threads.append(thread)
        while any(t.is_alive() for t in threads):
            if self._closing.is_set():
                return
            time.sleep(RESTORE_POLL_SECONDS)

    def _restore_task(
        self,
        workspace: Workspace,
        free: threading.BoundedSemaphore | None = None,
        only_switching: bool = False,
    ) -> None:
        """One workspace's restore: its container, its start command, then its server if it
        should be running and is out of any switch. With ``only_switching`` (``settle_left_over``)
        nothing is done unless it is still a ready workspace in the switch, and what fails is
        raised; else it is logged (``_step``)."""
        step = (lambda call, *args: call(*args)) if only_switching else self._step
        try:
            step(self._restore_workspace, workspace, only_switching)
            now = self.get(workspace.id)
            if now is not None and now.state == READY and now.wanted and not now.rebuild_state:
                if not self._closing.is_set():
                    step(self._restore_server, now)
        finally:
            if free is not None:
                free.release()

    @staticmethod
    def _step(call, *args) -> None:
        try:
            call(*args)
        except Exception:  # noqa: BLE001 - one workspace never stops the others
            log.exception("restoring %s failed", args)

    def _table_is_empty_of_workspaces_there_are(self) -> bool:
        """Whether the runtime has workspaces and the table has none: a database that was lost
        or is not the one they were made for. Restore then removes nothing - not the orphans,
        not the builds and images no plan names, not the Claude Code versions no workspace
        uses - since every one of them would be taken for unused; the administrator decides.
        Said loudly, once per restore."""
        there = self.runtime.labelled()
        if not there or self.everything():
            return False
        log.error(
            "the workspaces table has no workspaces but the runtime has %d (%s); "
            "nothing is removed - check that this is the database these workspaces belong to",
            len(there),
            ", ".join(sorted(there)),
        )
        return True

    def _remove_orphan(self, name: str, labels: dict) -> None:
        """Discard the container of a workspace no row is for, checked again just before: a
        request may have made its row since restore read the table. Its volume stays, with
        whatever work is in it: ids are never used again, so nothing adopts it, and removing it
        is for a person to decide."""
        try:
            workspace_id = int(labels.get(LABEL_WORKSPACE, ""))
        except ValueError:
            workspace_id = None
        if workspace_id is not None:
            with self.lock(workspace_id):
                if self.get(workspace_id) is not None or self._being_made(workspace_id):
                    return
                self.runtime.discard(name)
                with contextlib.suppress(ValueError):  # the slice it was in goes with it
                    self._drop_limits(int(labels.get(LABEL_PERSON, "")), workspace_id)
        elif name not in {w.container for w in self.everything()}:
            self.runtime.discard(name)

    def _collect_images(self) -> None:
        """Remove the images in the runtime's storage that no workspace refers to: pulled
        images and old default images that nothing stored names, and what a load that was
        killed left. Kept: what a stored plan names (its image and its id), the id of the
        default image each workspace was made from, the image built for each workspace, and
        what the runtime itself keeps (the current default image and any made after it, the
        builder's). An image a container is made from stays whatever is said here, since it is
        never removed by force.
        Nothing is collected while a workspace is being made, whose image may be pulled and not
        yet in a plan, or when a stored plan cannot be read: what is kept is then not known."""
        if not self.prunes:
            return  # the service collects, at its next opportunity
        with self._images_lock:
            self._collect_unreferenced()

    def _collect_unreferenced(self) -> None:
        everything = self.everything()
        me = threading.current_thread()  # a rebuild collects when it is done
        with self._lock:
            making = any(t.is_alive() and t is not me for t in self._making.values())
        if making or any(w.state == CREATING for w in everything):
            return
        ids: set[str] = set()
        names: set[str] = set()
        for workspace in everything:
            names.add(built_name(workspace.id))
            names.add(built_name(workspace.id, NEXT_TAG))  # a rebuild's, until it is moved
            if workspace.image_id:
                ids.add(workspace.image_id)
            try:
                plan = self._plan(workspace)
            except RuntimeFailed:
                return
            if plan is not None:
                names.add(plan.image)
                if plan.image_id:
                    ids.add(plan.image_id)
        default = self.runtime.default_image_id()
        if default:
            ids.add(default)
        for removed in self.runtime.collect_images(ids, names):
            log.info("the image %s was removed: nothing refers to it", removed)

    def _collect_images_quietly(self) -> None:
        """``_collect_images``, which never makes what called it fail: what it would remove is
        removed by the next one."""
        try:
            self._collect_images()
        except Exception:  # noqa: BLE001 - see above
            log.warning("the images nothing refers to could not be collected", exc_info=True)

    def _remove_orphan_builds(self) -> None:
        """Remove what builds left that no workspace needs: the builder and volume of every
        workspace but those being made, and every built image that no stored plan names, those
        of workspaces that have none among them. A workspace whose plan cannot be read keeps the
        image named for it, since what it is made from is not known."""
        used = set()
        for workspace in self.everything():
            try:
                plan = self._plan(workspace)
            except RuntimeFailed:
                used.add(built_name(workspace.id))
                continue
            if plan is not None:
                used.add(plan.image)
        for workspace_id in self.runtime.builders():
            if not self._being_made(workspace_id):
                self._step(self.runtime.remove_build, workspace_id)
        for workspace_id, name in self.runtime.built_images().items():
            if name not in used and not self._being_made(workspace_id):
                self._step(self.runtime.remove_build, workspace_id, True)
        for workspace_id in self.runtime.next_images():  # what a rebuild cut short left
            if not self._being_made(workspace_id):
                self._step(self._discard_next, workspace_id)

    def _restore_workspace(self, workspace: Workspace, only_switching: bool = False) -> None:
        """Bring one workspace back as its row is now; one a request is making meanwhile is that
        request's. What is quick - the row's state, finishing a deletion, starting a container
        that stopped - is done under the workspace's lock. What can be slow - pulling the image
        and making the container again, the start command - is done without it, so that
        nothing waits for it; a deletion meanwhile sets the event that stops it
        (``_working_on``). With ``only_switching``, only a ready workspace in the switch that no
        thread works on is looked at (``_left_in_switch``)."""
        if self._being_made(workspace.id):
            return
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is None or self._being_made(workspace.id):
                return
            if only_switching and not self._left_in_switch(now):
                return
            if now.state == FAILED and now.rebuild_state is not None:
                self._restore_failed_rebuild(now)
                return
            if now.state != READY:
                self._restore_one(now)
                return
            again = self._restore_rebuild(now)  # a rebuild a restart cut short
            now = self.get(now.id)
            lost = not self.runtime.exists(now.container)
            kept = not lost and self.runtime.running(now.container)
            if not lost and not kept:
                self.runtime.start(now.container)
        if kept:
            self.on_kept_running(now)  # outside the lock: it runs through the channel
            return
        with self._working_on(now.id) as cancel:
            if lost and self._closing.is_set():
                return  # rcman is closing: its next start makes it again
            if lost and not self._remake(now, cancel):
                if again:
                    self._not_put_back(now.id)
                return
            if again:
                self._create_commands_again(now, cancel)
                self._put_back(now.id, cancel)
            else:
                self._start_commands(now, cancel)

    def _put_back(self, workspace_id: int, cancel: threading.Event) -> None:
        """End the switch of a workspace whose container was made again from its stored plan,
        under its lock: the rebuild's error says what happened (``_put_back_error``)."""
        with self.lock(workspace_id):
            now = self.get(workspace_id)
            if now is None or cancel.is_set() or now.rebuild_state != REBUILD_SWITCHING:
                return
            self._set(
                now.id, rebuild_state=None, rebuild_error=self._put_back_error(now.rebuild_error)
            )
        with self._lock:
            self._put_back_failing.discard(workspace_id)
        log.info("workspace %s was put back after its rebuild did not finish", workspace_id)

    def _not_put_back(self, workspace_id: int) -> None:
        """After a switch's container could not be made again: one whose image is gone, failed
        (``_image_gone``), keeps what failed it and leaves the switch; one that may yet be made,
        the pull failing for now or the like, stays in it, for the next look
        (``settle_left_over``) or restore to try again."""
        with self.lock(workspace_id):
            now = self.get(workspace_id)
            if now is not None and now.state == FAILED and now.rebuild_state is not None:
                self._set(now.id, rebuild_state=None)

    @staticmethod
    def _put_back_error(error: str | None) -> str:
        """What a workspace put back by a restore or ``settle_left_over`` is told: that rcman
        restarted while its container was replaced; or, where its rebuild had failed and it could
        not be put back at once (REBUILD_MESSAGES' ``rollback_failed``), that it was put back
        afterwards, with why the rebuild failed."""
        failed = REBUILD_MESSAGES["rollback_failed"].split("{cause}")[0]
        if not error or not error.startswith(failed):
            return REBUILD_MESSAGES["restarted_switching"]
        head = REBUILD_MESSAGES["put_back_later"].split("{cause}")[0]
        return head + Workspaces._fit(error[len(failed) :], MAX_ERROR_CHARS - len(head))

    def _restore_rebuild(self, workspace: Workspace) -> bool:
        """Settle the rebuild of a ready workspace that a restart cut short, or that could not
        put it back, under its lock: while the new image was prepared nothing was changed, and
        only the image is removed; while the container was replaced, any container there is
        discarded, whatever image it is of, to be made again from the stored plan, with its
        create commands run again, for which True is returned and the phase kept until that is
        done (``_put_back``); after the new plan was stored, the built image's name is moved, as
        the rebuild would have. The rebuild's error says what happened."""
        state = workspace.rebuild_state
        if state is None:
            return False
        if state == REBUILD_PREPARING:
            error = REBUILD_MESSAGES["restarted"]
            self._discard_next(workspace.id)
        elif state == REBUILD_SWITCHING:
            # Whatever image it is of, a container there may be the new one: one that keeps the
            # image may change the user, the environment or the commands.
            if self.runtime.exists(workspace.container):
                self.close_channel(workspace.container)
                self.runtime.discard(workspace.container)
            self._discard_next(workspace.id)
            return True  # the phase stays until it is put back (``_put_back``)
        elif not self._promote(workspace):
            return False  # left in its phase: the next restore tries again
        else:
            error = workspace.rebuild_error
        self._set(workspace.id, rebuild_state=None, rebuild_error=error)
        log.info(
            "workspace %s: a rebuild cut short by a restart was settled (%s)", workspace.id, state
        )
        return False

    def _restore_failed_rebuild(self, workspace: Workspace) -> None:
        """Settle the rebuild of a failed workspace that a restart cut short: it stays failed,
        with what failed it kept and its volume, and may be rebuilt again; a container being made
        meanwhile is discarded."""
        if workspace.rebuild_state == REBUILD_SWITCHING:
            self.close_channel(workspace.container)
            self.runtime.discard(workspace.container)
        self._discard_next(workspace.id)
        self._set(workspace.id, rebuild_state=None, error=REBUILD_MESSAGES["restarted"])

    def _create_commands_again(self, workspace: Workspace, cancel: threading.Event) -> None:
        """Run the create commands of the workspace's plan again, then its postStartCommand, in
        its container made again: a failure is logged and nothing more."""
        try:
            plan = self._plan(workspace)
            if plan is None:
                return
            layout = self._plan_layout(self.runtime.layout(workspace.container), plan)
            self._run_commands(
                workspace,
                layout,
                plan.create_commands(),
                cancel,
                lambda text: log.info("workspace %s: %s", workspace.id, text),
            )
        except Exception as failed:  # noqa: BLE001 - a command never stops a restore
            log.warning("workspace %s: its create commands failed: %s", workspace.id, failed)

    def _remake(self, workspace: Workspace, cancel: threading.Event) -> bool:
        """Make a ready workspace's lost container again from the image it was made from: the
        plan's by its id, else (a plan with none) its name pulled again, else the default image
        the workspace was made from. An image the runtime's storage lacks is pulled by its
        digest where the plan has one. If the registry says it is not there, or may not be had,
        the workspace is failed, with the kind of failure recorded (RECOVERABLE): its words say
        to rebuild it, which makes it from what its file asks for now on the same volume, or to
        delete it and make it again; if the pull fails otherwise - the network, a timeout, a
        registry that is down - nothing is decided: it stays ready, without its container, and
        is made again the next time rcman starts: only a restore makes a lost container again,
        and it runs once, as rcman starts. The same holds for limits that cannot be applied
        before it is made. Only postStartCommand is run in it afterwards, but after a rebuild a
        restart cut short while the container was replaced (``_restore_rebuild``), when every
        create command is. False when there is no container to go on with, because of either or
        a deletion meanwhile."""
        con = self._con()
        try:
            slot = people.by_id(con, workspace.person).slot
        finally:
            con.close()
        plan = self._plan(workspace)
        if plan is None:
            workspace = self._pinned(workspace)
            if workspace.image_id and not self.runtime.has_image(workspace.image_id):
                # A new default image is for new workspaces: this one is not made from it.
                self._image_gone(workspace, "default_image_gone")
                return False
        elif plan.image_id:
            if not self._pinned_image_is_there(workspace, plan, cancel):
                return False
        elif workspace_of_built_name(plan.image) is not None:
            # An image rcman built is in its storage or nowhere: there is no registry to ask.
            if plan.image not in self.runtime.built_images().values():
                self._image_gone(workspace, "built_image_gone")
                return False
        elif not self._pulled_again(workspace, plan, plan.image, cancel):
            return False
        if self._cancelled(workspace.id, cancel):
            return False
        self._apply_stored(workspace.id)  # before its container is in the slice
        self.runtime.create(self._spec(workspace, slot, plan))
        if self._cancelled(workspace.id, cancel):
            with self.lock(workspace.id):  # made after a deletion removed it: nothing is for it
                if self.get(workspace.id) is None:
                    self.runtime.remove(workspace.container)
            return False
        return True

    def _pulled_again(
        self, workspace: Workspace, plan: devcontainer.Plan, name: str, cancel: threading.Event
    ) -> bool:
        """Pull ``name``, the plan's image or the same image by its digest, because the runtime's
        storage may no longer have it. If the registry says it is not there, or may not be had,
        the workspace is failed (``image_gone``), in words that say to rebuild it or delete it;
        if the pull fails otherwise - the network, a timeout, a registry that is down - nothing is
        decided: False, and it stays ready without its container, made again the next time rcman
        starts."""
        try:
            self.runtime.pull(name, None, cancel)
        except RuntimeFailed as failed:
            if self._cancelled(workspace.id, cancel):
                return False
            log.warning("the image of workspace %s could not be had: %s", workspace.id, failed)
            if not image_is_gone(str(failed)):
                return False  # it may pass: the next restore tries again
            self._image_gone(
                workspace,
                "image_gone",
                DEVCONTAINER_MESSAGES["image_gone"].format(image=plan.image),
            )
            return False
        return True

    def _pinned_image_is_there(
        self, workspace: Workspace, plan: devcontainer.Plan, cancel: threading.Event
    ) -> bool:
        """Whether the image a plan was made from, by its id, is in the runtime's storage, so
        that the container is made again from it whatever its tag names now. One that is not is
        pulled by its digest where it has one and came from a registry, and must then be the
        very image; otherwise, or when the registry no longer has it, the workspace is failed
        in plain words. An image rcman built is in the storage or nowhere."""
        built = workspace_of_built_name(plan.image) is not None
        if built and plan.image not in self.runtime.built_images().values():
            self._image_gone(workspace, "built_image_gone")
            return False
        if self.runtime.has_image(plan.image_id):
            return True
        if built:
            self._image_gone(workspace, "built_image_gone")
            return False
        if not DIGEST.match(plan.image_digest):
            self._image_gone(
                workspace,
                "image_gone",
                DEVCONTAINER_MESSAGES["image_gone"].format(image=plan.image),
            )
            return False
        by_digest = f"{repository_of(plan.image)}@{plan.image_digest}"
        if not self._pulled_again(workspace, plan, by_digest, cancel):
            return False
        if not self.runtime.has_image(plan.image_id):  # the digest names another image than this
            self._image_gone(
                workspace,
                "image_gone",
                DEVCONTAINER_MESSAGES["image_gone"].format(image=plan.image),
            )
            return False
        return True

    def _image_gone(self, workspace: Workspace, kind: str, error: str | None = None) -> None:
        """Fail a ready workspace whose image is not to be had: ``kind`` is the failure, and
        the key of its message in DEVCONTAINER_MESSAGES unless ``error`` gives the words."""
        with self.lock(workspace.id):
            if (self.get(workspace.id) or workspace).state == READY:
                words = error or DEVCONTAINER_MESSAGES[kind]
                self._set(workspace.id, state=FAILED, error=words, failure=kind)

    def _restore_one(self, workspace: Workspace) -> None:
        if workspace.state == DELETING:
            self._remove(workspace)
        elif workspace.state == CREATING and workspace.source:
            # A clone is not made again, whatever it had cloned: the person decides.
            self._set(workspace.id, state=FAILED, error=CLONE_MESSAGES["restarted"], failure=None)
        elif workspace.state == CREATING:
            self.runtime.remove(workspace.container)  # whatever was made of it
            con = self._con()
            try:
                slot = people.by_id(con, workspace.person).slot
            finally:
                con.close()
            self._make(workspace, slot)

    def settle_signing_out(self) -> None:
        """Finish each sign-out that waits for servers that no longer run, those that
        ended by themselves among them. A person whose lock is held, as a sign-in holds it
        for a while, is left for the next round rather than waited for."""
        for person in self._people_signing_out():
            lock = self.person_lock(person)
            if not lock.acquire(blocking=False):
                continue
            try:
                self._stopped(person)
            finally:
                lock.release()

    def _people_signing_out(self) -> list[int]:
        con = self._con()
        try:
            rows = con.execute("select id from people where claude_signing_out = 1").fetchall()
        finally:
            con.close()
        return [row[0] for row in rows]

    def _finish_deleting(self, person_id: int) -> None:
        """Remove the person, if they are being deleted and none of their workspaces is
        left, and write the allowed list again."""
        con = self._con()
        try:
            person = people.by_id(con, person_id)
            if person is None or person.state != people.DELETING or self.of_person(person_id):
                return
            self._remove_account(person.id)
            people.remove(con, person)
            people.write_allowed(con, self.settings.allowed_emails)
        finally:
            con.close()

    def _finish_deleting_people(self) -> None:
        """Remove each person whose deletion an earlier rcman began, once none of their
        workspaces is left, and write the allowed list again."""
        con = self._con()
        try:
            for person in people.everyone(con):
                if person.state == people.DELETING and not self.of_person(person.id):
                    try:
                        self._remove_account(person.id)
                    except Problem:
                        continue  # the next start tries again
                    people.remove(con, person)
            people.write_allowed(con, self.settings.allowed_emails)
        finally:
            con.close()

    def _restore_server(self, workspace: Workspace) -> None:
        status = self.server(workspace).status() if workspace.server else None
        if status is not None and status.get("exit_code") is None:
            return  # it runs
        try:
            self.start_server(workspace, workspace.prefix or workspace.name)
        except Problem as refused:
            log.warning("the server of workspace %s did not start: %s", workspace.id, refused.code)

    # --- running ---------------------------------------------------------------------------

    def start_background(self) -> None:
        self._restoring = threading.Thread(target=self.restore, daemon=True)
        self._restoring.start()
        threading.Thread(target=self._close_idle_channels, daemon=True).start()

    def _close_idle_channels(self) -> None:
        """Every IDLE_CHECK_SECONDS: close idle channels, finish the sign-outs that wait for
        servers that have ended, and finish what rebuilds left that nothing works on
        (``settle_left_over``)."""
        while not self._closing.wait(IDLE_CHECK_SECONDS):
            self.close_idle()
            self.settle_signing_out()
            try:
                self.settle_left_over()
            except Exception:  # noqa: BLE001 - the next round tries again
                log.exception("what rebuilds left could not be finished")

    def settle_left_over(self) -> None:
        """In the service, once its restore is done: finish what a rebuild left in a row that
        no thread of this process works on. A workspace rcman-admin deleted during a rebuild,
        which it leaves to the service, is removed, and its person with it if they are being
        deleted and it was their last; the last phase of a rebuild whose built image's name could
        not be moved is finished (``_promote``); and a ready workspace whose rebuild could not put
        it back is put back as the restore does it, in a thread of its own, its server started
        again if it should run, and stays in the switch, for the next look, if that fails too."""
        if not self.prunes or not self.restored.is_set():
            return
        for workspace in self.everything():
            if workspace.rebuild_state is None or self._worked_on(workspace.id):
                continue
            if self._left_in_switch(workspace):
                # In a thread of its own, as it may be slow: the other looks go on meanwhile.
                thread = threading.Thread(
                    target=self._put_back_left, args=(workspace,), daemon=True
                )
                with self._lock:
                    self._putting_back[workspace.id] = thread
                thread.start()
                continue
            if workspace.state != DELETING and workspace.rebuild_state != REBUILD_PROMOTING:
                continue
            removed = False
            with self.lock(workspace.id):
                now = self.get(workspace.id)
                if now is None or now.rebuild_state is None or self._worked_on(now.id):
                    continue
                if now.state == DELETING:
                    removed = self._remove_deleted(now)
                elif now.rebuild_state == REBUILD_PROMOTING and self._promote(now):
                    self._set(now.id, rebuild_state=None, rebuild_log=None, rebuild_error=None)
            if removed:
                self._stopped(workspace.person)
                self._collect_images_quietly()

    def _put_back_left(self, workspace: Workspace) -> None:
        """``settle_left_over``'s thread putting back a workspace left in the switch. The first
        failure is logged as a warning, and the ones after it, at each look until it is put back,
        only for debugging."""
        try:
            self._restore_task(workspace, only_switching=True)
        except Exception as failed:  # noqa: BLE001 - the next look tries again
            with self._lock:
                again = workspace.id in self._put_back_failing
                self._put_back_failing.add(workspace.id)
            level = logging.DEBUG if again else logging.WARNING
            log.log(level, "workspace %s was not put back yet: %s", workspace.id, failed)
        finally:
            with self._lock:
                if self._putting_back.get(workspace.id) is threading.current_thread():
                    del self._putting_back[workspace.id]

    def _worked_on(self, workspace_id: int) -> bool:
        """Whether a thread of this process makes, rebuilds or restores the workspace, or puts
        it back (``settle_left_over``) - another than the one asking."""
        with self._lock:
            working = workspace_id in self._working
            putting = self._putting_back.get(workspace_id)
        if putting is not None and putting.is_alive() and putting is not threading.current_thread():
            return True
        return working or self._being_made(workspace_id)

    def close(self) -> None:
        """Stop: let restore finish the step it is on, wait for the workspaces being made or put
        back, and close every channel."""
        self._closing.set()
        if self._restoring is not None:
            self._restoring.join(timeout=2 * RUNTIME_WAIT_SECONDS)
        with self._lock:
            threads = [*self._making.values(), *self._putting_back.values()]
        for thread in threads:
            thread.join(timeout=30)
        self.close_channels()
