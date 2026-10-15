"""Git in a workspace of the Podman runtime, against a stand-in for github.com.

``tests/test_container_suite.py`` runs this module in the podman host as the service
account, once the tools and a store holding the fake `claude` are made as root, and
with ``github.com`` in the host's /etc/hosts at the host's own address, which
``RCMAN_GITHUB_ADDRESS`` names. Anywhere else it skips. Here the stand-in
(``tests/github_server.py``) is served as https://github.com at that address, under a
certificate from a certificate authority minted here, which the workspaces' image
trusts: the image is the base image with git, python3 for the fake `claude`, and that
authority. Nothing in a workspace knows it is not the real GitHub, so what runs is what
runs in production: rcman's own push (``Repository``), Claude's git (the fake `claude`'s
``fake-exec``, started with Claude's environment), the credential helper that reads the
person's credential file, and the writes of that file through the runtime's
``put_account`` (``GitAccount``).
"""

import os
import socket
import ssl
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest
from rcman import claude_server, db, people
from rcman import runtime as rt
from rcman.gitaccount import GitAccount
from rcman.identity import Identity
from rcman.problems import CLONE_MESSAGES, Problem
from rcman.repository import Repository
from rcman.settings import Settings
from rcman.workspaces import Workspaces

from helpers import git_run
from tests.container import github_stand_in as stand_in
from tests.container import test_runtime_in_podman as base
from tests.github_server import GitHubServer

ADDRESS = os.environ.get("RCMAN_GITHUB_ADDRESS", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1"
    or "RCMAN_PODMAN_IMAGE" not in os.environ
    or not ADDRESS,
    reason="runs only as the service account inside the podman host, with github.com in its hosts",
)

TOKEN = "github_pat_podman-test-token"
EMAIL = "bob@example.com"


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> Path:
    """A certificate authority, and a certificate it signed for github.com."""
    return stand_in.mint(tmp_path_factory.mktemp("pki"))


@pytest.fixture(scope="module")
def github_com(pki, tmp_path_factory):
    """The tests' GitHub at https://github.com, which the host's /etc/hosts sends to the
    host's own address, on that address and the port 443."""
    assert socket.gethostbyname("github.com") == ADDRESS  # the runner's line in /etc/hosts
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki / "leaf.crt", pki / "leaf.key")
    server = GitHubServer()
    url = server.start(
        tmp_path_factory.mktemp("github"), TOKEN, ssl_context=context, host=ADDRESS, port=443
    )
    assert url == f"https://{ADDRESS}:443/"
    yield server
    server.stop()


@pytest.fixture(scope="module")
def image(pki, github_com) -> str:
    """The base image with git, python3 and the authority; built in the account's Podman."""
    name = f"localhost/rcman-test-git:{os.getpid()}"
    (pki / "Dockerfile").write_text(
        stand_in.dockerfile(os.environ["RCMAN_PODMAN_IMAGE"], packages=True)
    )
    built = subprocess.run(
        ["podman", "--remote", "--url", f"unix://{base.socket()}", "build", "-q", "-t", name,
         str(pki)],
        capture_output=True,
        text=True,
        timeout=1200,
    )  # fmt: skip
    assert built.returncode == 0, built.stdout + built.stderr
    yield name
    base.podman_cli("rmi", "--force", name)


def runtime_of(image: str) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=base.socket(), image=image, tools_dir=base.TOOLS, claude_store=base.CLAUDE
        )
    )


@pytest.fixture(scope="module")
def podman(image) -> rt.PodmanRuntime:
    return runtime_of(image)


@pytest.fixture(scope="module")
def person(tmp_path_factory):
    """A person of rcman's database, in a slot the podman host's id range holds, and the
    git account that writes their credential."""
    path = tmp_path_factory.mktemp("rcman") / "rcman.db"
    db.migrate(path)
    con = db.connect(path)
    try:
        who = people.add(con, EMAIL, False)
    finally:
        con.close()
    return who, path


@pytest.fixture(scope="module")
def account(podman, person) -> tuple[GitAccount, Identity]:
    who, path = person
    service = types.SimpleNamespace(
        runtime=podman,
        person_lock=lambda _person: threading.RLock(),
        settings=types.SimpleNamespace(database=path),
    )
    return GitAccount(service), Identity(email=EMAIL, admin=False, person=who.id, slot=who.slot)


@pytest.fixture(scope="module")
def workspace(podman, person):
    who, _ = person
    spec = rt.Spec(rt.container_name(600), slot=who.slot, workspace=600, person=who.id)
    podman.create(spec)
    try:
        yield spec.name
    finally:
        podman.remove(spec.name)
        podman.remove_account(who.id)


