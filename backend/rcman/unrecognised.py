"""Output rcman does not recognise, written to the journal.

Every rc-helper answer passes through :func:`watched`, which tells a
:class:`Watch` about it. The watch writes one JSON line on the
``rcman.unrecognised`` logger when something enters a state rcman does not
recognise: a server whose screen it does not know (``server_unrecognised``), or
a call failing with one of :data:`UNRECOGNISED_OUTPUT`. It writes nothing
while the state lasts, and again only once it has ended and begun again. A
line names the person, the call and the folder, and counts and fingerprints
the screen or git's output; it never holds their text, which may show anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading

from helpers import users_file as uf
from rcman.helpers_client import HelperRunner
from rcman.settings import Settings

log = logging.getLogger("rcman.unrecognised")
log.setLevel(logging.INFO)

SERVER_UNRECOGNISED = "server_unrecognised"
# rc-helper's failures that carry output it does not recognise.
UNRECOGNISED_OUTPUT = frozenset({"claude_unrecognised_output", "git_unrecognised_output"})
# How many hexadecimal characters of the output's sha256 a line holds.
FINGERPRINT_CHARS = 16
# How many failing calls are remembered before all are forgotten.
MAX_REMEMBERED = 1000


def fingerprint(lines: list) -> str:
    """The start of a sha256 of the lines, each without its trailing spaces, so that
    two lines with the same fingerprint are very likely the same output."""
    text = "\n".join(str(line).rstrip() for line in lines)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:FINGERPRINT_CHARS]


def _call(args: list) -> tuple[str | None, str | None]:
    """A call's verb, and the folder after ``--``, if it names one; nothing else of it."""
    verb = args[0] if args and isinstance(args[0], str) else None
    path = None
    if "--" in args:
        rest = args[args.index("--") + 1 :]
        path = rest[0] if rest and isinstance(rest[0], str) else None
    return verb, path


class Watch:
    """What one rcman process has seen and written, shared by every request."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        # Each person's servers last seen in state unrecognised, by folder.
        self.servers: dict[str, set[str]] = {}
        # Calls last seen failing with unrecognised output, (linux, verb, path), with the
        # codes they failed with.
        self.failing: dict[tuple[str, str | None, str | None], set[str]] = {}

    def observe(self, linux: str, args: list, code: int, out: object) -> None:
        """Note one answer, and write a line for anything it shows entering a state rcman
        does not recognise. Never raises: a fault here never fails the request."""
        try:
            self._observe(linux, args, code, out)
        except Exception:
            log.exception("rcman could not look at rc-helper's answer for unrecognised output")

    def _observe(self, linux: str, args: list, code: int, out: object) -> None:
        verb, path = _call(args)
        if not isinstance(out, dict):
            return
        if code != 0:
            self._failed(linux, verb, path, out.get("error"))
            return
        with self._lock:
            self.failing.pop((linux, verb, path), None)
        if verb == "list":
            self._listed(linux, out.get("servers"))
        elif isinstance(server := out.get("server"), dict):
            self._seen(linux, verb, server)

    def _failed(self, linux: str, verb: str | None, path: str | None, error: object) -> None:
        if not isinstance(error, dict) or error.get("code") not in UNRECOGNISED_OUTPUT:
            return
        call = (linux, verb, path)
        with self._lock:
            if error["code"] in self.failing.get(call, ()):
                return
            if call not in self.failing and len(self.failing) >= MAX_REMEMBERED:
                self.failing.clear()
            self.failing.setdefault(call, set()).add(error["code"])
        self._write(linux, error["code"], verb, path, error.get("screen", error.get("git")))

    def _listed(self, linux: str, servers: object) -> None:
        """A list is all of a person's servers: those it does not show unrecognised,
        or does not show at all, are forgotten."""
        if not isinstance(servers, list):
            return
        now = {
            s["path"]: s
            for s in servers
            if isinstance(s, dict)
            and s.get("state") == "unrecognised"
            and isinstance(s.get("path"), str)
        }
        with self._lock:
            before = self.servers.get(linux, set())
            # Answers arrive in the order their calls end, so an older list can land after
            # a newer one; at worst a line is written again.
            self.servers[linux] = set(now)
        for path in sorted(set(now) - before):
            self._write(linux, SERVER_UNRECOGNISED, "list", path, now[path].get("screen"))

    def _seen(self, linux: str, verb: str | None, server: dict) -> None:
        """One server, as an action or its output shows it."""
        path = server.get("path")
        if not isinstance(path, str):
            return
        with self._lock:
            remembered = self.servers.setdefault(linux, set())
            if server.get("state") != "unrecognised":
                remembered.discard(path)
                return
            if path in remembered:
                return
            remembered.add(path)
        self._write(linux, SERVER_UNRECOGNISED, verb, path, server.get("screen"))

    def _email(self, linux: str) -> str | None:
        try:
            user = uf.by_linux(uf.load(self.settings.users_file), linux)
        except (uf.UsersFileError, OSError):
            return None
        return user.email if user else None

    def _write(self, linux: str, code: str, verb: str | None, path: str | None, lines: object):
        lines = lines if isinstance(lines, list) else []
        event = {
            "event": "unrecognised",
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "email": self._email(linux),
            "linux": linux,
            "code": code,
            "verb": verb,
            "path": path,
            "lines": len(lines),
            "fingerprint": fingerprint(lines),
        }
        log.info(json.dumps(event))


def watched(run: HelperRunner, watch: Watch) -> HelperRunner:
    """``run``, with every answer it gives shown to ``watch`` before it is returned."""

    def run_watched(linux: str, args: list[str], *rest, **options) -> tuple[int, dict]:
        code, out = run(linux, args, *rest, **options)
        watch.observe(linux, args, code, out)
        return code, out

    return run_watched
