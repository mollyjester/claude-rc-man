"""Checks a candidate Claude Code version must pass before rcman rolls it out.

They run the candidate with a throwaway configuration, so no account is
needed, as rcman runs it: ``auth status`` through pipes, with no terminal,
and the rest on a pseudo-terminal of the size rcman gives Claude's screens
(``WIDTH`` and ``HEIGHT`` in screens.py). They look for exactly what
rcman relies on: the version, ``auth status`` JSON, the logged-out refusal of
``remote-control``, the sign-in screen, the options rcman passes to
``auth login``, and ``auth logout``. What needs a signed-in account — the
trust question and the server screens — is covered by the recorded tests
only.

Python standard library and the helpers' own modules only. The canary
(claude_canary.py) runs them against a candidate in a throwaway container, before the
version is rolled out to workspaces.
"""

from __future__ import annotations

import codecs
import fcntl
import json
import os
import re
import selectors
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from pathlib import Path

from helpers import screens

# One control token at a time: CSI sequences, OSC strings, other escapes, or a
# single control character: C0, DEL or C1.
TOKEN = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"  # CSI: parameters, final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, e.g. window titles
    r"|\x1b[ -/]*[0-~]"  # other escapes, e.g. save/restore cursor, character sets
    r"|[\x00-\x1f\x7f-\x9f]"
)
# The start of a control token that the next piece of output may complete.
UNFINISHED = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?|[ -/]*)\Z")
MAX_UNFINISHED = 4096  # an unfinished token longer than this is read as it stands
MAX_PARAM_DIGITS = 6  # a cursor move by a longer number goes as far as the line allows


