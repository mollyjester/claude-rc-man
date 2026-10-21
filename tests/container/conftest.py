"""What the modules of tests/container/ share on the tests' own engine (tests/podman_service.py):
the tools every workspace mounts, an empty Claude store and one holding the fake `claude`, the
default workspace image and the builder image built from the installer's own files, Podman
runtimes and rcman's settings made with the suites' sizes (tests/container/sizes.py), the
stand-ins of tests/container/stand_ins.py as programs, and the whole stack
(tests/container/stack.py) with what it is made of.

The tools are those ``installer/install.py`` writes: busybox and its shell, the terminal
descriptions, tmux's configuration, the Podman tools and the workspaces' seccomp profile of
``ensure_podman_tools``, the instructions for Claude of ``ensure_instructions``, and tmux,
built on the engine with the installer's own argument list from its pinned source in the
toolchain (the ``toolchain`` fixture), as the installer builds it through rcman's own Podman.
They are in held folders under /tmp, readable by everyone, as the installer's are: a
container's ids are none of the user's, and pytest's own folders are the user's alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest
from rcman import runtime as rt

from devtools import held_folders as hf
from installer import install as ins
from tests import podman_service as ps
from tests.container import sizes, stack

REPO = Path(__file__).resolve().parent.parent.parent


def readable(folder: Path) -> None:
    for top, _dirs, files in os.walk(folder):
        os.chmod(top, 0o755)
        for name in files:
            path = Path(top, name)
            if not path.is_symlink():
                os.chmod(path, 0o755 if os.access(path, os.X_OK) else 0o644)


def built_tmux(engine, toolchain: Path) -> bytes:
    """tmux, built on ``engine`` as the installer builds it (``ensure_tools``): its argument
    list, the program on standard output and its sha256 on standard error."""
    program = engine.cli(*ins.tmux_build_argv(ins.tmux_tarball(toolchain)), timeout=1800)
    assert program.returncode == 0, program.stderr[-4000:]
    reported = ins.TMUX_SHA256_LINE.findall(program.stderr)
    assert reported and reported[-1].decode() == hashlib.sha256(program.stdout).hexdigest()
    return program.stdout


@pytest.fixture(scope="session")
def engine_tools(podman_service, toolchain):
    with hf.held("rctools-") as folder:
        os.chmod(folder, 0o755)
        (folder / "bin").mkdir()
        (folder / "bin" / "tmux").write_bytes(built_tmux(podman_service, toolchain))
        os.chmod(folder / "bin" / "tmux", 0o755)
        assert ins.is_static(folder / "bin" / "tmux")
        shutil.copyfile(ins.BUSYBOX, folder / "bin" / "busybox")
        assert ins.is_static(folder / "bin" / "busybox")
        os.chmod(folder / "bin" / "busybox", 0o755)
        os.symlink("busybox", folder / "bin" / "sh")
        for entry in ins.TERMINFO:
            source = next(d / entry for d in ins.TERMINFO_DIRS if (d / entry).exists())
            (folder / "terminfo" / entry).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, folder / "terminfo" / entry)
        shutil.copyfile(REPO / "backend" / "rcman" / "tmux.conf", folder / "tmux.conf")
        ins.ensure_podman_tools(folder, ps.SHARED)
        ins.ensure_instructions(folder, REPO / "backend" / "rcman" / "instructions")
        readable(folder)
        yield folder


@pytest.fixture(scope="session")
def engine_store():
    with hf.held("rcstore-") as folder:
        os.chmod(folder, 0o755)
        yield folder


def fill_store(store: Path, version: str) -> None:
    """The Claude Code store as the installer leaves it, holding the fake `claude` as
    ``version`` (tests/claude_download.py), the one ``current`` names and ``bin/claude`` leads
    to; readable by everyone, as a workspace's ids are none of the user's."""
    from tests import claude_download

    for folder in (store / "bin", store / "versions"):
        folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    binary = store / "versions" / version
    binary.write_bytes(claude_download.bundle(version))
    binary.chmod(0o755)
    for folder, name, target in (
        (store, "current", f"versions/{version}"),
        (store / "bin", "claude", "../current"),
    ):
        (folder / name).unlink(missing_ok=True)
        os.symlink(target, folder / name)
    readable(store)


@pytest.fixture(scope="session")
def fake_store():
    """A Claude Code store holding the fake `claude`, for the modules whose workspaces run it."""
    from tests.fake_store import FAKE_VERSION

    with hf.held("rcfake-") as folder:
        fill_store(folder, FAKE_VERSION)
        yield folder


@pytest.fixture(scope="session")
def engine_scratch():
    with hf.held("rcscratch-") as folder:
        yield folder


@pytest.fixture(scope="session")
def base_image(podman_service) -> str:
    """The image the default image starts from, by its digest, pulled from its registry and
    held for the session: an image with no Podman and no git."""
    from tests.containers import digest_references

    reference = digest_references(ins.CONTAINERFILE)[0]
    pulled = podman_service.cli("pull", "-q", reference, timeout=1200)
    assert pulled.returncode == 0, pulled.stderr
    return ps.hold(podman_service, reference)


@pytest.fixture(scope="session")
def default_image(podman_service, tmp_path_factory) -> str:
    """The default workspace image, built on the engine from the build context the installer
    makes for it."""
    name = ins.workspace_image()  # the installer's own name for it
    context = tmp_path_factory.mktemp("default-image")
    ins.image_context(context)
    built = podman_service.cli(
        "build", "-q", "-t", name, "-f", str(context / "Containerfile"), str(context),
        timeout=1800,
    )  # fmt: skip
    assert built.returncode == 0, built.stdout + built.stderr
    return ps.hold(podman_service, name)


@pytest.fixture(scope="session")
def builder_image(podman_service, tmp_path_factory) -> str:
    """The builder image, built on the engine from the installer's own folder."""
    context = tmp_path_factory.mktemp("builder-image")
    shutil.copytree(ins.BUILDER_DIR, context, dirs_exist_ok=True)
    name = ins.devcontainer_builder_image()
    built = podman_service.cli("build", "-q", "-t", name, "--file", str(context / "Containerfile"),
                               str(context), timeout=1800)  # fmt: skip
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-4000:]
    return ps.hold(podman_service, name)


