"""backend/rcman/filelock.py: a lock that the service and ``rcman-admin`` share, two processes
of one user, by a lock file."""

import os
import subprocess
import sys
import threading
from pathlib import Path

from rcman.filelock import FileLock

from tests.hang_guard import HANG_GUARD
from tests.until import until

# Holds the lock until a line, or the end, comes on its stdin.
HOLD_UNTIL_TOLD = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.readline()\n"
)


def other_process_holding_until_told(path):
    """Another process holding the lock file at ``path`` until the test writes a line to its
    stdin, or closes it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_UNTIL_TOLD, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"  # the control: it has the lock
    return proc


def waiting_on(path: Path) -> bool:
    """Whether a lock of the file at ``path`` is waited for, as the kernel's table of locks
    (/proc/locks) says: a waiter's line is marked ``->``, and names the file by its inode, the
    last part of its ``major:minor:inode``. The device is not matched: on btrfs a file's st_dev
    is not the device the table names."""
    inode = f":{os.stat(path).st_ino}"
    return any(
        "->" in fields and any(f.count(":") == 2 and f.endswith(inode) for f in fields)
        for fields in (line.split() for line in Path("/proc/locks").read_text().splitlines())
    )


def test_a_lock_waits_for_another_process_that_holds_the_file(tmp_path):
    """Asked for while another process holds the file, the lock is not got until that process
    lets go: the absence is read once the kernel says the lock is waited for."""
    path = tmp_path / "rcman.lock"
    holder = other_process_holding_until_told(path)
    lock = FileLock(path)
    got = threading.Event()

    def take():
        with lock:
            got.set()

    taking = threading.Thread(target=take, daemon=True)
    taking.start()
    try:
        until(lambda: got.is_set() or waiting_on(path))
        assert not got.is_set()  # it did not get through while the other process held it
    finally:
        holder.stdin.close()  # the holder lets go
    taking.join(HANG_GUARD)
    assert not taking.is_alive() and got.is_set()
    holder.wait(HANG_GUARD)


def test_a_lock_is_the_users_alone_free_once_released_and_reentrant_in_its_own_thread(tmp_path):
    path = tmp_path / "rcman.lock"
    lock = FileLock(path)
    with lock, lock:  # a thread that holds it may take it again
        assert path.stat().st_mode & 0o777 == 0o600  # the lock file is the user's alone
        got = []
        thread = threading.Thread(target=lambda: got.append(lock.acquire(blocking=False)))
        thread.start()
        thread.join(HANG_GUARD)
        assert got == [False]  # another thread does not
    holder = other_process_holding_until_told(path)  # the file is free for another process again
    holder.stdin.close()
    holder.wait(HANG_GUARD)
