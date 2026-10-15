"""backend/rcman/devcontainer.py: reading a repository's devcontainer.json (JSON with comments),
the policy over every key, the plan made of the file and the image's metadata, and the
image's users and groups. Pure: nothing here starts a workspace.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from rcman import devcontainer as dc
from rcman import problems
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
      "image": "ubuntu:24.04", // after a value
      "forwardPorts": [1, 2,],
      "containerEnv": {"A": "b",},
    }
    """
    assert dc.parse(text) == {
        "image": "ubuntu:24.04",
        "forwardPorts": [1, 2],
        "containerEnv": {"A": "b"},
    }


def test_what_looks_like_a_comment_or_a_comma_inside_a_string_is_kept():
    text = (
        '{"image": "x", "postCreateCommand": "curl http://example.com/a // not a comment",'
        ' "name": "/* not one */ ,]", "b": "a\\" // still inside"}'
    )
    found = dc.parse(text)
    assert found["postCreateCommand"] == "curl http://example.com/a // not a comment"
    assert found["name"] == "/* not one */ ,]"
    assert found["b"] == 'a" // still inside'


def test_a_byte_order_mark_is_ignored():
    assert dc.parse("\ufeff" + '{"image": "x"}') == {"image": "x"}
    assert dc.parse_bytes("\ufeff".encode() + b'{"image": "x"}') == {"image": "x"}


def test_a_syntax_error_says_where_and_never_what_the_file_holds():
    secret = "hunter2-a-secret-the-file-holds"
    text = f'{{\n  // a comment\n  "image": "x",\n  "name": {secret}\n}}'
    with pytest.raises(Refused) as refused:
        dc.parse(text)
    assert refused.value.kind == "invalid_file"
    assert refused.value.fields["line"] == 4  # the comment above did not move it
    assert str(refused.value.fields["column"]) in str(refused.value)
    assert secret not in str(refused.value)
    assert "name" not in str(refused.value)


def test_a_comment_does_not_move_the_column_of_an_error():
    with pytest.raises(Refused) as plain:
        dc.parse('{"a": 1, "b" x}')
    with pytest.raises(Refused) as commented:
        dc.parse('{"a": 1, /* c */ "b" x}')
    assert plain.value.fields["line"] == commented.value.fields["line"] == 1
    assert commented.value.fields["column"] == plain.value.fields["column"] + len(" /* c */")


@pytest.mark.parametrize("text", ["[1]", '"x"', "3", "null"])
def test_the_file_must_be_an_object(text):
    with pytest.raises(Refused) as refused:
        dc.parse(text)
    assert refused.value.kind == "not_an_object"
    assert text not in str(refused.value)


def test_bytes_that_are_not_text_are_refused_without_being_shown():
    with pytest.raises(Refused) as refused:
        dc.parse_bytes(b'{"image": "\xff\xfe-secret"}')
    assert refused.value.kind == "invalid_file"
    assert "secret" not in str(refused.value)


def test_an_unterminated_comment_or_string_is_a_syntax_error():
    for text in ('{"a": 1 /* never closed', '{"a": "never closed'):
        with pytest.raises(Refused) as refused:
            dc.parse(text)
        assert refused.value.kind == "invalid_file"


# --- the policy over every key --------------------------------------------------------------

# For each key of the policy, a file fragment that asks for what the key says. A key added to
# the policy without a fragment here fails the test that holds the two equal.
FRAGMENTS = {
    "image": {"image": "ubuntu:24.04"},
    "remoteUser": {"remoteUser": "root"},
    "containerUser": {"containerUser": "root"},
    "updateRemoteUserUID": {"updateRemoteUserUID": True},
    "containerEnv": {"containerEnv": {"A": "b"}},
    "remoteEnv": {"remoteEnv": {"A": "b"}},
    "init": {"init": True},
    "waitFor": {"waitFor": "postCreateCommand"},
    "overrideCommand": {"overrideCommand": True},
    "onCreateCommand": {"onCreateCommand": "true"},
    "updateContentCommand": {"updateContentCommand": "true"},
    "postCreateCommand": {"postCreateCommand": "true"},
    "postStartCommand": {"postStartCommand": "true"},
    "userEnvProbe": {"userEnvProbe": "none"},
    "forwardPorts": {"forwardPorts": [3000]},
    "portsAttributes": {"portsAttributes": {"3000": {"label": "x"}}},
    "otherPortsAttributes": {"otherPortsAttributes": {"onAutoForward": "silent"}},
    "appPort": {"appPort": 3000},
    "postAttachCommand": {"postAttachCommand": "true"},
    "customizations": {"customizations": {"vscode": {"extensions": ["a.b"]}}},
    "shutdownAction": {"shutdownAction": "none"},
    "secrets": {"secrets": {"TOKEN": {"description": "x"}}},
    "hostRequirements": {"hostRequirements": {"cpus": 4, "memory": "8gb", "storage": "32gb"}},
    "name": {"name": "A name"},
    "$schema": {"$schema": "https://example.com/schema.json"},
    "extensions": {"extensions": ["a.b"]},
    "settings": {"settings": {"editor.tabSize": 2}},
    "id": {"id": "a-feature"},
    "build": {"build": {"dockerfile": "Dockerfile", "context": ".."}},
    "build.options": {"build": {"options": ["--network=host"]}},
    "dockerFile": {"dockerFile": "Dockerfile"},
    "context": {"context": ".."},
    "features": {"features": {"ghcr.io/devcontainers/features/node:1": {}}},
    "overrideFeatureInstallOrder": {"overrideFeatureInstallOrder": ["a"]},
    "mounts": {"mounts": ["type=volume,target=/x"]},
    "workspaceMount": {"workspaceMount": "source=x,target=/y,type=bind"},
    "workspaceFolder": {"workspaceFolder": "/y"},
    "runArgs": {"runArgs": ["--privileged"]},
    "initializeCommand": {"initializeCommand": "true"},
    "dockerComposeFile": {"dockerComposeFile": "compose.yml"},
    "service": {"service": "app"},
    "runServices": {"runServices": ["app"]},
    "privileged": {"privileged": True},
    "capAdd": {"capAdd": ["SYS_PTRACE"]},
    "securityOpt": {"securityOpt": ["seccomp=unconfined"]},
    "hostRequirements.gpu": {"hostRequirements": {"gpu": True}},
    "overrideCommand=false": {"overrideCommand": False},
    "entrypoint": {"entrypoint": "/bin/x"},
}


