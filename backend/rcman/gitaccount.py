"""A person's git account in their workspaces: the commit author and the GitHub token.

Both are kept in SQLite (``user_settings``, ``github_tokens``) and written into the
git folder of the person's volume, which every workspace of theirs binds at
``~/.config/git`` (runtime.py), so that git there — rcman's and Claude's — uses them
with nothing in an argv, and so does gh, which ``GH_CONFIG_DIR`` points at the same folder
(runtime.child_env). Four files, all rcman's: ``config``, which rcman owns whole (the author,
and a credential helper that only reads ``credential``, after an empty one that drops every
helper listed before it); ``credential`` (the token, owner-only, empty once forgotten);
``config.yml``, gh's configuration, which rcman owns whole and which says only its version;
and ``hosts.yml``, gh's hosts (the token, owner-only, empty once forgotten). Edits made to
them in a workspace are replaced by the next save.

A change writes the volume first and SQLite second, under the person's lock, so one
that fails changes nothing: a save whose write fails puts back the token files it had begun to
write (``config`` and ``config.yml`` are written as they already are). A
forget whose write fails puts nothing back, so the token is in fewer places, never more, and
its row stays for Remove to be tried again. Reading the state never asks the runtime. At every
restore, gh's files are written for everyone let in (``write_gh_for_everyone``), so that every
volume has them, whatever wrote it.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from rcman import db, people
from rcman.identity import Identity
from rcman.problems import Problem
from rcman.runtime import GH_CONFIG, GH_CONFIG_FILE, GH_HOSTS_FILE
from rcman.workspaces import Workspaces, answered

log = logging.getLogger("rcman.workspaces")

# What git asks of the credential helper for GitHub: the credential file, as it is.
CREDENTIAL_HELPER = '!f() { cat "$HOME/.config/git/credential"; }; f'
GITHUB = "https://github.com"
USERNAME = "x-access-token"
AUTHOR_COLUMNS = ("author_name", "author_email")


@dataclass(frozen=True)
class Author:
    name: str
    email: str


def author_in(row: Mapping | None, signed_in_as: str) -> Author | None:
    """Who a person's commits are recorded as, from their ``user_settings`` row: None
    until a name is set; the sign-in address stands in for an address left unset."""
    if row is None or row["author_name"] is None:
        return None
    return Author(name=row["author_name"], email=row["author_email"] or signed_in_as)


def _quote(value: str) -> str:
    """``value`` as git reads a quoted value of its configuration back unchanged."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def config_text(author: Author | None) -> str:
    """The whole of ``config``: the author, if there is one, and the credential helper."""
    lines = ["# Written by rcman. Changes made here are replaced when rcman writes it again."]
    if author is not None:
        lines += ["[user]", f"\tname = {_quote(author.name)}", f"\temail = {_quote(author.email)}"]
    # An empty helper empties the list git has built from the configuration read before this
    # file (the image's /etc/gitconfig): a helper that stores or caches what git approves would
    # keep the token where rcman does not forget it.
    lines += [f'[credential "{GITHUB}"]', "\thelper =", f"\thelper = {_quote(CREDENTIAL_HELPER)}"]
    return "\n".join(lines) + "\n"


def credential_text(token: str | None) -> str:
    """The whole of ``credential``: GitHub takes a token as the password of any user."""
    return "" if token is None else f"username={USERNAME}\npassword={token}\n"


def gh_config_text() -> str:
    """The whole of ``config.yml``: only the version of gh's configuration it is."""
    return GH_CONFIG


def gh_hosts_text(token: str | None) -> str:
    """The whole of ``hosts.yml``: the token for github.com, empty when there is none. The token
    is one single-quoted YAML string, in which a quote is doubled and nothing else is special;
    no ``user`` is named, since rcman does not know the account's login and gh reads the token
    without it."""
    if token is None:
        return ""
    quoted = "'" + token.replace("'", "''") + "'"
    return f"github.com:\n    oauth_token: {quoted}\n    git_protocol: https\n"


