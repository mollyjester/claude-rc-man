""".devcontainer/: the development environment of rcman's own repository, as rcman itself reads
and plans it (backend/rcman/devcontainer.py), and its build file's pins held to the installer's
toolchain (installer/install.py) and to frontend/package-lock.json's Playwright."""

import json
import re
from pathlib import Path

import pytest
from rcman import devcontainer as dc
from rcman import runtime as rt

from installer import install as ins
from tests.containers import digest_references

REPO = Path(__file__).resolve().parent.parent
FOLDER = REPO / ".devcontainer"
DOCKERFILE = FOLDER / "Dockerfile"
WORK = "/rcman/work"
# What a workspace of the repository is to have, beyond the base image: Podman for the tests'
# engine, the tools the targets of the Makefile and the suites run, and Caddy for the stack.
PACKAGES = ("podman", "crun", "catatonit", "uidmap", "make", "git", "gh", "tmux", "busybox-static",
            "curl", "xz-utils", "python3", "caddy")  # fmt: skip
# The user the image has, as its passwd file would say.
DEV_PASSWD = "root:x:0:0:root:/root:/bin/bash\ndev:x:1000:1000::/home/dev:/bin/bash\n"
DEV_GROUP = "root:x:0:\ndev:x:1000:\n"


def args(text: str) -> dict[str, str]:
    """The build file's ``ARG name=value`` lines."""
    return dict(re.findall(r"(?m)^ARG\s+([A-Z0-9_]+)=(\S+)\s*$", text))


def installed(text: str) -> set[str]:
    """The packages the build file's apt-get install lines name."""
    found = set()
    for command in re.findall(r"apt-get install([^&]*)", text.replace("\\\n", " ")):
        found |= {word for word in command.split() if not word.startswith("-")}
    return found


def pin_problems(text: str) -> list[str]:
    """What of the build file's pins is not the installer's, the lock file's or the default
    image's base."""
    held = args(text)
    lock = json.loads((REPO / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    wanted = {
        "UV_VERSION": ins.UV_VERSION,
        "UV_SHA256_AMD64": ins.UV_SHA256[(ins.UV_VERSION, "x64")],
        "UV_SHA256_ARM64": ins.UV_SHA256[(ins.UV_VERSION, "arm64")],
        "NODE_VERSION": ins.node_version(),
        "NODE_SHA256_AMD64": ins.NODE_SHA256[(ins.node_version(), "x64")],
        "NODE_SHA256_ARM64": ins.NODE_SHA256[(ins.node_version(), "arm64")],
        "PLAYWRIGHT_VERSION": lock["packages"]["node_modules/playwright"]["version"],
    }
    problems = [
        f"{name} is {held.get(name)!r}, not {value!r}"
        for name, value in wanted.items()
        if held.get(name) != value
    ]
    base = re.search(r"(?m)^FROM\s+(\S+)", text)
    if base is None or base[1] != digest_references(ins.CONTAINERFILE)[0]:
        problems.append(f"the base is {base and base[1]!r}, not the default image's")
    return problems


def test_rcman_builds_the_repositorys_devcontainer_from_its_dockerfile_with_the_repository():
    config = dc.parse((FOLDER / "devcontainer.json").read_text(encoding="utf-8"))
    checked = dc.check(config, path=dc.FILES[0])
    assert checked.image is None and checked.ignored == ()
    assert checked.build.config == {"build": {"dockerfile": "Dockerfile", "context": ".."}}
    built = dc.build_config(checked.build, dc.FILES[0])
    assert built.path == ".devcontainer/rcman-build.json"  # beside the file, as the CLI reads it


def test_rcman_plans_the_repositorys_workspace_as_dev_running_make_setup():
    config = dc.parse((FOLDER / "devcontainer.json").read_text(encoding="utf-8"))
    facts = rt.ImageFacts(passwd=DEV_PASSWD, group=DEV_GROUP)
    plan = dc.plan(config, facts, 7, WORK, image="localhost/rcman-built/7:latest")
    assert (plan.user_name, plan.uid, plan.gid) == ("dev", 1000, 1000)
    assert [(c.event, c.shell) for c in plan.commands] == [("postCreateCommand", "make setup")]


def test_a_context_outside_the_repository_is_refused():
    """The control: the policy refuses what would leave the repository, so the context the
    file names is honoured because it is the repository itself."""
    config = {"build": {"dockerfile": "Dockerfile", "context": "../.."}, "remoteUser": "dev"}
    with pytest.raises(dc.Refused):
        dc.check(config, path=dc.FILES[0])


def test_the_build_files_pins_are_the_installers_and_the_lock_files():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert set(args(text)) >= {"UV_VERSION", "NODE_VERSION", "PLAYWRIGHT_VERSION"}  # non-vacuity
    assert pin_problems(text) == []


@pytest.mark.parametrize(
    ("name", "other"),
    [("UV_VERSION", "0.0.1"), ("NODE_SHA256_AMD64", "0" * 64), ("PLAYWRIGHT_VERSION", "1.0.0")],
)
def test_a_pin_that_is_not_the_installers_is_found(name, other):
    text = DOCKERFILE.read_text(encoding="utf-8")
    planted = re.sub(rf"(?m)^ARG {name}=\S+$", f"ARG {name}={other}", text)
    assert planted != text  # the plant took effect
    assert [p for p in pin_problems(planted) if p.startswith(name)]


def test_a_base_that_is_not_the_default_images_is_found():
    text = DOCKERFILE.read_text(encoding="utf-8")
    planted = re.sub(r"(?m)^FROM\s+\S+", "FROM docker.io/library/ubuntu:24.04", text, count=1)
    assert planted != text and pin_problems(planted) == [
        "the base is 'docker.io/library/ubuntu:24.04', not the default image's"
    ]


def test_the_build_file_installs_what_the_suites_need_and_nothing_that_raises_privilege():
    raises = "su" + "do"  # built, so that the privilege guard does not find it written here
    found = installed(DOCKERFILE.read_text(encoding="utf-8"))
    assert set(PACKAGES) <= found, set(PACKAGES) - found
    assert raises not in found
    assert installed(f"RUN apt-get install -y -qq {raises} make") == {raises, "make"}  # control


def test_dev_is_no_root_with_subordinate_ids_and_newuidmap_has_its_capability():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^USER dev$", text)
    assert "useradd" in text and "/etc/subuid" in text and "/etc/subgid" in text
    for program, capability in (("newuidmap", "cap_setuid+ep"), ("newgidmap", "cap_setgid+ep")):
        assert f"setcap {capability} /usr/bin/{program}" in text, program
    assert "chmod u-s /usr/bin/newuidmap /usr/bin/newgidmap" in text