def engine_runtime(
    engine,
    image: str,
    tools: Path,
    store: Path,
    scratch: Path,
    network: str = sizes.TEST_NETWORK,
    kind: type[rt.PodmanRuntime] = rt.PodmanRuntime,
    **more,
) -> rt.PodmanRuntime:
    """A Podman runtime (or ``kind``, a subclass of it) on the engine with the suites' sizes, as
    the product makes one but for what is injected (rcman/runtime.py INJECTED): ``network`` is
    the engine's own unless a module gives its workspaces a network of their own
    (tests/container/network.py)."""
    return kind(
        rt.PodmanConfig(
            socket=engine.socket,
            image=image,
            tools_dir=tools,
            claude_store=store,
            scratch_dir=scratch,
            range=sizes.TEST_RANGE,
            network=network,
            slices=False,
            **more,
        )
    )


@pytest.fixture(scope="session")
def podman(podman_service, default_image, engine_tools, engine_store, engine_scratch):
    return engine_runtime(podman_service, default_image, engine_tools, engine_store, engine_scratch)


def engine_of(runtime: rt.PodmanRuntime) -> ps.Engine:
    """The engine a runtime on it drives, found by its socket, which is in the engine's folder."""
    return ps.Engine(Path(runtime.config.socket).parent)


def cli(runtime: rt.PodmanRuntime, *args: str, timeout: float = 1200):
    """podman on the engine ``runtime`` drives, as text."""
    return engine_of(runtime).cli(*args, timeout=timeout, text=True, errors="replace")


def image_of(runtime: rt.PodmanRuntime, name: str) -> str | None:
    """The id of the image the container ``name`` was made from, asked of the engine ``runtime``
    drives; None where there is no such container."""
    found = cli(runtime, "container", "inspect", "--format", "{{.Image}}", name)
    if found.returncode != 0:
        return None
    return found.stdout.strip().removeprefix("sha256:")


