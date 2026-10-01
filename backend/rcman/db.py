"""The SQLite database and its migrations.

Each entry of :data:`MIGRATIONS` is applied once, in order; ``user_version``
records how many have been applied.
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
            con.executescript(script)
            con.execute(f"pragma user_version = {number}")
            con.commit()
    finally:
        con.close()
