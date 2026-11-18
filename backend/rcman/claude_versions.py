"""Claude Code versions: the one that is approved, rolling out another, and pruning the rest.

Every workspace runs Claude Code from one store, ``claude_store``: ``versions/<version>``,
one program each, and ``current``, a link to the approved one. Approvals are the rows of the
``claude_approvals`` table, never changed or deleted: the latest is the approved version, the
latest one before it that names another version and is not marked failed is the previous,
and a rollback is a new row. The database is the truth and the link follows it
(``reconcile``); the first time rcman runs it adopts what the installer left ``current``
naming as the first approval.

An administrator may mark any version but the approved one as failed, with a reason or
none: the rows of ``claude_failures``, one per version, deleted when the mark is removed. A
version marked failed is neither rolled out nor approved again, even if it was approved
before, until its mark is removed; it is never the previous version; and it leaves the store
by the usual pruning once nothing else keeps it. The mark outlives its file.

A server pins the version it starts with (workspaces.py): it resolves the approved version
here, records it on its workspace's row and runs that exact file, so approving another
version changes nothing of a server that runs, and a stop and a start does. A version is
removed from the store (``prune``) only when it is neither the approved nor the previous
one (the latest approved before it that is not marked failed), and no server that runs, or
cannot be read, started with it.

A rollout (``upgrade``) is synchronous and one at a time. The candidate is downloaded into a
staging folder outside the store (or copied there, if the store has it already), held to the
canary's checks in a throwaway container, and only if it passes is it copied into the store
under a partial name, renamed into place, approved and made ``current``. A candidate that
cannot be had or fails leaves the store, and the approvals, exactly as they were, and the
staging folder is removed whatever happens. ``lock`` is held while the approved version is
read and a server records it, while the store is changed and while it is pruned, so a server
never starts on a version that is being removed.

An administrator may delete a version from the store by hand (``delete``): any version but the
approved one, the one ``current`` names and one a workspace's record names, which a server
started with, even one that has ended on its own; the previous version too, going back to
which then downloads it again and runs the canary. It holds the rollouts' lock for its whole
run, as a rollout does, and decides under ``lock``, as ``prune`` does, and it leaves a
version's mark as failed alone.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from helpers import claude_release, rules
from rcman import db
from rcman.problems import (
    CHECKS_FAILED_MESSAGES,
    CLAUDE_INSTALL_MESSAGES,
    CLAUDE_INSTALL_STATUS,
    VERSION_KEPT_MESSAGES,
    Problem,
)
from rcman.problems import body as problem_body
from rcman.runtime import CANARY_NAME, CANDIDATE, RuntimeFailed, RuntimeUnavailable
from rcman.settings import Settings

log = logging.getLogger("rcman.claude_versions")
log.setLevel(logging.INFO)

# The folder of rcman's state, beside the database, where candidates are staged.
STAGING_FOLDER = "claude-staging"
# Who an approval is by when rcman made it itself: adopting what the installer left.
ADOPTED_BY = "rcman"
# The store's files and folders are readable by every uid range (the workspaces') and
# writable by their owner alone.
MODE = 0o755
VERSIONS, CURRENT = "versions", "current"


@dataclass(frozen=True)
class Approval:
    version: str
    at: str
    by: str


@dataclass(frozen=True)
class Failure:
    """A version marked as failed: by whom, when, and why (None where no reason was given)."""

    version: str
    at: str
    by: str
    reason: str | None


def reason_of(text: str | None) -> str | None:
    """A mark's reason as it is kept: trimmed, and none where nothing is left."""
    if text is None:
        return None
    return text.strip() or None


def _set_link(folder: Path, name: str, target: str) -> None:
    """Make ``folder/name`` a link to ``target`` in one step, so that a reader sees the old
    link or the new one and never none."""
    temporary = folder / f".{name}.new"
    temporary.unlink(missing_ok=True)
    os.symlink(target, temporary)
    os.replace(temporary, folder / name)


