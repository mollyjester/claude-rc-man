"""A workspace's Claude Remote Control server: its tmux session, its state, its verbs.

Each workspace runs at most one server, in the tmux session ``SESSION`` on a
socket of the workspace's own, configured by ``tmux.conf`` beside this module.
Everything here runs in the workspace through ``run``, a channel's (channel.py):
tmux, Claude, the reads of ``/proc`` and every signal, since the processes and
their ids are the workspace's, not rcman's. Nothing is run through a shell but
the one read of every process's ``/proc/<pid>/stat``, whose glob a shell
expands; every program gets an environment of exactly the variables given.

What a screen means is helpers/screens.py's. Refusals are problems
(problems.py), whose codes and words are the backend's catalogue.
"""

from __future__ import annotations

import json
import signal as signals
import time
from collections.abc import Callable

from helpers import rules, screens
from rcman.channel import REQUEST_TIMEOUT, Result
from rcman.problems import Problem
from rcman.runtime import Layout

SESSION = "rc-server"
START_TIMEOUT = 30.0
# How long a stop waits for a server to exit on Ctrl+C before ending its process.
STOP_TIMEOUT = 30.0
# Once a stop ends a server's process: how long it waits after SIGTERM, and after SIGKILL.
END_GRACE_SECONDS = 5.0
# The trust prompt refuses an answer that arrives too quickly after the question.
ANSWER_PACE_SECONDS = 0.5
POLL_SECONDS = 0.1
# A screen in transition matches no known state for a moment.
UNRECOGNISED_GRACE_SECONDS = 3.0
# How long `claude auth status` may take.
SIGN_IN_CHECK_TIMEOUT = 60.0

Run = Callable[..., Result]


class ServerError(Problem):
    """A refusal of a server verb: a problem, with what it carries as ``detail``."""

    @property
    def detail(self) -> dict:
        return self.extra


class TmuxFailed(Exception):
    pass


def tmux_env(layout: Layout) -> dict[str, str]:
    """The environment tmux runs with, and so its server: a shell of the mounted tools,
    since a slim image has none of its own."""
    env = {
        "HOME": layout.home,
        "PATH": layout.path,
        "SHELL": layout.shell,
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
    }
    if layout.terminfo:
        env["TERMINFO"] = layout.terminfo
    return env


def child_env(layout: Layout) -> dict[str, str]:
    """The only environment Claude Code is started with. Its configuration is the
    workspace's own, and it never updates itself: the store it runs from is read-only,
    and an update would go into the workspace unapproved."""
    return {
        "HOME": layout.home,
        "USER": layout.user,
        "LOGNAME": layout.user,
        "PATH": layout.path,
        "SHELL": layout.shell,
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": layout.config,
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/bin/false",
    }


def claude_command(layout: Layout, *args: str) -> list[str]:
    return layout.with_env(child_env(layout), [layout.claude, *args])


def server_command(layout: Layout, prefix: str) -> list[str]:
    return claude_command(
        layout,
        "remote-control",
        "--remote-control-session-name-prefix",
        prefix,
        "--permission-mode",
        "default",
        "--spawn",
        "same-dir",
    )


