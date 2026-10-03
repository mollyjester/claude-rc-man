"""The git routes: each a call to rc-helper as the signed-in person, against real git."""

import pytest
from rcman import routes

from helpers import rc_helper
from tests.conftest import ALICE, BOB, CALLS, as_user, changing
from tests.git_fixtures import fixture_git, make_repo, write

PATH = "projects/alice-project"
AUTHOR = {"author_name": "Alice Author"}


class Recording:
    """The app's way of reaching rc-helper, recording each call on the way through."""

    def __init__(self, run):
        self.run = run
        self.calls: list[dict] = []

    def __call__(self, linux, args, stdin=None, timeout=None):
        self.calls.append({"linux": linux, "args": list(args), "stdin": stdin, "timeout": timeout})
        return self.run(linux, args, stdin, timeout=timeout)


@pytest.fixture
def repo(helper_homes, tmp_path):
    """alice's projects/alice-project as a repository, with an origin outside her home."""
    return make_repo(
        helper_homes.homes["alice"] / PATH, origin=tmp_path / "origins" / "alice-project.git"
    )


@pytest.fixture
def recorded(app, client):
    recording = Recording(app.state.helper)
    app.state.helper = recording
    return recording


def git_routes(app):
    found = [
        (method, c.path)
        for c in routes.api_routes(app)
        if c.path.startswith("/api/git/")
        for method in c.methods
    ]
    assert len(found) >= 10  # non-vacuity: status, branches and the eight changes
    return sorted(found)


def call(client, method, path, who=ALICE, **kwargs):
    kwargs = kwargs or CALLS[(method, path)]
    return client.request(method, path, headers=changing(who), **kwargs)


def set_author(client, who=ALICE, **settings):
    r = client.patch("/api/me/settings", json=settings or AUTHOR, headers=changing(who))
    assert r.status_code == 200, r.text


def test_every_git_route_calls_the_helper_as_the_signed_in_person(app, client, repo, recorded):
    set_author(client)
    verbs = set()
    for method, path in git_routes(app):
        before = len(recorded.calls)
        r = call(client, method, path)
        assert r.status_code != 422, (path, r.text)
        made = recorded.calls[before:]
        assert made and all(c["linux"] == "alice" for c in made), (path, made)
        for c in made:
            assert c["args"][0].startswith("git-"), made
            assert c["args"][-2:] == ["--", PATH], made  # the folder, after --
        verbs |= {c["args"][0] for c in made}
    # Every route but Commit & Push is one verb of its own; it is two of theirs.
    assert len(verbs) == len(git_routes(app)) - 1


def staged_file(repo, name="a.txt"):
    write(repo / name)
    fixture_git(repo, "add", name)


def test_commit_and_push_checks_then_commits_then_pushes(client, repo, recorded):
    set_author(client)
    staged_file(repo)
    r = call(client, "POST", "/api/git/commit-push")
    assert r.status_code == 200, r.text
    assert [c["args"][:-2] for c in recorded.calls] == [
        ["git-push", "--check"],
        ["git-commit", f"--author-name={AUTHOR['author_name']}", f"--author-email={ALICE}"],
        ["git-push"],
    ]
    assert recorded.calls[1]["stdin"] == "A message"  # the message on stdin, as for Commit


def test_status_says_who_commits_are_recorded_as(client, repo):
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["author"] is None and r.json()["branch"]["name"] == "main"
    set_author(client)
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE))
    assert r.json()["author"] == {"name": "Alice Author", "email": ALICE}


def test_commit_is_refused_until_an_author_name_is_set(client, repo, recorded, helper_homes):
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    body = {"path": PATH, "message": "Add a.txt"}
    r = client.post("/api/git/commit", json=body, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "author_not_set")
    assert recorded.calls == []  # the helper is not asked
    set_author(client, author_name="Alice Author", author_email="alice@work.example")
    r = client.post("/api/git/commit", json=body, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    [made] = recorded.calls
    assert made["args"] == [
        "git-commit",
        "--author-name=Alice Author",
        "--author-email=alice@work.example",
        "--",
        PATH,
    ]
    shown = fixture_git(repo, "log", "-1", "--format=%an <%ae>")
    assert shown.strip() == "Alice Author <alice@work.example>"


def test_the_commit_message_reaches_the_helper_on_stdin_only(client, repo, recorded):
    set_author(client)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    message = "Add a.txt\n\nThe body, ünïcode included."
    r = client.post(
        "/api/git/commit", json={"path": PATH, "message": message}, headers=changing(ALICE)
    )
    assert r.status_code == 200, r.text
    [made] = recorded.calls
    assert made["stdin"] == message
    assert not any("a.txt" in arg for arg in made["args"])
    assert fixture_git(repo, "log", "-1", "--format=%B").rstrip("\n") == message
    assert "Add a.txt" not in r.text


def test_the_files_reach_the_helper_on_stdin_as_a_list(client, repo, recorded):
    import json

    write(repo / "a b.txt")
    r = call(client, "POST", "/api/git/stage", json={"path": PATH, "files": ["a b.txt"]})
    assert r.status_code == 200, r.text
    assert json.loads(recorded.calls[-1]["stdin"]) == ["a b.txt"]
    status = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE)).json()
    assert status["staged"]["files"] == [{"path": "a b.txt", "change": "added"}]


