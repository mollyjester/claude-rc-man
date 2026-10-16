"""A lock two processes of one user share: the service, and ``rcman-admin`` run beside it.

A ``threading`` lock holds back the threads of one process only; what the service and the
command line both do to the slices' limits would otherwise be judged and applied by each
without the other in sight. This is a re-entrant lock that is also an exclusive ``flock`` of a
file, held from the thread's first acquisition to its last release. Python standard library
only.
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

MODE = 0o600


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._thread = threading.RLock()
        self._depth = 0  # how many times the thread that holds it has taken it
        self._fd: int | None = None

    def acquire(self, blocking: bool = True) -> bool:
        if not self._thread.acquire(blocking):
            return False
        if self._depth == 0:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BaseException:
                os.close(fd)
                self._thread.release()
                if not blocking:
                    return False
                raise
            self._fd = fd
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._thread.release()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
