"""until and until_async (tests/until.py), the tier's guard (tests.hang_guard.tier_guard), and the
helpers that shorten a product's deadline or pace (tests.hang_guard.runs_out and pace).
"""

import asyncio
import inspect
import subprocess
import sys
import types

import pytest

from tests import hang_guard
from tests.collected import child_env
from tests.hang_guard import CONTAINER_HANG_GUARD, HANG_GUARD, pace, runs_out, tier_guard
from tests.until import PACE, SLOW, until, until_async

REPO = hang_guard.PYPROJECT.parent


def counting(*values):
    """A condition giving ``values`` in turn, and how often it was asked."""
    asked = []

    def condition():
        asked.append(None)
        return values[len(asked) - 1]

    return condition, asked


# --- until --------------------------------------------------------------------------------------


def test_until_gives_the_first_truthy_value_and_looks_again_until_then():
    condition, asked = counting(0, None, "", "found", "later")
    assert until(condition) == "found"
    assert len(asked) == 4


def test_until_async_gives_the_first_truthy_value_and_looks_again_until_then():
    condition, asked = counting(0, None, [], {"a": 1}, "later")
    assert asyncio.run(until_async(condition)) == {"a": 1}
    assert len(asked) == 4


@pytest.mark.parametrize("helper", [until, until_async])
def test_until_takes_no_limit_but_a_pace(helper):
    """There is no per-call limit: the bound is the tier's guard."""
    assert list(inspect.signature(helper).parameters) == ["condition", "pace"]
    assert inspect.signature(helper).parameters["pace"].default == PACE
    assert 0 < PACE < SLOW


def test_a_condition_that_raises_ends_until_with_its_error():
    def raising():
        raise ValueError("planted")

    with pytest.raises(ValueError, match="planted"):
        until(raising)
    with pytest.raises(ValueError, match="planted"):
        asyncio.run(until_async(raising))


def never():
    return "nothing yet" and None


@pytest.mark.parametrize("run", [until, lambda c: asyncio.run(until_async(c))])
def test_a_condition_that_never_holds_fails_at_the_tiers_guard_with_what_it_last_gave(
    monkeypatch, run
):
    """At a tier's guard of 0 the condition has not come about at any speed. The failure is an
    AssertionError, an Exception like any failed assertion, so the test's teardown runs."""
    monkeypatch.setattr(hang_guard, "HANG_GUARD", 0)
    assert tier_guard() == 0  # the plant took effect
    with pytest.raises(AssertionError) as failed:
        run(never)
    said = str(failed.value)
    assert "did not come about within the hang bound" in said
    assert "never" in said and "None" in said


# --- the tier's guard ---------------------------------------------------------------------------


def test_a_host_tests_tier_guard_is_the_host_hang_guard():
    assert tier_guard() == HANG_GUARD


def test_a_container_tests_tier_guard_is_the_container_hang_guard(time_doubles, monkeypatch):
    """The tier is the one the registry of time_doubles took from the container marker."""
    monkeypatch.setattr(time_doubles, "tier", "CONTAINER_HANG_GUARD")
    assert tier_guard() == CONTAINER_HANG_GUARD


def in_a_child(env: dict[str, str]) -> subprocess.CompletedProcess:
    """tier_guard() in a program the tests start, with no test's registry."""
    return subprocess.run(
        [sys.executable, "-c", "from tests.hang_guard import tier_guard; print(tier_guard())"],
        cwd=REPO,
        env=child_env({"PYTHONPATH": str(REPO), **env}),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )


def test_a_program_the_tests_start_takes_the_guard_it_is_handed():
    done = in_a_child({"RCMAN_HANG_GUARD": str(CONTAINER_HANG_GUARD)})
    assert done.returncode == 0, done.stderr
    assert float(done.stdout) == CONTAINER_HANG_GUARD


def test_a_program_handed_no_guard_has_no_tier_and_says_so():
    done = in_a_child({})
    assert done.returncode != 0
    assert "RCMAN_HANG_GUARD" in done.stderr


HANDED_IN_A_FIXTURE = """
from tests.hang_guard import handed, tier_guard
with handed(7.0):
    with handed(5.0):
        print(tier_guard())
    print(tier_guard())
try:
    tier_guard()
except RuntimeError:
    print("none")
"""


def test_a_fixture_of_wider_scope_hands_its_tiers_guard_while_it_needs_one():
    """In a session or module fixture, which no test's registry covers, as in a program with
    nothing handed in its environment: the innermost guard handed, and none once it is over."""
    done = subprocess.run(
        [sys.executable, "-c", HANDED_IN_A_FIXTURE],
        cwd=REPO,
        env=child_env({"PYTHONPATH": str(REPO)}),
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["5.0", "7.0", "none"]


def test_a_tests_own_tier_comes_before_a_guard_handed_by_a_fixture():
    with hang_guard.handed(CONTAINER_HANG_GUARD):
        assert tier_guard() == HANG_GUARD


# --- runs_out and pace --------------------------------------------------------------------------


def a_module():
    return types.SimpleNamespace(DEADLINE_SECONDS=30.0, POLL_SECONDS=1.0)


def test_runs_out_puts_a_deadline_at_zero_and_undoes_it(monkeypatch):
    module = a_module()
    with monkeypatch.context() as patched:
        runs_out(patched, module, "DEADLINE_SECONDS")
        assert module.DEADLINE_SECONDS == 0
    assert module.DEADLINE_SECONDS == 30.0


def test_runs_out_puts_a_deadline_at_the_floor_the_product_takes(monkeypatch):
    module = a_module()
    runs_out(monkeypatch, module, "DEADLINE_SECONDS", at=1)
    assert module.DEADLINE_SECONDS == 1


def test_runs_out_refuses_a_name_the_module_does_not_have(monkeypatch):
    with pytest.raises(AttributeError):
        runs_out(monkeypatch, a_module(), "NO_SUCH_SECONDS")


def test_runs_out_refuses_a_deadline_that_is_not_shortened(monkeypatch):
    with pytest.raises(AssertionError, match="not shorter"):
        runs_out(monkeypatch, a_module(), "DEADLINE_SECONDS", at=30)


def test_pace_shortens_a_pace(monkeypatch):
    module = a_module()
    pace(monkeypatch, module, "POLL_SECONDS", 0.02)
    assert module.POLL_SECONDS == 0.02


@pytest.mark.parametrize("seconds", [1.0, 2.0, 0, -1])
def test_pace_refuses_a_pace_that_is_not_a_shorter_one(monkeypatch, seconds):
    with pytest.raises(AssertionError):
        pace(monkeypatch, a_module(), "POLL_SECONDS", seconds)


def test_pace_refuses_a_name_the_module_does_not_have(monkeypatch):
    with pytest.raises(AttributeError):
        pace(monkeypatch, a_module(), "NO_SUCH_SECONDS", 0.02)