class Screen:
    """The text a terminal would show, line by line, without its control sequences,
    from output fed in pieces as it arrives.

    Renders what the checks depend on — carriage return, newline, tab and
    cursor moves along a line (``C``, ``D``, ``G``), which go no further right
    than the terminal is wide (``WIDTH`` in screens.py) or than the cursor
    already is, and erasing in the line (``K``: to its end, to the cursor, or
    all of it) — and ignores every other control, moves between lines among
    them.
    """

    def __init__(self):
        self.lines: list[list[str]] = [[]]
        self.col = 0
        self._held = ""

    def _put(self, text: str) -> None:
        line = self.lines[-1]
        if self.col > len(line):
            line.extend(" " * (self.col - len(line)))
        line[self.col : self.col + len(text)] = text
        self.col += len(text)

    def feed(self, text: str, final: bool = False) -> None:
        text, self._held = self._held + text, ""
        if not final:
            unfinished = UNFINISHED.search(text)
            if unfinished and len(text) - unfinished.start() <= MAX_UNFINISHED:
                text, self._held = text[: unfinished.start()], text[unfinished.start() :]
        pos = 0
        for match in TOKEN.finditer(text):
            if match.start() > pos:
                self._put(text[pos : match.start()])
            pos = match.end()
            token = match.group(0)
            params, final_byte = match.group(1), match.group(2)
            if token == "\n":
                self.lines.append([])
                self.col = 0
            elif token == "\r":
                self.col = 0
            elif token == "\t":
                self._move_right((self.col // 8 + 1) * 8)
            elif final_byte in ("C", "D", "G") and (params == "" or params.isdigit()):
                if not params:
                    n = 1
                elif len(params) > MAX_PARAM_DIGITS:  # past every column, and not read
                    n = 10**MAX_PARAM_DIGITS
                else:
                    n = int(params)
                if final_byte == "D":
                    self.col = max(0, self.col - n)
                else:
                    self._move_right(self.col + n if final_byte == "C" else n - 1)
            elif final_byte == "K" and params in ("", "0"):
                del self.lines[-1][self.col :]
            elif final_byte == "K" and params == "1":
                line = self.lines[-1]
                line[: self.col + 1] = " " * min(len(line), self.col + 1)
            elif final_byte == "K" and params == "2":
                self.lines[-1] = []
        if pos < len(text):
            self._put(text[pos:])

    def _move_right(self, to: int) -> None:
        self.col = min(to, max(self.col, screens.WIDTH - 1))

    def line(self, index: int) -> str:
        return "".join(self.lines[index]).rstrip()

    def text(self) -> str:
        return "\n".join(self.line(i) for i in range(len(self.lines)))


def scrub(text: str) -> str:
    """``text`` without control sequences or control characters, but for newline and tab."""
    return TOKEN.sub(lambda m: m.group(0) if m.group(0) in ("\n", "\t") else "", text)


TIMEOUT = 60.0
CHECK_EMAIL = "check@example.com"
# How much of a program's output a run keeps in memory, for each stream it reads;
# the rest is read, so the program can finish, and counted.
# A run that keeps only the start of a stream leaves out the line the cap cut, so
# that nothing in it is cut short of what redaction recognises.
MAX_CAPTURE_BYTES = 256 * 1024
READ_SIZE = 65536
# How long a run's output, on a terminal or through pipes, is still read once its
# program has exited, for what it wrote last; something it started may hold it open
# and write on. POLL_SECONDS is how often a run looks for its program's exit, and is
# also the quiet window: once the program has exited, a poll with nothing to read
# ends the run.
DRAIN_SECONDS = 0.5
POLL_SECONDS = 0.05


class Ran(tuple):
    """What a run gives a check: (exit code, output), as a ``Runner`` returns it.

    It keeps, for the report only, what the check does not read: ``stdout`` as
    written even where the program did not exit, ``stderr``, and on a terminal
    ``raw``, what was written before the terminal drew it, without which a secret
    the program wrote over could not be told. ``dropped`` counts the bytes of
    output beyond what was kept."""

    def __new__(
        cls,
        code: int | None,
        output: str,
        stdout: str,
        stderr: str,
        dropped: int = 0,
        raw: str = "",
    ):
        ran = super().__new__(cls, (code, output))
        ran.stdout, ran.stderr, ran.dropped, ran.raw = stdout, stderr, dropped, raw
        return ran


# run(args, until=None, timeout=..., terminal=True) -> Ran: (exit code, or None if it
# was still running when stopped at ``until`` or at its timeout; output)
# ``until`` is looked for in each line of the output as a terminal shows it.
Runner = Callable[..., Ran]


class _Kept:
    """Output kept up to MAX_CAPTURE_BYTES, from its start or, with ``tail``, its end."""

    def __init__(self, tail: bool = False):
        self.data = bytearray()
        self.dropped = 0
        self.tail = tail

    def add(self, chunk: bytes) -> None:
        if self.tail:
            self.data += chunk
            over = len(self.data) - MAX_CAPTURE_BYTES
            if over > 0:
                del self.data[:over]
                self.dropped += over
        else:
            room = max(0, MAX_CAPTURE_BYTES - len(self.data))
            self.data += chunk[:room]
            self.dropped += len(chunk) - min(room, len(chunk))

    def finish(self) -> None:
        """Leave out, and count, the line the cap cut, where the start is kept."""
        if self.dropped and not self.tail:
            keep = self.data.rfind(b"\n") + 1
            self.dropped += len(self.data) - keep
            del self.data[keep:]

    def text(self) -> str:
        return self.data.decode(errors="replace")


def _read(
    proc: subprocess.Popen,
    fds: list[int],
    take: Callable[[int, bytes], bool],
    deadline: float | None,
) -> None:
    """Read what ``proc`` writes to ``fds``, handing each piece to ``take``, until every
    one has ended, ``take`` says to stop, or ``deadline``; once ``proc`` has exited,
    only for up to DRAIN_SECONDS more, until nothing more comes, since something it
    started may hold them open and write on."""
    with selectors.DefaultSelector() as selector:
        for fd in fds:
            selector.register(fd, selectors.EVENT_READ)
        drained = None  # when to stop, once proc has exited
        while selector.get_map():
            ends = [end for end in (deadline, drained) if end is not None]
            left = min(ends) - time.monotonic() if ends else None
            if left is not None and left <= 0:
                return
            wait = POLL_SECONDS if drained else 0.2
            events = selector.select(wait if left is None else min(wait, left))
            for key, _ in events:
                try:
                    data = os.read(key.fd, READ_SIZE)
                except OSError:  # a terminal whose every writer has closed it
                    data = b""
                if not data:
                    selector.unregister(key.fd)
                elif take(key.fd, data):
                    return
            if drained is not None and not events:
                return
            if drained is None and _exited(proc):
                drained = time.monotonic() + DRAIN_SECONDS
    if deadline is None:  # every stream ended: wait for it to exit, without reaping it
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    while not _exited(proc) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)


def run_in_pty(
    argv: list[str], env: dict[str, str], cwd: Path, until: str | None, timeout: float
) -> Ran:
    """Run ``argv`` on a pseudo-terminal of the size rcman gives Claude's screens; stop
    at ``until``, at ``timeout``, or once it has exited and what it wrote last is read
    (see _read). Output as the terminal shows it, from at most MAX_CAPTURE_BYTES of what
    it wrote."""
    pattern = re.compile(until) if until else None
    master, slave = os.openpty()
    try:
        size = struct.pack("HHHH", screens.HEIGHT, screens.WIDTH, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, start_new_session=True
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    screen = Screen()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    raw: list[str] = []
    kept = dropped = 0

    def take(_fd: int, data: bytes) -> bool:
        nonlocal kept, dropped
        piece = data[: max(0, MAX_CAPTURE_BYTES - kept)]
        kept += len(piece)
        dropped += len(data) - len(piece)
        if not piece:
            return False
        first = len(screen.lines) - 1  # the lines this piece can change
        text = decoder.decode(piece)
        raw.append(text)
        screen.feed(text)
        return bool(pattern) and any(
            pattern.search(screen.line(i)) for i in range(first, len(screen.lines))
        )

    try:
        _read(proc, [master], take, time.monotonic() + timeout)
    finally:
        exit_code = _end_group(proc)
        os.close(master)
    screen.feed(decoder.decode(b"", final=True), final=True)
    output = screen.text()
    if dropped:  # the line the cap cut is left out, and counted
        keep = output.rfind("\n") + 1
        dropped += len(output[keep:].encode())
        output = output[:keep].removesuffix("\n")
    return Ran(exit_code, output, output, "", dropped, scrub("".join(raw)))


def _exited(proc: subprocess.Popen) -> bool:
    """Whether ``proc`` has exited. It is not reaped, so its id, which is its process
    group's, cannot be another's before _end_group ends the group."""
    if proc.returncode is not None:
        return True
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def _end_group(proc: subprocess.Popen) -> int | None:
    """End everything ``proc`` started, which may outlive it holding its output open,
    then reap it; ``proc``'s own exit code, or None where it was still running."""
    if proc.returncode is not None:  # reaped already: its id may be another's now
        return proc.returncode
    exited = _exited(proc)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:  # nothing of it was left
        pass
    proc.wait()
    return proc.returncode if exited else None


def capture(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    timeout: float | None = None,
    merge: bool = False,
    tail: bool = False,
) -> Ran:
    """Run ``argv`` with no terminal and stdin empty, keeping at most MAX_CAPTURE_BYTES of
    each stream it writes: its first, or with ``tail`` its last. With ``merge``, standard
    error is written into standard output, in the order the program wrote them. A program
    still running at ``timeout`` has None for its exit code. Everything it started is
    ended when it is done or at ``timeout``, even if it is still holding the streams; once
    it has exited, they are read only until they go quiet (see _read)."""
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
        start_new_session=True,
    )
    out, err = _Kept(tail), None if merge else _Kept(tail)
    streams = {proc.stdout.fileno(): out}
    if err is not None:
        streams[proc.stderr.fileno()] = err

    def take(fd: int, data: bytes) -> bool:
        streams[fd].add(data)
        return False

    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        _read(proc, list(streams), take, deadline)
    finally:
        exit_code = _end_group(proc)
        proc.stdout.close()
        if not merge:
            proc.stderr.close()
    for kept in streams.values():
        kept.finish()
    return Ran(
        exit_code,
        out.text() if exit_code is not None else "",
        out.text(),
        err.text() if err else "",
        out.dropped + (err.dropped if err else 0),
    )


def run_command(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    until: str | None,
    timeout: float,
    terminal: bool = True,
) -> Ran:
    """Run ``argv`` on a terminal, or through pipes where ``terminal`` is false."""
    if terminal:
        return run_in_pty(argv, env, cwd, until, timeout)
    # as rcman runs ``auth status``: stdin empty, stdout read
    return capture(argv, env, cwd, timeout)


def local_runner(binary: str, env: dict[str, str], cwd: Path, config_dir: Path) -> Runner:
    """Run ``binary`` with ``CLAUDE_CONFIG_DIR`` set to a fresh ``config_dir``."""

    def run(
        args: list[str], until: str | None = None, timeout: float = TIMEOUT, terminal: bool = True
    ) -> Ran:
        config_dir.mkdir(parents=True, exist_ok=True)
        full_env = {**env, "CLAUDE_CONFIG_DIR": str(config_dir), "DISABLE_AUTOUPDATER": "1"}
        return run_command([binary, *args], full_env, cwd, until, timeout, terminal)

    return run


def _version(run: Runner, version: str) -> bool:
    code, out = run(["--version"])
    return code == 0 and version in out


def _auth_status(run: Runner, version: str) -> bool:
    _code, out = run(["auth", "status"], terminal=False)
    try:
        return json.loads(out.strip()).get("loggedIn") is False
    except (json.JSONDecodeError, AttributeError):
        return False


def _logged_out_refusal(run: Runner, version: str) -> bool:
    args = [
        "remote-control",
        "--remote-control-session-name-prefix",
        "check",
        "--permission-mode",
        "default",
    ]
    code, out = run(args)
    return (
        code not in (None, 0) and screens.classify(out.splitlines(), code)["state"] == "logged_out"
    )


def _sign_in_screen(run: Runner, version: str) -> bool:
    args = ["auth", "login", "--claudeai", "--email", CHECK_EMAIL]
    _code, out = run(args, until=re.escape(screens.LOGIN_PROMPT))
    return screens.classify_login(out.splitlines(), None)["state"] == "awaiting_code"


def _login_options(run: Runner, version: str) -> bool:
    _code, out = run(["auth", "login", "--help"])
    return all(option in out for option in ("--claudeai", "--email"))


LOGGED_OUT_MESSAGE = "logged out"  # "Successfully logged out from your Anthropic account."


def _logout(run: Runner, version: str) -> bool:
    code, out = run(["auth", "logout"])
    return code == 0 and LOGGED_OUT_MESSAGE in out.lower()


CHECKS: list[tuple[str, Callable[[Runner, str], bool]]] = [
    ("version", _version),
    ("auth_status", _auth_status),
    ("logged_out_refusal", _logged_out_refusal),
    ("sign_in_screen", _sign_in_screen),
    ("login_options", _login_options),
    ("logout", _logout),
]


# How much of what a failed check ran and got back is passed on; the rest is counted.
MAX_REPORT_LINES = 60
MAX_REPORT_LINE_LENGTH = 500
CUT = "…"  # ends a line cut at MAX_REPORT_LINE_LENGTH
EARLIER_NOT_SHOWN = "(earlier output not shown)"

# A run of text that can be, or be part of, a URL: everything up to a space,
# a quote or an angle bracket. Runs are judged without regard to a scheme, which
# a terminal can have written over or a program escaped.
RUN = re.compile(r"""[^\s"'<>]+""")
SIGN_IN = re.compile(r"oauth|authorize", re.IGNORECASE)
# A query or fragment parameter: a name and = after ?, & or #.
PARAMETER = re.compile(r"[?&#][A-Za-z0-9_.%-]+=")
# Starting only where a run of the characters before the @ starts keeps it linear.
EMAIL_ADDRESS = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
SESSION_ID = re.compile(r"\bcse_[A-Za-z0-9]+")


def _run(match: re.Match) -> str:
    """A run blanked where it is, or is the rest of, a URL that can carry something
    private, and left as it is otherwise. A sign-in URL, an environment URL and a
    callback carry their codes, states, challenges and ids as parameters, on
    whatever host and path they point at; an address alone has none, and a word
    that names OAuth is not a URL."""
    run = match.group(0)
    if SIGN_IN.search(run) and any(ch in run for ch in "/?=&"):
        return "{login_url}"
    if "environment=" in run:
        return "{environment_url}"
    if PARAMETER.search(run):
        return "{url}"
    return run


def _field(name: str, placeholder: str) -> tuple[re.Pattern, str]:
    """The value of a field whose name matches ``name``, as JSON, a Python dict or
    Node.js prints one: its name in double, single or no quotes, its value in double
    or single quotes; a value cut before its closing quote, to the end of its line."""
    return (
        re.compile(
            rf"""(?P<key>(?:(?P<q>["'])(?:{name})(?P=q)|(?<![A-Za-z0-9_])(?:{name}))\s*:\s*)"""
            rf"""(?P<vq>["'])(?P<value>(?:(?!(?P=vq))[^\\\n]|\\.)*)(?P=vq)?"""
        ),
        rf"\g<key>\g<vq>{placeholder}\g<vq>",
    )


# What is blanked from a report, in order: runs first, so that what a URL holds
# is blanked with it. Each but the runs names what it blanks ``value`` where it is
# less than the whole match.
SECRETS: list[tuple[re.Pattern, str | Callable[[re.Match], str]]] = [
    (RUN, _run),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "{token}"),
    _field(r"[A-Za-z_]*[Tt]oken", "{token}"),
    # NAME=value, as an environment variable is set, for a name ending in token
    (
        re.compile(r"(?<![A-Za-z0-9_])((?i:[A-Za-z0-9_]*token)=)(?P<value>[^\s&\"']+)"),
        r"\1{token}",
    ),
    (re.compile(r"\b(Bearer\s+)(?P<value>[A-Za-z0-9._~+/=-]+)", re.IGNORECASE), r"\1{token}"),
    _field(r"(?i:org|organi[sz]ation)[A-Za-z_]*", "{organisation}"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "{id}",
    ),
    (re.compile(r"(?<![A-Za-z0-9])env_[A-Za-z0-9]+"), "{environment_id}"),
    (SESSION_ID, "{session_id}"),
    (EMAIL_ADDRESS, "{email}"),
]
# A value the program wrote that a screen is searched for (see redact_screen): one
# shaped as a credential — at least MIN_CREDENTIAL_LENGTH of the characters tokens,
# codes and ids are made of, letters and digits both, nothing percent-encoded — so
# that ordinary words, and the scope or address a sign-in URL spells out, are not.
MIN_CREDENTIAL_LENGTH = 16
CREDENTIAL_SHAPE = re.compile(r"(?=[^0-9]*[0-9])(?=[^A-Za-z]*[A-Za-z])[A-Za-z0-9._~+/=-]+")
# More values than this in what a program wrote, and its screen is withheld, not searched.
MAX_CREDENTIALS = 64
CREDENTIAL = "{credential}"
WITHHELD = "(output withheld: it wrote more possible credentials than a report blanks)"


