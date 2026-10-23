"""The database and its migrations."""

import sqlite3

import pytest
from rcman import db


def test_migrate_creates_the_schema(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"audit", "user_settings", "people", "workspaces"} <= tables
    assert con.execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_a_database_of_the_first_schema_gains_each_persons_claude_account(tmp_path):
    """Migration 2 keeps every person, with no Claude account and none signing out."""
    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    con.executescript(f"begin;\n{db.MIGRATIONS[0]}\npragma user_version = 1;\ncommit;")
    con.execute("insert into people (email, admin, slot, state) values ('a@x.org', 1, 1, 'active')")
    con.commit()
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == len(db.MIGRATIONS) >= 2
    found = con.execute("select email, claude_account, claude_signing_out from people")
    assert found.fetchall() == [("a@x.org", None, 0)]


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


def migrated_before(tmp_path, marker: str) -> tuple:
    """A database migrated to just before the migration whose script holds ``marker``, with a
    person and a ready workspace of theirs: its path and its connection."""
    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    last = next(i for i, script in enumerate(db.MIGRATIONS) if marker in script)
    for number, script in enumerate(db.MIGRATIONS[:last], start=1):
        con.executescript(f"begin;\n{script}\npragma user_version = {number};\ncommit;")
    con.execute("insert into people (email, admin, slot, state) values ('a@x.org', 1, 1, 'active')")
    con.execute("insert into workspaces (person, name, state) values (1, 'old', 'ready')")
    con.commit()
    return path, con


def test_a_workspace_made_before_claude_versions_has_no_version_after_the_migration(tmp_path):
    path, con = migrated_before(tmp_path, "claude_approvals")
    assert "claude_version" not in [r[1] for r in con.execute("pragma table_info(workspaces)")]
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("select name, claude_version from workspaces").fetchall() == [("old", None)]
    assert con.execute("select count(*) from claude_approvals").fetchone() == (0,)


def test_a_workspace_made_before_plans_has_none_after_the_migration(tmp_path):
    path = tmp_path / "x.db"
    con = sqlite3.connect(path)
    last = next(i for i, script in enumerate(db.MIGRATIONS) if "add column environment" in script)
    for number, script in enumerate(db.MIGRATIONS[:last], start=1):
        con.executescript(f"begin;\n{script}\npragma user_version = {number};\ncommit;")
    con.execute("insert into people (email, admin, slot, state) values ('a@x.org', 1, 1, 'active')")
    con.execute("insert into workspaces (person, name, state) values (1, 'old', 'ready')")
    con.commit()
    assert "environment" not in [r[1] for r in con.execute("pragma table_info(workspaces)")]
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    found = con.execute("select name, state, environment from workspaces").fetchall()
    assert found == [("old", "ready", None)]


def test_a_connection_survives_a_wal_file_that_another_connection_removes_meanwhile(
    tmp_path, monkeypatch
):
    """The -wal and -shm files vanish when the last connection closes: one that was looked at
    and gone by the time it is touched is not a failure."""
    from pathlib import Path

    path = tmp_path / "x.db"
    db.migrate(path)
    real = Path.exists
    seen = []

    def exists(self):
        if self.name.endswith(("-shm", "-wal")) and not real(self):
            seen.append(self.name)
            return True  # looked at, and gone before it is touched
        return real(self)

    monkeypatch.setattr(Path, "exists", exists)
    db.connect(path).close()
    assert seen  # the plant took effect


def test_a_person_and_a_workspace_made_before_limits_have_none_after_the_migration(tmp_path):
    path, con = migrated_before(tmp_path, "memory_mib")
    for table in ("people", "workspaces"):
        assert "memory_mib" not in [r[1] for r in con.execute(f"pragma table_info({table})")]
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    for table in ("people", "workspaces"):
        columns = {r[1]: r for r in con.execute(f"pragma table_info({table})")}
        assert {"memory_mib", "cpu_percent", "pids"} <= set(columns), table
        for name in ("memory_mib", "cpu_percent", "pids"):
            assert columns[name][2] == "INTEGER" and columns[name][3] == 0, (table, name)  # null
    found = con.execute("select memory_mib, cpu_percent, pids from people").fetchall()
    assert found == [(None, None, None)]
    found = con.execute("select memory_mib, cpu_percent, pids from workspaces").fetchall()
    assert found == [(None, None, None)]


@pytest.mark.parametrize("table", ["people", "workspaces"])
@pytest.mark.parametrize("column", ["memory_mib", "cpu_percent", "pids"])
def test_a_limit_is_a_positive_number_or_null(tmp_path, column, table):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "a@x.org", 1)
    con.execute("insert into workspaces (person, name, state) values (1, 'w', 'ready')")
    row = "id = 1"
    con.execute(f"update {table} set {column} = 5 where {row}")  # positive control
    con.execute(f"update {table} set {column} = null where {row}")
    for refused in (0, -1):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(f"update {table} set {column} = ? where {row}", (refused,))


