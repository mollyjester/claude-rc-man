"""backend/rcman/devcontainer.py: reading a repository's devcontainer.json (JSON with comments),
the policy over every key, the plan made of the file and the image's metadata, and the
image's users and groups. Pure: nothing here starts a workspace.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest
from rcman import devcontainer as dc
from rcman import runtime as rt
from rcman.devcontainer import HONOUR, IGNORE, REFUSE, Refused

REPO = Path(__file__).resolve().parent.parent
WORK = "/rcman/work"
PASSWD = (
    "# a comment\n\n"
    "root:x:0:0:root:/root:/bin/bash\n"
    "dev:x:1234:1235:Dev,,,:/home/dev:/bin/zsh\n"
    "nogroup-user:x:4000:65534::/nonexistent:/usr/sbin/nologin\n"
    "this line is not a passwd line\n"
)
GROUP = "root:x:0:\ndev:x:1235:\nextra:x:5000:dev,someone\nother:x:5001:someone\nbroken\n"


def facts(**more) -> rt.ImageFacts:
    return rt.ImageFacts(**{"passwd": PASSWD, "group": GROUP, **more})


def planned(config: dict, image_facts: rt.ImageFacts | None = None, workspace: int = 7):
    return dc.plan(config, image_facts or facts(), workspace, WORK)


# --- JSON with comments -----------------------------------------------------------------


def test_comments_and_trailing_commas_are_read():
    text = """// the line
    {
      /* a block
         over lines */
      "image": "ubuntu:26.04", // after a value
      "forwardPorts": [1, 2,],
      "containerEnv": {"A": "b",},
    }
    """
    assert dc.parse(text) == {
        "image": "ubuntu:26.04",
        "forwardPorts": [1, 2],
        "containerEnv": {"A": "b"},
    }


# --- the policy over every key --------------------------------------------------------------


def with_image(fragment: dict) -> dict:
    return {"image": "ubuntu:26.04", **fragment}


def test_a_key_is_honoured_ignored_or_refused_as_the_policy_says():
    """The policy refuses by design, so the refusal is held here with what is taken."""
    checked = dc.check(with_image({"remoteUser": "root", "name": "A name"}))
    assert checked.image == "ubuntu:26.04"
    assert checked.ignored == ("name",)  # remoteUser is honoured, name logged as ignored
    with pytest.raises(Refused) as refused:
        dc.check(with_image({"privileged": True}))
    assert refused.value.kind == "refused_key"
    assert refused.value.fields["key"] == "privileged"
    assert dc.POLICY["privileged"].reason in str(refused.value)


# --- forwarded ports ----------------------------------------------------------------------------


def test_forward_ports_merged_from_image_and_file_into_plan_ports():
    entries = label(
        {"id": "f", "forwardPorts": [3000, "localhost:808"],
         "portsAttributes": {"3000": {"label": "From the image"}, "808": {"label": "Web"}}},
        {"forwardPorts": [9000, 3000]},
    )  # fmt: skip
    config = with_image(
        {"forwardPorts": [808, 4000, 4000], "portsAttributes": {"808": {"label": "Mine"}}}
    )
    found = planned(config, facts(labels=entries))
    assert found.ports == (
        dc.Port(3000, "From the image"),
        dc.Port(808, "Mine"),  # the file's label, the last, wins
        dc.Port(9000),
        dc.Port(4000),  # duplicates dropped, the first place kept
    )


# --- variables -----------------------------------------------------------------------------


def test_the_workspace_folder_variables_are_the_work_folder_and_none_is_rcmans_own(monkeypatch):
    monkeypatch.setenv("RCMAN_SECRET_OF_THE_SERVICE", "do-not-leak")
    config = with_image(
        {
            "containerEnv": {
                "A": "${containerWorkspaceFolder}/sub",
                "B": "${containerWorkspaceFolderBasename}",
                "C": "${localEnv:RCMAN_SECRET_OF_THE_SERVICE}",
            }
        }
    )
    assert planned(config).container_env == {"A": f"{WORK}/sub", "B": "work", "C": ""}


# --- environment ----------------------------------------------------------------------------


def test_remote_environment_overrides_the_containers_and_null_removes_a_variable():
    config = with_image(
        {"containerEnv": {"A": "1", "B": "2", "C": "3"}, "remoteEnv": {"A": "9", "B": None}}
    )
    found = planned(config)
    assert found.container_env == {"A": "1", "B": "2", "C": "3"}
    assert found.env == {"A": "9", "C": "3"}


# --- lifecycle commands ------------------------------------------------------------------


def test_a_command_is_a_string_for_a_shell_an_array_for_exec_or_an_object_run_in_order():
    config = with_image(
        {
            "onCreateCommand": "make a",
            "updateContentCommand": ["make", "b"],
            "postCreateCommand": {"first": "make c", "second": ["make", "d"]},
            "postStartCommand": "make e",
        }
    )
    found = planned(config)
    assert [(c.event, c.label, c.shell, c.argv) for c in found.commands] == [
        ("onCreateCommand", None, "make a", None),
        ("updateContentCommand", None, None, ("make", "b")),
        ("postCreateCommand", "first", "make c", None),
        ("postCreateCommand", "second", None, ("make", "d")),
        ("postStartCommand", None, "make e", None),
    ]
    assert [c.shell for c in found.start_commands()] == ["make e"]  # run again at each start


# --- the user ----------------------------------------------------------------------------------


def test_the_remote_user_is_looked_up_in_the_images_passwd_and_keeps_its_groups_by_name():
    found = planned(with_image({"remoteUser": "dev"}))
    assert (found.uid, found.gid, found.run_as, found.groups, found.user_name) == (
        1234,
        1235,
        "dev",
        (),
        "dev",
    )


# --- the image's metadata ----------------------------------------------------------------------


def label(*entries) -> dict:
    return {"devcontainer.metadata": json.dumps(list(entries))}


def test_the_metadata_of_the_image_and_the_file_are_merged_the_file_last():
    entries = label(
        {
            "id": "feature-a",
            "containerEnv": {"A": "from-a", "ONLY_A": "a"},
            "onCreateCommand": "a-create",
            "postStartCommand": "a-start",
            "init": True,
        },
        {"remoteUser": "dev", "remoteEnv": {"R": "1"}, "postCreateCommand": "b-post"},
    )
    config = with_image(
        {
            "containerEnv": {"A": "from-config"},
            "remoteEnv": {"R": "2"},
            "postCreateCommand": "config-post",
            "init": False,
        }
    )
    found = planned(config, facts(labels=entries))
    assert found.container_env == {"A": "from-config", "ONLY_A": "a"}
    assert found.remote_env == {"R": "2"}
    assert found.uid == 1234  # the label's remoteUser, as the file names none
    assert found.init is True  # booleans are ORed
    # in the specs order of the events, and within one event the images' entries first
    assert [c.shell for c in found.commands] == ["a-create", "b-post", "config-post", "a-start"]


# --- the plan, stored ----------------------------------------------------------------------


def test_a_plan_survives_being_stored_and_read_back():
    config = with_image(
        {
            "remoteUser": "dev:dev",
            "containerEnv": {"A": "1"},
            "remoteEnv": {"B": "2"},
            "postCreateCommand": {"x": "make", "y": ["make", "y"]},
            "postStartCommand": "serve",
            "forwardPorts": [1, "localhost:300"],
            "portsAttributes": {"300": {"label": "App"}},
        }
    )
    found = planned(config)
    assert found.ports == (dc.Port(1), dc.Port(300, "App"))  # non-vacuity: it has ports
    again = dc.Plan.from_json(found.to_json())
    assert again == found
    assert json.loads(found.to_json())["image"] == "ubuntu:26.04"  # non-vacuity: it is JSON


# --- reading the file ------------------------------------------------------------------------


class Workspace:
    """A work folder on the host that answers ``read`` as a container's exec would."""

    def __init__(self, folder: Path):
        self.folder = folder
        host = rt.HostRuntime(folder, folder, folder, folder)
        self.layout = dataclasses.replace(host.layout("x"), work=str(folder))

    def run(self, argv: list[str], timeout: float, cwd: str | None = None) -> rt.Result:
        proc = subprocess.run(
            argv,
            cwd=cwd or self.folder,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=timeout,
        )
        return rt.Result(proc.returncode, proc.stdout, proc.stderr)


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return Workspace(folder)