def redact(text: str) -> str:
    """``text`` with every secret ``SECRETS`` names replaced by a placeholder."""
    for pattern, placeholder in SECRETS:
        text = pattern.sub(placeholder, text)
    return text


def _credentials_in(text: str) -> set[str] | None:
    """The values shaped as credentials (CREDENTIAL_SHAPE) in what ``redact`` would blank
    in ``text``: a parameter's in a run, a field's, a variable's, a Bearer credential's,
    and the whole of any other match; None where there are more than MAX_CREDENTIALS."""
    found: set[str] = set()

    def values():
        for match in RUN.finditer(text):
            if _run(match) != match.group(0):
                yield from re.findall(r"=([^&#=\s]+)", match.group(0))
        for pattern, _ in SECRETS[1:]:
            for match in pattern.finditer(text):
                yield match.group("value") if "value" in pattern.groupindex else match.group(0)

    for value in values():
        if len(value) >= MIN_CREDENTIAL_LENGTH and CREDENTIAL_SHAPE.fullmatch(value):
            found.add(value)
            if len(found) > MAX_CREDENTIALS:
                return None
    return found


def redact_screen(text: str, raw: str) -> str:
    """``text``, as a terminal of rcman's width showed what was written to it,
    ``raw``, redacted.

    A line exactly as wide as the terminal ran on to the next, as the program wrapped
    it, and is judged joined to it; it is shown joined only where that blanks
    something. And since what is shown can be what was written with parts of it
    written over, leaving a value no rule recognises on its own, every value shaped
    as a credential that ``redact`` finds in ``raw`` is blanked wherever the screen
    still shows it whole; where there are too many, the screen is withheld."""
    credentials = _credentials_in(raw)
    if credentials is None:
        return WITHHELD
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        j = i
        while len(lines[j]) == screens.WIDTH and j + 1 < len(lines):
            j += 1
        group = lines[i : j + 1]
        if len(group) > 1 and redact(joined := "".join(group)) != joined:
            group = [joined]
        out += group
        i = j + 1
    shown = redact("\n".join(out))
    for credential in sorted(credentials, key=len, reverse=True):
        shown = shown.replace(credential, CREDENTIAL)
    return shown


