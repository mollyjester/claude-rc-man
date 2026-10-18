"""What the container suites share that is not a suite of its own, on the tests' engine
(tests/podman_service.py): the engine itself, a checkout's snapshot cloned in a container, the
containers a killed run left, and the whole stack in a browser.

Every test here takes the ``podman_service`` fixture, so it carries the ``container`` marker
(tests/container_marker.py). Where the user's rootless Podman cannot run the tests skip, unless
``RCMAN_REQUIRE_CONTAINER=1`` (set by ``make test-container`` and ``make check-ui``, the
on-demand runs that ``make check-all`` runs after ``make check``), in which case they fail.
Every container started here is labelled as its run's, so a later run removes one a killed run
left, with its anonymous volumes (tests/containers.py). The checkout reaches a container as a
snapshot of its files (tests/snapshot.py), mounted read-only at /src.
"""

import json
import subprocess
from pathlib import Path

from tests import containers
from tests.git_fixtures import fixture_git, make_repo, write
from tests.snapshot import snapshot

REPO = Path(__file__).resolve().parent.parent


def test_podman_is_usable(podman_service):
    answered = podman_service.cli("info", "--format", "{{.Host.Security.Rootless}}")
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout.decode().strip() == "true"


# --- a checkout that is a git worktree ---------------------------------------------------

# Run in a container with a checkout at /src and tests/container at /tools: the clone
# make_clone makes, and git run from /src, as make_clone runs it.
CLONES = """
import json, sys
sys.path.insert(0, "/tools")
import clone
made = clone.make_clone()
user = clone.sh("git", "config", "--global", "--get", "user.name", user="dev").stdout.strip()
print(json.dumps({
    "files": clone.git("ls-tree", "-r", "--name-only", "HEAD", cwd=made).splitlines(),
    "readme": (made / "README.md").read_text(),
    "user": user,
}))
"""


def _worktree(tmp_path: Path) -> Path:
    """A git worktree, its .git a file naming a folder in its main checkout, with a
    tracked file changed and a file added on disk. forced.txt is tracked though its
    own .gitignore names it, as a file added with ``git add -f`` is."""
    main = make_repo(tmp_path / "main")
    write(main / ".gitignore", "forced.txt\n")
    write(main / "forced.txt")
    fixture_git(main, "add", ".gitignore")
    fixture_git(main, "add", "-f", "forced.txt")
    fixture_git(main, "commit", "-q", "-m", "forced")
    worktree = tmp_path / "wt"
    fixture_git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    write(worktree / "README.md", "changed on disk\n")
    write(worktree / "added.txt")
    assert (worktree / ".git").is_file()
    return worktree


def _clone_at_src(engine, image, labelled, source, checkout):
    """CLONES in a container of ``image`` with no network, ``checkout`` read-only at /src."""
    tools = source / "tests" / "container"
    return engine.cli(
        "run", "--rm", "--pull=never", "--network=none", *labelled, "-v", f"{checkout}:/src:ro",
        "-v", f"{tools}:/tools:ro", "-w", "/src", image, "python3", "-c", CLONES,
        timeout=600, text=True,
    )  # fmt: skip


def test_a_worktrees_snapshot_is_cloned_in_a_container(
    podman_service, system_image, labelled, source, tmp_path
):
    worktree = _worktree(tmp_path)
    copy = tmp_path / "copy"
    copy.mkdir()
    snapshot(worktree, copy)

    proc = _clone_at_src(podman_service, system_image, labelled, source, copy)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = json.loads(proc.stdout.splitlines()[-1])
    assert sorted(found["files"]) == [".gitignore", "README.md", "added.txt", "forced.txt"]
    assert found["readme"] == "changed on disk\n"
    assert found["user"] == "dev"


def test_a_worktree_mounted_as_it_is_is_not_a_repository_in_a_container(
    podman_service, system_image, labelled, source, tmp_path
):
    """The twin: the worktree itself at /src, its .git naming a folder the container
    does not have."""
    worktree = _worktree(tmp_path)

    proc = _clone_at_src(podman_service, system_image, labelled, source, worktree)

    assert proc.returncode != 0
    assert "not a git repository" in proc.stdout + proc.stderr


# --- the containers a run starts itself ----------------------------------------------------

# What a probe container runs: long enough to outlive a check, never so long that one a
# failed test leaves runs on.
PROBE = ["sleep", "300"]


def _probe(engine: list[str], image: str, labels: list[str]) -> str:
    """A container labelled with ``labels``, with an anonymous volume."""
    started = subprocess.run(
        [
            *engine,
            "run",
            "-d",
            "--rm",
            "--pull=never",
            "--network=none",
            *labels,
            "-v",
            "/v",
            image,
            *PROBE,
        ],
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    return started.stdout.strip()


def _volume_of(engine: list[str], container: str) -> str:
    found = subprocess.run(
        [*engine, "inspect", "--format", "{{range .Mounts}}{{.Name}}{{end}}", container],
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0 and found.stdout.strip(), found.stderr
    return found.stdout.strip()


def _volume_exists(engine: list[str], volume: str) -> bool:
    found = subprocess.run([*engine, "volume", "ls", "-q"], capture_output=True, text=True)
    assert found.returncode == 0, found.stderr
    return volume in found.stdout.split()


def _exists(engine: list[str], container: str) -> bool:
    found = subprocess.run(
        [*engine, "ps", "-a", "-q", "--no-trunc", "--filter", f"id={container}"],
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0, found.stderr
    return container in found.stdout.split()


def test_a_runs_containers_are_removed_with_their_volumes_when_it_ends(
    podman_service, system_image
):
    engine = podman_service.argv()
    with containers.holder(engine) as folder:
        mine = _probe(engine, system_image, containers.labels(folder))
        volume = _volume_of(engine, mine)
        assert _exists(engine, mine) and _volume_exists(engine, volume)  # positive control
    assert not _exists(engine, mine)
    assert not _volume_exists(engine, volume)


def test_a_container_another_holder_labels_is_left(podman_service, system_image):
    """Planted twin: a run removes the containers labelled with its own holder, and no other."""
    engine = podman_service.argv()
    with containers.holder(engine) as other:
        theirs = _probe(engine, system_image, containers.labels(other))
        with containers.holder(engine) as folder:
            assert folder != other
        assert _exists(engine, theirs)
    assert not _exists(engine, theirs)
