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


def test_author_migration_keeps_existing_prefixes(tmp_path):
    found = [i for i, script in enumerate(db.MIGRATIONS) if "author_name" in script]
    assert len(found) == 1, found
    number = found[0]
    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    for script in db.MIGRATIONS[:number]:
        con.executescript(script)
    con.execute(f"pragma user_version = {number}")
    con.execute("insert into user_settings (email, prefix) values ('bob@example.com', 'bobs')")
    con.commit()
    columns = {r[1] for r in con.execute("pragma table_info(user_settings)")}
    assert "prefix" in columns and "author_name" not in columns  # positive control
    con.close()

    db.migrate(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    assert dict(con.execute("select * from user_settings").fetchone()) == {
        "email": "bob@example.com",
        "prefix": "bobs",
        "author_name": None,
        "author_email": None,
    }
    con.execute("insert into user_settings (email, prefix) values ('carol@example.com', null)")
    assert con.execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)
