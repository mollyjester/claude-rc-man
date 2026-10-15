"""Workspaces: each a container of a person's, with at most one Claude server.

The ``workspaces`` table is what should exist and what should run; the runtime
(runtime.py) is what does. rcman is the only one who starts anything, so when it
starts it brings the runtime back to the table (``restore``), in the background,
while it already answers requests. A workspace is made in the background too:
the first of a person's waits while Podman copies the image into their range.

Every command in a workspace goes through its channel (channel.py), opened when
it is first needed and closed once it has been idle for CHANNEL_IDLE_SECONDS; a
workspace whose table row records no server is never reached at all to list it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from helpers import git_run
from rcman import db, people
from rcman.channel import Channel, ChannelBroken, ChannelTimeout
from rcman.claude_server import Server, TmuxFailed
from rcman.problems import Problem
from rcman.repository import Repository
from rcman.runtime import (
    LABEL_WORKSPACE,
    Runtime,
    RuntimeFailed,
    RuntimeUnavailable,
    Spec,
    container_name,
)
from rcman.settings import Settings

log = logging.getLogger("rcman.workspaces")
log.setLevel(logging.INFO)

CREATING, READY, FAILED, DELETING = "creating", "ready", "failed", "deleting"
# A channel unused this long is closed; it is opened again when next needed.
CHANNEL_IDLE_SECONDS = 300.0
# How often idle channels, and sign-outs that wait for servers that have ended, are
# looked for.
IDLE_CHECK_SECONDS = 30.0
# How long one wait for the runtime lasts before restore tries again, or stops when
# rcman does; and how long rcman's shutdown waits for restore to stop.
RUNTIME_WAIT_SECONDS = 5.0
# How many workspaces' servers are read at once for a listing.
LISTING_WORKERS = 8
# How long deleting a person waits for a workspace still being made.
MADE_WITHIN_SECONDS = 600.0
# Most of what the runtime said that a failed workspace keeps.
MAX_ERROR_CHARS = 2000


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
    )


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


class Workspaces:
    def __init__(self, settings: Settings, runtime: Runtime):
        self.settings = settings
        self.runtime = runtime
        self.restored = threading.Event()
        self._lock = threading.Lock()
        self._channels: dict[str, Channel] = {}
        self._locks: dict[int, threading.RLock] = {}
        self._person_locks: dict[int, threading.RLock] = {}
        self._making: dict[int, threading.Thread] = {}  # by workspace
        self._closing = threading.Event()
        self._restoring: threading.Thread | None = None
        # Called with a person's id once a server of theirs may have stopped: accounts.py
        # finishes a sign-out that waits for their last running server.
        self.on_stopped: Callable[[int], None] = lambda person: None

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

    def of_person(self, person: int) -> list[Workspace]:
        return [w for w in self.everything() if w.person == person]

    def owned(self, person: int, workspace_id: int) -> Workspace:
        """The person's workspace; another's is as if it were not there."""
        found = self.get(workspace_id)
        if found is None or found.person != person:
            raise Problem("workspace_not_found")
        return found

    def ready(self, person: int, workspace_id: int) -> Workspace:
        found = self.owned(person, workspace_id)
        if found.state != READY:
            raise Problem("workspace_not_ready")
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
        is finished later by the periodic check, the Claude page or restore."""
        try:
            self.on_stopped(person)
        except Exception:  # noqa: BLE001 - see above
            log.exception("finishing a sign-out of person %s failed; it is tried again", person)

    def _being_made(self, workspace_id: int) -> bool:
        with self._lock:
            thread = self._making.get(workspace_id)
        return thread is not None and thread.is_alive()

    # --- making and deleting ---------------------------------------------------------

    def create(self, person: people.Person, name: str) -> Workspace:
        """Record the workspace and make it in the background."""
        con = self._con()
        try:
            # Only for a person still let in: none for one being deleted meanwhile.
            cursor = con.execute(
                "insert into workspaces (person, name, state) select ?, ?, ? "
                "where exists (select 1 from people where id = ? and state = ?)",
                (person.id, name, CREATING, person.id, people.ACTIVE),
            )
            con.commit()
            if cursor.rowcount == 0:
                raise Problem("not_allowed")
        except sqlite3.IntegrityError:
            raise Problem("workspace_name_taken") from None
        finally:
            con.close()
        made = self.get(cursor.lastrowid)
        thread = threading.Thread(target=self._make, args=(made, person.slot), daemon=True)
        with self._lock:
            self._making = {k: t for k, t in self._making.items() if t.is_alive()}
            self._making[made.id] = thread
        thread.start()
        return made

    def _make(self, workspace: Workspace, slot: int) -> None:
        """Make the workspace; one deleted meanwhile is removed once it is made."""
        spec = Spec(workspace.container, slot=slot, workspace=workspace.id, person=workspace.person)
        try:
            self.runtime.create(spec)
            self._check_git(workspace)
            error = None
        except Exception as failed:  # noqa: BLE001 - whatever it is, the workspace failed
            log.warning("workspace %s could not be made: %s", workspace.id, failed)
            error = (str(failed) or type(failed).__name__)[:MAX_ERROR_CHARS]
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
                self._set(workspace.id, state=READY, error=None)
            else:
                self._set(workspace.id, state=FAILED, error=error)

    def _check_git(self, workspace: Workspace) -> None:
        """The image must hold a git that rcman can use: one new enough, which the workspace
        is failed without otherwise."""
        run, run_stdin = self._execs(workspace.container)
        repository = Repository(self.runtime.layout(workspace.container), run, run_stdin)
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

    def delete(self, workspace: Workspace) -> None:
        """Delete the workspace: its container and volume, its server with them, and its
        row. One still being made goes once it is made."""
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is None:
                return
            self._set(workspace.id, state=DELETING)
            if now.state == CREATING:
                return
            self._remove(now)
        self._stopped(workspace.person)

    def _remove(self, workspace: Workspace) -> None:
        self.close_channel(workspace.container)
        with answered():
            self.runtime.remove(workspace.container)
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
        for workspace in self.of_person(person.id):
            self.delete(workspace)
            with self._lock:
                making = self._making.get(workspace.id)
            if making is not None:  # still being made: it goes once it is
                making.join(MADE_WITHIN_SECONDS)
        if self.of_person(person.id):  # one could not be removed: rcman's next start tries
            raise Problem("workspace_failed")
        self._remove_account(person.id)
        con = self._con()
        try:
            people.remove(con, person)
            people.write_allowed(con, self.settings.allowed_emails)
        finally:
            con.close()

    def _remove_account(self, person_id: int) -> None:
        """Remove the person's sign-in before their row: ids and slots are given again, so
        a file left behind would sign the next person in. One that cannot be removed
        leaves the row, for rcman's next start."""
        try:
            with answered():
                self.runtime.remove_account(person_id)
        except Problem:
            log.warning("the sign-in of person %s could not be removed", person_id)
            raise

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

    def server(self, workspace: Workspace) -> Server:
        name = workspace.container
        channel = self._channel(name)
        return Server(self.runtime.layout(name), channel.run, workspace.id)

    def _execs(self, name: str):
        """The runtime's exec as a repository's runners: without and with stdin."""

        def run(argv: list[str], timeout: float):
            return self.runtime.exec(name, argv, timeout=timeout)

        def run_stdin(argv: list[str], stdin: bytes, timeout: float):
            return self.runtime.exec(name, argv, stdin, timeout=timeout)

        return run, run_stdin

    def repository(self, workspace: Workspace) -> Repository:
        """The git repository at the root of the workspace's work folder, run through its
        channel, and through ``exec`` for what carries stdin. Nothing is started for it."""
        name = workspace.container
        return Repository(self.runtime.layout(name), self._channel(name).run, self._execs(name)[1])

    def server_status(self, workspace: Workspace) -> dict | None:
        """The workspace's server, or None; a workspace that records none is not reached."""
        if not workspace.server:
            return None
        with answered():
            return self.server(workspace).status()

    def listing(self, workspaces: list[Workspace]) -> list[dict]:
        """Each workspace as the API shows it, its server read side by side with the others'."""

        def shown(workspace: Workspace) -> dict:
            entry = {
                "id": workspace.id,
                "name": workspace.name,
                "state": workspace.state,
                "error": workspace.error,
                "server": None,
                "server_error": None,
            }
            if workspace.state == READY:
                try:
                    entry["server"] = self.server_status(workspace)
                except Problem as problem:
                    entry["server_error"] = {"code": problem.code, "title": problem.title}
            return entry

        if not workspaces:
            return []
        with ThreadPoolExecutor(max_workers=min(len(workspaces), LISTING_WORKERS)) as pool:
            return list(pool.map(shown, workspaces))

    def start_server(self, workspace: Workspace, prefix: str, resume: bool = False) -> dict:
        with self.person_lock(workspace.person), self.lock(workspace.id), answered():
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
            if server.status() is None or server.tmux.exit_status() is not None:
                self._seed(workspace, account)
            try:
                answer = server.resume(prefix) if resume else server.start(prefix)
            finally:
                self._set(workspace.id, server=int(server.tmux.exists()))
            self._set(workspace.id, wanted=1, prefix=prefix)
            return answer

    def _seed(self, workspace: Workspace, account: dict) -> None:
        """Put the account the sign-in named into the workspace's ``.claude.json``, owner-
        only, keeping every other key, before its server starts: Claude's Remote Control
        needs it, and the credentials file alone does not hold it."""
        name = workspace.container
        layout = self.runtime.layout(name)
        path = f"{layout.config}/.claude.json"
        found = self.runtime.exec(name, [*layout.tool("cat"), path])
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

    def stop_server(self, workspace: Workspace) -> dict:
        with self.lock(workspace.id), answered():
            self._set(workspace.id, wanted=0)
            answer = self.server(workspace).stop()
        self._stopped(workspace.person)
        return answer

    def forget_server(self, workspace: Workspace) -> dict:
        with self.lock(workspace.id), answered():
            answer = self.server(workspace).forget()
            self._set(workspace.id, server=0, wanted=0)
        self._stopped(workspace.person)
        return answer

    def running(self, person: int) -> list[Workspace]:
        """The person's workspaces whose server runs; one that cannot be read counts as
        running, so nothing waiting for them all to stop goes on too soon."""
        found = []
        for workspace in self.of_person(person):
            if workspace.state != READY or not workspace.server:
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
                try:
                    self.stop_server(workspace)
                except Problem as refused:
                    log.warning(
                        "the server of workspace %s did not stop: %s", workspace.id, refused.code
                    )

    def confirm(self, workspace: Workspace, answer: str) -> dict:
        with self.lock(workspace.id), answered():
            return self.server(workspace).confirm(answer)

    def output(self, workspace: Workspace) -> dict:
        with answered():
            return self.server(workspace).output()

    # --- bringing the runtime back to the table ----------------------------------------

    def restore(self) -> None:
        """Wait for the runtime, then: finish what was being made or deleted, make again
        or start what should exist, start the servers that should run, remove the
        workspaces no row is for, and finish deleting the people whose deletion an
        earlier rcman began. A step that fails is logged, and the rest go on."""
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
            for workspace in self.everything():
                if self._closing.is_set():
                    return
                self._step(self._restore_workspace, workspace)
            for workspace in self.everything():
                if self._closing.is_set():
                    return
                if workspace.state == READY and workspace.wanted:
                    self._step(self._restore_server, workspace)
            if self._closing.is_set():
                return
            for name, labels in self.runtime.labelled().items():
                self._step(self._remove_orphan, name, labels)
            self._step(self._finish_deleting_people)
            self._step(self.settle_signing_out)
        except Exception:  # noqa: BLE001 - restore never stops rcman from answering
            log.exception("restoring the workspaces failed")
        finally:
            self.restored.set()

    @staticmethod
    def _step(call, *args) -> None:
        try:
            call(*args)
        except Exception:  # noqa: BLE001 - one workspace never stops the others
            log.exception("restoring %s failed", args)

    def _remove_orphan(self, name: str, labels: dict) -> None:
        """Remove a workspace no row is for, checked again just before: a request may have
        made its row since restore read the table."""
        try:
            workspace_id = int(labels.get(LABEL_WORKSPACE, ""))
        except ValueError:
            workspace_id = None
        if workspace_id is not None:
            with self.lock(workspace_id):
                if self.get(workspace_id) is not None or self._being_made(workspace_id):
                    return
                self.runtime.remove(name)
        elif name not in {w.container for w in self.everything()}:
            self.runtime.remove(name)

    def _restore_workspace(self, workspace: Workspace) -> None:
        """Bring one workspace back, under its lock, as its row is now; one a request is
        making meanwhile is that request's."""
        if self._being_made(workspace.id):
            return
        with self.lock(workspace.id):
            now = self.get(workspace.id)
            if now is not None and not self._being_made(workspace.id):
                self._restore_one(now)

    def _restore_one(self, workspace: Workspace) -> None:
        if workspace.state == DELETING:
            self._remove(workspace)
        elif workspace.state == CREATING:
            self.runtime.remove(workspace.container)  # whatever was made of it
            con = self._con()
            try:
                slot = people.by_id(con, workspace.person).slot
            finally:
                con.close()
            self._make(workspace, slot)
        elif workspace.state == READY:
            if not self.runtime.exists(workspace.container):
                con = self._con()
                try:
                    slot = people.by_id(con, workspace.person).slot
                finally:
                    con.close()
                self.runtime.create(Spec(workspace.container, slot, workspace.id, workspace.person))
            elif not self.runtime.running(workspace.container):
                self.runtime.start(workspace.container)

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
        """Every IDLE_CHECK_SECONDS: close idle channels, and finish the sign-outs that
        wait for servers that have ended."""
        while not self._closing.wait(IDLE_CHECK_SECONDS):
            self.close_idle()
            self.settle_signing_out()

    def close(self) -> None:
        """Stop: let restore finish the step it is on, wait for the workspaces being made,
        and close every channel."""
        self._closing.set()
        if self._restoring is not None:
            self._restoring.join(timeout=2 * RUNTIME_WAIT_SECONDS)
        with self._lock:
            threads = list(self._making.values())
        for thread in threads:
            thread.join(timeout=30)
        self.close_channels()
