"""The SQLite database and its migrations.

Each entry of :data:`MIGRATIONS` is applied once, in order; ``user_version``
records how many have been applied. A script and its version are applied in
one transaction, so a script never begins or commits one of its own.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    create table audit (
        id integer primary key,
        at text not null,
        email text not null,
        action text not null,
        target text,
        result text not null
    );
    create index audit_at on audit (at);
    -- Audit events are never changed; retention deletes them, nothing else touches them.
    create trigger audit_append_only before update on audit
    begin
        select raise(abort, 'append-only');
    end;

    -- The people who may use rcman. Each has a slot of their own: the uid range their
    -- workspaces run in (workspaces.py), never another's and never rcman's own, 0.
    create table people (
        id integer primary key,
        email text not null unique collate nocase,
        admin integer not null check (admin in (0, 1)),
        slot integer not null unique check (slot >= 1),
        state text not null check (state in ('active', 'deleting'))
    );

    -- Each person's own settings. A setting left unset is null and means its default:
    -- the workspace's name for the prefix, the person's address for the author's email.
    create table user_settings (
        email text primary key collate nocase,
        prefix text,
        author_name text,
        author_email text
    );

    -- A person's workspaces, and what rcman brings back when it starts: whether a
    -- server is recorded in it (from its start until it is forgotten), whether it
    -- should be running, and the prefix it was started with.
    create table workspaces (
        id integer primary key,
        person integer not null references people (id),
        name text not null,
        state text not null check (state in ('creating', 'ready', 'failed', 'deleting')),
        error text,
        server integer not null default 0 check (server in (0, 1)),
        wanted integer not null default 0 check (wanted in (0, 1)),
        prefix text,
        unique (person, name)
    );
    """,
    """
    -- Each person's Claude account, as their last sign-in named it ({"oauthAccount": ...},
    -- seeded into a workspace before its server starts; null before a sign-in and after
    -- a sign-out), and whether a sign-out waits for their running servers to stop.
    alter table people add column claude_account text;
    alter table people add column claude_signing_out integer not null default 0
        check (claude_signing_out in (0, 1));
    """,
    """
    -- A person's GitHub token, kept for git in their workspaces (gitaccount.py): it is
    -- written into their volume first, and recorded here only once it is there.
    create table github_tokens (
        person integer primary key references people (id) on delete cascade,
        token text not null
    );
    """,
    """
    -- A workspace made by cloning a repository: the address as it was given (no credentials
    -- are in one, rules.GITHUB_URL), null for an empty one; and the steps rcman wrote while
    -- making it (workspaces.py), cut to their last characters, cleared once it is ready.
    alter table workspaces add column source text;
    alter table workspaces add column log text;
    """,
    """
    -- What a workspace made from a repository's devcontainer.json is made from, as JSON
    -- (devcontainer.Plan): the image, the user, the environment, the lifecycle commands and the
    -- keys that were ignored. Null for a workspace made from rcman's own image. A restore makes
    -- the container again from it.
    alter table workspaces add column environment text;
    """,
]


class DatabaseTooNew(Exception):
    """The database was written by a newer rcman than this one."""


class DatabaseOfAnOldInstall(Exception):
    """The database was written by an rcman from before workspaces: there is no upgrade."""


# The database names everyone, and oauth2-proxy shares rcman's group to read the allowed
# list: the database is its owner's alone. SQLite makes its -wal and -shm files with the
# database file's mode.
MODE = 0o600


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    for file in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        # The -wal and -shm files are removed when the last connection closes, so one that was
        # there a moment ago may not be.
        with contextlib.suppress(FileNotFoundError):
            if file.exists() and (file.stat().st_mode & 0o777) != MODE:
                file.chmod(MODE)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode = wal")
    con.execute("pragma foreign_keys = on")
    return con


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        version = con.execute("pragma user_version").fetchone()[0]
        tables = {row[0] for row in con.execute("select name from sqlite_master")}
        if version and "people" not in tables:
            raise DatabaseOfAnOldInstall(
                f"{path} was written by an rcman from before workspaces; install rcman "
                "afresh, as docs/installation.md says: there is no upgrade from it"
            )
        if version > len(MIGRATIONS):
            raise DatabaseTooNew(
                f"{path} is at schema version {version}, newer than this rcman's "
                f"{len(MIGRATIONS)}; install the rcman that wrote it"
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # A script and the version that records it are one transaction, so a
            # migration that stops part way leaves nothing behind.
            try:
                con.executescript(f"begin;\n{script}\npragma user_version = {number};\ncommit;")
            except BaseException:
                if con.in_transaction:
                    con.rollback()
                raise
    finally:
        con.close()
