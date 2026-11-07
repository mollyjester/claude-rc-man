"""make screenshot: one page of the built frontend, captured for a look at the design.

Not a test: it asserts nothing. Given a page's path and, optionally, one of its states
(``python -m devtools.screenshots <page> [<state>]``; an empty state is none), it starts the
world's backend (``python -m devtools.world``), reads the origin it prints, runs
frontend/screenshots/capture.ts with Node against it, which says what pages and states there
are and writes the page's screenshots to frontend/test-results/screenshots/, and then closes the
backend's standard input and waits for it to shut down, which removes the world. Its exit code
is the capture's. It sets no time limit: Ctrl-C ends a run that hangs, the backend with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
OUT = FRONTEND / "test-results" / "screenshots"


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print("usage: python -m devtools.screenshots <page> [<state>]", file=sys.stderr)
        return 2
    backend = subprocess.Popen(
        [sys.executable, "-m", "devtools.world"],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        origin = backend.stdout.readline().strip()
        if not origin:
            print("screenshot: the world's backend did not start", file=sys.stderr)
            return 1
        return subprocess.run(
            ["node", "screenshots/capture.ts", origin, str(OUT), *argv], cwd=FRONTEND
        ).returncode
    finally:
        backend.stdin.close()
        backend.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
