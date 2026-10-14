"""Drive one program in a private tmux server, for recording and testing.

Each :class:`Pane` owns its own tmux server (a socket file under
``socket_dir``), runs exactly one program in it with exactly the environment it
is given, and reads the screen back as plain text. The program is executed
directly, never through a shell. When the program exits the pane stays, so its
final screen and exit status can still be read.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from helpers.screens import pane_exit_status

WIDTH = 200
HEIGHT = 50
POLL_SECONDS = 0.1


def server_running(socket: Path) -> bool:
    proc = subprocess.run(["tmux", "-S", str(socket), "has-session"], capture_output=True)
    return proc.returncode == 0


class Pane:
    def __init__(self, socket_dir: Path, width: int = WIDTH, height: int = HEIGHT):
        self.socket = Path(socket_dir) / "tmux.sock"
        self.width = width
        self.height = height
        self._started = False

    def __enter__(self) -> Pane:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _tmux(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def run(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        if self._started:
            raise RuntimeError("a Pane runs one program; respawn with rerun()")
        self._tmux(
            "new-session", "-d", "-s", "p", "-x", str(self.width), "-y", str(self.height),
            "sleep", "86400",
        )  # fmt: skip
        self._tmux("set-option", "-t", "p", "remain-on-exit", "on")
        self._tmux("set-option", "-t", "p", "remain-on-exit-format", "")
        self._started = True
        self.rerun(argv, cwd, env)

    def rerun(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        """Replace whatever runs in the pane, on a cleared screen."""
        command = ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items()), *argv]
        self._tmux("respawn-pane", "-k", "-t", "p", "-c", str(cwd), *command)
        self._tmux("clear-history", "-t", "p")

    def screen(self) -> list[str]:
        lines = [
            line.rstrip() for line in self._tmux("capture-pane", "-p", "-J", "-t", "p").splitlines()
        ]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def text(self) -> str:
        return "\n".join(self.screen())

    def type_line(self, text: str) -> None:
        self._tmux("send-keys", "-t", "p", "-l", text)
        self._tmux("send-keys", "-t", "p", "Enter")

    def interrupt(self) -> None:
        self._tmux("send-keys", "-t", "p", "C-c")

    def signal(self, signum: int) -> None:
        """Send ``signum`` to the program itself, as ``kill`` would."""
        os.kill(int(self._tmux("display-message", "-p", "-t", "p", "#{pane_pid}")), signum)

    def exit_status(self) -> int | None:
        fmt = "#{pane_dead}:#{pane_dead_status}:#{pane_dead_signal}:#{pane_pid}"
        fields = self._tmux("display-message", "-p", "-t", "p", fmt).strip().split(":")
        return pane_exit_status(*fields)

    def wait_for(self, pattern: str, timeout: float) -> re.Match:
        regex = re.compile(pattern, re.MULTILINE)
        deadline = time.monotonic() + timeout
        while True:
            text = self.text()
            match = regex.search(text)
            if match:
                return match
            if time.monotonic() > deadline:
                raise TimeoutError(f"{pattern!r} did not appear; screen:\n{text}")
            time.sleep(POLL_SECONDS)

    def wait_exit(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while (status := self.exit_status()) is None:
            if time.monotonic() > deadline:
                raise TimeoutError(f"program still running; screen:\n{self.text()}")
            time.sleep(POLL_SECONDS)
        return status

    def wait_stable(self, quiet: float = 1.0, timeout: float = 15.0) -> list[str]:
        """Wait until the screen has not changed for ``quiet`` seconds."""
        deadline = time.monotonic() + timeout
        last, since = self.screen(), time.monotonic()
        while time.monotonic() - since < quiet:
            if time.monotonic() > deadline:
                raise TimeoutError(f"screen kept changing; last:\n{chr(10).join(last)}")
            time.sleep(POLL_SECONDS)
            now = self.screen()
            if now != last:
                last, since = now, time.monotonic()
        return last

    def close(self) -> None:
        """Kill the tmux server and wait until it is gone, so the socket can be reused."""
        if self._started:
            self._tmux("kill-server", check=False)
            deadline = time.monotonic() + 10
            while server_running(self.socket) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            self.socket.unlink(missing_ok=True)
            self._started = False