def test_people_reach_only_their_own_repositories(client, repo, helper_homes):
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(BOB))
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    write(repo / "a.txt")
    r = call(client, "POST", "/api/git/stage", who=BOB, json={"path": PATH, "files": ["a.txt"]})
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    ok = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE)).json()
    assert ok["staged"]["files"] == []  # positive control: alice's is there, unchanged
    assert ok["not_staged"]["files"] == [{"path": "a.txt", "change": "untracked"}]


def test_gits_refusal_comes_back_in_its_words(client, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'Not on main, please.' >&2\nexit 1\n")
    hook.chmod(0o755)
    set_author(client)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    r = client.post("/api/git/commit", json={"path": PATH, "message": "m"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "git_failed")
    assert r.json()["git"] == ["Not on main, please."] and r.json()["git_more"] == 0
    assert r.json()["title"] == rc_helper.MESSAGES["git_failed"]


def test_a_folder_that_is_not_a_repository_is_a_problem(client):
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "not_a_repository")


NETWORK_VERBS = ("git-fetch", "git-push")


def test_git_routes_give_the_helper_time_to_answer(app, client, repo, recorded):
    set_author(client)
    staged_file(repo)
    for method, path in git_routes(app):
        call(client, method, path)
    network = [c for c in recorded.calls if c["args"][0] in NETWORK_VERBS]
    local = [c for c in recorded.calls if c["args"][0] not in NETWORK_VERBS]
    assert {c["args"][0] for c in network} == set(NETWORK_VERBS)  # non-vacuity
    assert all(c["timeout"] > rc_helper.GIT_NETWORK_TIMEOUT for c in network), network
    assert local and all(c["timeout"] > rc_helper.GIT_TIMEOUT for c in local), local


def test_the_commit_message_field_is_the_helpers_rule(app):
    from tests.test_problems import request_fields

    [message] = [
        f for f in request_fields(app) if f.path == "/api/git/commit" and f.name == "message"
    ]
    assert message.schema["pattern"] == rc_helper.COMMIT_MESSAGE.anchored
    assert message.schema["maxLength"] == rc_helper.MAX_COMMIT_MESSAGE_LENGTH
    [files] = [f for f in request_fields(app) if f.path == "/api/git/stage" and f.name == "files"]
    assert (files.schema["minItems"], files.schema["maxItems"]) == (1, rc_helper.MAX_FILES_PER_CALL)


def test_a_message_too_long_is_refused_in_the_rules_words(client, repo):
    set_author(client)
    long = "x" * (rc_helper.MAX_COMMIT_MESSAGE_LENGTH + 1)
    r = client.post(
        "/api/git/commit", json={"path": PATH, "message": long}, headers=changing(ALICE)
    )
    assert r.json()["errors"] == [
        {"field": "message", "message": rc_helper.COMMIT_MESSAGE.sentence}
    ]


def test_switch_and_new_branch_and_fetch_change_the_repository(client, repo):
    r = call(
        client,
        "POST",
        "/api/git/switch",
        json={"path": PATH, "branch": "origin/feature", "remote": True},
    )
    assert r.status_code == 200, r.text
    assert fixture_git(repo, "branch", "--show-current").strip() == "feature"
    r = call(client, "POST", "/api/git/branch", json={"path": PATH, "name": "topic"})
    assert r.status_code == 200 and r.json()["branch"] == "topic"
    r = call(client, "POST", "/api/git/fetch", json={"path": PATH})
    assert r.status_code == 200, r.text
    names = client.get("/api/git/branches", params={"path": PATH}, headers=as_user(ALICE)).json()
    assert [b["name"] for b in names["local"]["branches"]] == ["feature", "main", "topic"]


# --- push -----------------------------------------------------------------------------


@pytest.fixture
def on_github(helper_homes, github, tmp_path):
    """alice's projects/alice-project as a repository whose origin is on the tests' GitHub;
    its bare repository there."""
    name = f"alice-{tmp_path.name}"
    bare = github.bare("alice", name)
    make_repo(helper_homes.homes["alice"] / PATH, origin=bare, url=github.github_url("alice", name))
    return bare


def on_remote(bare, branch):
    import subprocess

    found = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() or None


def store_token(client, token):
    r = client.post("/api/github/token", json={"token": token}, headers=changing(ALICE))
    assert r.status_code == 200, r.text


def test_status_says_whether_a_push_goes_to_github_and_a_token_is_stored(client, github, on_github):
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE))
    assert (r.json()["push_to_github"], r.json()["github_token"]) == (True, False)
    store_token(client, github.token)
    r = client.get("/api/git/status", params={"path": PATH}, headers=as_user(ALICE))
    assert r.json()["github_token"] is True