class ClaudeVersions:
    def __init__(self, settings: Settings, runtime, workspaces):
        self.settings = settings
        self.runtime = runtime
        self.workspaces = workspaces
        self.store = settings.claude_store
        self.versions_dir = self.store / VERSIONS
        self.staging = settings.database.parent / STAGING_FOLDER
        self.lock = threading.RLock()
        self._upgrading = threading.Lock()
        # False in a process that is not the service: see ``Workspaces``.
        self.prunes = True

    # --- the approvals ---------------------------------------------------------------------

    def approvals(self) -> list[Approval]:
        con = db.connect(self.settings.database)
        try:
            rows = con.execute("select version, at, by from claude_approvals order by id")
            return [Approval(row[0], row[1], row[2]) for row in rows.fetchall()]
        finally:
            con.close()

    def approved(self) -> str | None:
        found = self.approvals()
        return found[-1].version if found else None

    def previous(self) -> str | None:
        """The latest approved version that is neither the approved one nor marked failed:
        the last good one, which an administrator goes back to. Pruning keeps it while it is
        in the store; it may be one that left the store before, when it was neither, and going
        back to it then downloads it and runs the canary."""
        found = self.approvals()
        if not found:
            return None
        failed = {f.version for f in self.failures()}
        approved = found[-1].version
        return next(
            (
                a.version
                for a in reversed(found)
                if a.version != approved and a.version not in failed
            ),
            None,
        )

    def _approve(self, version: str, by: str) -> None:
        con = db.connect(self.settings.database)
        try:
            con.execute(
                "insert into claude_approvals (version, at, by) values (?, ?, ?)",
                (version, dt.datetime.now(dt.UTC).isoformat(), by),
            )
            con.commit()
        finally:
            con.close()

    # --- versions marked as failed -----------------------------------------------------------

    def failures(self) -> list[Failure]:
        """The versions marked as failed, the newest mark first."""
        con = db.connect(self.settings.database)
        try:
            rows = con.execute(
                "select version, at, by, reason from claude_failures order by at desc, rowid desc"
            )
            return [Failure(row[0], row[1], row[2], row[3]) for row in rows.fetchall()]
        finally:
            con.close()

    def _is_failed(self, version: str) -> bool:
        con = db.connect(self.settings.database)
        try:
            found = con.execute("select 1 from claude_failures where version = ?", (version,))
            return found.fetchone() is not None
        finally:
            con.close()

    def mark_failed(self, version: str, by: str, reason: str | None) -> dict:
        """Mark ``version`` as failed, by ``by``, with ``reason`` (trimmed; blank is none), and
        the report afterwards. ``claude_version_approved`` for the approved version, and
        ``claude_version_failed`` for one marked already, whose mark stays as it was. Any other
        version may be marked, in the store or not, also while a rollout or an upgrade of rcman
        runs: a rollout of the version that is under way is refused before it is approved
        (``_commit``). The version may then leave the store, by the prune that follows, which
        does nothing while a rollout or a deletion runs; the next prune after it does."""
        with self.lock:
            if self.approved() is None:
                self.reconcile()  # nothing is approved yet: adopt what the store names
            if version == self.approved():
                raise Problem("claude_version_approved")
            con = db.connect(self.settings.database)
            try:
                con.execute(
                    "insert into claude_failures (version, at, by, reason) values (?, ?, ?, ?)",
                    (version, dt.datetime.now(dt.UTC).isoformat(), by, reason_of(reason)),
                )
                con.commit()
            except sqlite3.IntegrityError:
                raise Problem("claude_version_failed") from None
            finally:
                con.close()
        self.prune_quietly()
        return self.report()

    def clear_failed(self, version: str) -> dict:
        """Remove the mark of ``version``, and the report afterwards; ``claude_version_not_failed``
        where it has none."""
        with self.lock:
            con = db.connect(self.settings.database)
            try:
                deleted = con.execute(
                    "delete from claude_failures where version = ?", (version,)
                ).rowcount
                con.commit()
            finally:
                con.close()
        if not deleted:
            raise Problem("claude_version_not_failed")
        return self.report()

    # --- the store -------------------------------------------------------------------------

    def _usable(self, path: Path) -> bool:
        """Whether ``path`` is a file with something in it; a store that cannot be looked into
        holds none, and a file with nothing in it is what a power loss between a rename and
        the write behind it leaves, so it is no version."""
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError as failed:
            log.warning("%s cannot be looked at: %s", path, failed)
            return False

    def installed(self) -> list[str]:
        """The versions in the store, oldest first."""
        try:
            if not self.versions_dir.is_dir():
                return []
        except OSError as failed:
            log.warning("%s cannot be looked at: %s", self.versions_dir, failed)
            return []
        found = [
            p.name for p in self.versions_dir.iterdir()
            if rules.VERSION.fullmatch(p.name) and self._usable(p)
        ]  # fmt: skip
        return sorted(found, key=lambda v: tuple(int(n) for n in v.split(".")))

    def _current(self) -> str | None:
        """The version the link ``current`` names, if it names one in the store's form."""
        try:
            target = os.readlink(self.store / CURRENT)
        except OSError:  # no link, or a store that cannot be looked into
            return None
        name = target.removeprefix(f"{VERSIONS}/")
        if name == target or not rules.VERSION.fullmatch(name):
            return None
        return name

    def _switch(self, version: str) -> None:
        _set_link(self.store, CURRENT, f"{VERSIONS}/{version}")

    def reconcile(self) -> None:
        """Bring the link to the database: with no approval yet, adopt the version the link
        names as the first one; otherwise point the link at the approved version. A link
        that cannot be changed is logged, not fatal: the database is what servers read."""
        with self.lock:
            approved = self.approved()
            current = self._current()
            if approved is None:
                if current is not None and self._usable(self.versions_dir / current):
                    self._approve(current, ADOPTED_BY)
                    log.info(
                        "Claude Code %s, which the store names, is the approved version", current
                    )
                return
            if current == approved:
                return
            if not self._usable(self.versions_dir / approved):
                log.warning("the approved Claude Code %s is not in the store", approved)
                return
            try:
                self._switch(approved)
            except OSError:
                log.exception(
                    "the link %s could not be pointed at %s", self.store / CURRENT, approved
                )

    def resolve(self) -> str:
        """The approved version, which is in the store; ``claude_not_installed`` if there is
        none, or its file is not there."""
        with self.lock:
            version = self.approved()
            if version is None:
                self.reconcile()  # nothing is approved yet: adopt what the store names
                version = self.approved()
            if version is None or not self._usable(self.versions_dir / version):
                raise Problem("claude_not_installed")
            return version

    # --- rolling out a version ----------------------------------------------------------------

    def upgrade(self, version: str, by: str) -> dict:
        """Approve ``version``: a new one, one in the store, or an earlier one again (a
        rollback). The report of what is approved afterwards. ``claude_upgrade_busy`` while
        another rollout or a deletion runs, ``claude_version_failed`` where the version is
        marked as failed, before anything is fetched or checked or once it is marked while it
        is checked,
        ``claude_install_failed`` where the version is not a version or cannot be had,
        ``claude_checks_failed`` where it fails the canary's checks, naming it and the approved
        version the workspaces stay on, if one is (``_checks_failed``), a RuntimeFailed where
        the canary could not run, and ``rcman_upgrading`` while an upgrade of rcman is under
        way; in each the store and the approvals are as they were. A version that was approved
        before and is still in the store is approved again at once: it passed the checks then,
        so a rollback needs neither a download nor a canary."""
        if not rules.VERSION.fullmatch(version):
            raise self._install_failed("no_such_version")
        if not self._upgrading.acquire(blocking=False):
            raise Problem("claude_upgrade_busy")
        try:
            # after the lock, which the admission of an upgrade of rcman holds as it records one
            if self.workspaces.upgrading():
                raise Problem("rcman_upgrading")
            if version != self.approved():
                self._roll_out(version, by)
        finally:
            self._upgrading.release()
        self.prune_quietly()
        return self.report()

    def rolling_out(self) -> bool:
        """Whether a rollout, a deletion, or the sweep of what a rollout left, runs."""
        return self._upgrading.locked()

    @contextlib.contextmanager
    def rollouts_held(self):
        """The rollouts' lock, held for what is in the block if it is free now, without waiting:
        whether it was had. The admission of an upgrade of rcman holds it, so that a rollout
        either runs before it or is refused after it."""
        held = self._upgrading.acquire(blocking=False)
        try:
            yield held
        finally:
            if held:
                self._upgrading.release()

    @staticmethod
    def _install_failed(kind: str) -> Problem:
        return Problem(
            "claude_install_failed",
            status=CLAUDE_INSTALL_STATUS[kind],
            title=CLAUDE_INSTALL_MESSAGES[kind],
            kind=kind,
        )

    def _roll_out(self, version: str, by: str) -> None:
        with self.lock:
            if self._is_failed(version):
                raise Problem("claude_version_failed")
            if self._approved_before(version) and self._usable(self.versions_dir / version):
                self._commit(version, by, None)
                return
        self.staging.mkdir(parents=True, exist_ok=True)
        self.staging.chmod(MODE)  # the umask is owner-only, and the canary's range is not the owner
        folder = self.staging / uuid.uuid4().hex
        folder.mkdir()
        folder.chmod(MODE)
        partial = self.versions_dir / f".{version}{claude_release.PARTIAL_SUFFIX}"
        try:
            candidate = folder / CANDIDATE
            self._stage(version, candidate)
            failed = self.runtime.run_canary(
                folder, CANARY_NAME, version, slot=self.workspaces.builder_slot
            )
            if failed:
                raise self._checks_failed(version, failed)
            self._install(candidate, version, partial)
            self._commit(version, by, partial)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            partial.unlink(missing_ok=True)

    def _checks_failed(self, version: str, failed: dict[str, dict]) -> Problem:
        """``claude_checks_failed`` for ``version``: the checks it failed and their reports, and
        the version that failed and the approved one, which the workspaces stay on (None where
        none is approved), in its title and of their own. It reads the approvals and writes
        none."""
        approved = self.approved()
        title = CHECKS_FAILED_MESSAGES["none" if approved is None else "approved"]
        return Problem(
            "claude_checks_failed",
            title=title.format(version=version, approved=approved),
            failed=sorted(failed),
            report=failed,
            version=version,
            approved=approved,
        )

    def _approved_before(self, version: str) -> bool:
        return any(a.version == version for a in self.approvals())

    def _stage(self, version: str, candidate: Path) -> None:
        """The candidate as a file of its staging folder: copied from the store if it is
        there, else downloaded. The store is read under ``lock``, which a prune deletes
        under, so the file is copied whole or not at all."""
        installed = self.versions_dir / version
        with self.lock:
            if self._usable(installed):
                shutil.copyfile(installed, candidate)
                candidate.chmod(MODE)
                return
        try:
            claude_release.fetch(self.settings.claude_download, version, candidate)
        except claude_release.ReleaseError as failed:
            log.warning("Claude Code %s could not be had: %s", version, failed)
            raise self._install_failed(failed.kind) from None

    def _install(self, candidate: Path, version: str, partial: Path) -> None:
        """The candidate as a partial file of the store, whole and on the disk, so that the
        rename that makes it a version is the only step that changes what the store holds."""
        if self._usable(self.versions_dir / version):
            return
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        with candidate.open("rb") as source, partial.open("wb") as sink:
            shutil.copyfileobj(source, sink)
            sink.flush()
            os.fsync(sink.fileno())
        partial.chmod(MODE)

    def _sync_folder(self, folder: Path) -> None:
        """Make the names in ``folder`` durable."""
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _commit(self, version: str, by: str, partial: Path | None) -> None:
        """Make ``version`` the approved one, under ``lock``: its file takes its name in the
        store (``partial``, if there is one to rename), the link ``current`` is switched to
        it, and only then is the approval recorded, so that what is approved is always what
        the link names. A link that cannot be switched fails the rollout with nothing
        approved; an approval that cannot be recorded puts the link back. A version marked
        failed meanwhile, while the canary ran, is ``claude_version_failed``, with nothing
        changed."""
        with self.lock:
            if self._is_failed(version):
                raise Problem("claude_version_failed")
            if partial is not None and not self._usable(self.versions_dir / version):
                os.replace(partial, self.versions_dir / version)
                self._sync_folder(self.versions_dir)
            before = self._current()
            try:
                self._switch(version)
            except OSError:
                log.exception(
                    "the link %s could not be pointed at %s", self.store / CURRENT, version
                )
                raise
            try:
                self._approve(version, by)
            except BaseException:
                if before is not None:
                    try:
                        self._switch(before)
                    except OSError:
                        log.exception("the link %s could not be put back", self.store / CURRENT)
                raise

    # --- deleting a version by hand ----------------------------------------------------------

    @staticmethod
    def _kept_problem(reason: str) -> Problem:
        """``claude_version_kept``, with why in plain words and as ``reason``, one of
        problems.VERSION_KEPT_MESSAGES."""
        return Problem("claude_version_kept", title=VERSION_KEPT_MESSAGES[reason], reason=reason)

    def _kept_reason(self, version: str, approved: str | None, current: str | None, recorded):
        """Why this version, of its own, is not deleted: it is approved, ``current`` names it,
        or a workspace's record names it; None where nothing keeps it."""
        if version == approved:
            return "approved"
        if version == current:
            return "current"
        if version in recorded:
            return "in_use"
        return None

    def _kept_body(self, reason: str) -> dict:
        problem = self._kept_problem(reason)
        return problem_body(problem.code, problem.status, problem.title, **problem.extra)

    def delete(self, version: str) -> dict:
        """Remove ``version`` from the store, as an administrator asked; the report afterwards.
        ``claude_version_kept`` with its reason while a rollout, another deletion or a sweep
        runs (``rolling_out``) or an upgrade of rcman is under way (``rcman_upgrading``), and
        for the approved version, the one ``current`` names and one a workspace's record names
        (``in_use``); ``claude_version_not_installed`` where the store does not have it. The
        rollouts' lock is held throughout, so no rollout and no admission of an upgrade of rcman
        comes between the checks and the removal; the store and the records are read under
        ``lock``, which a server's start pins its version under. A mark as failed stays."""
        if not self._upgrading.acquire(blocking=False):
            raise self._kept_problem("rolling_out")
        try:
            # outside ``lock``: it may ask systemd
            if self.workspaces.upgrading():
                raise self._kept_problem("rcman_upgrading")
            with self.lock:
                if not rules.VERSION.fullmatch(version) or not self._usable(
                    self.versions_dir / version
                ):
                    raise Problem("claude_version_not_installed")
                if self.approved() is None:
                    self.reconcile()  # nothing is approved yet: adopt what the store names
                why = self._kept_reason(
                    version,
                    self.approved(),
                    self._current(),
                    set(self.workspaces.recorded_versions().values()),
                )
                if why is not None:
                    raise self._kept_problem(why)
                (self.versions_dir / version).unlink(missing_ok=True)
                log.info("Claude Code %s was removed from the store", version)
        finally:
            self._upgrading.release()
        return self.report()

    # --- pruning and sweeping ------------------------------------------------------------------

    def prune_quietly(self) -> None:
        """``prune``, which never makes what called it fail."""
        try:
            self.prune()
        except Exception:  # noqa: BLE001 - what it would remove is removed by the next one
            log.exception("the Claude Code store could not be pruned")

    def _kept(self) -> set[str | None]:
        """The versions pruning never removes: the approved, the previous (never one marked
        failed, so a marked version goes once nothing else keeps it), which an administrator
        can go back to from the page, and the one the link names."""
        return {self.approved(), self.previous(), self._current()}

    def prune(self) -> None:
        """Remove the versions in the store that are neither the approved, the previous nor
        the one the link names, and that no server which runs, cannot be read or is starting
        started with.

        What the servers say is asked without ``lock``, since a server that hangs would
        otherwise hold up every start and approval. Then, under ``lock``, what is to go is
        decided again from the database, and a version is also kept if a workspace's record
        differs from what it was before the asking: a start may have pinned it meanwhile.
        Nothing is done while a rollout or a deletion runs."""
        if not self.prunes or self._upgrading.locked():
            return
        candidates = set(self.installed()) - self._kept()
        if not candidates:
            return
        before = self.workspaces.recorded_versions()
        in_use = self.workspaces.versions_in_use(candidates)
        with self.lock:
            if self._upgrading.locked():
                return
            keep = self._kept()
            now = self.workspaces.recorded_versions()
            changed = {
                version for workspace, version in now.items() if before.get(workspace) != version
            }
            for version in sorted(set(self.installed()) - keep - in_use - changed):
                (self.versions_dir / version).unlink(missing_ok=True)
                log.info("Claude Code %s was removed from the store", version)

    def sweep(self) -> None:
        """Remove what a rollout that was cut short left: staging folders, partial files in
        the store, and a canary container. It does nothing while a rollout or a deletion
        runs."""
        if not self._upgrading.acquire(blocking=False):
            return
        try:
            if self.staging.is_dir():
                for child in self.staging.iterdir():
                    shutil.rmtree(child, ignore_errors=True)
            if self.versions_dir.is_dir():
                for partial in self.versions_dir.glob(f".*{claude_release.PARTIAL_SUFFIX}"):
                    partial.unlink(missing_ok=True)
                with self.lock:  # a version with nothing in it is no version
                    for file in self.versions_dir.iterdir():
                        if rules.VERSION.fullmatch(file.name) and file.is_file():
                            if file.stat().st_size == 0:
                                file.unlink(missing_ok=True)
            try:
                self.runtime.end_canary(CANARY_NAME)
            except (RuntimeFailed, RuntimeUnavailable, OSError) as failed:
                log.warning("a canary left from before could not be removed: %s", failed)
        finally:
            self._upgrading.release()

    # --- reporting ----------------------------------------------------------------------------

    def report(self) -> dict:
        """What the Versions page shows: the approved and previous versions, each
        version in the store with whether it is approved, previous or marked failed and how
        many servers use it and why it may not be deleted, if it may not (the problem a
        deletion would be refused with, for a reason of the version's own), the servers that
        run another version than the approved one and whether theirs is marked failed, the
        marks themselves (the newest first), whether a rollout or a deletion runs, and whether
        an upgrade of rcman is under way, which no rollout may start in."""
        with self.lock:
            approved, previous = self.approved(), self.previous()
            installed = self.installed()
            failures = self.failures()
            current = self._current()
            recorded = set(self.workspaces.recorded_versions().values())
        refused = {v: self._kept_reason(v, approved, current, recorded) for v in installed}
        failed = {f.version for f in failures}
        pinned = self.workspaces.pinned_servers()
        used: dict[str, int] = {}
        for _, version in pinned:
            used[version] = used.get(version, 0) + 1
        emails = self.workspaces.emails()
        return {
            "approved": approved,
            "previous": previous,
            "installed": [
                {
                    "version": v,
                    "approved": v == approved,
                    "previous": v == previous,
                    "in_use": used.get(v, 0),
                    "failed": v in failed,
                    "delete_refused": None if refused[v] is None else self._kept_body(refused[v]),
                }
                for v in installed
            ],
            "drift": [
                {
                    "workspace": w.id,
                    "name": w.name,
                    "email": emails.get(w.person),
                    "version": version,
                    "approved": approved,
                    "failed": version in failed,
                }
                for w, version in pinned
                if version != approved
            ],
            "failed": [
                {"version": f.version, "at": f.at, "by": f.by, "reason": f.reason} for f in failures
            ],
            "upgrading": self.rolling_out(),
            "rcman_upgrading": self.workspaces.upgrading(),
        }
