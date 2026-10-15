"""backend/rcman/gitaccount.py: a person's commit author and GitHub token, written into their
volume's git folder and kept in SQLite, against a host runtime that records what it is asked."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from rcman import db, people
from rcman import runtime as rt
from rcman.gitaccount import CREDENTIAL_HELPER, Author, config_text, credential_text
from rcman.runtime import container_name

from tests.conftest import ALICE, BOB, as_user, changing, host_runtime, made_workspace

TOKEN = "ghp_a-token-for-the-tests"


class Recording(rt.HostRuntime):
    """The host runtime, noting every call that could carry the token, and able to fail
    a write or to look at the database as a write begins."""

    def __init__(self, *args):
        super().__init__(*args)
        self.puts: list[tuple[int, int, str, bytes]] = []
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.fail: Exception | None = None
        self.during = lambda name: None

    def put_account(self, person, slot, name, data):
        self.puts.append((person, slot, name, data))
        self.during(name)
        if self.fail is not None:
            raise self.fail
        super().put_account(person, slot, name, data)

    def exec(self, name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT):
        self.execs.append((argv, stdin))
        return super().exec(name, argv, stdin, timeout)


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Recording) as made:
        yield made


def git_dir(runtime, person: int = 1) -> Path:
    return runtime.root / rt.HostRuntime.ACCOUNTS / str(person) / rt.GIT_DIR


def written(runtime, name: str, person: int = 1) -> str:
    return (git_dir(runtime, person) / name).read_text()


def git_config(file: Path, key: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "config", "--file", str(file), "--get", key],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
    )


def save(client, token=TOKEN, who=ALICE):
    return client.post("/api/github/token", json={"token": token}, headers=changing(who))


def forget(client, who=ALICE):
    return client.delete("/api/github/token", headers=changing(who))


def patch(client, body, who=ALICE):
    return client.patch("/api/me/settings", json=body, headers=changing(who))


def token_rows(app) -> list[tuple[int, str]]:
    con = db.connect(app.state.settings.database)
    try:
        return [tuple(r) for r in con.execute("select person, token from github_tokens")]
    finally:
        con.close()


def fill(home: str, host: str = "github.com") -> subprocess.CompletedProcess:
    """git's credential for ``host``, as a git in a workspace with this HOME asks for it."""
    return subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": home,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        },
    )


# --- what is written ---------------------------------------------------------------


NASTY = [
    "Ann Author",
    'Ann "The Quote" O\'Neil',
    "back\\slash",
    "hash # and semicolon ;",
    " spaced out ",
    "tab\there",
    "line\nbreak",
    "ünïcode ✓",
    "[section] = value",
    '\\"',
]


@pytest.mark.parametrize("name", NASTY)
def test_a_name_and_an_address_are_read_back_by_git_as_they_were_written(tmp_path, name):
    file = tmp_path / "config"
    file.write_text(config_text(Author(name, f"{name.strip()[:5] or 'x'}@example.com")))
    assert git_config(file, "user.name").stdout == name + "\n"
    assert git_config(file, "user.email").returncode == 0


def test_the_credential_helper_is_read_back_as_written(tmp_path):
    file = tmp_path / "config"
    file.write_text(config_text(None))
    found = git_config(file, "credential.https://github.com.helper")
    assert found.stdout == CREDENTIAL_HELPER + "\n"
    assert "$HOME/.config/git/credential" in CREDENTIAL_HELPER
    assert git_config(file, "user.name").returncode == 1  # no author, no [user]


def test_the_credential_is_the_token_as_the_password_of_a_fixed_user():
    assert credential_text(TOKEN) == f"username=x-access-token\npassword={TOKEN}\n"
    assert credential_text(None) == ""


# --- the token ----------------------------------------------------------------------


def test_a_saved_token_is_in_the_volume_and_in_sqlite_and_nowhere_else(client, app, runtime):
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}
    r = save(client)
    assert (r.status_code, r.json()) == (200, {"token": True})
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": True}
    assert client.get("/api/github", headers=as_user(BOB)).json() == {"token": False}
    assert written(runtime, "credential") == f"username=x-access-token\npassword={TOKEN}\n"
    assert ((git_dir(runtime) / "credential").stat().st_mode & 0o777) == 0o600
    assert "credential" in written(runtime, "config")  # git is told to use it
    assert token_rows(app) == [(1, TOKEN)]
    assert TOKEN not in r.text