def test_push_sets_the_upstream_of_a_new_branch_and_reaches_github(client, github, on_github):
    store_token(client, github.token)
    assert call(client, "POST", "/api/git/branch", json={"path": PATH, "name": "topic"}).is_success
    r = call(client, "POST", "/api/git/push", json={"path": PATH})
    assert r.status_code == 200, r.text
    assert r.json() == {"path": PATH, "branch": "topic", "upstream": "origin/topic",
                        "upstream_set": True}  # fmt: skip
    assert on_remote(on_github, "topic") is not None


def test_push_to_github_without_a_token_is_refused(client, on_github):
    r = call(client, "POST", "/api/git/push", json={"path": PATH})
    assert (r.status_code, r.json()["code"]) == (409, "github_token_not_set")
    assert r.json()["title"] == rc_helper.MESSAGES["github_token_not_set"]


def test_commit_and_push_both_work(client, github, on_github, helper_homes):
    repo = helper_homes.homes["alice"] / PATH
    set_author(client)
    store_token(client, github.token)
    staged_file(repo)
    body = {"path": PATH, "message": "Add a.txt and push"}
    r = client.post("/api/git/commit-push", json=body, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    assert r.json() == {
        "path": PATH,
        "commit": head,
        "push": {"path": PATH, "branch": "main", "upstream": "origin/main", "upstream_set": False},
        "push_problem": None,
    }
    assert on_remote(on_github, "main") == head


def test_when_the_commit_works_and_the_push_fails_the_answer_says_both(
    client, on_github, helper_homes, settings
):
    import sqlite3

    repo = helper_homes.homes["alice"] / PATH
    set_author(client)
    store_token(client, "a-token-github-refuses")
    staged_file(repo)
    before = on_remote(on_github, "main")
    body = {"path": PATH, "message": "Committed, not pushed"}
    r = client.post("/api/git/commit-push", json=body, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip()
    assert fixture_git(repo, "log", "-1", "--format=%s").strip() == "Committed, not pushed"
    assert answer["push"] is None
    problem = answer["push_problem"]
    assert (problem["code"], problem["status"]) == ("git_failed", 409)
    assert problem["type"] == "urn:rcman:problem:git_failed"
    assert problem["title"] == rc_helper.MESSAGES["git_failed"]
    assert any("Authentication failed" in line for line in problem["git"]), problem
    assert on_remote(on_github, "main") == before
    con = sqlite3.connect(settings.database)
    action, target, result = con.execute(
        "select action, target, result from audit order by id desc limit 1"
    ).fetchone()
    assert (action, target, result) == ("git.commit_push", PATH, "committed_not_pushed")


def test_commit_and_push_to_github_without_a_token_commits_nothing(
    client, on_github, helper_homes, settings
):
    import sqlite3

    repo = helper_homes.homes["alice"] / PATH
    set_author(client)
    staged_file(repo)
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    r = client.post(
        "/api/git/commit-push", json={"path": PATH, "message": "m"}, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (409, "github_token_not_set")
    assert fixture_git(repo, "rev-parse", "HEAD").strip() == head
    assert fixture_git(repo, "diff", "--cached", "--name-only").split() == ["a.txt"]
    con = sqlite3.connect(settings.database)
    last = con.execute("select action, result from audit order by id desc limit 1").fetchone()
    assert last == ("git.commit_push", "github_token_not_set")


def test_commit_and_push_needs_an_author_first(client, on_github, recorded):
    r = client.post(
        "/api/git/commit-push", json={"path": PATH, "message": "m"}, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (409, "author_not_set")
    assert recorded.calls == []


def test_commit_and_push_message_is_the_commits_rule(app):
    from tests.test_problems import request_fields

    [message] = [
        f for f in request_fields(app) if f.path == "/api/git/commit-push" and f.name == "message"
    ]
    assert message.schema["pattern"] == rc_helper.COMMIT_MESSAGE.anchored
    assert message.schema["maxLength"] == rc_helper.MAX_COMMIT_MESSAGE_LENGTH


# --- clone ----------------------------------------------------------------------------


@pytest.fixture
def published(github, tmp_path):
    """A public repository on the tests' GitHub: its address, and the folder a clone makes."""
    name = f"clone-{tmp_path.name}"
    make_repo(tmp_path / "published", origin=github.bare("alice", name))
    return f"https://github.com/alice/{name}", name


def test_clone_runs_the_helper_as_the_person_with_the_address_and_the_folder(
    client, recorded, published, helper_homes
):
    url, name = published
    r = call(client, "POST", "/api/git/clone", json={"parent": PATH, "url": url})
    assert r.status_code == 200, r.text
    assert r.json() == {"created": f"{PATH}/{name}"}
    [made] = recorded.calls
    assert made["linux"] == "alice"
    assert made["args"] == ["git-clone", f"--url={url}", "--", PATH]
    assert made["stdin"] is None
    assert (helper_homes.homes["alice"] / PATH / name / "README.md").is_file()


def test_a_clone_into_the_home_folder_sends_no_folder(client, recorded, published, helper_homes):
    url, name = published
    r = call(client, "POST", "/api/git/clone", json={"url": url})
    assert r.status_code == 200, r.text
    assert r.json() == {"created": name}
    assert recorded.calls[-1]["args"] == ["git-clone", f"--url={url}", "--"]
    assert (helper_homes.homes["alice"] / name / ".git").is_dir()


def test_people_clone_only_into_their_own_folders(client, published, helper_homes):
    url, name = published
    r = call(client, "POST", "/api/git/clone", who=BOB, json={"parent": PATH, "url": url})
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    assert not (helper_homes.homes["alice"] / PATH / name).exists()
    r = call(client, "POST", "/api/git/clone", who=BOB, json={"url": url})
    assert r.status_code == 200, r.text  # positive control: into bob's own home
    assert (helper_homes.homes["bob"] / name / ".git").is_dir()
    assert not (helper_homes.homes["alice"] / name).exists()


def test_a_clone_waits_longer_than_the_helpers_own_deadline(client, recorded, published):
    url, _ = published
    call(client, "POST", "/api/git/clone", json={"parent": PATH, "url": url})
    [made] = recorded.calls
    assert made["timeout"] > rc_helper.CLONE_TIMEOUT


def test_the_clone_address_field_is_the_helpers_rule(app):
    from tests.test_problems import request_fields

    [url] = [f for f in request_fields(app) if f.path == "/api/git/clone" and f.name == "url"]
    assert url.schema["pattern"] == rc_helper.GITHUB_URL.anchored


def test_an_address_with_a_token_in_it_is_refused_and_never_repeated(
    client, recorded, settings, caplog
):
    import sqlite3

    caplog.set_level("DEBUG")
    secret = "SECRET-TOKEN-IN-AN-ADDRESS-5"
    url = f"https://alice:{secret}@github.com/alice/repo"
    r = call(client, "POST", "/api/git/clone", json={"parent": PATH, "url": url})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert r.json()["errors"] == [{"field": "url", "message": rc_helper.GITHUB_URL.sentence}]
    assert secret not in r.text
    assert recorded.calls == []  # the helper is not asked
    con = sqlite3.connect(settings.database)
    rows = con.execute("select * from audit").fetchall()
    last = con.execute("select action, target, result from audit order by id desc").fetchone()
    assert last == ("git.clone", None, "validation_failed")  # positive control: recorded
    assert caplog.records  # positive control: the audit event was logged
    assert all(secret not in str(value) for row in rows for value in row)
    assert secret not in caplog.text


def test_a_clone_git_made_elsewhere_is_a_server_error(app, client):
    """clone_missing: git said it cloned, but not where rc-helper made the folder for it,
    which nothing the person did explains."""
    message = rc_helper.MESSAGES["clone_missing"]
    app.state.helper = lambda linux, args, stdin=None, timeout=None: (
        1,
        {"error": {"code": "clone_missing", "message": message}},
    )
    r = call(client, "POST", "/api/git/clone", json={"url": "https://github.com/alice/r"})
    assert (r.status_code, r.json()["code"]) == (500, "clone_missing")
    assert r.json()["title"] == message
