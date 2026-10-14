"""The database and its migrations."""

import sqlite3

from rcman import db


def test_migrate_creates_the_schema(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"audit", "user_settings", "people", "workspaces"} <= tables
    assert con.execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_the_schema_is_one_migration():
    """A fresh schema: no migration of an older one is kept."""
    assert len(db.MIGRATIONS) == 1


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    db.migrate(path)
    assert sqlite3.connect(path).execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_migrate_refuses_a_database_newer_than_the_code(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    db.migrate(path)  # positive control: its own version is taken
    con = sqlite3.connect(path)
    con.execute(f"pragma user_version = {len(db.MIGRATIONS) + 1}")
    con.commit()
    con.close()
    with pytest.raises(db.DatabaseTooNew):
        db.migrate(path)
    assert sqlite3.connect(path).execute("pragma user_version").fetchone()[0] == (
        len(db.MIGRATIONS) + 1
    )


def _person(con, email, slot, admin=0):
    con.execute(
        "insert into people (email, admin, slot, state) values (?, ?, ?, 'active')",
        (email, admin, slot),
    )


def test_an_email_is_one_persons_whatever_its_case(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "bob@example.com", 1)
    _person(con, "carol@example.com", 2)  # positive control: another address is taken
    with pytest.raises(sqlite3.IntegrityError):
        _person(con, "BOB@Example.com", 3)


def test_a_slot_is_one_persons_and_never_zero(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "bob@example.com", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _person(con, "carol@example.com", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _person(con, "carol@example.com", 0)  # rcman's own range


def test_a_person_with_workspaces_cannot_be_removed(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "bob@example.com", 1)
    con.execute("insert into workspaces (person, name, state) values (1, 'one', 'ready')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("delete from people where id = 1")
    con.execute("delete from workspaces")
    con.execute("delete from people where id = 1")  # positive control: without them it goes


def test_a_persons_workspace_names_are_unique(tmp_path):
    import pytest

    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "bob@example.com", 1)
    _person(con, "carol@example.com", 2)
    con.execute("insert into workspaces (person, name, state) values (1, 'one', 'ready')")
    con.execute("insert into workspaces (person, name, state) values (2, 'one', 'ready')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("insert into workspaces (person, name, state) values (1, 'one', 'creating')")


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
        "insert into audit (at, email, action, target, result) "
        "values ('2026-10-01', 'e', 'a', 't', 'ok')"
    )  # positive control: rows are added as usual
    con.commit()
    assert con.execute("select count(*) from audit").fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("update audit set result = 'forged'")
    assert con.execute("select result from audit").fetchone()[0] == "ok"


TRANSACTION_CONTROL = {"begin", "commit", "end", "rollback", "savepoint", "release"}


def statements(script: str) -> list[str]:
    """A script's statements, as SQLite splits them: a trigger's body stays in its statement."""
    found, pending = [], ""
    for part in script.split(";"):
        pending += part + ";"
        if sqlite3.complete_statement(pending):
            found.append(pending)
            pending = ""
    return found


def transaction_control(script: str) -> list[str]:
    """The statements of a script that begin, end or mark a transaction of their own."""
    import re

    # A quoted string or name is kept whole; a comment of either kind becomes a space.
    lexemes = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|/\*.*?(?:\*/|$)|--[^\n]*""", re.S)

    def uncommented(match: re.Match) -> str:
        return " " if match.group().startswith(("/*", "--")) else match.group()

    flagged = []
    for statement in statements(script):
        words = re.findall(r"\w+", lexemes.sub(uncommented, statement))
        if words and words[0].lower() in TRANSACTION_CONTROL:
            flagged.append(statement.strip())
    return flagged


def test_no_migration_controls_its_own_transaction():
    """migrate runs each script in a transaction with its version; a script must not end it."""
    trigger = next(s for s in statements(db.MIGRATIONS[0]) if "create trigger" in s)
    assert "begin" in trigger and "end;" in trigger  # positive control
    assert transaction_control(trigger) == []  # a trigger's begin ... end is not flagged
    flagged = {i: transaction_control(s) for i, s in enumerate(db.MIGRATIONS)}
    assert db.MIGRATIONS and all(v == [] for v in flagged.values()), flagged


def test_the_transaction_check_catches_a_script_that_commits():
    """Planted twin: a migration that commits, or begins, part way is caught."""
    assert transaction_control("create table t1(x); commit;") == ["commit;"]
    assert transaction_control("begin;\ncreate table t1(x);") == ["begin;"]
    assert transaction_control("create table t1(x);\n-- note\nSAVEPOINT a;") != []
    assert transaction_control("create table t(x); /* */ commit;") != []
    assert transaction_control("/* c */\ncommit;") != []
    assert transaction_control("commit/**/;") != []
    assert transaction_control("insert into t values ('/* x */ commit');") == []
    assert transaction_control("create table t1(x);") == []


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


def test_the_database_is_its_owners_alone_whatever_the_umask(tmp_path):
    """oauth2-proxy shares rcman's group, to read the allowed list; the database, which
    names everyone, is not for it."""
    import os
    import stat

    path = tmp_path / "x.db"
    old = os.umask(0o022)
    try:
        db.migrate(path)
        con = db.connect(path)
        con.execute("insert into audit (at, email, action, result) values ('t', 'e', 'a', 'ok')")
        con.commit()
        db.migrate(path)
    finally:
        os.umask(old)
    found = [p for p in tmp_path.iterdir() if p.name.startswith("x.db")]
    assert path in found  # positive control
    for file in found:
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file


def test_a_database_of_the_old_model_is_named_as_one_not_as_newer(tmp_path):
    """One written before workspaces, with no people table, cannot be upgraded."""
    import pytest

    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    con.executescript("create table user_settings (email text primary key, prefix text);")
    con.execute("pragma user_version = 4")
    con.commit()
    con.close()
    with pytest.raises(db.DatabaseOfAnOldInstall, match="install rcman afresh"):
        db.migrate(path)