def test_the_id_of_a_person_or_a_workspace_is_never_given_again(tmp_path):
    """An id names a volume, a slice and a container: one given to the next row would adopt
    what the last one left."""
    path = tmp_path / "x.db"
    db.migrate(path)
    con = sqlite3.connect(path)
    for n in range(2):
        con.execute(
            "insert into people (email, admin, slot, state) values (?, 0, ?, 'active')",
            (f"p{n}@x.org", n + 1),
        )
    last = con.execute("select max(id) from people").fetchone()[0]
    con.execute("insert into workspaces (person, name, state) values (1, 'w', 'ready')")
    last_workspace = con.execute("select max(id) from workspaces").fetchone()[0]
    con.execute("delete from workspaces")
    con.execute("delete from people where id = ?", (last,))
    con.execute("insert into people (email, admin, slot, state) values ('q@x.org', 0, 2, 'active')")
    con.execute("insert into workspaces (person, name, state) values (1, 'w', 'ready')")
    assert con.execute("select max(id) from people").fetchone()[0] > last
    assert con.execute("select max(id) from workspaces").fetchone()[0] > last_workspace
    con.close()


def test_a_database_has_an_install_id_of_its_own_that_never_changes(tmp_path):
    """What tells volumes made for this database from those of another one with the same ids,
    a restored backup among them."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    for path in (first, second):
        db.migrate(path)
    found = []
    for path in (first, second):
        con = db.connect(path)
        try:
            found.append(db.install_id(con))
        finally:
            con.close()
    assert all(len(i) >= 16 and i.isalnum() for i in found) and found[0] != found[1]
    db.migrate(first)  # again
    con = db.connect(first)
    try:
        assert db.install_id(con) == found[0]
        assert con.execute("select count(*) from meta").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into meta (id, install) values (2, 'x')")
    finally:
        con.close()


def test_a_database_of_the_schema_before_the_install_id_gains_one(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    last = next(i for i, script in enumerate(db.MIGRATIONS) if "create table meta" in script)
    for number, script in enumerate(db.MIGRATIONS[:last], start=1):
        con.executescript(f"begin;\n{script}\npragma user_version = {number};\ncommit;")
    con.close()
    db.migrate(path)
    con = db.connect(path)
    try:
        assert db.install_id(con)
    finally:
        con.close()


REBUILD_COLUMNS = (
    "devcontainer_digest",
    "rebuild_state",
    "rebuild_log",
    "rebuild_error",
    "failure",
)


def test_a_workspace_made_before_rebuilds_has_none_of_their_columns_set_after_the_migration(
    tmp_path,
):
    """An old row's digest is unknown (null), and it is neither being rebuilt nor failed in a
    way that is known."""
    path, con = migrated_before(tmp_path, "rebuild_state")
    names = [r[1] for r in con.execute("pragma table_info(workspaces)")]
    assert "image_id" in names and not set(REBUILD_COLUMNS) & set(names)
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    found = con.execute(f"select name, {', '.join(REBUILD_COLUMNS)} from workspaces").fetchall()
    assert found == [("old", *(None,) * len(REBUILD_COLUMNS))]


def test_a_rebuild_state_is_one_of_its_phases_or_null(tmp_path):
    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    _person(con, "a@x.org", 1)
    con.execute("insert into workspaces (person, name, state) values (1, 'w', 'ready')")
    for phase in ("preparing", "switching", "promoting", None):
        con.execute("update workspaces set rebuild_state = ? where id = 1", (phase,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("update workspaces set rebuild_state = 'other' where id = 1")


def test_the_monitor_settings_are_one_row_with_switches_and_positive_intervals(tmp_path):
    """No row is made by the migration: a missing row is the defaults (monitor.py)."""
    path = tmp_path / "x.db"
    db.migrate(path)
    con = db.connect(path)
    assert con.execute("select count(*) from monitor_settings").fetchone()[0] == 0
    insert = (
        "insert into monitor_settings (id, usage_interval_seconds, disk_interval_seconds, "
        "host, people, containers) values (?, ?, ?, ?, ?, ?)"
    )
    con.execute(insert, (1, 60, 3600, 1, 0, 1))  # positive control
    for row in (
        (2, 60, 3600, 1, 1, 1),  # a second row
        (1, 60, 3600, 1, 1, 1),  # the first again
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, row)
    con.execute("delete from monitor_settings")
    for row in (
        (1, 0, 3600, 1, 1, 1),
        (1, 60, -1, 1, 1, 1),
        (1, 60, 3600, 2, 1, 1),
        (1, 60, 3600, 1, -1, 1),
        (1, 60, 3600, 1, 1, None),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, row)


def test_a_database_of_the_schema_before_the_monitor_gains_its_table_and_no_row(tmp_path):
    path, con = migrated_before(tmp_path, "monitor_settings")
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert "workspaces" in tables and "monitor_settings" not in tables
    con.close()
    db.migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("select count(*) from monitor_settings").fetchone()[0] == 0
    assert con.execute("select name from workspaces").fetchall() == [("old",)]
