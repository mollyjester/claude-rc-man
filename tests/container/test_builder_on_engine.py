"""The devcontainer builder (installer/builder, builder.py, ``PodmanRuntime.build_image``) on the
tests' engine: the image the installer builds, run in the builder's slot of the suites' sizes
(tests/container/sizes.py), with a Podman inside on native overlay and no FUSE device, building a
Dockerfile and a local feature into an image a workspace then runs from.

The builder image's Node stage and the Dockerfile's base come from their registries.
"""

from __future__ import annotations

import json

import pytest
from rcman import builder, devcontainer
from rcman import runtime as rt

from installer import install as ins
from tests.container import sizes
from tests.container.conftest import engine_runtime
from tests.containers import digest_references

BUILD_ID, SOURCE_ID, PROBE_ID, MADE_ID = 721, 722, 723, 724
PERSON, SLOT = 5, 5
# A glibc base without a package to install, a file of the repository, and a feature's tool.
FILES = {
    ".devcontainer/Dockerfile": (
        f"FROM {digest_references(ins.CONTAINERFILE)[0]}\n"
        "COPY .devcontainer/marker.txt /etc/marker\n"
        "RUN rm /etc/legal\n"  # a whiteout in the image's own layer
        "ENV FROM_DOCKERFILE=yes\n"
    ),
    ".devcontainer/marker.txt": "from the repository\n",
    ".devcontainer/tool/devcontainer-feature.json": json.dumps(
        {
            "id": "tool",
            "version": "1.0.0",
            "name": "a tool",
            "options": {"greeting": {"type": "string", "default": "hello"}},
            "containerEnv": {"MYTOOL_HOME": "/opt/mytool"},
        }
    ),
    ".devcontainer/tool/install.sh": (
        "#!/bin/sh\nset -e\nmkdir -p /opt/mytool/bin\n"
        "printf '#!/bin/sh\\necho mytool %s\\n' \"$GREETING\" > /opt/mytool/bin/mytool\n"
        "chmod +x /opt/mytool/bin/mytool\n"
    ),
}
BUILD = {
    "build": {"dockerfile": "Dockerfile", "context": ".."},
    "features": {"./tool": {"greeting": "from-the-file"}},
}


@pytest.fixture(scope="module")
def building(podman_service, default_image, engine_tools, engine_store, engine_scratch,
             builder_image):  # fmt: skip
    return engine_runtime(podman_service, default_image, engine_tools, engine_store,
                          engine_scratch, builder_image=builder_image)  # fmt: skip


def ok(result: rt.Result) -> str:
    assert result.code == 0, result
    return result.out.decode()


def test_the_builder_runs_in_its_slot_on_native_overlay_with_no_fuse_device(building):
    name = builder.builder_name(PROBE_ID)
    building._podman("volume", "create", name)
    try:
        building._podman(*builder.run_argv(PROBE_ID, {}, building._ids(sizes.BUILDER_SLOT),
                                           building.config.builder_image,
                                           network=building.config.network))  # fmt: skip
        inside = ["exec", name]
        mapped = building._podman(*inside, "cat", "/proc/self/uid_map").stdout.decode().split()
        assert mapped == ["0", str(sizes.BUILDER_SLOT * sizes.TEST_RANGE), str(sizes.TEST_RANGE)]
        info = building._podman(*inside, "podman", "info", "--format",
                                "{{.Store.GraphDriverName}}|{{.Store.GraphOptions}}|"
                                "{{.Store.GraphStatus}}").stdout.decode()  # fmt: skip
        driver, options, status = info.strip().split("|")
        assert driver == "overlay" and "mount_program" not in options
        assert "Native Overlay Diff:true" in status
        devices = building._podman(*inside, "ls", "/dev").stdout.decode().split()
        assert "null" in devices and "fuse" not in devices
    finally:
        building.remove_build(PROBE_ID)


def test_a_dockerfile_and_a_feature_are_built_in_the_builders_slot_and_a_workspace_runs_it(
    building,
):
    source = rt.Spec(rt.container_name(SOURCE_ID), slot=SLOT, workspace=SOURCE_ID, person=PERSON)
    building.create(source)
    made = None
    try:
        layout = building.layout(source.name)
        for path, text in FILES.items():
            ok(
                building.exec(
                    source.name,
                    ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
                    stdin=text.encode(),
                    cwd=layout.work,
                )
            )
        ok(building.exec(source.name, ["chmod", "755", ".devcontainer/tool/install.sh"],
                         cwd=layout.work))  # fmt: skip
        request = rt.BuildRequest(BUILD_ID, PERSON, sizes.BUILDER_SLOT, source.name,
                                  f".devcontainer/{devcontainer.BUILD_CONFIG_NAME}",
                                  json.dumps(BUILD))  # fmt: skip
        said: list[str] = []
        image = building.build_image(request, said.append, None, devcontainer.check_metadata)
        assert image == rt.built_name(BUILD_ID)
        assert building.built_images() == {BUILD_ID: image}
        assert BUILD_ID not in building.builders()  # the builder and its volume are gone
        assert any(builder.STEP.match(line) for line in said), said
        made = rt.Spec(rt.container_name(MADE_ID), slot=SLOT, workspace=MADE_ID, person=PERSON,
                       image=image)  # fmt: skip
        building.create(made)
        assert ok(building.exec(made.name, ["/opt/mytool/bin/mytool"])) == "mytool from-the-file\n"
        assert ok(building.exec(made.name, ["cat", "/etc/marker"])) == "from the repository\n"
        assert building.exec(made.name, ["test", "-e", "/etc/legal"]).code != 0  # the whiteout
        assert ok(building.exec(made.name, ["test", "-e", "/etc/os-release"])) == ""  # control
    finally:
        if made is not None:
            building.remove(made.name)
        building.remove(source.name)
        building.remove_build(BUILD_ID, image=True)
        building.remove_account(PERSON)


# --- what native overlay rests on, shown by a builder without it --------------------------------

TWIN_ID = 725


def nested_in_builder(runtime, image: str, drop=()) -> rt.Result:
    """A builder of ``image`` run with run_argv but for ``drop``, its Podman importing a busybox
    and running it."""
    from tests.container.test_nest_in_podman import tarball

    name = builder.builder_name(TWIN_ID)
    runtime._podman("volume", "create", name)
    try:
        argv = builder.run_argv(TWIN_ID, {}, runtime._ids(sizes.BUILDER_SLOT), image,
                                network=runtime.config.network)  # fmt: skip
        assert set(drop) <= set(argv)  # what is taken away is there to take
        argv = [a for a in argv if a not in drop]
        runtime._podman(*argv)
        made = runtime._podman("exec", "--interactive", name, "podman", "import", "-",
                               "localhost/rcman-test-bb", stdin=tarball(), check=False)  # fmt: skip
        if made.returncode != 0:
            return rt.Result(made.returncode, made.stdout, made.stderr)
        ran = runtime._podman(
            "exec",
            name,
            "podman",
            "run",
            "--rm",
            "--pull=never",
            "localhost/rcman-test-bb",
            "busybox",
            "echo",
            "ran",
            check=False,
        )
        return rt.Result(ran.returncode, ran.stdout, ran.stderr)
    finally:
        runtime.remove_build(TWIN_ID)


def test_the_builders_podman_runs_a_container_with_sys_admin_and_not_without_it(building):
    image = building.config.builder_image
    assert nested_in_builder(building, image).out == b"ran\n"  # the control
    failed = nested_in_builder(building, image, drop=["--cap-add=SYS_ADMIN"])
    assert failed.code != 0, failed
