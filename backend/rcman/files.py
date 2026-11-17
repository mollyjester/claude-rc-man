"""A workspace's files, as the Files tab reaches them: fixed scripts the workspace's shell runs
with ``-c``, through its channel (a path's kind, a listing, an upload's early checks), as an exec
of its own (a folder's scan), or as a stream (runtime.py ``Runtime.stream``: a download, an
upload).

**One resolver.** Every script begins with ``RESOLVE``, in the same process as what it reads or
writes. Its arguments are the volume (``Layout.volume``), the names at its top that are hidden
(``Layout.hidden``) and the path, relative to the volume. The path is resolved by ``realpath``,
links and ``..`` followed, and refused (``REFUSED``) unless it stays in the volume and out of
the hidden folders at its top; what is not there is ``NOT_FOUND``, tested by the script itself,
since ``realpath`` passes a path whose last part is missing. A link is so followed only where
its target stays reachable.

**A folder** goes out as a zip that the workspace's own bsdtar (``Layout.bsdtar``) writes on its
standard output: deflated, each link stored as a link with its target as text and never
followed, each name of a hard link with its bytes, and every name taken as it is. A name is
handed to bsdtar after ``./``, so that none is read as an option or as an archive to take in
(``@``), and ``./`` is taken off the names it stores (``-s``), but not off a link's target (its
``S``). Below the top a folder is zipped under the name it resolved to; the top by naming each
of its entries but the hidden ones. Before a folder downloads, ``SCAN`` looks through it with
stats only, reading no file's bytes: the first path the workspace cannot read, and, with a
limit, the folder's size.

**A file** comes in through a stream of ``UPLOAD``: written to a temporary file of its own beside
where it goes (``TEMPORARY_PREFIX``), then linked to its name, which fails where the name is
taken, so that no file is replaced; or, with ``overwrite``, renamed over the file of that name,
which never writes through a link. A stream ended before that has its temporary file removed by
its end (``Stream.end``). A name hidden at the top, and the name of a folder or a link, are
refused.

Python standard library and the backend's runtime only.
"""

from __future__ import annotations

import secrets
import shlex
import stat
from dataclasses import dataclass

from rcman.runtime import Layout

# What an upload's temporary file is called, in the folder the file goes to, before its token.
TEMPORARY_PREFIX = ".rcman-upload-"
# How many entries a listing holds at most: folders first, then the rest.
LISTING_CAP = 500

# The exit codes the scripts refuse with, beside their programs' own.
REFUSED = 10  # outside the volume, or in a hidden folder at its top
NOT_FOUND = 11  # nothing is there
NOT_A_FOLDER = 12
EXISTS = 13  # a file of the upload's name is there, and no overwrite was asked for
TAKEN = 14  # a folder, a link or something else than a file has the upload's name
NOT_A_FILE = 15  # neither a regular file nor a folder

# The resolver, the head of every script: $1 the volume, $2 the hidden names as one word, each
# after a slash and the last one before one too (``hidden_word``), $3 the path; the rest are
# the script's own. It leaves the path resolved in $p.
RESOLVE = """volume=$1 hidden=$2 path=$3;
shift 3;
reachable() {
  case $1 in
    "$volume") return 0 ;;
    "$volume"/*) top=${1#"$volume"/}; top=${top%%/*} ;;
    *) return 1 ;;
  esac;
  case $hidden in */"$top"/*) return 1 ;; esac;
};
volume=$(@REALPATH@ "$volume") || exit 1;
p=$(@REALPATH@ "$volume/$path" 2>/dev/null) || exit 11;
reachable "$p" || exit 10;
[ -e "$p" ] || [ -L "$p" ] || exit 11;
"""

# A path's kind (folder, file, other), the volume and where the path resolved to, and a file's
# size, a NUL after each but the last.
KIND = (
    RESOLVE
    + """size=;
if [ -d "$p" ]; then kind=folder;
elif [ -f "$p" ]; then kind=file; size=$(@STAT@ -c %s "$p") || exit 1;
else kind=other;
fi;
printf '%s\\0%s\\0%s\\0%s' "$kind" "$volume" "$p" "$size";
"""
)

