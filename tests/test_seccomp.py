"""helpers/seccomp.py: the workspaces' seccomp profile, Podman's default with the two UTS calls
allowed, and the check that the tools' profile is still the one derived from Podman's current
default, read from tests/fakes/podman-seccomp.json, a fixed recording of Podman's default that is
recorded again when the product starts failing against real Podman."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_the_workspaces_profile_is_podmans_with_the_two_uts_calls_allowed_and_not_stale(tmp_path):
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
    # and the tools' profile, derived from the current default, is not stale
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / seccomp.NAME).write_text(seccomp.workspace_profile(RECORDED.read_text()))
    assert seccomp.stale(tools, RECORDED) is None


def test_the_seccomp_guard_fires_on_a_profile_that_allows_more():
    default = json.loads(RECORDED.read_text())
    derived = json.loads(seccomp.workspace_profile(RECORDED.read_text()))
    assert seccomp_problems(default, derived) == []  # before the plant
    rule = next(r for r in derived["syscalls"] if UTS_CALLS & set(r["names"]))
    rule["names"].append("mount")  # a third call
    assert seccomp_problems(default, derived)
