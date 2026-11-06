"""Output rcman does not recognise, written to the journal.

Every server rcman reads, and every failure of a server verb, is shown to the
app's :class:`Watch`. The watch writes one JSON line on the
``rcman.unrecognised`` logger when something enters a state rcman does not
recognise: a server whose screen it does not know (``server_unrecognised``), or
a verb failing with one of :data:`UNRECOGNISED_OUTPUT`. It writes nothing while
the state lasts, and again only once it has ended and begun again. A line names
the person, the verb and the workspace, and counts and fingerprints the screen;
it never holds its text, which may show anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading

log = logging.getLogger("rcman.unrecognised")
log.setLevel(logging.INFO)

SERVER_UNRECOGNISED = "server_unrecognised"
# The failures that carry output rcman does not recognise.
UNRECOGNISED_OUTPUT = frozenset({"claude_unrecognised_output"})
# How many hexadecimal characters of the output's sha256 a line holds.
FINGERPRINT_CHARS = 16
# How many failing calls are remembered before all are forgotten.
MAX_REMEMBERED = 1000


def fingerprint(lines: list) -> str:
    """The start of a sha256 of the lines, each without its trailing spaces, so that
    two lines with the same fingerprint are very likely the same output."""
    text = "\n".join(str(line).rstrip() for line in lines)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:FINGERPRINT_CHARS]


class Watch:
    """What one rcman process has seen and written, shared by every request. Nothing it
    is shown makes it raise: a fault here never fails the request."""

    def __init__(self):
        self._lock = threading.Lock()
        # Each person's workspaces whose server was last seen in state unrecognised.
        self.servers: dict[str, set[str]] = {}
        # Verbs last seen failing with unrecognised output, (email, verb, workspace), with
        # the codes they failed with.
        self.failing: dict[tuple[str, str | None, str | None], set[str]] = {}

    def _safely(self, call, *args) -> None:
        try:
            call(*args)
        except Exception:
            log.exception("rcman could not look at an answer for unrecognised output")

    # --- what is shown to it ---------------------------------------------------------

    def listed(self, email: str, servers: object) -> None:
        """A listing of all of a person's workspaces, ``{workspace: server or None}``:
        those it does not show unrecognised, or does not show at all, are forgotten."""
        self._safely(self._listed, email, servers)

    def seen(self, email: str, verb: str | None, workspace: str, server: object) -> None:
        """One server, as an action or its output shows it."""
        self._safely(self._seen, email, verb, workspace, server)

    def answered(self, email: str, verb: str | None, workspace: str | None) -> None:
        """A verb worked: it is no longer failing."""
        with self._lock:
            self.failing.pop((email, verb, workspace), None)

    def failed(
        self, email: str, verb: str | None, workspace: str | None, code: str, extra: object
    ) -> None:
        """A verb failed with ``code`` and what the problem carried."""
        self._safely(self._failed, email, verb, workspace, code, extra)

    # --- what it makes of it ---------------------------------------------------------

    def _listed(self, email: str, servers: object) -> None:
        if not isinstance(servers, dict):
            return
        now = {
            name: server
            for name, server in servers.items()
            if isinstance(name, str)
            and isinstance(server, dict)
            and server.get("state") == "unrecognised"
        }
        with self._lock:
            before = self.servers.get(email, set())
            # Answers arrive in the order their calls end, so an older listing can land
            # after a newer one; at worst a line is written again.
            self.servers[email] = set(now)
        for name in sorted(set(now) - before):
            self._write(email, SERVER_UNRECOGNISED, "list", name, now[name].get("screen"))

    def _seen(self, email: str, verb: str | None, workspace: str, server: object) -> None:
        if not isinstance(server, dict) or not isinstance(workspace, str):
            return
        with self._lock:
            remembered = self.servers.setdefault(email, set())
            if server.get("state") != "unrecognised":
                remembered.discard(workspace)
                return
            if workspace in remembered:
                return
            remembered.add(workspace)
        self._write(email, SERVER_UNRECOGNISED, verb, workspace, server.get("screen"))

    def _failed(self, email, verb, workspace, code, extra) -> None:
        if code not in UNRECOGNISED_OUTPUT:
            return
        call = (email, verb, workspace)
        with self._lock:
            if code in self.failing.get(call, ()):
                return
            if call not in self.failing and len(self.failing) >= MAX_REMEMBERED:
                self.failing.clear()
            self.failing.setdefault(call, set()).add(code)
        screen = extra.get("screen") if isinstance(extra, dict) else None
        self._write(email, code, verb, workspace, screen)

    def _write(self, email, code, verb, workspace, lines: object) -> None:
        lines = lines if isinstance(lines, list) else []
        event = {
            "event": "unrecognised",
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "email": email,
            "code": code,
            "verb": verb,
            "workspace": workspace,
            "lines": len(lines),
            "fingerprint": fingerprint(lines),
        }
        log.info(json.dumps(event))
