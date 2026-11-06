"""backend/rcman/filelock.py: a lock that the service and ``rcman-admin`` share, two processes
of one user, by a lock file."""

import os
import subprocess
import sys
import threading
from pathlib import Path

from rcman.filelock import FileLock

from tests.hang_guard import HANG_GUARD
from tests.time_doubles import RecordingLock
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
    (/proc/locks) says: a waiter's line is marked ``->``, and names the file by its inode."""
    found = os.stat(path)
    inode = f"{os.major(found.st_dev):02x}:{os.minor(found.st_dev):02x}:{found.st_ino}"
    return any(
        "->" in line and inode in line.split()
        for line in Path("/proc/locks").read_text().splitlines()
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


def test_a_lock_is_free_once_released_and_is_reentrant_in_its_own_thread(tmp_path):
    path = tmp_path / "rcman.lock"
    lock = FileLock(path)
    with lock, lock:  # a thread that holds it may take it again
        got = []
        thread = threading.Thread(target=lambda: got.append(lock.acquire(blocking=False)))
        thread.start()
        thread.join(HANG_GUARD)
        assert got == [False]  # another thread does not
    holder = other_process_holding_until_told(path)  # the file is free for another process again
    holder.stdin.close()
    holder.wait(HANG_GUARD)


def test_the_lock_file_is_the_users_alone(tmp_path):
    path = tmp_path / "rcman.lock"
    with FileLock(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_two_threads_take_turns(tmp_path):
    """Four threads, each come to the lock while the test holds it, so that they all contend
    for it once it is let go; each, once in, is alone there."""
    lock = FileLock(tmp_path / "rcman.lock")
    held = RecordingLock(lock._thread)
    lock._thread = held
    inside = []

    def work():
        with lock:
            inside.append(1)
            assert len(inside) == 1
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(4)]
    with lock:
        for thread in threads:
            thread.start()
        held.wait_blocked(4)  # every one of them waits for it
    for thread in threads:
        thread.join(HANG_GUARD)
    assert not any(t.is_alive() for t in threads) and inside == []