def _limit(line: str) -> str:
    if len(line) <= MAX_REPORT_LINE_LENGTH:
        return line
    return line[: MAX_REPORT_LINE_LENGTH - len(CUT)] + CUT


def report(
    text: str, lead: tuple[str, ...] = (), tail: bool = False, earlier: bool = False
) -> dict:
    """``text`` as a report: without control characters, redacted, then limited to
    MAX_REPORT_LINES lines, ``lead`` first, each cut to MAX_REPORT_LINE_LENGTH and
    ending in CUT where it was cut; ``more`` counts the lines left out.

    From its start; or, with ``tail``, from its end, with EARLIER_NOT_SHOWN where
    earlier lines were left out, as they were where ``earlier`` says ``text`` is
    itself the end of something longer, whose first line is then only part of one."""
    lines = redact(scrub(text)).split("\n")
    if lines[-1] == "":
        lines.pop()
    lead = [redact(scrub(line)) for line in lead]
    room = MAX_REPORT_LINES - len(lead)
    more = 0
    if tail:
        if earlier and lines:
            lines.pop(0)
            more += 1
        if earlier or len(lines) > room:
            keep = lines[max(0, len(lines) - (room - 1)) :]
            more += len(lines) - len(keep)
            lines = [EARLIER_NOT_SHOWN, *keep]
    else:
        more = max(0, len(lines) - room)
        lines = lines[:room]
    return {"lines": [_limit(line) for line in [*lead, *lines]], "more": more}