def on_host(runtime: rt.PodmanRuntime, path: str, format: str = "%u %g %a") -> list[str]:
    """``stat`` of a path in the engine's storage, as the engine's user namespace sees ids: a
    range's root is the range's base."""
    found = engine_of(runtime).local("unshare", "stat", "-c", format, path, timeout=120)
    assert found.returncode == 0, found.stderr
    return found.stdout.decode().split()


def engine_settings(database: Path, store: Path, **more):
    """rcman's settings for a service on the engine: the database, and beside it the allowed
    list, and the Claude store of the module's runtime. No path of the settings' defaults, which
    are an installation's (/var/lib/rcman), is left to be read or written."""
    from rcman.settings import Settings

    return Settings(
        **{
            "public_origin": "https://rcman.test",
            "database": database,
            "allowed_emails": database.parent / "allowed-emails",
            "claude_store": store,
            **more,
        }
    )


STAND_INS = REPO / "tests" / "container" / "stand_ins.py"


def stand_in(folder: Path, name: str, state: Path) -> Path:
    """The stand-in (tests/container/stand_ins.py) for the program ``name`` as a program of that
    name in ``folder``, keeping what it records and holds in ``state``."""
    program = folder / name
    program.write_text(
        f'#!/bin/sh\nRCMAN_STAND_IN_DIR={state} exec {sys.executable} {STAND_INS} {name} "$@"\n'
    )
    program.chmod(0o755)
    return program


# --- the whole stack (tests/container/stack.py) ----------------------------------------------


@pytest.fixture(scope="session")
def stack_prepared(podman_service, system_image, source, toolchain, labelled):
    """The backend's packages, as a virtual environment of the system image's Python, and the
    built frontend, both made from the checkout's files in a container of the system image with
    the engine's own network: a folder with ``venv`` and ``dist``."""
    with hf.held("rcprep-") as folder:
        os.chmod(folder, 0o755)
        for name in ("venv", "dist"):
            (folder / name).mkdir()
        node, uv = ins.node_bin(ins.TOOLCHAIN), ins.uv_binary(ins.TOOLCHAIN)
        script = (
            "set -e; cp -a /src /tmp/s; cd /tmp/s/frontend; "
            f"PATH={node}:$PATH npm ci --no-audit --no-fund; PATH={node}:$PATH npm run build; "
            f"cp -a dist/. {stack.DIST}/; cd /tmp/s; UV_PROJECT_ENVIRONMENT={stack.VENV} {uv} sync "
            "--frozen --no-dev --no-install-project --python /usr/bin/python3"
        )
        done = podman_service.cli(
            "run", "--rm", "--pull=never", "--network=host", *labelled, "-v", f"{source}:/src:ro",
            "-v", f"{toolchain}:{ins.TOOLCHAIN}:ro", "-v", f"{folder / 'venv'}:{stack.VENV}",
            "-v", f"{folder / 'dist'}:{stack.DIST}", system_image, "sh", "-c", script,
            timeout=2400,
        )  # fmt: skip
        assert done.returncode == 0, (done.stdout + done.stderr).decode(errors="replace")[-8000:]
        yield folder


@contextlib.contextmanager
def a_stack(engine, name, system_image, default_image, builder_image, engine_tools, prepared,
            toolchain, source, labelled):  # fmt: skip
    """A stack of its own, with a store of its own holding the fake claude as the tested
    version, as the installer leaves the store."""
    with hf.held("rcstore-") as store:
        version = (REPO / "helpers" / "claude-version").read_text().strip()
        fill_store(store, version)
        with stack.launched(
            engine, name=name, system_image=system_image, default_image=default_image,
            builder_image=builder_image, tools=engine_tools, store=store, prepared=prepared,
            toolchain=toolchain, source=source, labelled=labelled,
        ) as found:  # fmt: skip
            yield found