def in_it(podman, name: str, argv: list[str]) -> rt.Result:
    return podman.exec(name, argv)


def git_in_it(podman, name: str, *args: str) -> rt.Result:
    """git in the workspace as a terminal would run it: only a home and a path."""
    layout = podman.layout(name)
    env = {"HOME": layout.home, "PATH": layout.path}
    return podman.exec(name, layout.with_env(env, ["git", "-C", layout.work, *args]))


def repository(podman, name: str) -> Repository:
    """rcman's git in the workspace, with the runners the workspaces service gives it."""
    run, run_stdin = Workspaces._execs(types.SimpleNamespace(runtime=podman), name)
    return Repository(podman.layout(name), run, run_stdin)


def claude_git(podman, name: str, *args: str) -> rt.Result:
    """git run by the fake `claude` as one of Claude's children, which is what git is for
    Claude: its environment is exactly ``child_env``."""
    layout = podman.layout(name)
    command = claude_server.claude_command(layout, "fake-exec", "git", "-C", layout.work, *args)
    return podman.exec(name, command)


def on_remote(bare: Path, ref: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", ref], capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(podman, workspace, account, github_com, tmp_path):
    """The workspace's work folder as a repository with one commit on main, whose origin
    is a new empty repository on the tests' GitHub, with no token stored: the repository's
    bare path, and the commit."""
    forget, identity = account[0].forget_token, account[1]
    forget(identity)
    name = f"repo-{tmp_path.name}"
    bare = github_com.bare("bob", name)
    layout = podman.layout(workspace)
    assert in_it(podman, workspace, [*layout.tool("rm"), "-rf", f"{layout.work}/.git"]).code == 0
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.name=Bob", "-c", "user.email=bob@example.com",
         "commit", "-q", "--allow-empty", "-m", "First"],
        ["remote", "add", "origin", GitHubServer.github_url("bob", name)],
    ):  # fmt: skip
        done = git_in_it(podman, workspace, *args)
        assert done.code == 0, done.err
    head = git_in_it(podman, workspace, "rev-parse", "HEAD").out.decode().strip()
    return bare, head


def commit_again(podman, workspace) -> str:
    done = git_in_it(
        podman, workspace, "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
        "commit", "-q", "--allow-empty", "-m", "Another",
    )  # fmt: skip
    assert done.code == 0, done.err
    return git_in_it(podman, workspace, "rev-parse", "HEAD").out.decode().strip()


# --- git in the image, and the stand-in as github.com ------------------------------------


def test_the_image_check_accepts_the_image_with_git_and_refuses_one_without(
    podman, workspace, person
):
    """Workspaces._check_git, run against real containers: the first time git runs in one."""
    service = types.SimpleNamespace(runtime=podman)
    service._execs = lambda name: Workspaces._execs(service, name)
    Workspaces._check_git(service, types.SimpleNamespace(container=workspace))  # no refusal
    version = git_in_it(podman, workspace, "--version")
    assert version.code == 0 and version.out.startswith(b"git version "), version
    assert git_run.version_of(version.out.decode()) >= git_run.GIT_MIN_VERSION

    # The base image has no git: the same check fails the workspace, with the reason.
    bare = runtime_of(os.environ["RCMAN_PODMAN_IMAGE"])
    owner = person[0]
    who = rt.Spec(rt.container_name(601), slot=owner.slot, workspace=601, person=owner.id)
    bare.create(who)
    try:
        service = types.SimpleNamespace(runtime=bare)
        service._execs = lambda name: Workspaces._execs(service, name)
        with pytest.raises(rt.RuntimeFailed, match="git is not installed in the image"):
            Workspaces._check_git(service, types.SimpleNamespace(container=who.name))
    finally:
        bare.remove(who.name)


def test_a_workspace_has_the_hosts_hosts_file_and_reaches_the_stand_in_at_its_address(
    podman, workspace, github_com, tmp_path
):
    """Podman containers (here 4.9, with slirp4netns) get the podman host's /etc/hosts, so
    github.com is the host's own address there; and the stand-in listening on that address
    answers them, under the certificate of the authority the image trusts."""
    seen = in_it(podman, workspace, ["getent", "hosts", "github.com"])
    assert seen.out.split()[:2] == [ADDRESS.encode(), b"github.com"], seen
    name = f"public-{tmp_path.name}"
    bare = github_com.bare("bob", name)
    source = tmp_path / "source"
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
         "commit", "-q", "--allow-empty", "-m", "First"],
        ["git", "-C", str(source), "push", "-q", str(bare), "main"],
    ):  # fmt: skip
        subprocess.run(argv, check=True, capture_output=True)
    before = len(github_com.requests)
    # A public repository is read with no credential: only the address and the trust.
    read = git_in_it(podman, workspace, "ls-remote", GitHubServer.github_url("bob", name))
    assert read.code == 0, read.err
    assert read.out.split()[0].decode() == on_remote(bare, "refs/heads/main")
    assert len(github_com.requests) > before  # positive control: it was the stand-in