def with_image(fragment: dict) -> dict:
    return {"image": "ubuntu:24.04", **fragment}


def test_every_key_of_the_policy_has_a_fragment_and_every_fragment_a_key():
    assert set(FRAGMENTS) == set(dc.POLICY)
    assert {p.action for p in dc.POLICY.values()} == {HONOUR, IGNORE, REFUSE}  # non-vacuity
    assert all(p.reason.strip() for p in dc.POLICY.values())


def keys(action: str) -> list[str]:
    return sorted(k for k, p in dc.POLICY.items() if p.action == action)


@pytest.mark.parametrize("key", keys(HONOUR))
def test_a_key_that_is_honoured_is_accepted_and_not_logged_as_ignored(key):
    checked = dc.check(with_image(FRAGMENTS[key]))
    assert checked.image == "ubuntu:24.04"
    assert key.split(".")[0] not in checked.ignored


@pytest.mark.parametrize("key", keys(IGNORE))
def test_a_key_that_is_ignored_is_accepted_and_logged_by_name(key):
    checked = dc.check(with_image(FRAGMENTS[key]))
    assert key in checked.ignored


@pytest.mark.parametrize("key", keys(REFUSE))
def test_a_key_that_is_refused_is_refused_with_the_key_named(key):
    with pytest.raises(Refused) as refused:
        dc.check(with_image(FRAGMENTS[key]))
    assert refused.value.kind == "refused_key"
    shown = key.split("=")[0]
    assert refused.value.fields["key"] == shown
    assert f'"{shown}"' in str(refused.value)
    assert dc.POLICY[key].reason in str(refused.value)


@pytest.mark.parametrize("key", keys(REFUSE))
def test_a_refusal_in_the_plan_is_the_same_refusal(key):
    with pytest.raises(Refused) as refused:
        planned(with_image(FRAGMENTS[key]))
    assert refused.value.kind == "refused_key"
    assert refused.value.fields["key"] == key.split("=")[0]


@pytest.mark.parametrize(
    "fragment",
    [
        {"privileged": False},
        {"capAdd": []},
        {"securityOpt": []},
        {"overrideCommand": True},
        {"mounts": []},
        {"features": {}},
        {"runArgs": []},
        {"hostRequirements": {"gpu": False}},
        {"hostRequirements": {"cpus": 2}},
    ],
)
def test_a_refused_key_that_asks_for_nothing_is_not_refused(fragment):
    assert dc.check(with_image(fragment)).image == "ubuntu:24.04"


def test_a_refused_key_that_is_planted_is_what_stops_a_file_that_would_pass():
    assert dc.check(with_image({"remoteUser": "root"}))  # the positive control
    with pytest.raises(Refused):
        dc.check(with_image({"remoteUser": "root", "capAdd": ["SYS_ADMIN"]}))


def test_build_and_features_say_that_images_cannot_be_built():
    for fragment in (FRAGMENTS["build"], FRAGMENTS["features"], FRAGMENTS["dockerFile"]):
        with pytest.raises(Refused) as refused:
            dc.check(with_image(fragment))
        assert "ready-made image" in str(refused.value)


def test_an_unknown_top_level_key_is_refused_with_the_key_named():
    with pytest.raises(Refused) as refused:
        dc.check(with_image({"aKeyOfTheFuture": 1}))
    assert refused.value.kind == "unknown_key"
    assert '"aKeyOfTheFuture"' in str(refused.value)


def test_a_key_that_is_shown_is_cut_and_has_no_control_characters():
    key = "x\u0007" + "y" * 500
    with pytest.raises(Refused) as refused:
        dc.check(with_image({key: 1}))
    assert "\u0007" not in str(refused.value) and len(str(refused.value)) < 1000


def test_a_file_that_names_no_image_is_refused_once_nothing_else_is():
    with pytest.raises(Refused) as refused:
        dc.check({"remoteUser": "root"})
    assert refused.value.kind == "no_image"
    with pytest.raises(Refused) as first:  # a refusal of a key comes first
        dc.check({"capAdd": ["X"]})
    assert first.value.kind == "refused_key"


