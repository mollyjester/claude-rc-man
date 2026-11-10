"""backend/rcman/gitaccount.py: a person's commit author and GitHub token, written into their
volume's git folder and kept in SQLite, against the host runtime and real git."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rcman import claude_server, db, people
from rcman import runtime as rt
from rcman.gitaccount import (
    Author,
    config_text,
    credential_text,
    gh_config_text,
    gh_hosts_text,
)
from rcman.runtime import container_name

from tests.conftest import (
    ALICE,
    BOB,
    as_user,
    changing,
    made_workspace,
)

TOKEN = "ghp_a-token-for-the-tests"


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


def save(client):
    return client.post("/api/github/token", json={"token": TOKEN}, headers=changing(ALICE))


def forget(client):
    return client.delete("/api/github/token", headers=changing(ALICE))


def patch(client, body):
    return client.patch("/api/me/settings", json=body, headers=changing(ALICE))


def token_rows(app) -> list[tuple[int, str]]:
    con = db.connect(app.state.settings.database)
    try:
        return [tuple(r) for r in con.execute("select person, token from github_tokens")]
    finally:
        con.close()


def fill(home: str) -> subprocess.CompletedProcess:
    """git's credential for GitHub, as a git in a workspace with this HOME asks for it."""
    return subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
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


def test_a_name_and_an_address_are_read_back_by_git_as_they_were_written(tmp_path):
    name = 'Ann "The Quote" O\'Neil'
    file = tmp_path / "config"
    file.write_text(config_text(Author(name, "ann@example.com")))
    assert git_config(file, "user.name").stdout == name + "\n"
    assert git_config(file, "user.email").stdout == "ann@example.com\n"


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


# --- the token ----------------------------------------------------------------------


def test_a_saved_token_is_reported_and_written_to_the_volume_and_sqlite(client, app, runtime):
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


def test_git_in_a_workspace_gets_the_token_for_github(client, app, runtime):
    workspace = made_workspace(client, ALICE)
    home = runtime.layout(container_name(workspace["id"])).home
    assert fill(home).returncode != 0  # positive control: nothing yet
    save(client)
    got = fill(home)
    assert got.returncode == 0, got.stderr
    assert "username=x-access-token" in got.stdout and f"password={TOKEN}" in got.stdout


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


# --- gh's files for everyone, at every restore ---------------------------------------


def person_id(app, email: str) -> int:
    con = db.connect(app.state.settings.database)
    try:
        return people.by_email(con, email).id
    finally:
        con.close()


def without_gh_files(runtime, person: int) -> None:
    """The git folder as an rcman that wrote no gh files left it."""
    for name in rt.GH_FILES:
        (git_dir(runtime, person) / name).unlink(missing_ok=True)


def test_a_restore_of_rcman_writes_the_gh_files(client, app, runtime):
    alice = person_id(app, ALICE)
    save(client)
    without_gh_files(runtime, alice)
    hook = app.state.git_account.write_gh_for_everyone
    assert hook in app.state.workspaces.restore_hooks
    app.state.workspaces.restore()  # what rcman's next start does
    assert written(runtime, "hosts.yml", alice) == gh_hosts_text(TOKEN)
    assert written(runtime, "config.yml", alice) == gh_config_text()