# A folder's entries, but those hidden at the top: the container's time now, and 1 at the top or
# 0 below it; then, folders first and at most $1 of them, each entry's name, then its raw mode,
# size, time changed and, for a link, what its target is (folder, file, other; out where it
# cannot be reached or is not there), each followed by a NUL; last, how many more were left out.
LIST = (
    RESOLVE
    + """cap=$1;
[ -d "$p" ] || exit 12;
cd "$p" || exit 11;
top=0; [ "$p" = "$volume" ] && top=1;
printf '%s\\0%s\\0' "$(@DATE@ +%s)" "$top";
shown=0 more=0;
for pass in folders others; do
  for e in * .[!.]* ..?*; do
    [ -e "$e" ] || [ -L "$e" ] || continue;
    if [ "$p" = "$volume" ]; then case $hidden in */"$e"/*) continue ;; esac; fi;
    if [ -d "$e" ]; then [ $pass = folders ] || continue; else [ $pass = others ] || continue; fi;
    if [ $shown -ge "$cap" ]; then more=$((more + 1)); continue; fi;
    s=$(@STAT@ -c '%f %s %Y' "./$e" 2>/dev/null) || continue;
    t=-;
    if [ -L "$e" ]; then
      t=out;
      if [ -e "$e" ] && r=$(@REALPATH@ "$p/$e" 2>/dev/null) && reachable "$r"; then
        if [ -d "$e" ]; then t=folder; elif [ -f "$e" ]; then t=file; else t=other; fi;
      fi;
    fi;
    printf '%s\\0%s %s\\0' "$e" "$s" "$t";
    shown=$((shown + 1));
  done;
done;
printf '%s' "$more";
"""
)

# The entries at the top that are not hidden, each as ./name in "$@", from the volume.
_TOP_ENTRIES = """  cd "$volume" || exit 1;
  set --;
  for e in * .[!.]* ..?*; do
    [ -e "$e" ] || [ -L "$e" ] || continue;
    case $hidden in */"$e"/*) continue ;; esac;
    set -- "$@" "./$e";
  done;
"""

# What every zip is written with.
_BSDTAR = "@BSDTAR@ -c -f - --format zip --options zip:compression=deflate -s '|^\\./||S'"

# A folder's zip on standard output: below the top under its own name, so that it unzips to one
# folder of that name; the top as its entries.
ZIP = (
    RESOLVE
    + '[ -d "$p" ] || exit 12;\nif [ "$p" = "$volume" ]; then\n'
    + _TOP_ENTRIES
    + f"""  exec {_BSDTAR} -- "$@";
fi;
parent=${{p%/*}};
exec {_BSDTAR} -C "${{parent:-/}}" -- "./${{p##*/}}";
"""
)

# A file's bytes on standard output.
CAT = RESOLVE + '[ -f "$p" ] || exit 15;\nexec @CAT@ "$p";\n'

# A folder looked through before it downloads, from the volume: $1 the limit in bytes, 0 for
# none, $2 the seconds each of find and du may take, after which it is killed (TIMED_OUT). On
# standard output, each path the workspace cannot read, as ./path from the volume, followed by
# a NUL (find's batches each stop at their first); then, with a limit, how many entries were
# measured and du's line for each. The names reach the test as arguments, never as lines, and
# every test is a stat.
SCAN = (
    RESOLVE
    + 'limit=$1 seconds=$2;\n[ -d "$p" ] || exit 12;\nif [ "$p" = "$volume" ]; then\n'
    + _TOP_ENTRIES
    + """else
  cd "$volume" || exit 1;
  set -- "./${p#"$volume"/}";
fi;
if [ $# = 0 ]; then printf '0\\n'; exit 0; fi;
check='for f; do
  if [ -d "$f" ]; then [ -r "$f" ] && [ -x "$f" ]; else [ -r "$f" ]; fi ||
    { printf "%s\\0" "$f"; exit 1; };
done';
@TIMEOUT@ -s KILL "$seconds" @FIND@ "$@" ! -type l -exec @SH@ -c "$check" sh {} + 2>/dev/null;
case $? in 0|1) ;; 124|137) exit 124 ;; *) exit 1 ;; esac;
[ "$limit" = 0 ] && exit 0;
sizes=$(@TIMEOUT@ -s KILL "$seconds" @DU@ -sb "$@" 2>/dev/null);
case $? in 124|137) exit 124 ;; esac;
printf '%s\\n%s\\n' "$#" "$sizes";
"""
)

