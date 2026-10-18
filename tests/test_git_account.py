"""backend/rcman/gitaccount.py: a person's commit author and GitHub token, written into their
volume's git folder and kept in SQLite, against a host runtime that records what it is asked."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from rcman import claude_server, db, people
from rcman import runtime as rt
from rcman.gitaccount import (
    CREDENTIAL_HELPER,
    Author,
    config_text,
    credential_text,
    gh_config_text,
    gh_hosts_text,
)
from rcman.runtime import container_name

from tests.conftest import ALICE, BOB, CAROL, as_user, changing, host_runtime, made_workspace
from tests.github_tokens import AWKWARD_TOKENS

TOKEN = "ghp_a-token-for-the-tests"


class Recording(rt.HostRuntime):
    """The host runtime, noting every call that could carry the token, and able to fail
    a write or to look at the database as a write begins."""

    def __init__(self, *args):
        super().__init__(*args)
        self.puts: list[tuple[int, int, str, bytes]] = []
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.fail: Exception | None = None
        # Where ``fail`` is raised: every write, or only the writes of these files.
        self.fail_only: set[str] | None = None
        # Files whose next write lands and is then said to have failed, as podman may say
        # when the container's cleanup or its timeout fails after the rename: once each.
        self.landed_but_failed: set[str] = set()
        self.during = lambda name: None

    def put_account(self, person, slot, name, data):
        self.puts.append((person, slot, name, data))
        self.during(name)
        if self.fail is not None and (self.fail_only is None or name in self.fail_only):
            raise self.fail
        super().put_account(person, slot, name, data)
        if name in self.landed_but_failed:
            self.landed_but_failed.discard(name)
            raise rt.RuntimeFailed("the write landed, then podman failed")

    def exec(
        self, name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT, cwd=None, keep=None, again=False
    ):
        self.execs.append((argv, stdin))
        return super().exec(name, argv, stdin, timeout, cwd, keep, again)


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Recording) as made:
        yield made


def git_dir(runtime, person: int = 1) -> Path:
    return runtime.root / rt.HostRuntime.ACCOUNTS / str(person) / rt.GIT_DIR


def written(runtime, name: str, person: int = 1) -> str:
    return (git_dir(runtime, person) / name).read_text()


def git_config(file: Path, key: str, all: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "config", "--file", str(file), "--get-all" if all else "--get", key],
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


def test_the_config_empties_the_list_of_helpers_before_its_own(tmp_path):
    file = tmp_path / "config"
    file.write_text(config_text(None))
    found = git_config(file, "credential.https://github.com.helper", all=True)
    assert found.stdout.split("\n")[:-1] == ["", CREDENTIAL_HELPER]


def test_no_helper_of_the_image_or_the_user_is_asked_for_a_github_credential(tmp_path):
    """The image's /etc/gitconfig may name a helper that stores what git approves (store,
    cache): the token would be saved elsewhere, in a file the person can read and rcman does
    not forget. Git approves a credential it was given with every helper that is listed."""
    leak = tmp_path / "leak"
    system = tmp_path / "gitconfig"
    system.write_text(f'[credential]\n\thelper = "!f() {{ cat >> {leak}; }}; f"\n')
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_SYSTEM": str(system),
    }
    approval = "protocol=https\nhost=github.com\nusername=x\npassword=SECRET\n\n"

    def approve() -> None:
        subprocess.run(
            ["git", "credential", "approve"], input=approval, text=True, env=env, check=True
        )

    approve()
    assert "SECRET" in leak.read_text()  # the control: with no config of rcman's, it is stored
    leak.unlink()
    (home / ".config" / "git" / "config").write_text(config_text(None))
    (home / ".config" / "git" / "credential").write_text(credential_text(TOKEN))
    approve()
    assert not leak.exists()


def test_the_credential_is_the_token_as_the_password_of_a_fixed_user():
    assert credential_text(TOKEN) == f"username=x-access-token\npassword={TOKEN}\n"
    assert credential_text(None) == ""


def test_gh_is_told_its_configuration_is_of_the_current_version_and_nothing_else():
    """gh migrates a configuration with no version on every command, and rewrites it."""
    assert gh_config_text() == 'version: "1"\n' == rt.GH_CONFIG


def single_quoted(text: str) -> str:
    """The YAML single-quoted scalar ``text`` starts with, as YAML reads it: between the
    quotes, a doubled quote is one quote and nothing else is special; a lone quote ends it."""
    assert text.startswith("'"), text
    found, i = [], 1
    while True:
        if text[i] == "'":
            if text[i + 1 : i + 2] == "'":
                found.append("'")
                i += 2
                continue
            assert text[i + 1 :] == "", text  # nothing after the closing quote
            return "".join(found)
        found.append(text[i])
        i += 1


@pytest.mark.parametrize("token", AWKWARD_TOKENS)
def test_every_token_the_rule_allows_is_one_single_quoted_string_in_hosts_yml(token):
    from helpers import rules

    assert rules.GITHUB_TOKEN.fullmatch(token)  # one the rule lets in
    lines = gh_hosts_text(token).split("\n")
    assert lines[-1] == ""  # it ends with a newline
    head, oauth, protocol = lines[:-1]
    assert head == "github.com:"
    assert protocol == "    git_protocol: https"
    assert oauth.startswith("    oauth_token: ")
    assert single_quoted(oauth.removeprefix("    oauth_token: ")) == token


def test_no_token_is_an_empty_hosts_yml():
    assert gh_hosts_text(None) == ""


# --- the token ----------------------------------------------------------------------


def test_a_saved_token_is_in_the_volume_and_in_sqlite_and_nowhere_else(client, app, runtime):
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}
    r = save(client)
    assert (r.status_code, r.json()) == (200, {"token": True})
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": True}
    assert client.get("/api/github", headers=as_user(BOB)).json() == {"token": False}
    assert written(runtime, "credential") == f"username=x-access-token\npassword={TOKEN}\n"
    assert written(runtime, "hosts.yml") == gh_hosts_text(TOKEN)
    for name in ("credential", "hosts.yml"):
        assert ((git_dir(runtime) / name).stat().st_mode & 0o777) == 0o600, name
    assert "credential" in written(runtime, "config")  # git is told to use it
    assert written(runtime, "config.yml") == gh_config_text()
    assert token_rows(app) == [(1, TOKEN)]
    assert TOKEN not in r.text


def test_the_token_is_only_ever_data_given_to_the_write(client, runtime):
    save(client)
    holding = sorted(name for _, _, name, data in runtime.puts if TOKEN.encode() in data)
    assert holding == ["credential", "hosts.yml"]
    for argv, stdin in runtime.execs:
        assert TOKEN not in " ".join(argv) and TOKEN.encode() not in (stdin or b"")
    for name in sorted(set(rt.ACCOUNT_FILES) - set(holding)):
        assert TOKEN not in written(runtime, name), name


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


def gh_config_dir(runtime, workspace: dict) -> Path:
    """GH_CONFIG_DIR as Claude's server and its sessions have it in the workspace, from
    the environment the fake `claude` is started with there."""
    name = container_name(workspace["id"])
    layout = runtime.layout(name)
    seen = runtime.exec(name, claude_server.claude_command(layout, "fake-exec", "env"))
    assert seen.code == 0, seen.err
    env = dict(line.split("=", 1) for line in seen.out.decode().splitlines() if "=" in line)
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env  # the token is never in it
    return Path(env["GH_CONFIG_DIR"])


def test_gh_in_a_workspace_is_pointed_at_the_file_holding_the_token(client, runtime):
    workspace = made_workspace(client, ALICE)
    folder = gh_config_dir(runtime, workspace)
    assert not (folder / "hosts.yml").exists()  # positive control: nothing before a save
    assert (folder / "config.yml").read_text() == gh_config_text()
    save(client)
    assert (folder / "hosts.yml").read_text() == gh_hosts_text(TOKEN)
    assert (folder / "hosts.yml").resolve() == (git_dir(runtime) / "hosts.yml").resolve()


def test_forgetting_empties_hosts_yml_at_once_in_every_workspace(client, runtime):
    folders = [gh_config_dir(runtime, made_workspace(client, ALICE, n)) for n in ("one", "two")]
    save(client)
    assert all(TOKEN in (f / "hosts.yml").read_text() for f in folders)  # positive control
    assert forget(client).status_code == 200
    assert [(f / "hosts.yml").read_text() for f in folders] == ["", ""]


@pytest.mark.parametrize("failing", ["hosts.yml", "credential"])
def test_a_save_whose_token_file_fails_to_be_written_changes_nothing(client, app, runtime, failing):
    save(client, token="the-first-token")
    runtime.fail, runtime.fail_only = rt.RuntimeFailed("boom"), {failing}
    r = save(client, token="the-second-token")
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    assert failing in [name for _, _, name, _ in runtime.puts]  # positive control: tried
    runtime.fail = None
    assert written(runtime, "credential").endswith("password=the-first-token\n")
    assert written(runtime, "hosts.yml") == gh_hosts_text("the-first-token")
    assert token_rows(app) == [(1, "the-first-token")]


@pytest.mark.parametrize("failing", ["hosts.yml", "credential"])
def test_a_first_save_whose_token_file_fails_leaves_no_token_anywhere(
    client, app, runtime, failing
):
    runtime.fail, runtime.fail_only = rt.RuntimeFailed("boom"), {failing}
    assert save(client).status_code == 502
    runtime.fail = None
    for name in rt.ACCOUNT_FILES:
        file = git_dir(runtime) / name
        assert not file.exists() or TOKEN not in file.read_text(), name
    assert token_rows(app) == []


@pytest.mark.parametrize("failing", ["hosts.yml", "credential"])
@pytest.mark.parametrize("first", [True, False])
def test_a_save_whose_write_landed_but_was_said_to_fail_puts_that_file_back_too(
    client, app, runtime, failing, first
):
    """Nothing is left holding a token SQLite has no row for: Remove would not be offered."""
    if not first:
        save(client, token="the-first-token")
    runtime.landed_but_failed = {failing}
    r = save(client, token="the-second-token")
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    assert runtime.landed_but_failed == set()  # positive control: the write landed and failed
    for name in rt.ACCOUNT_FILES:
        assert "the-second-token" not in written(runtime, name), name
    if first:
        assert written(runtime, "credential") == written(runtime, "hosts.yml") == ""
        assert token_rows(app) == []
    else:
        assert written(runtime, "hosts.yml") == gh_hosts_text("the-first-token")
        assert written(runtime, "credential").endswith("password=the-first-token\n")
        assert token_rows(app) == [(1, "the-first-token")]


def holding_the_token(runtime) -> set[str]:
    return {name for name in rt.ACCOUNT_FILES if TOKEN in written(runtime, name)}


@pytest.mark.parametrize("failing", ["hosts.yml", "credential"])
def test_a_forget_that_half_fails_leaves_the_token_in_fewer_places_never_more(
    client, app, runtime, failing
):
    save(client)
    before = holding_the_token(runtime)
    assert before == {"credential", "hosts.yml"}  # positive control
    runtime.fail, runtime.fail_only = rt.RuntimeFailed("boom"), {failing}
    start = len(runtime.puts)
    r = forget(client)
    assert (r.status_code, r.json()["code"]) == (502, "workspace_failed")
    runtime.fail = None
    tried = [name for _, _, name, _ in runtime.puts[start:]]
    assert tried[-1] == failing  # the failing write was the last: nothing was put back
    after = holding_the_token(runtime)
    assert after == before - set(tried[:-1])  # what was emptied stays empty
    assert after <= before and failing in after
    assert token_rows(app) == [(1, TOKEN)]  # the row stays, so Remove can be tried again
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": True}
    assert forget(client).status_code == 200
    assert holding_the_token(runtime) == set() and token_rows(app) == []


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
    # the row is not there while the token files are written
    assert ("credential", []) in seen and ("hosts.yml", []) in seen
    seen.clear()
    forget(client)
    # and it is still there when they are emptied
    assert sorted(seen) == [("credential", [(1, TOKEN)]), ("hosts.yml", [(1, TOKEN)])]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (rt.RuntimeFailed("boom"), 502, "workspace_failed"),
        (rt.RuntimeUnavailable("down"), 503, "runtime_unavailable"),
    ],
)
def test_a_failed_write_changes_nothing(client, app, runtime, failure, status, code):
    runtime.puts.clear()  # what the restore wrote when rcman started
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
    assert written(runtime, "hosts.yml") == gh_hosts_text(TOKEN)


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
    runtime.puts.clear()  # what the restore wrote when rcman started
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
    assert written(runtime, "hosts.yml") == gh_hosts_text("the-first-token")
    assert written(runtime, "config") == config
    for name in rt.ACCOUNT_FILES:
        assert "the-second-token" not in written(runtime, name), name


def test_a_first_save_that_sqlite_refuses_leaves_the_credential_empty(client, app, runtime):
    break_sqlite(app, "github_tokens")
    with pytest.raises(sqlite3.Error):
        save(client)
    assert written(runtime, "credential") == ""
    assert written(runtime, "hosts.yml") == ""
    for name in rt.ACCOUNT_FILES:
        assert TOKEN not in written(runtime, name), name


def test_a_forget_that_sqlite_refuses_puts_the_token_back(client, app, runtime):
    save(client)
    break_sqlite(app, "github_tokens")
    with pytest.raises(sqlite3.Error):
        forget(client)
    assert written(runtime, "credential").endswith(f"password={TOKEN}\n")
    assert written(runtime, "hosts.yml") == gh_hosts_text(TOKEN)
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
    start = len(runtime.puts)

    def once(person, slot, name, data):
        # the restore, after the forget's two writes: the volume is gone
        if len(runtime.puts) - start >= 2:
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
    assert len(runtime.puts) == puts + 4  # the save's four files: nothing was put back
    assert not git_dir(runtime).exists()  # and what was written is removed


# --- gh's files for everyone, at every restore ---------------------------------------


def person_id(app, email: str) -> int:
    con = db.connect(app.state.settings.database)
    try:
        return people.by_email(con, email).id
    finally:
        con.close()


def without_gh_files(runtime, *persons: int) -> None:
    """The git folders as an rcman that wrote no gh files left them."""
    for person in persons:
        for name in rt.GH_FILES:
            (git_dir(runtime, person) / name).unlink(missing_ok=True)


def test_the_restore_writes_gh_files_for_everyone_let_in(client, app, runtime):
    alice, bob, carol = (person_id(app, e) for e in (ALICE, BOB, CAROL))
    save(client)
    without_gh_files(runtime, alice, bob, carol)
    assert not (git_dir(runtime, alice) / "hosts.yml").exists()  # positive control
    mark_deleting(app, carol)
    import shutil

    shutil.rmtree(git_dir(runtime, carol).parent)  # as the deletion removes the volume
    app.state.git_account.write_gh_for_everyone()
    assert written(runtime, "config.yml", alice) == gh_config_text()
    assert written(runtime, "hosts.yml", alice) == gh_hosts_text(TOKEN)
    assert written(runtime, "config.yml", bob) == gh_config_text()
    assert not (git_dir(runtime, bob) / "hosts.yml").exists()  # no token, no hosts.yml
    assert not git_dir(runtime, carol).exists()  # one being deleted gets nothing


def test_a_restore_of_rcman_writes_the_gh_files(client, app, runtime):
    alice = person_id(app, ALICE)
    save(client)
    without_gh_files(runtime, alice)
    hook = app.state.git_account.write_gh_for_everyone
    assert hook in app.state.workspaces.restore_hooks
    app.state.workspaces.restore()  # what rcman's next start does
    assert written(runtime, "hosts.yml", alice) == gh_hosts_text(TOKEN)
    assert written(runtime, "config.yml", alice) == gh_config_text()


def test_a_person_deleted_while_the_restore_writes_for_them_is_left_with_no_volume(
    client, app, runtime
):
    alice = person_id(app, ALICE)
    save(client)
    runtime.during = lambda name: mark_deleting(app, alice)
    app.state.git_account.write_gh_for_everyone()
    assert not git_dir(runtime, alice).exists()
    assert git_dir(runtime, person_id(app, BOB)).is_dir()  # the others are written still


def test_a_backfill_that_fails_for_one_person_goes_on_to_the_others(client, app, runtime):
    alice, bob = person_id(app, ALICE), person_id(app, BOB)
    without_gh_files(runtime, alice, bob)
    real = runtime.put_account

    def refused_for_alice(person, slot, name, data):
        if person == alice:
            raise rt.RuntimeFailed("boom")
        real(person, slot, name, data)

    runtime.put_account = refused_for_alice
    app.state.git_account.write_gh_for_everyone()
    assert not (git_dir(runtime, alice) / "config.yml").exists()  # positive control: refused
    assert written(runtime, "config.yml", bob) == gh_config_text()


def test_the_backfill_takes_each_persons_lock_and_reads_the_token_under_it(client, app, runtime):
    alice = person_id(app, ALICE)
    save(client)
    (git_dir(runtime, alice) / "hosts.yml").write_text("")  # as the forget left it
    lock = app.state.workspaces.person_lock(alice)
    done = threading.Event()
    lock.acquire()
    try:
        worker = threading.Thread(
            target=lambda: (app.state.git_account.write_gh_for_everyone(), done.set()),
            daemon=True,
        )
        worker.start()
        assert not done.wait(0.5)  # waits for the lock
        # a forget that came first, under the lock: what the backfill writes is read after it
        con = db.connect(app.state.settings.database)
        try:
            con.execute("delete from github_tokens where person = ?", (alice,))
            con.commit()
        finally:
            con.close()
    finally:
        lock.release()
    assert done.wait(10)
    assert TOKEN not in written(runtime, "hosts.yml", alice)
