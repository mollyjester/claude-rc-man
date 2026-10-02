"""The database and its migrations."""

import sqlite3

from rcman import db


def test_migrate_creates_the_schema(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"audit", "user_settings"} <= tables
    assert con.execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    db.migrate(path)
    assert sqlite3.connect(path).execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_migrate_applies_only_what_is_missing(tmp_path):
    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    con.executescript(db.MIGRATIONS[0])
    con.execute("pragma user_version = 1")
    con.commit()
    db.migrate(path)
    assert sqlite3.connect(path).execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_connections_use_wal_and_foreign_keys(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    assert con.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert con.execute("pragma foreign_keys").fetchone()[0] == 1


def test_database_directory_is_created(tmp_path):
    path = tmp_path / "deep" / "x.db"
    db.migrate(path)
    assert path.exists()


def test_audit_rows_cannot_be_changed(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    con.execute(
        "insert into audit (at, email, linux, action, target, result) "
        "values ('2026-10-01', 'e', 'l', 'a', 't', 'ok')"
    )  # positive control: rows are added as usual
    con.commit()
    assert con.execute("select count(*) from audit").fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("update audit set result = 'forged'")
    assert con.execute("select result from audit").fetchone()[0] == "ok"
