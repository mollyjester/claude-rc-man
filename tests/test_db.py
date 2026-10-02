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


def test_a_migration_that_fails_part_way_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    con.execute("insert into user_settings (email, prefix) values ('bob@example.com', 'bobs')")
    con.commit()
    con.close()
    version = len(db.MIGRATIONS)
    planted = """
    create table user_settings_new (email text primary key);
    drop table user_settings;
    insert into no_such_table values (1);
    """
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, planted])
    import pytest

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        db.migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == version
    assert con.execute("select email, prefix from user_settings").fetchall() == [
        ("bob@example.com", "bobs")
    ]
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert "user_settings_new" not in tables
    con.close()
    fixed = planted.replace("insert into no_such_table values (1);", "")
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:-1], fixed])
    db.migrate(path)  # and the corrected migration then runs
    assert sqlite3.connect(path).execute("pragma user_version").fetchone()[0] == version + 1


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
