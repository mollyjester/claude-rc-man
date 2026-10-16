"""backend/rcman/filelock.py: a lock that the service and ``rcman-admin`` share, two processes
of one user, by a lock file."""

import subprocess
import sys
import threading
import time

from rcman.filelock import FileLock

HOLD = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def other_process_holding(path, seconds):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(path), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "held"  # the control: it has the lock
    return proc


def test_a_lock_waits_for_another_process_that_holds_the_file(tmp_path):
    path = tmp_path / "rcman.lock"
    holder = other_process_holding(path, 1.5)
    lock = FileLock(path)
    began = time.monotonic()
    with lock:
        waited = time.monotonic() - began
    holder.wait(10)
    assert waited >= 1.0, waited  # it did not get through while the other process held it


def test_a_lock_is_free_once_released_and_is_reentrant_in_its_own_thread(tmp_path):
    path = tmp_path / "rcman.lock"
    lock = FileLock(path)
    with lock, lock:  # a thread that holds it may take it again
        got = []
        thread = threading.Thread(target=lambda: got.append(lock.acquire(blocking=False)))
        thread.start()
        thread.join(5)
        assert got == [False]  # another thread does not
    holder = other_process_holding(path, 0.2)  # the file is free for another process again
    holder.wait(10)


def test_the_lock_file_is_the_users_alone(tmp_path):
    path = tmp_path / "rcman.lock"
    with FileLock(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_two_threads_take_turns(tmp_path):
    lock = FileLock(tmp_path / "rcman.lock")
    inside = []

    def work():
        with lock:
            inside.append(1)
            assert len(inside) == 1
            time.sleep(0.1)
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(t.is_alive() for t in threads) and inside == []