# --- rcman's push, and Claude's, and the one token ---------------------------------------


def test_a_push_without_a_token_is_refused_before_git_is_asked(podman, workspace, repo, github_com):
    bare, _ = repo
    before = len(github_com.requests)
    with pytest.raises(Problem) as refused:
        repository(podman, workspace).push()
    assert refused.value.code == "github_token_not_set"
    assert len(github_com.requests) == before and on_remote(bare, "refs/heads/main") == ""


def test_a_push_with_the_token_saved_by_rcman_reaches_github(
    podman, workspace, repo, account, github_com
):
    bare, head = repo
    account[0].save_token(account[1], TOKEN)
    before = len(github_com.attempts)
    pushed = repository(podman, workspace).push()
    assert (pushed["branch"], pushed["upstream"], pushed["upstream_set"]) == (
        "main",
        "origin/main",
        True,
    )
    assert on_remote(bare, "refs/heads/main") == head
    assert set(github_com.attempts[before:]) == {("x-access-token", True)}
    # Nothing of the token is in the repository: git asked the helper for it.
    config = in_it(podman, workspace, ["cat", f"{podman.layout(workspace).work}/.git/config"])
    assert TOKEN.encode() not in config.out


def test_a_push_by_claudes_own_git_uses_the_same_token(
    podman, workspace, repo, account, github_com
):
    bare, head = repo
    refused = claude_git(podman, workspace, "push", "origin", "HEAD:refs/heads/by-claude")
    assert refused.code != 0 and on_remote(bare, "refs/heads/by-claude") == ""  # no token yet
    account[0].save_token(account[1], TOKEN)
    before = len(github_com.attempts)
    pushed = claude_git(podman, workspace, "push", "origin", "HEAD:refs/heads/by-claude")
    assert pushed.code == 0, pushed.err
    assert on_remote(bare, "refs/heads/by-claude") == head
    assert set(github_com.attempts[before:]) == {("x-access-token", True)}


def test_claudes_children_have_exactly_the_environment_of_a_server_and_no_token(
    podman, workspace, account
):
    account[0].save_token(account[1], TOKEN)
    layout = podman.layout(workspace)
    seen = podman.exec(workspace, claude_server.claude_command(layout, "fake-exec", "env"))
    assert seen.code == 0, seen.err
    names = {line.split("=", 1)[0] for line in seen.out.decode().splitlines()}
    assert names - {"PWD", "SHLVL", "_", "OLDPWD"} == set(claude_server.child_env(layout))
    assert TOKEN.encode() not in seen.out


def test_forgetting_the_token_makes_the_very_next_push_fail_for_both(
    podman, workspace, repo, account, github_com
):
    bare, head = repo
    account[0].save_token(account[1], TOKEN)
    assert repository(podman, workspace).push()["upstream_set"] is True  # positive controls
    assert claude_git(podman, workspace, "push", "origin", "HEAD:refs/heads/before").code == 0
    later = commit_again(podman, workspace)

    account[0].forget_token(account[1])
    accepted = len([a for a in github_com.attempts if a[1]])
    with pytest.raises(Problem) as refused:
        repository(podman, workspace).push()
    assert refused.value.code == "github_token_not_set"
    claude = claude_git(podman, workspace, "push", "origin", f"{later}:refs/heads/after")
    assert claude.code != 0
    assert on_remote(bare, "refs/heads/main") == head  # rcman's did not reach it
    assert on_remote(bare, "refs/heads/after") == ""  # nor Claude's
    assert len([a for a in github_com.attempts if a[1]]) == accepted  # nothing was accepted


# --- a workspace made by cloning, with the real service ----------------------------------
#
# rcman's own Workspaces against the account's real Podman and the tests' GitHub: the
# workspace container, git in it, the credential helper and the volume, with nothing
# stood in. Its rows are numbered from 1 in a database of their own, clear of the 600
# and 601 of the tests above.