# An upload into the folder the path resolved to: $1 the file's name there, $2 the temporary
# file's name, $3 1 to replace a file of that name, $4 1 to check only, writing nothing. A link
# of the name is never followed (``ln -n``, ``mv -T``).
UPLOAD = (
    RESOLVE
    + """name=$1 temporary=$2 overwrite=$3 only_check=$4;
[ -d "$p" ] || exit 12;
case $name in ""|*/*|.|..) exit 10 ;; esac;
if [ "$p" = "$volume" ]; then case $hidden in */"$name"/*) exit 10 ;; esac; fi;
target=$p/$name;
if [ -L "$target" ] || [ -d "$target" ]; then exit 14; fi;
if [ -e "$target" ]; then [ -f "$target" ] || exit 14; [ "$overwrite" = 1 ] || exit 13; fi;
[ "$only_check" = 1 ] && exit 0;
@CAT@ > "$p/$temporary" || exit 1;
if [ "$overwrite" = 1 ]; then exec @MV@ -f -T "$p/$temporary" "$target"; fi;
if ! @LN@ -n "$p/$temporary" "$target" 2>/dev/null; then
  { [ -e "$target" ] || [ -L "$target" ]; } && exit 13;
  exit 1;
fi;
exec @RM@ -f "$p/$temporary";
"""
)

# The layout's programs the scripts name, as @NAME@.
TOOLS = ("cat", "ln", "rm", "mv", "realpath", "stat", "date", "find", "du", "timeout")


# What a line of a script may end with, so that a space may join it to the next.
_JOINS = (";", "then", "do", "else", "{", " in", "||", "&&", "'")


def one_line(script: str) -> str:
    """The script as one line, as a channel's request is; ValueError for a line that a space
    would not join to the next."""
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    for line in lines:
        if not line.endswith(_JOINS):
            raise ValueError(f"a line of a script cannot be joined: {line!r}")
    return " ".join(lines)


def _script(layout: Layout, text: str) -> str:
    """``text`` as one line, with the layout's own programs in place of ``@NAME@``."""
    text = one_line(text)
    for tool in TOOLS:
        text = text.replace(f"@{tool.upper()}@", shlex.join(layout.tool(tool)))
    text = text.replace("@BSDTAR@", shlex.quote(layout.bsdtar))
    return text.replace("@SH@", shlex.quote(layout.shell))


def hidden_word(layout: Layout) -> str:
    """The layout's hidden names as the resolver takes them: ``/claude/home/``."""
    return "/" + "".join(f"{name}/" for name in layout.hidden)


def _argv(layout: Layout, script: str, path: str, *rest: str) -> list[str]:
    head = [layout.shell, "-c", _script(layout, script), "rcman-files"]
    return [*head, layout.volume, hidden_word(layout), path, *rest]


def kind_argv(layout: Layout, path: str) -> list[str]:
    """What prints the kind of ``path`` and where it resolved to (``KIND``)."""
    return _argv(layout, KIND, path)


def list_argv(layout: Layout, path: str) -> list[str]:
    return _argv(layout, LIST, path, str(LISTING_CAP))