def token_files(token: str | None) -> dict[str, str]:
    """The files that hold the token, each whole, in the order a change writes them: empty
    ones where there is none."""
    return {GH_HOSTS_FILE: gh_hosts_text(token), "credential": credential_text(token)}


class GitAccount:
    def __init__(self, workspaces: Workspaces):
        self.workspaces = workspaces

    def _con(self) -> sqlite3.Connection:
        return db.connect(self.workspaces.settings.database)

    def token_stored(self, person: int) -> bool:
        con = self._con()
        try:
            found = con.execute("select 1 from github_tokens where person = ?", (person,))
            return found.fetchone() is not None
        finally:
            con.close()

    def _row(self, con: sqlite3.Connection, identity: Identity) -> sqlite3.Row | None:
        return con.execute(
            "select * from user_settings where email = ?", (identity.email,)
        ).fetchone()

    def _put(self, identity: Identity, name: str, text: str) -> None:
        with answered():
            self.workspaces.runtime.put_account(
                identity.person, identity.slot, name, text.encode("utf-8")
            )

    def _put_all(
        self, identity: Identity, files: dict[str, str], before: dict[str, str] | None
    ) -> None:
        """Write ``files`` in their order. Where one fails, the ones ``before`` names that were
        written, and the one whose write failed, which may have landed before the failure was
        said, are put back as it has them, and the failure is raised; with no ``before``,
        nothing is put back."""
        done = []
        try:
            for name, text in files.items():
                done.append(name)  # before the write: a failed one may have landed
                self._put(identity, name, text)
        except Exception:
            if before is not None:
                self._restore(identity, {n: before[n] for n in reversed(done) if n in before})
            raise

    def _check_still_let_in(self, identity: Identity, written: bool = True) -> None:
        """A person being deleted meanwhile has had their volume removed, or is about to:
        what was written for them is removed again, and the write is refused. The deletion
        marks the person before it removes the volume, so a write that finishes after the
        removal always sees the mark."""
        con = self._con()
        try:
            person = people.by_id(con, identity.person)
        finally:
            con.close()
        if person is not None and person.state == people.ACTIVE:
            return
        if written:
            with contextlib.suppress(Exception):
                self.workspaces.runtime.remove_account(identity.person)
        raise Problem("not_allowed")

    def _restore(self, identity: Identity, files: dict[str, str]) -> None:
        """Put ``files`` back in the volume after SQLite refused what followed their change,
        so that the two agree; the failure being handled is the one raised, not this one's.
        Not into the volume of a person being deleted meanwhile: that is removed instead."""
        try:
            self._check_still_let_in(identity)
        except Problem:
            return
        for name, text in files.items():
            try:
                self._put(identity, name, text)
            except Exception:  # noqa: BLE001 - the SQLite failure is what the caller must see
                log.warning("person %s: %s could not be put back", identity.person, name)

    def _stored_token(self, con: sqlite3.Connection, identity: Identity) -> str | None:
        found = con.execute(
            "select token from github_tokens where person = ?", (identity.person,)
        ).fetchone()
        return found and found[0]

    def save_token(self, identity: Identity, token: str) -> None:
        """Store the token, in place of any stored before, for git and gh in every
        workspace."""
        with self.workspaces.person_lock(identity.person):
            self._check_still_let_in(identity, written=False)
            con = self._con()
            try:
                author = author_in(self._row(con, identity), identity.email)
                before = token_files(self._stored_token(con, identity))
                # config and config.yml are written as they already are, once rcman has
                # written them: only the token files have a before to put back.
                files = {"config": config_text(author), GH_CONFIG_FILE: gh_config_text()}
                self._put_all(identity, files | token_files(token), before)
                self._check_still_let_in(identity)
                try:
                    con.execute(
                        "insert into github_tokens (person, token) values (?, ?) "
                        "on conflict (person) do update set token = excluded.token",
                        (identity.person, token),
                    )
                    con.commit()
                except Exception:
                    con.rollback()  # release SQLite's lock before anything else is done
                    self._restore(identity, before)
                    raise
            finally:
                con.close()

    def forget_token(self, identity: Identity) -> None:
        """Forget the token: git and gh in every workspace have none from their next command
        on. A write that fails puts nothing back and keeps the row, so the token is in fewer
        places, never more, and forgetting it can be tried again."""
        with self.workspaces.person_lock(identity.person):
            self._check_still_let_in(identity, written=False)
            con = self._con()
            try:
                before = token_files(self._stored_token(con, identity))
                self._put_all(identity, token_files(None), None)
                self._check_still_let_in(identity)
                try:
                    con.execute("delete from github_tokens where person = ?", (identity.person,))
                    con.commit()
                except Exception:
                    con.rollback()  # release SQLite's lock before anything else is done
                    self._restore(identity, before)
                    raise
            finally:
                con.close()

    def write_gh_for_everyone(self) -> None:
        """Write gh's files for every person let in, each under their lock: ``config.yml``
        for all, and ``hosts.yml`` for those with a token stored, read under the lock. One
        no longer let in is skipped, and one whose write fails is logged; the others are
        written still. A restore hook (workspaces.py), run at every start."""
        con = self._con()
        try:
            everyone = [p for p in people.everyone(con) if p.state == people.ACTIVE]
        finally:
            con.close()
        for person in everyone:
            identity = Identity(
                email=person.email, admin=person.admin, person=person.id, slot=person.slot
            )
            try:
                self._write_gh(identity)
            except Problem as refused:
                if refused.code != "not_allowed":  # no longer let in: nothing is theirs
                    log.warning(
                        "person %s: gh's files were not written: %s", person.id, refused.code
                    )
            except Exception:  # noqa: BLE001 - one person never stops the others
                log.exception("person %s: gh's files were not written", person.id)

    def _write_gh(self, identity: Identity) -> None:
        with self.workspaces.person_lock(identity.person):
            self._check_still_let_in(identity, written=False)
            con = self._con()
            try:
                token = self._stored_token(con, identity)
            finally:
                con.close()
            files = {GH_CONFIG_FILE: gh_config_text()}
            if token is not None:
                files[GH_HOSTS_FILE] = gh_hosts_text(token)
            self._put_all(identity, files, None)
            self._check_still_let_in(identity)

    def save_settings(self, identity: Identity, values: dict[str, str | None]) -> None:
        """Set the settings in ``values`` (column names of ``user_settings`` other than the
        address, and their new values, None for the default). A change of the author is
        written into the volume's ``config`` first."""
        columns = list(values)
        with self.workspaces.person_lock(identity.person):
            self._check_still_let_in(identity, written=False)
            con = self._con()
            try:
                before = None
                if any(name in AUTHOR_COLUMNS for name in columns):
                    row = self._row(con, identity)
                    before = config_text(author_in(row, identity.email))
                    merged = {name: row[name] if row else None for name in AUTHOR_COLUMNS}
                    merged.update({n: v for n, v in values.items() if n in AUTHOR_COLUMNS})
                    author = author_in(merged, identity.email)
                    self._put(identity, "config", config_text(author))
                    self._check_still_let_in(identity)
                try:
                    # The names are the settings model's own fields, never anything sent.
                    con.execute(
                        f"insert into user_settings (email, {', '.join(columns)}) "
                        f"values (?{', ?' * len(columns)}) on conflict (email) do update set "
                        + ", ".join(f"{name} = excluded.{name}" for name in columns),
                        (identity.email, *values.values()),
                    )
                    con.commit()
                except Exception:
                    con.rollback()
                    if before is not None:
                        self._restore(identity, {"config": before})
                    raise
            finally:
                con.close()