@pytest.fixture(scope="module")
def service(podman, person):
    who, path = person
    made = Workspaces(Settings(public_origin="https://rcman.test", database=path), podman)
    yield made
    made.close()


def publish(github_com, tmp_path, name: str, private: bool = False) -> Path:
    """A repository ``octo/<name>`` on the tests' GitHub with one commit on main and a
    branch, feature: its bare repository."""
    bare = github_com.bare("octo", name, private=private)
    source = tmp_path / f"source-{name}"
    author = ["-c", "user.name=Octo", "-c", "user.email=octo@example.com"]
    (source / "docs").mkdir(parents=True)
    (source / "README.md").write_text(f"# {name}\n")
    (source / "docs" / "guide.txt").write_text("a file in a folder\n")
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "add", "."],
        ["git", "-C", str(source), *author, "commit", "-q", "-m", "First"],
        ["git", "-C", str(source), "branch", "feature"],
        ["git", "-C", str(source), "push", "-q", str(bare), "main", "feature"],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    return bare


def settled(service, workspace_id: int, within: float = 300):
    """The workspace once it is no longer being made."""
    deadline = time.monotonic() + within
    while True:
        now = service.get(workspace_id)
        if now is None or now.state != "creating":
            return now
        assert time.monotonic() < deadline, now
        time.sleep(0.2)


def gone(service, workspace_id: int, within: float = 120) -> None:
    """Wait until the workspace's row is gone, which a deletion of one being made leaves to
    the thread that was making it."""
    deadline = time.monotonic() + within
    while service.get(workspace_id) is not None:
        assert time.monotonic() < deadline, service.get(workspace_id)
        time.sleep(0.2)


def exists(kind: str, name: str) -> bool:
    return base.podman_cli(kind, "exists", name).returncode == 0


@pytest.fixture
def clones(service, person, account):
    """The workspaces a test makes, which are deleted afterwards and found gone, container
    and volume with them; the person has no token until the test saves one."""
    account[0].forget_token(account[1])
    made: list = []
    yield made
    for workspace in made:
        now = service.get(workspace.id)
        if now is not None:
            service.delete(now)
    for workspace in made:
        gone(service, workspace.id)
        assert not exists("container", workspace.container)
        assert not exists("volume", workspace.container)


def clone(service, person, clones, source: str, name: str | None = None):
    """Make a workspace from ``source`` and wait until it is made."""
    made = service.create(person[0], name, source)
    clones.append(made)
    return settled(service, made.id)


def test_a_public_repository_is_cloned_into_a_workspace_that_then_works(
    service, person, clones, podman, github_com, tmp_path
):
    bare = publish(github_com, tmp_path, "public-one")
    source = GitHubServer.github_url("octo", "public-one").removesuffix(".git")
    before = len(github_com.requests)
    done = clone(service, person, clones, source)
    assert (done.state, done.error, done.log, done.source) == ("ready", None, None, source)
    assert done.name == "public-one"  # named after the repository
    assert len(github_com.requests) > before  # positive control: it came from the stand-in
    work = podman.layout(done.container).work
    assert in_it(podman, done.container, ["cat", f"{work}/README.md"]).out == b"# public-one\n"
    assert in_it(podman, done.container, ["cat", f"{work}/docs/guide.txt"]).code == 0
    status = repository(podman, done.container).status()
    assert status["branch"]["name"] == "main"
    assert status["branch"]["upstream"] == "origin/main"
    assert (status["staged"]["files"], status["not_staged"]["files"]) == ([], [])
    assert on_remote(bare, "refs/heads/feature") != ""
    branches = git_in_it(podman, done.container, "branch", "--remotes").out.decode()
    assert "origin/feature" in branches  # every branch came, as the clone does
    config = in_it(podman, done.container, ["cat", f"{work}/.git/config"]).out
    assert config.count(b"url = " + source.encode()) == 1  # the address as it was given


def test_a_private_repository_without_a_token_fails_the_workspace_and_says_why(
    service, person, clones, podman, github_com, tmp_path
):
    publish(github_com, tmp_path, "private-one", private=True)
    source = "https://github.com/octo/private-one"
    accepted = len([a for a in github_com.attempts if a[1]])
    attempted = len(github_com.attempts)
    done = clone(service, person, clones, source, "private-one")
    assert done.state == "failed", done
    head, exit_line, *said = done.error.splitlines()
    assert head == CLONE_MESSAGES["private_no_token"]
    assert exit_line == "git clone failed, exit code 128:"
    assert any("could not read Username" in line for line in said), said  # git's own words
    assert done.log.splitlines()[-2:] == [f"Cloning {source}", "The clone failed"]
    assert len([a for a in github_com.attempts if a[1]]) == accepted  # nothing was accepted
    assert github_com.attempts[attempted:] == []  # no credential was even offered
    # The failed workspace stays, with its container, until it is deleted.
    assert exists("container", done.container)


