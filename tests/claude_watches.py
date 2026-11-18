"""Waiting on the fake `claude` (devtools/fake_claude.py) as the tests drive it through tmux.

Not a test module. A test never waits for a screen to have stayed still: it waits, with
tests.until, on what the fake says it has done, through its trace, and lets it leave its brief
connected stage through its gate.
"""

from __future__ import annotations

from pathlib import Path

from devtools import fake_claude as fake
from devtools import tmuxctl
from tests.until import until


def drawn(trace: Path) -> list[str] | None:
    """The lines of the last screen the fake traced drawn, or None before any."""
    screens = [e["lines"] for e in fake.trace_events(trace) if e["event"] == "drawn"]
    return screens[-1] if screens else None


def shown(pane: tmuxctl.Pane, trace: Path) -> list[str]:
    """The pane's screen, once it is exactly the last screen the fake traced drawn: the fake
    traces a screen once its write has returned, and tmux shows it once it has read it."""

    def now() -> list[str] | None:
        last = drawn(trace)
        screen = pane.screen()
        return screen if last is not None and screen == last else None

    return until(now)


def release(gate: Path, stage: str) -> None:
    """Lets the fake leave ``stage``: the file it waits for in its gate."""
    (gate / stage).touch()


def traced_env(folder: Path) -> dict[str, str]:
    """The fake's variables for a trace and a gate in ``folder``, and none of its trust answers
    too quick."""
    gate = folder / "gate"
    gate.mkdir(parents=True, exist_ok=True)
    return {
        fake.TRACE: str(folder / "trace"),
        fake.GATE: str(gate),
        fake.MIN_ANSWER: "0",
    }
