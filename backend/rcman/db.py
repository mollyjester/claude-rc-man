"""The SQLite database and its migrations.

Each entry of :data:`MIGRATIONS` is applied once, in order; ``user_version``
records how many have been applied. A script and its version are applied in
one transaction, so a script never begins or commits one of its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    create table audit (
        id integer primary key,
        at text not null,
        email text not null,
        linux text not null,
        action text not null,
        target text,
        result text not null
    );
    create index audit_at on audit (at);
    """,
    """
    create table user_settings (
        email text primary key,
        prefix text not null
    );
    """,
    # Audit events are never changed; retention deletes them, nothing else touches them.
    """
    create trigger audit_append_only before update on audit
    begin
        select raise(abort, 'append-only');
    end;
    """,
    # The commit author. A setting left unset is null and means its default:
    # the Linux user for the prefix, the users file's address for the email.
    """
    create table user_settings_new (
        email text primary key,
        prefix text,
        author_name text,
        author_email text
    );
    insert into user_settings_new (email, prefix) select email, prefix from user_settings;
    drop table user_settings;
    alter table user_settings_new rename to user_settings;
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode = wal")
    con.execute("pragma foreign_keys = on")
    return con


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        version = con.execute("pragma user_version").fetchone()[0]
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