def found(work: Workspace) -> dc.ConfigFile | None:
    return dc.read(work.run, work.layout)


def test_the_folder_file_comes_first_then_the_dotfile_and_the_place_is_said(work):
    (work.folder / ".devcontainer.json").write_text('{"image": "dotfile"}')
    assert found(work).path == dc.FILES[1]
    (work.folder / ".devcontainer").mkdir()
    (work.folder / ".devcontainer" / "devcontainer.json").write_text('{"image": "folder"}')
    assert found(work).path == dc.FILES[0]
    assert found(work).text == '{"image": "folder"}'


# --- building: a Dockerfile and features ------------------------------------------------------

# The only keys the build-only configuration the CLI is given may hold.
BUILD_KEYS = {"build", "image", "features", "overrideFeatureInstallOrder"}


def built(config: dict, **more) -> dc.BuildSpec:
    found = dc.check(config, **more).build
    assert found is not None
    return found


def test_a_dockerfile_is_a_build_and_needs_no_image():
    checked = dc.check({"build": {"dockerfile": "Dockerfile", "context": "..", "target": "dev"}})
    assert checked.image is None
    assert checked.build.config == {
        "build": {"dockerfile": "Dockerfile", "context": "..", "target": "dev"}
    }


def test_the_build_only_configuration_holds_the_build_keys_and_nothing_else_of_the_file():
    config = {
        "image": "ubuntu:26.04",
        "build": {"dockerfile": "Dockerfile", "context": "..", "args": {"A": "b"}},
        "features": {"./f": {}},
        "overrideFeatureInstallOrder": ["./f"],
        "onCreateCommand": "echo secret-command",
        "postStartCommand": "true",
        "containerEnv": {"A": "b"},
        "remoteEnv": {"R": "r"},
        "remoteUser": "dev",
        "forwardPorts": [80],
        "customizations": {"vscode": {"extensions": ["a.b"]}},
        "name": "a name",
        "init": True,
        "waitFor": "onCreateCommand",
    }
    made = dc.build_config(built(config), dc.FILES[0])
    written = json.loads(made.text)
    assert set(written) == BUILD_KEYS  # the positive control: all four are there
    for leaked in ("secret-command", "forwardPorts", "remoteUser", "vscode", "containerEnv"):
        assert leaked not in made.text
    assert written["build"]["args"] == {"A": "b"}


