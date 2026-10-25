"""What relays to a workspace's forwarded ports leave behind, ended.

A relay (``Runtime.port_argv``, runtime.py RELAY_SCRIPT) ends by itself once either of its sides
ends, and writes its shell's id to a pidfile of its own in the workspace's runtime folder
(``runtime.forward_pidfile``), which it removes as it ends. ``sweep`` is the backstop for a relay
that is left anyway, and for the pidfile of one that was killed: the
workspaces call it, through the workspace's channel, when a workspace is deleted or rebuilt,
and when rcman's restore finds its container running or starts it
(``Workspaces.sweep_forwards``); and the forwarding server calls it, through the same, for every
workspace it started a relay in, as it stops (forward.py).

A sweep is two fixed scripts, one run each, whatever the folder holds. The first reads: the
pidfiles that are not empty, at most MAX_PIDFILES of them, each by its first bytes alone, and
removes at once those beyond the bound and those that are links; every process's stat; and the
argv of each process a pidfile names, once. The second ends and removes: each process it is
given is sent SIGKILL only while it is still the one that was read, by its id and start time,
and the pidfiles that were read are removed. A pidfile is acted on only while the process it
names has exactly the argv of the relay that writes it (``is_relay``); then that shell and every
process descended from it are ended by id, and nothing else, no process group among them. A
pidfile naming anything else, or no process, signals nothing, and is removed; an empty one,
which a relay that has just started has not written yet, is left.
Python standard library and the backend only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from rcman import runtime as rt
from rcman.runtime import Layout, RuntimeFailed

log = logging.getLogger("rcman.forwards")
log.setLevel(logging.INFO)

Run = Callable[..., object]

# A pidfile is the relay's shell's id, as ``echo $$`` writes it: the most of it that is read,
# one byte more than the longest id and its newline, so that a longer one reads as none.
MAX_PIDFILE_BYTES = 12
MAX_PID = 1 << 22  # the most the kernel gives (PID_MAX_LIMIT)
PID = re.compile(r"[1-9][0-9]{0,9}\Z")
PORT = re.compile(r"[1-9][0-9]{0,4}\Z")
# The most pidfiles one sweep reads: what a workspace's own processes write in its runtime
# folder cannot make a sweep's work unbounded. Those beyond it are removed unread; a relay one
# of them named ends when either of its sides does.
MAX_PIDFILES = 64
# The most of a process's argv that is read: a relay's is far shorter.
MAX_CMDLINE_BYTES = 4096
# The pidfiles of relays, as a pattern of the shell's: the names FORWARD_PIDFILE takes.
_PIDFILES = "forward-" + "[0-9a-f]" * 16 + ".pid"

# The first script: $1 the runtime folder, $2 MAX_PIDFILES. For each pidfile that is a file, not
# a link and not empty, up to the bound, "P", its path and the id it holds if it holds exactly
# an id and a newline, as ``echo $$`` writes one (the shell drops a NUL from what it reads,
# which is harmless: the id is held to the relay's exact argv all the same), else "-"; links and
# those beyond the bound are removed, each as it is met, and empty ones left uncounted. Then "S"
# and every process's stat, as its path and its content; then "C" and, for each id read, the id
# and its argv, at most MAX_CMDLINE_BYTES of it, read once and written in hexadecimal by ``od``
# in the same pipe, so that nothing the workspace writes can stand for it and no length has to
# agree with it. Every field is followed by a NUL.
_READ = " ".join(
    [
        "d=$1 most=$2 n=0 pids=; nl=$(printf '\\n.'); nl=${nl%.};",
        f'for f in "$d"/{_PIDFILES}; do',
        'if [ -L "$f" ]; then @RM@ -f -- "$f"; continue; fi;',
        '[ -f "$f" ] && [ -s "$f" ] || continue;',
        "n=$((n + 1));",
        'if [ "$n" -gt "$most" ]; then @RM@ -f -- "$f"; continue; fi;',
        f'c=$(@HEAD@ -c {MAX_PIDFILE_BYTES} "$f" 2>/dev/null; echo .); c=${{c%.}}; v=${{c%"$nl"}};',
        'case $v in ""|*[!0-9]*) v=- ;; *) [ "$v$nl" = "$c" ] && pids="$pids $v" || v=- ;; esac;',
        'printf \'P\\0%s\\0%s\\0\' "$f" "$v";',
        "done;",
        "printf 'S\\0';",
        'for s in /proc/[0-9]*/stat; do printf "%s\\0" "$s"; @CAT@ "$s" 2>/dev/null;'
        ' printf "\\0"; done;',
        "printf 'C\\0';",
        "for p in $pids; do",
        "printf '%s\\0' \"$p\";",
        f'@HEAD@ -c {MAX_CMDLINE_BYTES} "/proc/$p/cmdline" 2>/dev/null | @OD@ -An -v -tx1;',
        "printf '\\0';",
        "done",
    ]
)
# The second script: pairs of a process's id and start time, then "--", then the pidfiles to
# remove. Each process still there with that start time is sent SIGKILL, all in one, and the
# pidfiles are removed.
_END = " ".join(
    [
        'start_of() { set -- $1; echo "${20}"; };',
        "ended=;",
        'while [ "$#" -gt 1 ] && [ "$1" != -- ]; do',
        'if read -r stat 2>/dev/null <"/proc/$1/stat"'
        ' && [ "$(start_of "${stat##*) }")" = "$2" ]; then ended="$ended $1"; fi;',
        "shift 2;",
        "done;",
        '[ "$1" = -- ] && shift;',
        '[ -z "$ended" ] || @KILL@ -KILL $ended;',
        '[ "$#" -eq 0 ] || @RM@ -f -- "$@"',
    ]
)


class Unread(Exception):
    """What the first script printed could not be read."""


def _script(text: str, layout: Layout) -> str:
    for name in ("head", "rm", "cat", "od", "kill"):
        text = text.replace(f"@{name.upper()}@", " ".join(layout.tool(name)))
    return text


def is_relay(layout: Layout, cmdline: bytes, pidfile: str) -> bool:
    """Whether ``cmdline``, a process's ``/proc/<pid>/cmdline``, is exactly the argv of the
    shell of the relay that writes ``pidfile``, to whatever port it names
    (``runtime.relay_shell``)."""
    argv = cmdline.split(b"\0")
    if len(argv) < 2 or argv[-1] != b"":
        return False
    argv = argv[:-1]
    port = argv[-1].decode("ascii", "replace")
    if not PORT.fullmatch(port):
        return False
    try:
        expected = rt.relay_shell(layout, pidfile, int(port))
    except (ValueError, RuntimeFailed):
        return False
    return argv == [part.encode() for part in expected]


def _field(data: bytes, at: int) -> tuple[bytes, int]:
    end = data.find(b"\0", at)
    if end < 0:
        raise Unread("a field has no end")
    return data[at:end], end + 1


def parsed(data: bytes) -> tuple[list[tuple[str, str]], dict[int, tuple[int, int]], dict]:
    """What the first script printed: the pidfiles read, each with what it holds; every
    process, by id, with its parent's id and its start time; and the argv of each id read."""
    pidfiles, table, cmdlines, at = [], {}, {}, 0
    while data.startswith(b"P\0", at):
        path, at = _field(data, at + 2)
        held, at = _field(data, at)
        pidfiles.append((path.decode("utf-8", "replace"), held.decode("ascii", "replace")))
    if not data.startswith(b"S\0", at):
        raise Unread("no processes")
    at += 2
    while not data.startswith(b"C\0", at):
        path, at = _field(data, at)
        stat, at = _field(data, at)
        pid = path.removeprefix(b"/proc/").removesuffix(b"/stat")
        if not pid.isdigit() or not stat.startswith(pid + b" ("):
            continue
        fields = stat.rpartition(b")")[2].split()
        try:
            table[int(pid)] = (int(fields[1]), int(fields[19]))
        except (ValueError, IndexError):
            continue
    at += 2
    while at < len(data):
        pid, at = _field(data, at)
        encoded, at = _field(data, at)
        if not pid.isdigit():
            raise Unread("an argv is not framed")
        try:
            cmdlines[int(pid)] = bytes.fromhex(encoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise Unread("an argv is not hexadecimal") from None
    return pidfiles, table, cmdlines


def descendants(table: dict[int, int], pid: int) -> list[int]:
    """The processes descended from ``pid`` in ``table`` (each process's parent), each once."""
    children: dict[int, list[int]] = {}
    for child, parent in table.items():
        children.setdefault(parent, []).append(child)
    found, pending = [], list(children.get(pid, []))
    while pending:
        child = pending.pop()
        if child in found or child == pid:
            continue
        found.append(child)
        pending.extend(children.get(child, []))
    return sorted(found)


def sweep(layout: Layout, run: Run) -> None:
    """End the relays the workspace's pidfiles name, and remove the pidfiles (see the module's
    docstring). It ends every relay of the workspace, so it is never run while a relay is to
    go on: the forwarding server starts none until the restore at rcman's start, which sweeps,
    is done (forward.py)."""
    read = run([layout.shell, "-c", _script(_READ, layout), "sweep", layout.run,
                str(MAX_PIDFILES)])  # fmt: skip
    try:
        pidfiles, table, cmdlines = parsed(read.out)
    except Unread as unread:
        log.warning("the relays in %s were not read: %s", layout.run, unread)
        return
    parents = {pid: parent for pid, (parent, _) in table.items()}
    ended: list[int] = []
    removed: list[str] = []
    for path, held in pidfiles:
        name = path.rpartition("/")[2]
        if path != f"{layout.run}/{name}" or not rt.FORWARD_PIDFILE.fullmatch(name):
            continue
        if held == "":
            continue  # being written by a relay that has just started
        removed.append(path)
        pid = int(held) if PID.fullmatch(held) else None
        if pid is None or pid > MAX_PID or pid not in table:
            continue
        if is_relay(layout, cmdlines.get(pid, b""), path):
            ended += [p for p in [pid, *descendants(parents, pid)] if p not in ended]
    if not ended and not removed:
        return
    pairs = [str(n) for pid in ended if pid in table for n in (pid, table[pid][1])]
    run([layout.shell, "-c", _script(_END, layout), "end", *pairs, "--", *removed])
    if ended:
        log.info("a relay left in %s was ended", layout.run)