def _describe(args: list[str], terminal: bool, ran: Ran) -> str:
    code, output = ran
    how = "on a terminal" if terminal else "through pipes"
    if code is not None:
        ended = f"exit code {code}"
    elif terminal:
        ended = "stopped before it exited"
    else:
        ended = "did not exit before its timeout"
    lines = [f"ran: claude {' '.join(args)} ({how}); {ended}"]
    if terminal:
        lines += ["output:", redact_screen(output, ran.raw)]
    else:
        lines += ["stdout:", ran.stdout]
    if ran.stderr:
        lines += ["stderr:", ran.stderr]
    if ran.dropped:
        lines += [f"(and {ran.dropped} more bytes of output, not kept)"]
    return "\n".join(lines)


def _failure(run: Runner, check: Callable[[Runner, str], bool], version: str) -> dict | None:
    """What was run and what came back, as a report, where ``check`` fails; None where
    it passes."""
    runs: list[str] = []

    def recording(args, until=None, timeout=TIMEOUT, terminal=True):
        ran = run(args, until=until, timeout=timeout, terminal=terminal)
        runs.append(_describe(args, terminal, ran))
        return ran

    try:
        if check(recording, version):
            return None
    except Exception as error:  # noqa: BLE001 - a check that cannot run has failed
        runs.append(f"raised: {error!r}")
    return report("\n".join(runs))


def candidate_failures(
    run: Runner, version: str, on_check: Callable[[str], None] | None = None
) -> dict[str, dict]:
    """The checks ``version`` fails, each with its report of what was run and what came
    back (see ``report``); empty when it passes them all."""
    failed = {}
    for name, check in CHECKS:
        if on_check:
            on_check(name)
        entry = _failure(run, check, version)
        if entry is not None:
            failed[name] = entry
    return failed