def test_the_same_repository_clones_once_the_token_is_saved_and_the_token_is_not_kept_in_it(
    service, person, clones, podman, github_com, account, tmp_path
):
    publish(github_com, tmp_path, "private-two", private=True)
    source = "https://github.com/octo/private-two"
    failed = clone(service, person, clones, source, "private-two-first")
    assert failed.state == "failed"
    account[0].save_token(account[1], TOKEN)
    before = len(github_com.attempts)
    done = clone(service, person, clones, source, "private-two-second")
    assert (done.state, done.error, done.log) == ("ready", None, None), done
    assert set(github_com.attempts[before:]) == {("x-access-token", True)}
    work = podman.layout(done.container).work
    assert in_it(podman, done.container, ["cat", f"{work}/README.md"]).out == b"# private-two\n"
    config = in_it(podman, done.container, ["cat", f"{work}/.git/config"]).out
    assert TOKEN.encode() not in config and b"x-access-token" not in config
    assert repository(podman, done.container).status()["branch"]["name"] == "main"
    row = (
        db.connect(person[1])
        .execute("select error, log, source from workspaces where id = ?", (done.id,))
        .fetchone()
    )
    assert TOKEN not in " ".join(str(v) for v in row)


def test_a_token_github_refuses_is_told_so(service, person, clones, github_com, account, tmp_path):
    publish(github_com, tmp_path, "private-three", private=True)
    account[0].save_token(account[1], "github_pat_not-the-one-the-stand-in-takes")
    accepted = len([a for a in github_com.attempts if a[1]])
    done = clone(service, person, clones, "https://github.com/octo/private-three", "refused")
    assert done.state == "failed"
    assert done.error.splitlines()[0] == CLONE_MESSAGES["token_refused"], done.error
    assert len([a for a in github_com.attempts if a[1]]) == accepted
    assert "not-the-one" not in done.error + (done.log or "")


def test_a_repository_that_is_not_there_is_said_not_to_be(service, person, clones, github_com):
    done = clone(service, person, clones, "https://github.com/octo/not-there", "gone")
    assert done.state == "failed"
    head, exit_line, *said = done.error.splitlines()
    assert head == CLONE_MESSAGES["not_found"], done.error  # recognised from git's real words
    assert exit_line == "git clone failed, exit code 128:"
    assert any("not found" in line for line in said), said


def test_a_github_that_cannot_be_reached_is_said_not_to_be(
    service, person, clones, github_com, pki
):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki / "leaf.crt", pki / "leaf.key")
    github_com.stop()  # nothing listens on its address now: a refused connection
    try:
        done = clone(service, person, clones, "https://github.com/octo/anything", "unreached")
    finally:
        github_com.start(github_com.root, TOKEN, ssl_context=context, host=ADDRESS, port=443)
    assert done.state == "failed"
    assert done.error.splitlines()[0] == CLONE_MESSAGES["network"], done.error
    assert "Failed to connect" in done.error or "unable to access" in done.error


def test_deleting_a_workspace_that_is_being_cloned_stops_git_and_leaves_nothing(
    service, person, clones, podman, github_com, tmp_path
):
    """The cancel: the clone is slow (the stand-in's gate is closed), and the workspace's
    container is removed by force by Podman, which ends git with it."""
    publish(github_com, tmp_path, "slow-one")
    before = len(github_com.requests)
    github_com.gate.clear()
    try:
        made = service.create(person[0], "slow", "https://github.com/octo/slow-one")
        clones.append(made)
        deadline = time.monotonic() + 120
        while len(github_com.requests) == before:  # git is connected, and waits for the answer
            assert time.monotonic() < deadline, service.get(made.id)
            time.sleep(0.1)
        now = service.get(made.id)
        assert now.state == "creating" and now.log.splitlines()[-1] == f"Cloning {now.source}"
        top = base.podman_cli("top", made.container, "args")
        assert "clone" in top.stdout, top.stdout + top.stderr  # positive: git is running there
        started = time.monotonic()
        service.delete(now)
        gone(service, made.id, 60)
        assert time.monotonic() - started < 60  # not after the gate's wait
        assert not exists("container", made.container)
        assert not exists("volume", made.container)
    finally:
        github_com.gate.set()