class Tmux:
    """The workspace's tmux server and its one session."""

    def __init__(self, layout: Layout, run: Run):
        self.layout = layout
        self.run_argv = run
        self.base = layout.with_env(
            tmux_env(layout), [layout.tmux, "-S", layout.tmux_socket, "-f", layout.tmux_conf]
        )

    def run(self, *args: str, check: bool = True) -> str:
        result = self.run_argv([*self.base, *args])
        if check and result.code != 0:
            said = result.err.decode("utf-8", "replace").strip()
            raise TmuxFailed(f"tmux {args[0]}: {said}")
        return result.out.decode("utf-8", "replace")

    def exists(self) -> bool:
        return self.run_argv([*self.base, "has-session", "-t", f"={SESSION}"]).code == 0

    def new(self, cwd: str, argv: list[str]) -> bool:
        """Make the session running ``argv``; False if it is there already."""
        made = self.run_argv(
            [*self.base, "new-session", "-d", "-s", SESSION, "-x", str(screens.WIDTH),
             "-y", str(screens.HEIGHT), "-c", cwd, *argv]
        )  # fmt: skip
        if made.code != 0:
            if self.exists():
                return False
            said = made.err.decode("utf-8", "replace").strip()
            raise TmuxFailed(f"tmux new-session: {said}")
        self.pin()
        return True

    def pin(self) -> None:
        """Keep the session's window at the size it was made with, whatever terminal
        attaches to it; one a terminal has resized returns to it.

        A resize makes Claude's next redraw erase log lines and, for a moment, show
        its status block twice. Pinned window by window: a global ``window-size
        manual`` makes tmux 3.4's server exit at the next detached new-session."""
        self.run("set-option", "-w", "-t", f"={SESSION}:", "window-size", "manual")

    def respawn(self, cwd: str, argv: list[str]) -> None:
        """Start the server again in its pane, pinning its window first."""
        self.pin()
        self.run("respawn-pane", "-k", "-t", f"={SESSION}:", "-c", cwd, *argv)
        self.run("clear-history", "-t", f"={SESSION}:")

    def kill(self) -> None:
        self.run("kill-session", "-t", f"={SESSION}", check=False)

    def screen(self) -> list[str]:
        raw = self.run("capture-pane", "-p", "-J", "-t", f"={SESSION}:")
        lines = [line.rstrip() for line in raw.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def _zombie_status(self, pid: int) -> int | None:
        found = self.run_argv([*self.layout.tool("cat"), f"/proc/{pid}/stat"])
        if found.code != 0:
            return None
        return screens.zombie_status_of(found.out.decode("utf-8", "replace"))

    def exit_status(self) -> int | None:
        fmt = "#{pane_dead}:#{pane_dead_status}:#{pane_dead_signal}:#{pane_pid}"
        fields = self.run("display-message", "-p", "-t", f"={SESSION}:", fmt).strip().split(":")
        return screens.pane_exit_status(*fields, zombie_status=self._zombie_status)

    def type(self, text: str) -> None:
        self.run("send-keys", "-t", f"={SESSION}:", "-l", text)
        self.run("send-keys", "-t", f"={SESSION}:", "Enter")

    def interrupt(self) -> None:
        self.run("send-keys", "-t", f"={SESSION}:", "C-c")

    def pane(self) -> tuple[bool, int]:
        """Whether the pane's process has exited, and its process ID."""
        out = self.run("display-message", "-p", "-t", f"={SESSION}:", "#{pane_dead}:#{pane_pid}")
        dead, pid = out.strip().split(":")
        return dead == "1", int(pid)

    def signal(self, pid: int, sig: int) -> None:
        """Send ``sig`` to ``pid``, the pane's process when ending it began, and nothing
        once the pane runs another (a server started again since).

        SIGTERM goes to that process, to end as it ends itself; SIGKILL to its whole
        process group, which tmux made for it and which holds whatever it started there.
        A process that is gone is no error."""
        if self.pane()[1] != pid:
            return
        if sig == signals.SIGKILL:
            self.run_argv(self.layout.kill_group_of(pid))
        else:
            self.run_argv(
                [*self.layout.tool("kill"), f"-{signals.Signals(sig).name[3:]}", str(pid)]
            )


def _processes(stat_lines: str) -> dict[int, tuple[int, str]]:
    """Each process, by id, with its parent's id and its start time, from the lines of
    every ``/proc/<pid>/stat``."""
    found = {}
    for line in stat_lines.splitlines():
        head, _, rest = line.rpartition(")")
        fields = rest.split()
        try:
            pid = int(head.split("(", 1)[0])
            found[pid] = (int(fields[1]), fields[19])
        except (ValueError, IndexError):
            continue
    return found


class Server:
    """The server of the workspace ``workspace`` (its id), whose layout is ``layout``."""

    def __init__(self, layout: Layout, run: Run, workspace: int):
        self.layout = layout
        self.run = run
        self.workspace = workspace
        self.tmux = Tmux(layout, run)

    # --- reading -------------------------------------------------------------------

    def _server(self) -> dict:
        info = screens.classify(self.tmux.screen(), self.tmux.exit_status())
        return {"workspace": self.workspace, **info}

    def status(self) -> dict | None:
        """The server and its state; None when there is none."""
        if not self.tmux.exists():
            return None
        return self._server()

    def _existing(self) -> None:
        if not self.tmux.exists():
            raise ServerError("unknown_server")

    def output(self) -> dict:
        self._existing()
        return {"workspace": self.workspace, "screen": self.tmux.screen(), "server": self._server()}

    def _wait(self, until: Callable[[dict], bool], timeout: float) -> dict:
        """Poll until ``until`` holds. A screen in transition matches no known state, so
        an unrecognised screen counts as an answer only once it has lasted
        UNRECOGNISED_GRACE_SECONDS."""
        deadline = time.monotonic() + timeout
        unrecognised_since = None
        while True:
            server = self._server()
            if server["state"] == "unrecognised":
                unrecognised_since = unrecognised_since or time.monotonic()
                if time.monotonic() - unrecognised_since >= UNRECOGNISED_GRACE_SECONDS:
                    return server
            else:
                unrecognised_since = None
                if until(server):
                    return server
            if time.monotonic() > deadline:
                raise ServerError("timeout", server=server)
            time.sleep(POLL_SECONDS)

    # --- the sign-in ------------------------------------------------------------------

    def signed_in(self) -> bool:
        """Whether Claude in the workspace says an account is signed in."""
        found = self.run(
            claude_command(self.layout, "auth", "status"), timeout=SIGN_IN_CHECK_TIMEOUT
        )
        if found.code == 127:
            raise ServerError("claude_not_installed")
        out = found.out.decode("utf-8", "replace")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise ServerError("claude_unrecognised_output", screen=out.splitlines()) from None
        return bool(isinstance(data, dict) and data.get("loggedIn"))

    # --- verbs ------------------------------------------------------------------------

    def start(self, prefix: str) -> dict:
        if not rules.PREFIX.fullmatch(prefix):
            raise ServerError("prefix_invalid")
        if not self.signed_in():
            raise ServerError("claude_not_logged_in")
        command = server_command(self.layout, prefix)
        if self.tmux.exists():
            if self.tmux.exit_status() is None:
                raise ServerError("already_running")
            self.tmux.respawn(self.layout.work, command)
        elif not self.tmux.new(self.layout.work, command):
            raise ServerError("already_running")
        return {"server": self._wait(lambda s: s["state"] != "starting", START_TIMEOUT)}

    def resume(self, prefix: str) -> dict:
        if not self.tmux.exists() or self._server()["state"] not in screens.RESUMABLE:
            raise ServerError("not_stopped")
        return self.start(prefix)

    def stop(self) -> dict:
        """Ctrl+C, and a wait for the server to exit; one that does not is ended (_end)."""
        self._existing()
        if self.tmux.exit_status() is not None:
            raise ServerError("not_running")
        if not self._end():
            raise ServerError("timeout", server=self._server())
        return {"server": self._server()}

    def _end(self) -> bool:
        """Stop the server; whether it is no longer running.

        It is sent Ctrl+C and has STOP_TIMEOUT to exit. Still running then, it is
        ended: SIGTERM, then SIGKILL to its process group, each followed by
        END_GRACE_SECONDS. Waits for the process to exit rather than for a screen,
        which may be one rcman does not recognise. The pane's process is read once,
        here, and only it is waited for and signalled. Whatever it started that is
        still running afterwards, in its process group or not, is killed: it is the
        server's own, and Claude's sessions are among it."""
        dead, pid = self.tmux.pane()
        if dead or self.tmux.exit_status() is not None:
            return True
        started = self._descendants(pid)
        self.tmux.interrupt()
        running = self._still_runs(pid, STOP_TIMEOUT)
        for sig in (signals.SIGTERM, signals.SIGKILL):
            if not running:
                break
            self.tmux.signal(pid, sig)
            running = self._still_runs(pid, END_GRACE_SECONDS)
        if not running:
            self._kill_left(started)
        return not running

    def _still_runs(self, pid: int, timeout: float) -> bool:
        """Wait up to ``timeout`` for the pane's process ``pid`` to exit; whether it has not."""
        deadline = time.monotonic() + timeout
        while self.tmux.pane()[1] == pid and self.tmux.exit_status() is None:
            if time.monotonic() > deadline:
                return True
            time.sleep(POLL_SECONDS)
        return False

    def _process_table(self) -> dict[int, tuple[int, str]]:
        cat = " ".join(self.layout.tool("cat"))
        found = self.run([self.layout.shell, "-c", f"{cat} /proc/[0-9]*/stat 2>/dev/null"])
        return _processes(found.out.decode("utf-8", "replace"))

    def _descendants(self, pid: int) -> dict[int, str]:
        """Every process ``pid`` started, and they started, with its start time."""
        table = self._process_table()
        found, pending = {}, [pid]
        while pending:
            parent = pending.pop()
            for child, (ppid, start) in table.items():
                if ppid == parent and child not in found:
                    found[child] = start
                    pending.append(child)
        return found

    def _kill_left(self, started: dict[int, str]) -> None:
        """Kill each of ``started`` that is still the same process: same id, same start."""
        if not started:
            return
        now = self._process_table()
        left = [pid for pid, start in started.items() if pid in now and now[pid][1] == start]
        if left:
            self.run([*self.layout.tool("kill"), "-KILL", *map(str, left)])

    def forget(self) -> dict:
        self._existing()
        if self.tmux.exit_status() is None:
            raise ServerError("still_running")
        self.tmux.kill()
        return {"forgotten": self.workspace}

    def confirm(self, answer: str) -> dict:
        self._existing()
        if self._server()["state"] != "trust_prompt":
            raise ServerError("nothing_to_confirm")
        server = None
        for _attempt in range(2):
            time.sleep(ANSWER_PACE_SECONDS)
            self.tmux.type(answer)
            server = self._wait(self._answered, START_TIMEOUT)
            if server["state"] != "trust_prompt":
                return {"server": server}
        raise ServerError("confirm_not_accepted", server=server)

    def _answered(self, server: dict) -> bool:
        """The prompt went away, or Claude asked again because the answer was too fast."""
        if server["state"] != "trust_prompt":
            return server["state"] != "starting"
        lines = [line for line in self.tmux.screen() if line.strip()]
        return len(lines) > 1 and screens.TOO_FAST in lines[-2]


__all__ = ["REQUEST_TIMEOUT", "Server", "ServerError", "Tmux", "TmuxFailed"]