def test_the_token_is_only_ever_data_given_to_the_write(client, runtime):
    save(client)
    assert [name for _, _, name, data in runtime.puts if TOKEN.encode() in data] == ["credential"]
    for argv, stdin in runtime.execs:
        assert TOKEN not in " ".join(argv) and TOKEN.encode() not in (stdin or b"")
    assert TOKEN not in written(runtime, "config")


def test_a_token_needs_no_workspace_and_no_running_server(client, runtime):
    assert runtime.labelled() == {}  # positive control: there is none
    assert save(client).status_code == 200
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")


def test_git_in_a_workspace_gets_the_token_for_github_only(client, app, runtime):
    workspace = made_workspace(client, ALICE)
    home = runtime.layout(container_name(workspace["id"])).home
    assert fill(home).returncode != 0  # positive control: nothing yet
    save(client)
    got = fill(home)
    assert got.returncode == 0, got.stderr
    assert "username=x-access-token" in got.stdout and f"password={TOKEN}" in got.stdout
    other = fill(home, host="example.com")
    assert TOKEN not in other.stdout and TOKEN not in other.stderr


def test_a_replaced_token_replaces_the_old_in_every_place(client, app, runtime):
    save(client)
    save(client, token="the-second-token")
    assert written(runtime, "credential").endswith("password=the-second-token\n")
    assert token_rows(app) == [(1, "the-second-token")]


def test_forgetting_empties_the_credential_at_once_in_every_workspace(client, app, runtime):
    first = made_workspace(client, ALICE, "one")
    second = made_workspace(client, ALICE, "two")
    homes = [runtime.layout(container_name(w["id"])).home for w in (first, second)]
    save(client)
    assert all(fill(h).returncode == 0 for h in homes)  # positive control
    r = forget(client)
    assert (r.status_code, r.json()) == (200, {"token": False})
    assert written(runtime, "credential") == ""
    for home in homes:
        refused = fill(home)
        assert refused.returncode != 0 and TOKEN not in refused.stdout + refused.stderr
    assert token_rows(app) == []
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}
    assert forget(client).status_code == 200  # forgetting what is not there is nothing


def test_a_person_has_their_own_token(client, app, runtime):
    save(client)
    save(client, token="bobs-token", who=BOB)
    assert written(runtime, "credential", person=2).endswith("password=bobs-token\n")
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")
    forget(client, who=BOB)
    assert token_rows(app) == [(1, TOKEN)]


def test_the_status_is_answered_from_sqlite_without_asking_the_runtime(
    client, app, runtime, monkeypatch
):
    save(client)

    def asked(*args, **kwargs):
        raise AssertionError("the runtime was asked")

    for name in rt.Runtime.__abstractmethods__:
        monkeypatch.setattr(runtime, name, asked)
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": True}


def test_the_volume_is_written_before_sqlite(client, app, runtime):
    seen = []
    runtime.during = lambda name: seen.append((name, token_rows(app)))
    save(client)
    assert ("credential", []) in seen  # the row is not there while the credential is written
    seen.clear()
    forget(client)
    assert seen == [("credential", [(1, TOKEN)])]  # and it is still there when it is emptied


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (rt.RuntimeFailed("boom"), 502, "workspace_failed"),
        (rt.RuntimeUnavailable("down"), 503, "runtime_unavailable"),
    ],
)
def test_a_failed_write_changes_nothing(client, app, runtime, failure, status, code):
    runtime.fail = failure
    r = save(client)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert runtime.puts  # positive control: the write was tried
    assert token_rows(app) == []
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}
    runtime.fail = None
    assert save(client).status_code == 200  # the same call works once the runtime does
    runtime.fail = failure
    r = forget(client)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert token_rows(app) == [(1, TOKEN)]  # the token stays while the credential does
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")


def test_a_save_and_a_forget_take_the_persons_lock(client, app, runtime):
    lock = app.state.workspaces.person_lock(1)
    done = threading.Event()
    lock.acquire()
    try:
        worker = threading.Thread(target=lambda: (save(client), done.set()), daemon=True)
        worker.start()
        assert not done.wait(0.5)  # waits for the lock
    finally:
        lock.release()
    assert done.wait(10)
    assert token_rows(app) == [(1, TOKEN)]