@pytest.mark.parametrize("image", ["", "-v", "--privileged", "a b", "a\nb", "x" * 600, 5])
def test_an_image_that_could_be_taken_for_an_option_or_is_not_a_name_is_refused(image):
    with pytest.raises(Refused) as refused:
        dc.check({"image": image})
    assert refused.value.kind in {"bad_value", "no_image"}


@pytest.mark.parametrize(
    "image",
    ["ubuntu", "ubuntu:24.04", "ghcr.io/o/r:tag", "registry.example.com/a/b@sha256:" + "a" * 64],
)
def test_an_image_name_is_taken(image):
    assert dc.check({"image": image}).image == image


@pytest.mark.parametrize(
    ("fragment", "key"),
    [
        ({"remoteUser": 5}, "remoteUser"),
        ({"containerEnv": ["A=b"]}, "containerEnv"),
        ({"containerEnv": {"A": 1}}, "containerEnv"),
        ({"remoteEnv": {"A": 1}}, "remoteEnv"),
        ({"init": "yes"}, "init"),
        ({"waitFor": "never"}, "waitFor"),
        ({"postCreateCommand": 5}, "postCreateCommand"),
        ({"postCreateCommand": ["a", 5]}, "postCreateCommand"),
        ({"postStartCommand": {"a": 5}}, "postStartCommand"),
        ({"userEnvProbe": "sometimes"}, "userEnvProbe"),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_with_the_key_named(fragment, key):
    with pytest.raises(Refused) as refused:
        dc.check(with_image(fragment))
    assert refused.value.kind == "bad_value"
    assert refused.value.fields["key"] == key


def test_a_policy_reason_is_in_plain_words_without_numbers():
    for key, policy in dc.POLICY.items():
        assert not re.search(r"\d", policy.reason), key


# --- variables -----------------------------------------------------------------------------


def test_a_variable_of_the_local_environment_is_empty_and_never_rcmans_own(monkeypatch):
    monkeypatch.setenv("RCMAN_SECRET_OF_THE_SERVICE", "do-not-leak")
    monkeypatch.setenv("HOME", "/home/rcman")
    config = with_image(
        {
            "containerEnv": {
                "A": "${localEnv:RCMAN_SECRET_OF_THE_SERVICE}",
                "B": "${localEnv:RCMAN_SECRET_OF_THE_SERVICE:fallback}",
                "C": "${env:HOME}",
                "D": "x${localEnv:NOT_THERE}y",
            }
        }
    )
    env = planned(config).container_env
    assert env == {"A": "", "B": "fallback", "C": "", "D": "xy"}


def test_the_workspace_folder_variables_are_the_work_folder():
    config = with_image(
        {
            "containerEnv": {
                "A": "${containerWorkspaceFolder}/sub",
                "B": "${containerWorkspaceFolderBasename}",
            }
        }
    )
    assert planned(config).container_env == {"A": f"{WORK}/sub", "B": "work"}


@pytest.mark.parametrize("variable", ["localWorkspaceFolder", "localWorkspaceFolderBasename"])
def test_a_variable_of_the_hosts_workspace_folder_is_refused(variable):
    config = with_image({"postCreateCommand": f"cp ${{{variable}}}/x ."})
    for call in (dc.check, planned):
        with pytest.raises(Refused) as refused:
            call(config)
        assert refused.value.kind == "variable"
        assert variable in str(refused.value) and "postCreateCommand" in str(refused.value)


def test_the_devcontainer_id_is_derived_from_the_workspace_and_differs_between_them():
    config = with_image({"containerEnv": {"ID": "${devcontainerId}"}})
    one, again, other = (
        planned(config, workspace=1),
        planned(config, workspace=1),
        planned(config, workspace=2),
    )
    assert one.container_env["ID"] == again.container_env["ID"] == dc.devcontainer_id(1)
    assert one.container_env["ID"] and one.container_env["ID"] != other.container_env["ID"]
    assert re.fullmatch(r"[a-z0-9]+", one.container_env["ID"])


def test_a_variable_of_the_container_environment_is_the_images():
    config = with_image(
        {"remoteEnv": {"PATH": "${containerEnv:PATH}:/extra", "G": "${containerEnv:NO:dflt}"}}
    )
    found = planned(config, facts(env={"PATH": "/usr/bin:/bin"}))
    assert found.remote_env == {"PATH": "/usr/bin:/bin:/extra", "G": "dflt"}


def test_a_container_variable_sees_those_of_the_file_and_of_earlier_entries():
    entries = label({"id": "f", "containerEnv": {"FROM_LABEL": "l"}})
    config = with_image(
        {
            "containerEnv": {
                "A": "a",
                "B": "${containerEnv:A}-b",
                "C": "${containerEnv:FROM_LABEL}",
            },
            "remoteEnv": {"R": "${containerEnv:B}!"},
        }
    )
    found = planned(config, facts(labels=entries))
    assert found.container_env == {"FROM_LABEL": "l", "A": "a", "B": "a-b", "C": "l"}
    assert found.remote_env == {"R": "a-b!"}


def test_a_shell_variable_that_is_not_a_devcontainer_one_is_left_for_the_shell():
    config = with_image({"postCreateCommand": "echo ${HOME} ${FOO:-x} $PATH"})
    (command,) = planned(config).commands
    assert command.shell == "echo ${HOME} ${FOO:-x} $PATH"


# --- environment ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(dc.RESERVED_ENV))
@pytest.mark.parametrize("where", ["containerEnv", "remoteEnv"])
def test_an_environment_variable_rcman_sets_itself_is_refused_by_name(name, where):
    with pytest.raises(Refused) as refused:
        planned(with_image({where: {name: "x"}}))
    assert refused.value.kind == "env_reserved"
    assert refused.value.fields["name"] == name and where in str(refused.value)


# The names rcman's own programs set that a file may set too. Each is common in a devcontainer
# file, and where it matters rcman's own value is put after the plan's, so the file's cannot
# undo it: git is run with LC_ALL=C because its words are parsed, and Claude's children keep
# rcman's LANG and BROWSER (no browser opens on the machine rcman runs on).
FILE_MAY_SET = {
    "LANG": "Claude's and tmux's own value wins, so that nothing runs in a locale the image lacks",
    "LC_ALL": "git's own value wins, since its output is parsed",
    "BROWSER": "Claude's own value wins, so that no browser is opened",
}


def own_environments():
    """The environment each of rcman's programs gets, with the plan's variables ``extra`` as
    ``Layout.env`` holds them."""
    from rcman import claude_server
    from rcman.repository import Repository

    def of(extra: dict) -> dict[str, dict]:
        layout = rt.PODMAN_LAYOUT.for_plan("dev", extra, None)
        return {
            "claude": claude_server.child_env(layout),
            "tmux": claude_server.tmux_env(layout),
            "git": Repository(layout, None, None).env,
        }

    return of


def test_the_reserved_names_are_what_rcmans_own_programs_set_but_for_those_a_file_may_set():
    own = {name for env in own_environments()({}).values() for name in env}
    assert "CLAUDE_CONFIG_DIR" in own  # non-vacuity
    assert own - {"PATH"} - set(FILE_MAY_SET) == dc.RESERVED_ENV
    assert "PATH" not in dc.RESERVED_ENV
    assert set(FILE_MAY_SET) <= own  # an exemption whose name rcman no longer sets is stale
    assert not set(FILE_MAY_SET) & dc.RESERVED_ENV


@pytest.mark.parametrize("name", sorted(FILE_MAY_SET))
@pytest.mark.parametrize("where", ["containerEnv", "remoteEnv"])
def test_a_variable_a_file_may_set_is_taken(name, where):
    made = planned(with_image({where: {name: "from-the-file"}}))
    assert made.env[name] == "from-the-file"


@pytest.mark.parametrize("name", sorted(FILE_MAY_SET))
def test_rcmans_own_value_of_such_a_variable_is_not_replaced_where_it_sets_one(name):
    ordinary = own_environments()({})
    replaced = own_environments()({name: "from-the-file"})
    for program, env in replaced.items():
        if name in ordinary[program]:
            assert env[name] == ordinary[program][name], program  # rcman's own wins
        else:
            assert env[name] == "from-the-file", program  # and where it sets none, the file's


def test_path_may_be_set_and_a_name_that_is_not_one_is_refused():
    assert planned(with_image({"containerEnv": {"PATH": "/a:/b"}})).container_env == {
        "PATH": "/a:/b"
    }
    for name in ("1A", "A-B", "A B", "", "A=B"):
        with pytest.raises(Refused) as refused:
            planned(with_image({"containerEnv": {name: "x"}}))
        assert refused.value.kind == "bad_value"


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
    found = planned(config).commands
    assert [(c.event, c.label, c.shell, c.argv) for c in found] == [
        ("onCreateCommand", None, "make a", None),
        ("updateContentCommand", None, None, ("make", "b")),
        ("postCreateCommand", "first", "make c", None),
        ("postCreateCommand", "second", None, ("make", "d")),
        ("postStartCommand", None, "make e", None),
    ]


def test_commands_are_in_the_specs_order_whatever_the_files():
    config = with_image(
        {"postStartCommand": "e", "postCreateCommand": "c", "onCreateCommand": "a",
         "updateContentCommand": "b"}
    )  # fmt: skip
    plan = planned(config)
    assert [c.shell for c in plan.create_commands()] == ["a", "b", "c", "e"]
    assert [c.shell for c in plan.start_commands()] == ["e"]


def test_an_empty_command_is_nothing():
    config = with_image({"onCreateCommand": "", "postCreateCommand": [], "postStartCommand": {}})
    assert planned(config).commands == ()


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


def test_a_user_given_with_its_group_is_run_by_number_with_its_groups_resolved_here():
    found = planned(with_image({"remoteUser": "dev:dev"}))
    assert (found.uid, found.gid, found.run_as, found.groups) == (1234, 1235, "1234:1235", (5000,))
    numeric = planned(with_image({"remoteUser": "1234:1235"}))
    assert (numeric.run_as, numeric.groups) == ("1234:1235", (5000,))
    other = planned(with_image({"remoteUser": "dev:other"}))
    assert (other.gid, other.run_as) == (5001, "1234:5001")


def test_a_number_that_the_image_knows_is_its_user_and_one_it_does_not_is_group_zero():
    known = planned(with_image({"remoteUser": "1234"}))
    assert (known.uid, known.gid, known.run_as, known.user_name) == (1234, 1235, "1234", "dev")
    unknown = planned(with_image({"remoteUser": "4321"}))
    assert (unknown.uid, unknown.gid, unknown.run_as, unknown.user_name) == (
        4321,
        0,
        "4321",
        "4321",
    )


def test_the_user_is_the_remote_user_else_the_container_user_else_the_images():
    assert planned(with_image({"containerUser": "dev"})).uid == 1234
    both = planned(with_image({"containerUser": "root", "remoteUser": "dev"}))
    assert both.uid == 1234
    assert planned(with_image({}), facts(user="dev")).uid == 1234
    root = planned(with_image({}), facts(user=""))
    assert (root.uid, root.gid, root.run_as, root.user_name) == (0, 0, "root", "root")


def test_a_user_the_image_does_not_have_is_refused_by_name():
    with pytest.raises(Refused) as refused:
        planned(with_image({"remoteUser": "nobody-here"}))
    assert refused.value.kind == "user_unknown"
    assert "nobody-here" in str(refused.value)


@pytest.mark.parametrize("user", [f"{rt.RANGE}", f"{rt.RANGE + 5}:0", f"0:{rt.RANGE}"])
def test_an_id_outside_the_persons_range_is_refused(user):
    with pytest.raises(Refused) as refused:
        planned(with_image({"remoteUser": user}))
    assert refused.value.kind == "id_out_of_range"


def test_the_last_id_of_the_range_is_taken():
    found = planned(with_image({"remoteUser": f"{rt.RANGE - 1}"}))
    assert found.uid == rt.RANGE - 1


def test_passwd_and_group_are_parsed_leniently():
    users = dc.parse_passwd(PASSWD)
    assert [u.name for u in users] == ["root", "dev", "nogroup-user"]
    assert (users[1].uid, users[1].gid, users[1].home) == (1234, 1235, "/home/dev")
    groups = dc.parse_group(GROUP)
    assert [(g.name, g.gid, g.members) for g in groups][:3] == [
        ("root", 0, ()),
        ("dev", 1235, ()),
        ("extra", 5000, ("dev", "someone")),
    ]
    assert dc.parse_passwd("") == [] and dc.parse_group("\n\n") == []


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


def test_the_files_own_user_wins_over_the_images():
    entries = label({"remoteUser": "dev"})
    assert planned(with_image({"remoteUser": "root"}), facts(labels=entries)).uid == 0


def test_the_files_own_entry_is_applied_once_and_after_the_images():
    entries = label({"containerEnv": {"A": "label"}})
    found = planned(with_image({"containerEnv": {"A": "file"}, "onCreateCommand": "once"}),
                    facts(labels=entries))  # fmt: skip
    assert [c.shell for c in found.commands].count("once") == 1
    assert found.container_env["A"] == "file"


def test_the_origin_of_each_command_is_named():
    entries = label({"id": "feature-a", "onCreateCommand": "a"})
    found = planned(with_image({"onCreateCommand": "b"}), facts(labels=entries))
    assert [c.origin for c in found.commands] == ["feature-a", dc.CONFIG_ORIGIN]


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        ({"privileged": True}, "privileged"),
        ({"capAdd": ["SYS_ADMIN"]}, "capAdd"),
        ({"securityOpt": ["seccomp=unconfined"]}, "securityOpt"),
        ({"mounts": [{"type": "bind", "source": "/", "target": "/host"}]}, "mounts"),
        ({"entrypoint": "/usr/local/share/init.sh"}, "entrypoint"),
        ({"overrideCommand": False}, "overrideCommand"),
        ({"workspaceMount": "x"}, "workspaceMount"),
    ],
)
def test_an_entry_of_the_images_metadata_that_asks_for_what_the_file_may_not_is_refused_by_id(
    entry, key
):
    entries = label({"id": "ghcr.io/o/features/greedy:1", **entry})
    with pytest.raises(Refused) as refused:
        planned(with_image({}), facts(labels=entries))
    assert refused.value.kind == "refused_by_image"
    assert refused.value.fields["key"] == key
    assert refused.value.fields["entry"] == "ghcr.io/o/features/greedy:1"
    assert "ghcr.io/o/features/greedy:1" in str(refused.value) and f'"{key}"' in str(refused.value)