# --- the names of images rcman builds ------------------------------------------------------------


def test_the_name_of_an_image_rcman_built_cannot_be_named():
    """Refusing the names of rcman's own images is this rule's whole point."""
    image = f"localhost/{rt.BUILT_REPOSITORY}/7:latest"
    for call in (lambda: dc.check({"image": image}), lambda: planned({"image": image})):
        with pytest.raises(Refused) as refused:
            call()
        assert refused.value.kind == "bad_value" and refused.value.fields["key"] == "image"


# --- the plan of a built image -----------------------------------------------------------

BUILT = f"localhost/{rt.BUILT_REPOSITORY}/7:latest"


def test_a_plan_is_made_of_the_image_it_is_given_when_the_file_builds_one_or_names_none():
    config = {"build": {"dockerfile": "Dockerfile"}, "remoteUser": "dev"}
    found = dc.plan(config, facts(), 7, WORK, image=BUILT)
    assert found.image == BUILT and found.uid == 1234
    default = "localhost/rcman-workspace"
    config = {"remoteUser": "dev", "containerEnv": {"A": "b"}, "postCreateCommand": "x"}
    found = dc.plan(config, facts(id="d" * 64), 7, WORK, image=default)
    assert (found.image, found.image_id) == (default, "d" * 64)
    assert found.uid == 1234 and found.container_env["A"] == "b"
    assert [c.shell for c in found.commands] == ["x"]


# --- what the built image's label asks for, read before the image is copied ------------------