def test_a_persons_token_goes_with_them(app, settings):
    con = db.connect(settings.database)
    try:
        person = people.by_email(con, BOB)
        con.execute("insert into github_tokens (person, token) values (?, ?)", (person.id, "t"))
        con.commit()
        assert con.execute("select count(*) from github_tokens").fetchone()[0] == 1
        people.remove(con, person)
        assert con.execute("select count(*) from github_tokens").fetchone()[0] == 0
    finally:
        con.close()


# --- the author -----------------------------------------------------------------------


def test_an_author_name_is_written_with_the_sign_in_address_as_the_default(client, runtime):
    config = git_dir(runtime) / "config"
    assert patch(client, {"author_name": "Ann Author"}).status_code == 200
    assert git_config(config, "user.name").stdout == "Ann Author\n"
    assert git_config(config, "user.email").stdout == f"{ALICE}\n"
    patch(client, {"author_email": "ann@elsewhere.example.com"})
    assert git_config(config, "user.email").stdout == "ann@elsewhere.example.com\n"
    patch(client, {"author_email": ""})
    assert git_config(config, "user.email").stdout == f"{ALICE}\n"  # the default again
    assert patch(client, {"author_name": ""}).status_code == 422  # a name cannot be unset
    assert git_config(config, "user.name").stdout == "Ann Author\n"


def test_a_token_saved_before_any_author_writes_a_config_with_no_author(client, runtime):
    save(client)
    config = git_dir(runtime) / "config"
    assert git_config(config, "user.name").returncode == 1  # no author, no [user]
    assert git_config(config, "credential.https://github.com.helper").returncode == 0


def test_an_author_is_written_beside_the_token_and_the_token_keeps_the_author(client, runtime):
    config = git_dir(runtime) / "config"
    patch(client, {"author_name": "Ann Author"})
    save(client)
    assert git_config(config, "user.name").stdout == "Ann Author\n"
    patch(client, {"author_name": "Ann B. Author"})
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")  # untouched


def test_settings_that_are_no_author_write_nothing_to_the_volume(client, runtime):
    assert patch(client, {"prefix": "alices-box"}).status_code == 200
    assert runtime.puts == []


def test_the_author_of_one_person_is_not_another_persons(client, runtime):
    patch(client, {"author_name": "Ann Author"})
    patch(client, {"author_name": "Bob Builder"}, who=BOB)
    assert git_config(git_dir(runtime, 2) / "config", "user.email").stdout == f"{BOB}\n"
    assert git_config(git_dir(runtime) / "config", "user.name").stdout == "Ann Author\n"


def test_a_failed_write_of_the_author_changes_nothing(client, runtime):
    patch(client, {"author_name": "Ann Author"})
    runtime.fail = rt.RuntimeFailed("boom")
    r = patch(client, {"author_name": "Someone Else", "prefix": "other"})
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    after = client.get("/api/me/settings", headers=as_user(ALICE)).json()
    assert after["author_name"] == "Ann Author" and after["prefix"] == ""  # the row is as it was
    assert git_config(git_dir(runtime) / "config", "user.name").stdout == "Ann Author\n"
    runtime.fail = None
    assert patch(client, {"author_name": "Someone Else"}).status_code == 200  # positive control


def test_the_config_is_written_before_the_row(client, app, runtime):
    seen = []

    def during(name):
        con = db.connect(app.state.settings.database)
        try:
            row = con.execute("select author_name from user_settings").fetchone()
        finally:
            con.close()
        seen.append((name, row and row[0]))

    runtime.during = during
    patch(client, {"author_name": "Ann Author"})
    assert seen == [("config", None)]  # nothing recorded yet while the file is written


# --- a person deleted while a write is under way ------------------------------------


def mark_deleting(app, person: int = 1) -> None:
    con = db.connect(app.state.settings.database)
    try:
        people.mark_deleting(con, people.by_id(con, person))
    finally:
        con.close()


