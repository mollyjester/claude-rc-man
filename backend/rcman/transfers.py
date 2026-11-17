"""The downloads and uploads under way: each a stream (runtime.py ``Runtime.stream``) in a
workspace, let through by admission, and ended in the workspace however it ends.

**Admission.** A transfer is let through while its person has fewer than
``TRANSFERS_PER_PERSON`` under way and everyone fewer than ``TRANSFERS_IN_ALL``
(``transfers_busy`` otherwise); its place is given back in its own ``finally``.

**Threads.** Every blocking read and write of a stream runs in a thread bounded by
``TRANSFER_THREADS``, a limiter of its own, so that transfers never hold the threads every other
request needs; it bounds threads, not transfers.

**Starting and ending.** A stream is started under the workspace's lock, once it is still ready
(``Workspaces._still_ready``), and is let go of once its launcher has written its pidfile, so a
Stop waits for a start, never for a transfer. A Stop, a rebuild and a deletion end every
transfer of the workspace first (``end_all``), with the code a transfer under way then answers.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable

import anyio

from rcman.channel import OPEN_TIMEOUT
from rcman.forward import _after
from rcman.problems import Problem
from rcman.runtime import Stream, StreamFailed, transfer_pidfile
from rcman.workspaces import Workspace, Workspaces

log = logging.getLogger("rcman.transfers")
log.setLevel(logging.INFO)

# How many transfers may be under way at once: one person's, and everyone's.
TRANSFERS_PER_PERSON = 4
TRANSFERS_IN_ALL = 16
# How long a transfer waits for a byte, from the workspace or from the browser, before it is
# ended (``transfer_idle``).
TRANSFER_IDLE_SECONDS = 60.0
# The threads the streams' reads and writes may hold at once.
TRANSFER_THREADS = anyio.CapacityLimiter(TRANSFERS_IN_ALL)
# How much of a stream is read at once.
CHUNK_BYTES = 64 * 1024


async def in_thread(call: Callable, *args):
    """``call(*args)`` in a thread of the transfers' own (TRANSFER_THREADS)."""
    return await anyio.to_thread.run_sync(call, *args, limiter=TRANSFER_THREADS)


class Transfers:
    def __init__(self, service: Workspaces):
        self.service = service
        self._lock = threading.Lock()
        self._persons: dict[int, int] = {}
        self._all = 0
        # Each stream under way, with its workspace's id and the files its end removes.
        self._live: dict[Stream, tuple[int, tuple[str, ...]]] = {}
        # The code each stream that end_all ended answers.
        self._ended: dict[Stream, str] = {}
        _after(service, "on_stopping", lambda w: self.end_all(w, "workspace_stopped"))
        _after(service, "on_rebuilding", lambda w: self.end_all(w, "workspace_rebuilding"))
        _after(service, "on_removing", lambda w: self.end_all(w, "workspace_not_found"))

    def room(self, person: int) -> None:
        """``transfers_busy`` where a transfer of ``person``'s would not be let through now;
        nothing is taken."""
        with self._lock:
            if (
                self._persons.get(person, 0) >= TRANSFERS_PER_PERSON
                or self._all >= TRANSFERS_IN_ALL
            ):
                raise Problem("transfers_busy")

    @contextlib.contextmanager
    def admit(self, person: int):
        """A place for a transfer of ``person``'s while the block runs; ``transfers_busy``
        where there is none."""
        with self._lock:
            mine = self._persons.get(person, 0)
            if mine >= TRANSFERS_PER_PERSON or self._all >= TRANSFERS_IN_ALL:
                raise Problem("transfers_busy")
            self._persons[person] = mine + 1
            self._all += 1
        try:
            yield
        finally:
            with self._lock:
                self._all -= 1
                left = self._persons[person] - 1
                if left:
                    self._persons[person] = left
                else:
                    del self._persons[person]

    def start(self, workspace: Workspace, argv: list[str], *temporary: str) -> Stream:
        """``argv`` started as a stream in the workspace, once its pidfile is written, the files
        ``temporary`` (paths in the workspace) removed by its end; ``transfer_failed`` where it
        did not start."""
        service = self.service
        with service.lock(workspace.id):
            service._still_ready(workspace.id)
            layout = service.layout_of(workspace)
            stream = service.runtime.stream(workspace.container, argv, transfer_pidfile(layout))
            with self._lock:
                self._live[stream] = (workspace.id, temporary)
            try:
                stream.ready(OPEN_TIMEOUT)
            except StreamFailed:
                log.warning("a transfer in %s did not start: %s", workspace.name, stream.said)
                self.end(workspace, stream)
                raise Problem("transfer_failed") from None
        return stream

    def end(self, workspace: Workspace, stream: Stream) -> None:
        """End the stream in the workspace (``Stream.end``), with the files it leaves."""
        with self._lock:
            _, temporary = self._live.pop(stream, (None, ()))
        stream.end(self.service.run_in(workspace), *temporary)

    def ended_code(self, stream: Stream) -> str | None:
        """The code ``end_all`` ended the stream with, if it did."""
        with self._lock:
            return self._ended.get(stream)

    def forget(self, stream: Stream) -> None:
        with self._lock:
            self._ended.pop(stream, None)

    def end_all(self, workspace: Workspace, code: str) -> None:
        """End every transfer of the workspace, each to answer ``code``."""
        with self._lock:
            mine = [s for s, (w, _) in self._live.items() if w == workspace.id]
            for stream in mine:
                self._ended[stream] = code
        for stream in mine:
            log.info("a transfer in %s was ended: %s", workspace.name, code)
            self.end(workspace, stream)
