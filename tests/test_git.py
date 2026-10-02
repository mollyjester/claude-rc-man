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
    assert len(found) >= 8  # non-vacuity: status, branches and the six changes
    return sorted(found)


def call(client, method, path, who=ALICE, **kwargs):
    kwargs = kwargs or CALLS[(method, path)]
    return client.request(method, path, headers=changing(who), **kwargs)


def set_author(client, who=ALICE, **settings):
    r = client.patch("/api/me/settings", json=settings or AUTHOR, headers=changing(who))
    assert r.status_code == 200, r.text


def test_every_git_route_calls_the_helper_as_the_signed_in_person(app, client, repo, recorded):
    set_author(client)
    for method, path in git_routes(app):
        before = len(recorded.calls)
        r = call(client, method, path)
        assert r.status_code != 422, (path, r.text)
        made = recorded.calls[before:]
        assert [c["linux"] for c in made] == ["alice"], (path, made)
        assert made[0]["args"][0].startswith("git-"), made
        assert made[0]["args"][-2:] == ["--", PATH], made  # the folder, after --
    assert len({c["args"][0] for c in recorded.calls}) == len(git_routes(app))


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


def test_git_routes_give_the_helper_time_to_answer(app, client, repo, recorded):
    set_author(client)
    for method, path in git_routes(app):
        call(client, method, path)
    timeouts = {c["args"][0]: c["timeout"] for c in recorded.calls}
    assert timeouts["git-fetch"] > rc_helper.GIT_NETWORK_TIMEOUT
    local = {verb: t for verb, t in timeouts.items() if verb != "git-fetch"}
    assert local and all(t > rc_helper.GIT_TIMEOUT for t in local.values()), local


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