def test_an_entry_without_an_id_is_named_by_its_place():
    entries = label({"remoteUser": "dev"}, {"privileged": True})
    with pytest.raises(Refused) as refused:
        planned(with_image({}), facts(labels=entries))
    assert refused.value.kind == "refused_by_image"
    assert refused.value.fields["entry"]  # something to point at
    assert refused.value.fields["entry"] != dc.CONFIG_ORIGIN


def test_an_unknown_key_in_an_entry_is_refused_by_name_and_entry():
    with pytest.raises(Refused) as refused:
        planned(with_image({}), facts(labels=label({"id": "f", "aNewKey": 1})))
    assert refused.value.kind == "unknown_key_in_image"
    assert "aNewKey" in str(refused.value) and "f" in str(refused.value)


@pytest.mark.parametrize("text", ["not json", '"a string"', "[1, 2]", '[{"id": "a"}, 3]', ""])
def test_a_metadata_label_that_is_not_a_list_of_objects_is_refused_without_being_shown(text):
    with pytest.raises(Refused) as refused:
        planned(with_image({}), facts(labels={"devcontainer.metadata": text}))
    assert refused.value.kind == "metadata_invalid"
    assert text.strip() == "" or text not in str(refused.value)


def test_a_single_object_label_is_one_entry_and_no_label_is_none():
    one = {"devcontainer.metadata": json.dumps({"containerEnv": {"A": "1"}})}
    assert planned(with_image({}), facts(labels=one)).container_env == {"A": "1"}
    assert planned(with_image({}), facts(labels={})).container_env == {}