def zip_argv(layout: Layout, path: str) -> list[str]:
    """What writes the zip of the folder ``path`` on standard output."""
    return _argv(layout, ZIP, path)


def cat_argv(layout: Layout, path: str) -> list[str]:
    """What writes the file ``path`` on standard output."""
    return _argv(layout, CAT, path)


def scan_argv(layout: Layout, path: str, limit_bytes: int, seconds: int) -> list[str]:
    """What looks through the folder ``path`` (``SCAN``), each of its programs for at most
    ``seconds``; a limit of 0 measures nothing."""
    return _argv(layout, SCAN, path, str(limit_bytes), str(seconds))


def upload_argv(
    layout: Layout, path: str, name: str, temporary: str, overwrite: bool, only_check: bool
) -> list[str]:
    """What writes its standard input into the file ``name`` of the folder ``path``, through
    the temporary file ``temporary`` there; with ``only_check``, what only says whether it may."""
    flags = (str(int(overwrite)), str(int(only_check)))
    return _argv(layout, UPLOAD, path, name, temporary, *flags)


def temporary_name() -> str:
    """A new name for an upload's temporary file."""
    return f"{TEMPORARY_PREFIX}{secrets.token_hex(8)}"


@dataclass(frozen=True)
class Kind:
    kind: str  # folder, file or other
    volume: str  # the volume, resolved
    path: str  # where the path resolved to
    size: int | None  # a file's

    @property
    def download_name(self) -> str:
        """What a download of it is saved as: a file by its name, a folder as a zip of its
        name, and the top as rcman.zip."""
        if self.path == self.volume:
            return "rcman.zip"
        name = self.path.rpartition("/")[2]
        return f"{name}.zip" if self.kind == "folder" else name


def parse_kind(out: bytes) -> Kind:
    """What ``KIND`` printed."""
    kind, volume, path, size = (text(field) for field in out.split(b"\0"))
    return Kind(kind, volume, path, int(size) if size else None)


def text(raw: bytes) -> str:
    """A name as the page is given it: what is not UTF-8 replaced."""
    return raw.decode("utf-8", "replace")


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # folder, file, link or other
    size: int | None  # a file's; none for the rest
    changed: int  # seconds since the epoch, by the workspace's clock
    target: str | None  # a link's: folder, file or other; none where it cannot be reached

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder" or (self.kind == "link" and self.target == "folder")


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "folder"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


@dataclass(frozen=True)
class Listing:
    now: int  # by the workspace's clock
    top: bool  # the volume itself
    entries: list[Entry]  # folders first, then the rest, each group by name
    more: int  # how many more there were


def parse_listing(out: bytes) -> Listing:
    """What ``LIST`` printed."""
    now, top, *records, more = out.split(b"\0")
    entries = []
    for name, fields in zip(records[::2], records[1::2], strict=True):
        mode, size, changed, target = fields.split(b" ")
        kind = _kind(int(mode, 16))
        entries.append(
            Entry(
                name=text(name),
                kind=kind,
                size=int(size) if kind == "file" else None,
                changed=int(changed),
                target=None if target in (b"-", b"out") else target.decode(),
            )
        )
    entries.sort(key=lambda e: (not e.is_folder, e.name))
    return Listing(int(now), top == b"1", entries, int(more))


def parse_scan(out: bytes, measured: bool) -> tuple[str | None, int | None]:
    """What ``SCAN`` printed: the first path that cannot be read, from the volume, if any; and
    where it ``measured``, the size du gave. ValueError where du did not give one line for each
    entry, so that a size it could not take never reads as none."""
    *unreadable, sizes = out.split(b"\0")
    first = text(unreadable[0]).removeprefix("./") if unreadable else None
    if not measured or first is not None:
        return first, None
    count, *lines = [line for line in sizes.splitlines() if line.strip()]
    if len(lines) != int(count):
        raise ValueError(f"du measured {len(lines)} of {int(count)} entries")
    return first, sum(int(line.split()[0]) for line in lines)
