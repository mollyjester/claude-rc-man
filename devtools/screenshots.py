"""make screenshots: every page of the built frontend, captured for a look at the design.

Not a test: it asserts nothing. It starts the world's backend (``python -m devtools.world``),
reads the origin it prints, runs frontend/screenshots/capture.ts with Node against it, which
writes the screenshots to frontend/test-results/screenshots/, and then closes the backend's
standard input and waits for it to shut down, which removes the world. It sets no time limit:
Ctrl-C ends a run that hangs, the backend with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
OUT = FRONTEND / "test-results" / "screenshots"


def main() -> int:
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
            print("screenshots: the world's backend did not start", file=sys.stderr)
            return 1
        return subprocess.run(
            ["node", "screenshots/capture.ts", origin, str(OUT)], cwd=FRONTEND
        ).returncode
    finally:
        backend.stdin.close()
        backend.wait()


if __name__ == "__main__":
    sys.exit(main())
