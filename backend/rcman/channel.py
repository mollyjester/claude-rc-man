"""One long-lived shell per workspace, through which rcman runs its commands there.

Starting a process in a container costs hundreds of milliseconds, which polls and
screen reads cannot afford, so a workspace's commands go through one shell that
stays open: ``podman --remote exec -i <container> busybox sh -s``, or the host's
``sh -s`` for the host runtime (runtime.py). Python standard library and helpers/clock.py only.

The protocol: each request is one line, the argv quoted for the shell, run with
stdin from /dev/null and its two output streams into files of the channel's own
in the workspace's runtime folder; then a header line, the request's nonce, the
exit code and the two files' sizes, and the two files' bytes. Only argv crosses:
nothing a request runs reads anything from rcman, and anything that needs stdin
runs as an exec of its own (Runtime.exec).

Requests are answered one at a time. A channel that cannot be opened because of
conmon's error (``no logs from conmon``), and one found dead before a request was
written, is opened again, once. A request that was written is never sent again,
whatever happens to it: it may have run. A channel that was closed is never opened again:
every request to it fails as broken.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from helpers.clock import Deadline, Grace

# How long opening a channel, and a request by default, may take.
OPEN_TIMEOUT = Deadline(60.0)
REQUEST_TIMEOUT = Deadline(60.0)
# How long the shell is given to end of itself once its input ends, before it is killed.
END_GRACE_SECONDS = Grace(2)
# The most of one stream a request's answer keeps; the rest is read and dropped.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
# How much of what the channel's command writes on stderr is kept, for a failure's message.
MAX_ERROR_BYTES = 64 * 1024
# What conmon says when an exec could not be started at all, so nothing ran.
CONMON_FAILED = "no logs from conmon"
# The slowest the 95th percentile of a request may be (tests/test_channel.py, and
# tests/container/test_runtime_in_podman.py on the tests' rootless engine).
LATENCY_P95_SECONDS = 0.1

# tools(name): the argv that runs the tool ``name`` (wc, cat, rm) where the shell runs.
Tools = Callable[[str], list[str]]


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes
    err: bytes


class ChannelBroken(Exception):
    """The channel could not be opened, or ended without answering a request."""


class ChannelClosed(ChannelBroken):
    """The channel was closed before the request was sent to it: nothing of it ran, so it may be
    asked of another channel."""


class ChannelTimeout(Exception):
    """A request was not answered in time; the channel was closed."""


def quote(arg: str) -> str:
    """An argument as the shell reads it back, whatever it holds but a newline or NUL."""
    if "\n" in arg or "\0" in arg:
        raise ValueError("an argument of a channel request must be one line, without NUL")
    return "'" + arg.replace("'", "'\\''") + "'"


class Channel:
    def __init__(self, argv: list[str], tools: Tools, run_dir: str, env: dict | None = None):
        self.argv = list(argv)
        self.tools = tools
        self.run_dir = run_dir
        self.env = env
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._buffer = b""
        self._errors = bytearray()
        self._error_reader: threading.Thread | None = None
        self._files: tuple[str, str] = ("", "")
        self._last = time.monotonic()
        self._closed = False

    # --- opening and closing ------------------------------------------------------

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _open(self) -> None:
        """Start the shell and wait for it to answer; once more on conmon's error. A closed
        channel is never opened again: ChannelClosed, also when the close came while the shell
        was being started, which run() then ends."""
        for attempt in (1, 2):
            if self._closed:
                raise ChannelClosed("the channel was closed")
            try:
                self._start()
            except ChannelBroken as error:
                if self._closed:
                    raise ChannelClosed("the channel was closed") from None
                if attempt == 2 or CONMON_FAILED not in str(error):
                    raise
                continue
            if self._closed:
                raise ChannelClosed("the channel was closed")
            return

    def _start(self) -> None:
        self.process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            start_new_session=True,
        )
        self._buffer = b""
        self._errors = bytearray()
        self._error_reader = threading.Thread(
            target=self._read_errors, args=(self.process, self._errors), daemon=True
        )
        self._error_reader.start()
        name = f"{self.run_dir}/channel-{secrets.token_hex(6)}"
        self._files = (f"{name}.out", f"{name}.err")
        nonce = secrets.token_hex(8)
        try:
            self._write(f"printf '%s\\n' {nonce}\n")
            line = self._readline(time.monotonic() + OPEN_TIMEOUT)
        except (BrokenPipeError, ChannelBroken, ChannelTimeout):
            raise self._broken("the channel did not open") from None
        if line.strip() != nonce.encode():
            raise self._broken(f"the channel answered {line[:80]!r}")

    @staticmethod
    def _read_errors(process: subprocess.Popen, errors: bytearray) -> None:
        """Keep the first MAX_ERROR_BYTES of what ``process`` writes on stderr, into ``errors``,
        until nothing holds the stream open any more; then close it. This thread alone closes
        it: closing it under a read would fail the read, and a program the shell started may
        hold the stream past the shell's own end."""
        try:
            while chunk := process.stderr.read1(4096):
                room = MAX_ERROR_BYTES - len(errors)
                if room > 0:
                    errors += chunk[:room]
        finally:
            process.stderr.close()

    def _broken(self, said: str) -> ChannelBroken:
        """The channel ended, with what its command wrote on stderr, the shell made to end."""
        self._end(grace=0)
        if self._error_reader is not None:
            self._error_reader.join(timeout=5)
        return ChannelBroken(bytes(self._errors).decode("utf-8", "replace").strip() or said)

    def _end(self, grace: float = END_GRACE_SECONDS) -> None:
        """End the shell: told to by the end of its input, and killed, with whatever it
        started in its session, if it has not ended within ``grace`` seconds."""
        process, self.process = self.process, None
        if process is None:
            return
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, 9)
            process.kill()
            process.wait()
        with contextlib.suppress(OSError):
            process.stdout.close()  # stderr is its reader's to close (_read_errors)

    def close(self) -> None:
        """Remove the channel's files and end its shell, for good: a request afterwards fails
        as broken, and nothing opens it again. A request in flight is not waited for: the shell
        is killed, and the request fails as broken, leaving its files to the workspace's
        tmpfs."""
        self._closed = True
        if not self._lock.acquire(blocking=False):
            process = self.process
            if process is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, 9)
            return
        try:
            if self._alive():
                rm = " ".join(quote(a) for a in [*self.tools("rm"), "-f", *self._files])
                with contextlib.suppress(OSError):
                    self._write(f"{rm}; exit\n")
            self._end()
        finally:
            self._lock.release()

    def mark_closed_if_idle(self, seconds: float) -> bool:
        """Mark the channel closed if no request holds it and none has begun or ended for longer
        than ``seconds``, without waiting for a request that holds it: whether it was. A request
        afterwards is refused as sent nowhere (ChannelClosed), and leaves the shell to close(),
        which removes the channel's files and ends it."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if time.monotonic() - self._last <= seconds:
                return False
            self._closed = True
            return True
        finally:
            self._lock.release()

    # --- requests -------------------------------------------------------------------

    def run(self, argv: list[str], timeout: float = REQUEST_TIMEOUT) -> Result:
        """Run ``argv`` with stdin from /dev/null; its exit code and two streams."""
        line = " ".join(quote(arg) for arg in argv)  # refuses before anything is sent
        with self._lock:
            if self._closed:  # marked closed as idle (mark_closed_if_idle), or closed: its
                # shell, if it has one, is left to close(), which removes the files through it
                raise ChannelClosed("the channel was closed")
            self._last = time.monotonic()  # in use from now: not idle while it runs
            try:
                if not self._alive():
                    self._open()
                try:
                    self._write_request(line)
                except BrokenPipeError:  # it died before the request reached it
                    self._end()
                    self._open()
                    self._write_request(line)
                return self._answer(time.monotonic() + timeout)
            finally:
                self._last = time.monotonic()
                if self._closed:  # closed while it opened, before there was a shell to kill
                    self._end(grace=0)

    def _write_request(self, line: str) -> None:
        out, err = (quote(f) for f in self._files)
        wc = " ".join(quote(a) for a in self.tools("wc"))
        cat = " ".join(quote(a) for a in self.tools("cat"))
        self._nonce = secrets.token_hex(8)
        self._write(
            f"{line} </dev/null >{out} 2>{err}; c=$?; "
            f'printf \'%s %s %s %s\\n\' {self._nonce} "$c" "$({wc} -c <{out})" '
            f'"$({wc} -c <{err})"; {cat} {out} {err}\n'
        )

    def _answer(self, deadline: float) -> Result:
        try:
            header = self._readline(deadline).split()
            if len(header) != 4 or header[0] != self._nonce.encode():
                raise ChannelBroken(f"the channel answered {b' '.join(header)[:80]!r}")
            code, out_size, err_size = (int(field) for field in header[1:])
            out = self._read(out_size, deadline)
            err = self._read(err_size, deadline)
        except ChannelTimeout:
            self._end()
            raise
        except ChannelBroken as error:
            raise self._broken(str(error)) from None
        except ValueError:
            self._end()
            raise ChannelBroken("the channel's answer could not be read") from None
        return Result(code, out, err)

    def _write(self, text: str) -> None:
        self.process.stdin.write(text.encode())
        self.process.stdin.flush()

    def _fill(self, deadline: float) -> None:
        """Read what the shell has written, waiting until ``deadline`` for some."""
        fd = self.process.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(left):
                raise ChannelTimeout()
        chunk = os.read(fd, 65536)
        if not chunk:
            raise ChannelBroken("the channel ended")
        self._buffer += chunk

    def _readline(self, deadline: float) -> bytes:
        while b"\n" not in self._buffer:
            self._fill(deadline)
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def _read(self, size: int, deadline: float) -> bytes:
        kept = bytearray()
        while size > 0:
            if not self._buffer:
                self._fill(deadline)
            chunk, self._buffer = self._buffer[:size], self._buffer[size:]
            size -= len(chunk)
            room = MAX_OUTPUT_BYTES - len(kept)
            if room > 0:
                kept += chunk[:room]
        return bytes(kept)