@pytest.mark.parametrize("what", ["save", "forget", "author"])
def test_a_write_racing_a_deletion_leaves_no_volume_and_no_token(client, app, runtime, what):
    """The deletion marks the person and removes their volume while a write has already
    begun: the write notices once it is done, and removes what it made."""
    if what == "forget":
        save(client)
    runtime.during = lambda name: mark_deleting(app)
    call = {
        "save": lambda: save(client),
        "forget": lambda: forget(client),
        "author": lambda: patch(client, {"author_name": "Ann Author"}),
    }[what]
    assert call().json()["code"] == "not_allowed"
    assert runtime.puts  # positive control: the write was made
    assert not git_dir(runtime).exists()  # and its volume removed again
    if what == "save":
        assert token_rows(app) == []


def test_a_write_by_a_person_still_let_in_removes_nothing(client, runtime):
    assert save(client).status_code == 200
    assert git_dir(runtime).is_dir()


# --- the two stores never disagree --------------------------------------------------


def break_sqlite(app, table: str) -> None:
    """Every write to ``table`` is refused from now on."""
    con = db.connect(app.state.settings.database)
    try:
        for event in ("insert", "update", "delete"):
            con.execute(
                f"create trigger broken_{event} before {event} on {table} "
                "begin select raise(abort, 'refused'); end"
            )
        con.commit()
    finally:
        con.close()


def test_a_save_that_sqlite_refuses_puts_the_volume_back_as_it_was(client, app, runtime):
    patch(client, {"author_name": "Ann Author"})
    config = written(runtime, "config")
    save(client, token="the-first-token")
    break_sqlite(app, "github_tokens")
    with pytest.raises(sqlite3.Error):
        save(client, token="the-second-token")
    assert written(runtime, "credential").endswith("password=the-first-token\n")
    assert written(runtime, "config") == config
    assert "the-second-token" not in written(runtime, "credential")


def test_a_first_save_that_sqlite_refuses_leaves_the_credential_empty(client, app, runtime):
    break_sqlite(app, "github_tokens")
    with pytest.raises(sqlite3.Error):
        save(client)
    assert written(runtime, "credential") == ""
    assert TOKEN not in written(runtime, "config")


def test_a_forget_that_sqlite_refuses_puts_the_token_back(client, app, runtime):
    save(client)
    break_sqlite(app, "github_tokens")
    with pytest.raises(sqlite3.Error):
        forget(client)
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")
    assert token_rows(app) == [(1, TOKEN)]


def test_an_author_that_sqlite_refuses_puts_the_config_back(client, app, runtime):
    patch(client, {"author_name": "Ann Author"})
    config = written(runtime, "config")
    break_sqlite(app, "user_settings")
    with pytest.raises(sqlite3.Error):
        patch(client, {"author_name": "Someone Else"})
    assert written(runtime, "config") == config
    assert git_config(git_dir(runtime) / "config", "user.name").stdout == "Ann Author\n"


def test_a_restore_that_fails_too_does_not_hide_the_first_failure(client, app, runtime):
    save(client)
    break_sqlite(app, "github_tokens")
    runtime.during = lambda name: setattr(runtime, "fail", None)
    puts = runtime.put_account

    def once(person, slot, name, data):
        if len(runtime.puts) >= 3:  # the restore: the volume is gone
            raise rt.RuntimeFailed("gone")
        puts(person, slot, name, data)

    runtime.put_account = once
    with pytest.raises(sqlite3.Error):
        forget(client)


def test_a_restore_does_not_put_a_token_back_into_the_volume_of_a_person_being_deleted(
    client, app, runtime, monkeypatch
):
    from rcman.gitaccount import GitAccount

    save(client, token="the-first-token")
    break_sqlite(app, "github_tokens")
    real = GitAccount._check_still_let_in
    calls = []

    def checked(self, identity, written=True):
        calls.append(written)
        if len(calls) <= 2:  # at the start and before the SQLite write: still let in
            return
        mark_deleting(app)  # the deletion comes between that and the restore
        return real(self, identity, written)

    monkeypatch.setattr(GitAccount, "_check_still_let_in", checked)
    puts = len(runtime.puts)
    with pytest.raises(sqlite3.Error):
        save(client, token="the-second-token")
    assert len(calls) == 3  # positive control: the restore looked first
    assert len(runtime.puts) == puts + 2  # config and credential: nothing was put back
    assert not git_dir(runtime).exists()  # and what was written is removed