# The entry the CLI writes for a feature that asks for everything it may (recorded from the
# CLI's output, spike for the builder): the keys it copies from a feature.
FEATURE_ENTRY = {
    "id": "./feature2",
    "onCreateCommand": "echo f2-create",
    "updateContentCommand": "echo f2-update",
    "postCreateCommand": "echo f2-post",
    "postStartCommand": "echo f2-start",
    "postAttachCommand": "echo f2-attach",
    "init": True,
    "customizations": {"vscode": {"extensions": ["a.b"]}},
}


def test_a_built_images_label_is_accepted_unless_it_asks_for_what_the_file_may_not():
    """The check exists to refuse; the accepted label of a plain feature is its control."""
    dc.check_metadata(label(FEATURE_ENTRY))  # accepted whole
    entries = label({"id": "./ok"}, {**FEATURE_ENTRY, "id": "./greedy", "privileged": True})
    with pytest.raises(Refused) as refused:
        dc.check_metadata(entries)
    assert refused.value.kind == "refused_by_image"
    assert refused.value.fields["key"] == "privileged"
    assert refused.value.fields["entry"] == "./greedy"


# --- the guard that holds the administrators' page equal to the policy ----------------------


ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(honoured|ignored|refused)\s*\|\s*(.*?)\s*\|\s*$")
WORDS = {HONOUR: "honoured", IGNORE: "ignored", REFUSE: "refused"}


def documented(text: str) -> dict[str, tuple[str, str]]:
    """The keys the page's tables name: key -> (what rcman does, the reason as written)."""
    found = {}
    for line in text.splitlines():
        matched = ROW.match(line)
        if matched:
            found[matched.group(1)] = (matched.group(2), matched.group(3))
    return found


def differences(text: str, policy: dict) -> list[str]:
    """How the page's tables differ from ``policy``: one line for each key that is missing,
    extra, or says something else."""
    page = documented(text)
    found = []
    for key, entry in policy.items():
        want = (WORDS[entry.action], entry.reason)
        if key not in page:
            found.append(f"{key}: not in the page")
        elif page[key] != want:
            found.append(f"{key}: the page says {page[key]}, the policy {want}")
    found += [f"{key}: not in the policy" for key in page if key not in policy]
    return found


def page_for(policy: dict) -> str:
    rows = [f"| `{k}` | {WORDS[p.action]} | {p.reason} |" for k, p in policy.items()]
    return "\n".join(["# A page", "", "| Key | What | Why |", "|---|---|---|", *rows, ""])


def test_the_administrators_page_holds_the_same_table_as_the_policy():
    text = (REPO / "docs" / "devcontainer.md").read_text(encoding="utf-8")
    assert len(documented(text)) >= len(dc.POLICY) // 2  # non-vacuity: rows were found
    assert differences(text, dc.POLICY) == []


def test_the_guard_fires_on_a_planted_twin_with_a_row_missing():
    text = page_for(dc.POLICY)
    assert differences(text, dc.POLICY) == []  # the control: a page made from the policy is clean
    some = next(iter(dc.POLICY))
    row = next(line for line in text.splitlines() if line.startswith(f"| `{some}`"))
    missing = text.replace(row + "\n", "")
    assert missing != text and differences(missing, dc.POLICY) == [f"{some}: not in the page"]


# --- the image's own environment -------------------------------------------------------------


def test_the_images_variables_are_in_the_plans_environment_under_the_files_and_without_rcmans_own():
    image_env = {
        "JAVA_HOME": "/jdk",
        "SHARED": "image",
        "PATH": "/image/bin",
        "HOME": "/root",  # rcman's own, whatever the image says
        "TERM": "dumb",
        "CLAUDE_CONFIG_DIR": "/elsewhere",
    }
    made = planned(
        with_image({"containerEnv": {"SHARED": "file"}, "remoteEnv": {"R": "r"}}),
        facts(env=image_env),
    )
    assert made.env == {"JAVA_HOME": "/jdk", "SHARED": "file", "R": "r"}
    assert made.path == "/image/bin" and "PATH" not in made.env  # the image's PATH, as before
    assert dc.RESERVED_ENV & set(image_env) == {"HOME", "TERM", "CLAUDE_CONFIG_DIR"}  # non-vacuity