def test_what_an_entry_ignores_is_logged_by_name_once():
    entries = label({"id": "f", "customizations": {}}, {"forwardPorts": [1]})
    found = planned(with_image({"forwardPorts": [2], "name": "n"}), facts(labels=entries))
    assert sorted(found.ignored) == ["customizations", "forwardPorts", "id", "name"]


# --- the plan, stored ----------------------------------------------------------------------


def test_a_plan_survives_being_stored_and_read_back():
    config = with_image(
        {
            "remoteUser": "dev:dev",
            "containerEnv": {"A": "1"},
            "remoteEnv": {"B": "2"},
            "postCreateCommand": {"x": "make", "y": ["make", "y"]},
            "postStartCommand": "serve",
            "forwardPorts": [1],
        }
    )
    found = planned(config)
    again = dc.Plan.from_json(found.to_json())
    assert again == found
    assert json.loads(found.to_json())["image"] == "ubuntu:24.04"  # non-vacuity: it is JSON


def test_a_stored_plan_that_is_damaged_is_not_taken():
    for text in ("", "not json", "[]", '{"image": 5}'):
        with pytest.raises(ValueError):
            dc.Plan.from_json(text)


# --- reading the file ------------------------------------------------------------------------


class Workspace:
    """A work folder on the host that answers ``read`` as a container's exec would."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.asked: list[list[str]] = []
        host = rt.HostRuntime(folder, folder, folder, folder)
        self.layout = dataclasses.replace(host.layout("x"), work=str(folder))

    def run(self, argv: list[str], timeout: float, cwd: str | None = None) -> rt.Result:
        self.asked.append(argv)
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


def read(work: Workspace) -> str | None:
    return dc.read(work.run, work.layout)


def read_by(work: Workspace, tool: str) -> list[list[str]]:
    """The commands the reader ran that were the tool ``tool``."""
    return [a for a in work.asked if Path(a[0]).name == tool]


def test_nothing_is_read_where_there_is_no_file(work):
    assert read(work) is None
    (work.folder / "devcontainer.json").write_text("{}")  # not a place that is looked in
    (work.folder / ".devcontainer").mkdir()
    (work.folder / ".devcontainer" / "other.json").write_text("{}")
    (work.folder / ".devcontainer" / "sub").mkdir()
    (work.folder / ".devcontainer" / "sub" / "devcontainer.json").write_text("{}")
    assert read(work) is None


def test_the_folder_file_comes_first_and_then_the_dotfile(work):
    (work.folder / ".devcontainer.json").write_text('{"image": "dotfile"}')
    assert read(work) == '{"image": "dotfile"}'
    (work.folder / ".devcontainer").mkdir()
    (work.folder / ".devcontainer" / "devcontainer.json").write_text('{"image": "folder"}')
    assert read(work) == '{"image": "folder"}'


def test_a_file_that_is_too_big_is_refused_unread(work):
    (work.folder / ".devcontainer.json").write_text("{" + " " * dc.MAX_BYTES + "}")
    with pytest.raises(Refused) as refused:
        read(work)
    assert refused.value.kind == "too_big"
    assert not read_by(work, "cat")  # positive control: wc was asked, cat was not
    assert read_by(work, "wc")


def test_a_file_at_the_limit_is_read(work):
    text = "{" + " " * (dc.MAX_BYTES - 2) + "}"
    (work.folder / ".devcontainer.json").write_text(text)
    assert read(work) == text


def test_a_symbolic_link_is_never_followed(work, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-the-token"}}')
    (work.folder / ".devcontainer.json").symlink_to(secret)
    assert secret.read_text()  # the plant took effect
    with pytest.raises(Refused) as refused:
        read(work)
    assert refused.value.kind == "symlink"
    assert "sk-ant" not in str(refused.value) and "claudeAiOauth" not in str(refused.value)
    assert not read_by(work, "cat") and not read_by(work, "wc")


def test_a_folder_that_is_a_symbolic_link_is_never_followed(work, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "devcontainer.json").write_text('{"image": "x"}')
    (work.folder / ".devcontainer").symlink_to(elsewhere)
    with pytest.raises(Refused) as refused:
        read(work)
    assert refused.value.kind == "symlink"


def test_a_directory_in_the_place_of_the_file_is_no_file(work):
    (work.folder / ".devcontainer.json").mkdir()
    assert read(work) is None


def test_a_file_that_cannot_be_read_says_so_without_its_content(work):
    file = work.folder / ".devcontainer.json"
    file.write_text('{"image": "the-content"}')
    file.chmod(0)
    try:
        assert os.geteuid() != 0, "this case needs a user that permissions apply to"
        with pytest.raises(Refused) as refused:
            read(work)
        assert refused.value.kind == "unreadable"
        assert "the-content" not in str(refused.value)
    finally:
        file.chmod(0o600)


def test_the_places_looked_in_are_what_the_log_says():
    assert dc.FILES == (".devcontainer/devcontainer.json", ".devcontainer.json")
    for place in dc.FILES:
        assert place in dc.LOOKED_IN


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


def test_the_guard_fires_on_a_planted_twin_of_each_kind_of_drift():
    text = page_for(dc.POLICY)
    assert differences(text, dc.POLICY) == []  # the control: a page made from the policy is clean
    some = next(iter(dc.POLICY))
    row = next(line for line in text.splitlines() if line.startswith(f"| `{some}`"))
    missing = text.replace(row + "\n", "")
    assert missing != text and differences(missing, dc.POLICY) == [f"{some}: not in the page"]
    flipped = text.replace(row, row.replace("| honoured |", "| refused |", 1).replace(
        "| ignored |", "| refused |", 1).replace("| refused |", "| ignored |", 1)
    )  # fmt: skip
    assert flipped != text and differences(flipped, dc.POLICY)
    reworded = text.replace(row, row[: -len("|")].rstrip() + " And more words. |")
    assert reworded != text and differences(reworded, dc.POLICY)
    extra = text + "| `aKeyThePolicyLacks` | refused | A reason. |\n"
    assert differences(extra, dc.POLICY) == ["aKeyThePolicyLacks: not in the policy"]
    empty = "# A page\n\nNo table.\n"
    assert len(differences(empty, dc.POLICY)) == len(dc.POLICY)  # empty is not clean


def test_every_message_of_the_catalogue_that_names_a_key_is_filled_by_what_raises_it():
    assert set(problems.DEVCONTAINER_MESSAGES) >= {
        "refused_key",
        "refused_by_image",
        "unknown_key",
        "unknown_key_in_image",
        "invalid_file",
        "not_an_object",
        "too_big",
        "symlink",
        "unreadable",
        "no_image",
        "bad_value",
        "variable",
        "env_reserved",
        "user_unknown",
        "id_out_of_range",
        "metadata_invalid",
        "pull_failed",
        "musl",
        "hook_failed",
        "hook_timeout",
        "image_unreadable",
        "image_gone",
        "image_unrunnable",
        "invalid_value",
        "too_deep",
    }
    for message in problems.DEVCONTAINER_MESSAGES.values():
        assert not re.search(r"\d", message), message


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


def test_a_null_remote_variable_removes_one_of_the_images():
    made = planned(with_image({"remoteEnv": {"JAVA_HOME": None}}), facts(env={"JAVA_HOME": "/j"}))
    assert "JAVA_HOME" not in made.env


# --- review: image names, variables, odd numbers, depth, messages ------------------------------

TRANSPORTS = [
    "docker-archive:/tmp/image.tar",
    "docker-archive:image.tar",
    "oci:/path/to/layout",
    "oci:layout",
    "oci-archive:image.tar",
    "dir:/some/dir",
    "dir:folder",
    "containers-storage:localhost/image",
    "containers-storage:[overlay@/x+/y]image",
    "docker://ubuntu",
    "docker://docker.io/library/ubuntu:24.04",
    "docker-daemon:ubuntu:latest",
    "ostree:image@/repo",
    "/an/absolute/path",
    "./relative/path",
    "../up",
    "a//b",
    "Ubuntu:24.04",
    "ubuntu:",
    "ubuntu@",
    "ubuntu@sha256:abc",
    "ubuntu:tag with space",
    "ubuntu\n",
    ":24.04",
    "@sha256:" + "a" * 64,
]


@pytest.mark.parametrize("image", TRANSPORTS)
def test_an_image_that_names_a_transport_or_a_path_is_refused(image):
    with pytest.raises(Refused) as refused:
        dc.check({"image": image})
    assert refused.value.kind == "bad_value" and refused.value.fields["key"] == "image"


@pytest.mark.parametrize(
    "image",
    [
        "ubuntu",
        "ubuntu:24.04",
        "library/ubuntu",
        "ghcr.io/o/r:tag",
        "ghcr.io/o/r/deeper/path:v1.2-rc_3",
        "registry.example.com/a/b@sha256:" + "a" * 64,
        "registry.example.com/a/b:1@sha256:" + "b" * 64,
        "localhost/image:latest",
        "my-registry.example.com/a_b/c.d--e",
    ],
)
def test_a_plain_registry_reference_is_taken(image):
    assert dc.check({"image": image}).image == image


@pytest.mark.parametrize(
    "image", ["ubuntu:${containerEnv:TAG:latest}", "${devcontainerId}", "x/${localEnv:Y}"]
)
def test_an_image_with_a_variable_in_it_is_refused_with_the_key_named_by_both_check_and_plan(image):
    for call in (dc.check, planned):
        with pytest.raises(Refused) as refused:
            call({"image": image})
        assert refused.value.kind == "bad_value" and refused.value.fields["key"] == "image"


def test_the_image_of_the_plan_is_the_one_that_was_checked():
    checked = dc.check(with_image({}))
    assert planned(with_image({})).image == checked.image


def test_a_digit_that_is_not_an_ascii_one_is_no_number():
    for who in ("\u00b2", "\u0663", "dev:\u00b2", "1234:\u0663"):
        with pytest.raises(Refused) as refused:
            dc.resolve_user(who, PASSWD, GROUP)
        assert refused.value.kind == "user_unknown", who
    assert dc.parse_passwd("x:x:\u00b2:0::/h:/s\n") == []
    assert dc.parse_group("x:x:\u0663:\n") == []


def test_an_id_with_more_digits_than_any_id_has_is_out_of_range_not_a_crash():
    with pytest.raises(Refused) as refused:
        dc.resolve_user("9" * 6000, PASSWD, GROUP)
    assert refused.value.kind == "id_out_of_range"


def test_a_supplementary_group_beyond_the_range_is_refused():
    group = GROUP + f"big:x:{rt.RANGE + 7}:dev\n"
    with pytest.raises(Refused) as refused:
        dc.resolve_user("dev:dev", PASSWD, group)
    assert refused.value.kind == "id_out_of_range"
    last = GROUP + f"last:x:{rt.RANGE - 1}:dev\n"
    assert (rt.RANGE - 1) in dc.resolve_user("dev:dev", PASSWD, last).groups


def test_a_number_too_big_to_read_is_refused_as_a_value():
    with pytest.raises(Refused) as refused:
        dc.parse('{"image": "x", "appPort": ' + "9" * 6000 + "}")
    assert refused.value.kind == "invalid_value"
    assert "9999" not in str(refused.value)


def nested(depth: int):
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("depth", [dc.MAX_DEPTH + 1, 800, 5000])
def test_a_value_nested_too_deeply_is_refused_wherever_it_is_met(depth):
    deep = with_image({"forwardPorts": nested(depth)})
    for call in (dc.check, planned):
        with pytest.raises(Refused) as refused:
            call(deep)
        assert refused.value.kind == "too_deep"
    with pytest.raises(Refused) as refused:
        dc.parse(
            json.dumps({"image": "x", "forwardPorts": nested(depth)})
            if depth < 900
            else "[" * depth
        )
    assert refused.value.kind in {"too_deep", "not_an_object", "invalid_file"}
    label = {dc.METADATA_LABEL: json.dumps([{"forwardPorts": nested(min(depth, 900))}])}
    if depth <= 900:
        with pytest.raises(Refused) as refused:
            planned(with_image({}), facts(labels=label))
        assert refused.value.kind == "too_deep"


def test_a_value_at_the_depth_limit_is_taken():
    assert dc.check(with_image({"forwardPorts": nested(dc.MAX_DEPTH - 2)}))


def test_a_variable_in_a_key_that_is_ignored_is_not_looked_at():
    config = with_image(
        {
            "customizations": {"x": "${localWorkspaceFolder}/a"},
            "name": "${localWorkspaceFolderBasename}",
        }
    )
    assert dc.check(config).ignored == ("customizations", "name")
    assert planned(config).ignored == ("customizations", "name")
    with pytest.raises(Refused):  # the control: in a key that is honoured it is refused
        dc.check(with_image({"containerEnv": {"A": "${localWorkspaceFolder}"}}))


def test_a_label_of_an_object_command_is_cut_short_and_cleaned():
    label = "x\u0007" + "y" * 500
    found = planned(with_image({"postCreateCommand": {label: "true"}}))
    (command,) = found.commands
    assert "\u0007" not in command.name and len(command.name) < 200
    assert command.event in command.name


def test_the_origin_of_a_reserved_name_in_an_image_entry_is_cleaned_and_cut():
    entries = label({"id": "x\u0007" + "y" * 500, "containerEnv": {"HOME": "/x"}})
    with pytest.raises(Refused) as refused:
        planned(with_image({}), facts(labels=entries))
    assert refused.value.kind == "env_reserved"
    assert "\u0007" not in str(refused.value) and len(str(refused.value)) < 600


def test_where_rcmans_own_value_wins_over_the_files_is_where_the_text_says():
    from rcman import claude_server
    from rcman.repository import Repository

    layout = dataclasses.replace(
        rt.PODMAN_LAYOUT, env={"LANG": "file", "LC_ALL": "file", "BROWSER": "file"}
    )
    claude, tmux = claude_server.child_env(layout), claude_server.tmux_env(layout)
    git = Repository(layout, None, None).env
    assert (claude["LANG"], claude["BROWSER"]) != ("file", "file")  # rcman's, for Claude
    assert tmux["LANG"] != "file" and tmux["BROWSER"] == "file"  # LANG rcman's, BROWSER the file's
    assert git["LC_ALL"] != "file" and git["LANG"] == "file" and git["BROWSER"] == "file"
    assert "LC_ALL" in claude and claude["LC_ALL"] == "file"  # the file's, for Claude
