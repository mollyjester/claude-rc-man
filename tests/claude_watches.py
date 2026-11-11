"""The recorder's watches for tests (devtools/record_claude.py, Watch), and what they rest on.

Not a test module. A test never waits for a screen to have stayed still, as the recorder's
command line does against the real `claude` (RealWatch): it waits, with tests.until, on what
the fake says it has done, or on the screen it expects.

- ``TraceWatch`` drives the fake (devtools/fake_claude.py) through its trace and its gate: a
  screen is settled once the fake has traced it drawn and the pane shows exactly that; the
  brief connected stage is left only once released, after its capture; and the trust answer of
  a sequence that is to be too quick is so at any speed, the fake's threshold put beyond any
  test.
- ``ExpectWatch`` drives the real `claude`, which says nothing of what it has done: a screen
  is settled once it is, sanitised, the fixture of the capture it is for, and differs at once
  when the program has exited showing something else. It has no gate and no person to wait
  on.

Each gives the pane's waits the tier's guard (tier_guard()), so that a hang ends there with
tmuxctl's TimeoutError and the screen it last showed, as until's own bound ends the other waits.
"""

from __future__ import annotations

from pathlib import Path

from devtools import fake_claude as fake
from devtools import record_claude as rec
from devtools import tmuxctl
from tests.hang_guard import tier_guard
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


def traced_env(folder: Path, *, too_fast: bool = False) -> dict[str, str]:
    """The fake's variables for a trace and a gate in ``folder``; with ``too_fast`` its every
    trust answer is too quick, at any speed, and otherwise none is."""
    gate = folder / "gate"
    gate.mkdir(parents=True, exist_ok=True)
    return {
        fake.TRACE: str(folder / "trace"),
        fake.GATE: str(gate),
        fake.MIN_ANSWER: str(3 * tier_guard() if too_fast else 0),
    }


class TraceWatch:
    """The recorder's watch over the fake, through its trace and its gate."""

    def on_screen(self, pane, pattern):
        return pane.wait_for(pattern, timeout=tier_guard())

    def exited(self, pane):
        return pane.wait_exit(timeout=tier_guard())

    def env(self, sequence: rec.Sequence, folder: Path) -> dict[str, str]:
        return traced_env(folder, too_fast=sequence.answer_too_fast)

    def settled(self, run: rec.Run, name: str | None, kind: str) -> list[str]:
        return shown(run.pane, Path(run.env[fake.TRACE]))

    def release(self, run: rec.Run, stage: str) -> None:
        release(Path(run.env[fake.GATE]), stage)

    def handed_over(self, path: Path) -> str:
        return until(lambda: rec._ended_line(path))


class ExpectWatch:
    """The recorder's watch over the real `claude`: each capture's screen is waited for as its
    fixture in ``expected`` has it, sanitised. A program that has exited draws nothing more, so
    its screen fails at once when it is not the fixture; a running one's is waited for up to
    the tier's guard. Either fails with how the last screen differs."""

    def on_screen(self, pane, pattern):
        return pane.wait_for(pattern, timeout=tier_guard())

    def exited(self, pane):
        return pane.wait_exit(timeout=tier_guard())

    def __init__(self, expected: dict[str, rec.Recorded]) -> None:
        self.expected = expected

    def env(self, sequence: rec.Sequence, folder: Path) -> dict[str, str]:
        return {}

    def settled(self, run: rec.Run, name: str | None, kind: str) -> list[str]:
        if name is None or name not in self.expected:
            raise RuntimeError(f"ExpectWatch knows no fixture of {name!r} to wait for")
        want = self.expected[name].lines
        last: list[str] = []

        def now() -> list[str] | bool | None:
            """The screen once it is the fixture; True once the program has exited and its
            last screen, which tmux shows whole once the pane is dead, is not; else None."""
            nonlocal last
            exited = kind == rec.EXITED or run.pane.exit_status() is not None
            screen = run.pane.screen()
            last = rec.sanitise("\n".join(screen), run.ctx).split("\n")
            if last == want:
                return screen
            return exited or None

        try:
            found = until(now)
        except AssertionError:
            found = True
        if found is True:
            got = {name: rec.Recorded(lines=last, exit_code=None)}
            wanted = {name: rec.Recorded(lines=want, exit_code=None)}
            raise AssertionError(
                f"{name} never showed its fixture:\n" + "\n".join(rec.compare(got, wanted))
            )
        return found

    def release(self, run: rec.Run, stage: str) -> None:
        pass

    def handed_over(self, path: Path) -> str:
        raise RuntimeError("ExpectWatch has no person to wait on: no such sequence runs")
