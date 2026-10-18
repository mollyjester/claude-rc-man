"""helpers/seccomp.py: the workspaces' seccomp profile, Podman's default with the two UTS calls
allowed; the check that the tools' profile is still the one derived from Podman's current
default; and the recorded copy of Podman's default the host tests use, held to the real one."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers import seccomp

REPO = Path(__file__).resolve().parent.parent
RECORDED = REPO / "tests" / "fakes" / "podman-seccomp.json"
UTS_CALLS = {"sethostname", "setdomainname"}


def seccomp_problems(default: dict, derived: dict) -> list[str]:
    """How ``derived`` is not ``default`` with UTS_CALLS allowed whatever the capabilities:
    every rule as it was but for those two names, and one rule that allows exactly them, with
    no condition."""
    problems = []

    def without(rules):
        kept = []
        for rule in rules:
            names = [n for n in rule.get("names", []) if n not in UTS_CALLS]
            if names:
                kept.append({**rule, "names": names})
        return kept

    if {k: v for k, v in default.items() if k != "syscalls"} != {
        k: v for k, v in derived.items() if k != "syscalls"
    }:
        problems.append("something besides the rules changed")
    if without(default["syscalls"]) != without(derived["syscalls"]):
        problems.append("a rule besides those of the two calls changed")
    naming = [r for r in derived["syscalls"] if UTS_CALLS & set(r.get("names", []))]
    if len(naming) != 1:
        problems.append(f"{len(naming)} rules name the two calls")
    for rule in naming:
        if set(rule["names"]) != UTS_CALLS:
            problems.append(f"the rule allows {sorted(rule['names'])}")
        if rule.get("action") != "SCMP_ACT_ALLOW" or rule.get("args"):
            problems.append("the rule is not an allow without arguments")
        if any(rule.get(k) for k in ("includes", "excludes")):
            problems.append("the rule holds a condition")
    return problems


def test_the_workspaces_profile_is_podmans_with_the_two_uts_calls_allowed():
    default = json.loads(RECORDED.read_text())
    # positive control: Podman's own refuses them without CAP_SYS_ADMIN
    assert any(
        UTS_CALLS <= set(r["names"]) and r["action"] == "SCMP_ACT_ERRNO"
        for r in default["syscalls"]
    )
    assert set(seccomp.UTS_CALLS) == UTS_CALLS
    derived = json.loads(seccomp.workspace_profile(RECORDED.read_text()))
    assert seccomp_problems(default, derived) == []
    assert seccomp_problems(default, default)  # the default itself is not it


@pytest.mark.parametrize(
    "plant",
    ["a third call", "a condition", "another rule changed", "the default action", "two rules"],
)
def test_the_seccomp_guard_fires_on_a_profile_that_allows_more_or_less(plant):
    default = json.loads(RECORDED.read_text())
    derived = json.loads(seccomp.workspace_profile(RECORDED.read_text()))
    assert seccomp_problems(default, derived) == []  # before the plant
    rule = next(r for r in derived["syscalls"] if UTS_CALLS & set(r["names"]))
    if plant == "a third call":
        rule["names"].append("mount")
    elif plant == "a condition":
        rule["includes"] = {"caps": ["CAP_SYS_ADMIN"]}
    elif plant == "another rule changed":
        derived["syscalls"].pop(0)
    elif plant == "the default action":
        derived["defaultAction"] = "SCMP_ACT_ALLOW"
    else:
        derived["syscalls"].append(dict(rule))
    assert seccomp_problems(default, derived), plant


def test_the_recorded_copy_is_podmans_own_on_this_machine(podman_service):
    """The contract of the recording: the real profile, where the tests' engine runs, is the one
    the host tests read. A new containers-common is recorded again."""
    assert seccomp.PODMAN_DEFAULT.is_file(), seccomp.PODMAN_DEFAULT
    assert json.loads(seccomp.PODMAN_DEFAULT.read_text()) == json.loads(RECORDED.read_text())


def tools_with(tmp_path: Path, text: str | None) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    if text is not None:
        (tools / seccomp.NAME).write_text(text)
    return tools


def test_a_profile_derived_from_the_current_default_is_not_stale(tmp_path):
    tools = tools_with(tmp_path, seccomp.workspace_profile(RECORDED.read_text()))
    assert seccomp.stale(tools, RECORDED) is None


def test_a_missing_profile_and_one_of_an_older_default_are_stale(tmp_path):
    assert "is not there" in seccomp.stale(tools_with(tmp_path / "a", None), RECORDED)
    older = json.loads(RECORDED.read_text())
    older["syscalls"].pop()  # a default as an older containers-common had it
    stale = tools_with(tmp_path / "b", seccomp.workspace_profile(json.dumps(older)))
    assert "not the one derived" in seccomp.stale(stale, RECORDED)
    assert "cannot be read" in seccomp.stale(stale, tmp_path / "no-default.json")
    for folder in ("a", "b"):
        assert os.path.isdir(tmp_path / folder / "tools")  # what was looked at is there
