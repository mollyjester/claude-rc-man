"""The devcontainer builder: what it is run with, what it is asked, how its words are read, and
the pump that copies bytes between two commands.

A repository's devcontainer.json may ask for an image to be built (a Dockerfile, features). That
is done by the devcontainer CLI in a *builder*, a container of its own with a Podman inside,
started for one build in a uid range no person has, given a copy of the repository and nothing
else of the machine's, and removed when the build is over (runtime.py ``build_image``). This
module holds the parts that need no Podman and so are tried on the host: the arguments of each
command, the reading of the CLI's standard error (progress for the person, the end of it for a
failure, and the cause in words), and the byte pump that copies the repository in and the image
out.

Python standard library only, and nothing of the backend: runtime.py imports it.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import posixpath
import re
import subprocess
import tarfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

# --- what the builder is run with ----------------------------------------------------------------

BUILDER_PREFIX = "rcman-build-"
# Where the builder keeps its images and layers: its own volume, so that a build does not touch
# anything of rcman's.
STORAGE_MOUNT = "/var/lib/containers"
# Where the repository is in the builder, and the PATH the command is run with there.
REPOSITORY_FOLDER = "/work/repo"
BUILDER_PATH = "/usr/local/bin:/usr/bin:/bin"
# What the image is called in the builder until it is copied out: a name no person's image has.
IN_BUILDER_IMAGE = "localhost/rcman-in-builder:image"
# How the image is saved to be copied out. docker-archive is not compressed, and was the faster
# of the two formats Podman can load, with the image's id kept across the copy.
ARCHIVE_FORMAT = "docker-archive"


def builder_name(workspace: int) -> str:
    """The builder of the workspace's build; its volume has the same name."""
    return f"{BUILDER_PREFIX}{workspace}"


def run_argv(
    workspace: int,
    labels: Mapping[str, str],
    ids: Sequence[str],
    image: str,
    cgroup_parent: str | None = None,
) -> list[str]:
    """The arguments of ``podman run`` for the builder: detached, in the uid range ``ids`` maps
    (never a person's), with the network of every workspace and the one capability and the one
    device a Podman inside needs; not privileged. The image is never pulled. With
    ``cgroup_parent`` it runs in that systemd slice, which is how the limits of the person who
    asked for the build hold it (limits.py)."""
    name = builder_name(workspace)
    return [
        "run",
        "--detach",
        f"--name={name}",
        *(f"--label={key}={value}" for key, value in labels.items()),
        "--init",
        "--network=slirp4netns:allow_host_loopback=false",
        *ids,
        *([f"--cgroup-parent={cgroup_parent}"] if cgroup_parent else []),
        # Podman gives a container a limit on processes of its own unless it is told there is
        # none; the limit that holds is the slice's TasksMax (limits.py), as for a workspace.
        "--pids-limit=-1",
        "--device=/dev/fuse",
        "--cap-add=SYS_ADMIN",
        "--security-opt=label=disable",
        f"--volume={name}:{STORAGE_MOUNT}",
        "--pull=never",
        image,
        "sleep",
        "infinity",
    ]


def cli_argv(config_path: str) -> list[str]:
    """The devcontainer CLI's build, run in the builder with an environment of nothing but PATH.
    ``config_path`` is the build-only configuration's, relative to the repository. The lockfile
    is not written, so the repository is never changed, and the image is named for the builder's
    own use."""
    return [
        "env",
        "-i",
        f"PATH={BUILDER_PATH}",
        "devcontainer",
        "build",
        "--workspace-folder",
        REPOSITORY_FOLDER,
        "--config",
        f"{REPOSITORY_FOLDER}/{config_path}",
        "--no-lockfile",
        "--docker-path",
        "podman",
        "--image-name",
        IN_BUILDER_IMAGE,
    ]


# Lists the repository's top-level entries but the git folder, and runs the tar given as $2 and
# on with them added: $1 is the repository. Busybox's tar can be told neither to exclude nor to
# read a list, so the entries are named here. Every name is given from "./", so none can be taken
# for an option. A git folder further down is not left out.
SOURCE_SCRIPT = (
    'cd "$1" || exit 1; shift; '
    "for f in .[!.]* ..?* *; do "
    '{ [ -e "$f" ] || [ -L "$f" ]; } || continue; '
    '[ "$f" = .git ] && continue; '
    'set -- "$@" "./$f"; '
    "done; "
    'exec "$@"'
)


def source_argv(shell: str, work: str, tar: Sequence[str]) -> list[str]:
    """The command that writes the repository in ``work`` as a tar archive on its standard
    output, run in the workspace: ``tar`` is the program and the options that create one."""
    return [shell, "-c", SOURCE_SCRIPT, "sh", work, *tar]


def sink_argv(tar: Sequence[str], folder: str) -> list[str]:
    """The command, run in the builder, that unpacks the archive into ``folder``: as the
    builder's own user, whatever the archive says, with members that leave the folder refused
    by GNU tar."""
    return [*tar, "-x", "--no-same-owner", "-C", folder]


# --- limits --------------------------------------------------------------------------------------

# The most of a repository that is copied into the builder, and of an image that is copied out.
MAX_CONTEXT_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 20 * 1024**3
# How long a build may take, and how long each copy may take.
BUILD_SECONDS = 3600.0
COPY_SECONDS = 3600.0
# How often a running build is looked at for a stop, and how many bytes the pump moves at once.
BUILD_POLL_SECONDS = 1.0
CHUNK_BYTES = 1024 * 1024

# The most of an image's description (``podman image inspect``) that is read from the builder,
# the most of the CLI's last line of output that is held, and, of an archive of an image, the
# most members it may have and the most of its manifest and its configuration that are read.
MAX_INSPECT_BYTES = 4 * 1024 * 1024
MAX_OUTCOME_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# An image's id: the SHA-256 of its configuration, written out. Nothing else is passed to podman
# as an id, so one cannot be taken for an option or for a name.
IMAGE_ID = re.compile(r"[0-9a-f]{64}\Z")

# --- the builder's words -------------------------------------------------------------------------

# How often a step of the build is reported to the person, and how many of the last lines of what
# the builder said are kept for a failure, how long a line may be, how much of a line that has
# not ended is held, and the most of what a command says that is kept.
PROGRESS_SECONDS = 5.0
TAIL_LINES = 40
MAX_LINE_CHARS = 300
MAX_LINE_BYTES = 64 * 1024
SAID_BYTES = 4096
MAX_FEATURE_CHARS = 300

STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+Z\] ")
# Podman's own marker of a step of the build, which is the one thing worth telling a person.
STEP = re.compile(r"^\[\d+/\d+\] STEP \d+/\d+:")
LINE_END = re.compile(rb"[\r\n]+")


def _cut(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "..."


class ProgressLines:
    """The CLI's standard error, fed in as it arrives. It is line by line, the lines ending in a
    carriage return and a newline, each led by the time, and some - apt's progress - arrive in
    pieces. The last ``keep`` lines are kept (``tail``); Podman's markers of a step are passed
    to ``report`` at most once in ``interval`` seconds by ``clock``, the latest one held back
    being passed on later or at ``close``."""

    def __init__(
        self,
        report: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        *,
        keep: int = TAIL_LINES,
        interval: float = PROGRESS_SECONDS,
    ):
        self._report, self._clock, self._interval = report, clock, interval
        self._lines: deque[str] = deque(maxlen=keep)
        self._partial = b""
        self._last: float | None = None
        self._waiting: str | None = None

    def feed(self, chunk: bytes) -> None:
        *whole, self._partial = LINE_END.split(self._partial + chunk)
        for raw in whole:
            self._line(raw)
        if len(self._partial) > MAX_LINE_BYTES:  # a line that never ends is cut where it is
            self._line(self._partial)
            self._partial = b""
        self._release()

    def close(self) -> None:
        """No more is coming: what was left of a line is a line, and a step held back is
        reported."""
        if self._partial:
            self._line(self._partial)
            self._partial = b""
        self._release(final=True)

    def tail(self) -> str:
        return "\n".join(self._lines)

    def _line(self, raw: bytes) -> None:
        line = STAMP.sub("", raw.decode("utf-8", "replace")).strip()
        if not line:
            return
        line = _cut(line)
        self._lines.append(line)
        if STEP.match(line):
            self._waiting = line
            self._release()

    def _release(self, final: bool = False) -> None:
        """Pass on the step that waits, if the interval has passed since the last, or at the end."""
        if self._waiting is None:
            return
        now = self._clock()
        if final or self._last is None or now - self._last >= self._interval:
            line, self._waiting, self._last = self._waiting, None, now
            self._report(line)


class Cause(NamedTuple):
    """Why a build failed, in the words of ``problems.DEVCONTAINER_MESSAGES``: the kind, and the
    fields that fill the message in."""

    kind: str
    fields: dict


FEATURE_FAILED = re.compile(r'ERROR: Feature "([^"\n]*)"[^\n]*failed to install')
# What the CLI says when it cannot fetch its control manifest, which it goes on without: said in
# builds that fail for another reason, so not a cause by itself.
IGNORED_LINE = "control manifest"
# The words, lower case, by which a failure's cause is recognised, in the order they are looked
# for: a registry's refusal, no network, an image or a feature that is not there, a full disk,
# and last a step of the build that failed.
CAUSES = (
    (
        "build_sign_in",
        (
            "requested access to the resource is denied",
            "unauthorized:",
            "authentication required",
            "insufficient_scope",
            "returned error: 401",
        ),
    ),
    (
        "build_registry_unreachable",
        (
            "eai_again",
            "getaddrinfo",
            "no route to host",
            "network is unreachable",
            "i/o timeout",
            "connection refused",
            "tls handshake timeout",
            "temporary failure in name resolution",
            "dial tcp",
        ),
    ),
    (
        "build_image_missing",
        (
            "no manifest found",
            "image not known",
            "manifest unknown",
            "name unknown",
            "repository does not exist",
        ),
    ),
    ("build_no_space", ("no space left on device",)),
    ("build_step_failed", ("error: building at step", "while running runtime: exit status")),
)
KINDS = ("build_feature_failed", *(kind for kind, _ in CAUSES))


def explain(said: str) -> Cause | None:
    """The cause the end of what the builder said shows, or None. A feature that failed to
    install is named, and comes first: it ends in a failing step too."""
    kept = "\n".join(line for line in said.splitlines() if IGNORED_LINE not in line)
    named = FEATURE_FAILED.search(kept)
    if named:
        return Cause("build_feature_failed", {"feature": named.group(1)[:MAX_FEATURE_CHARS]})
    text = kept.lower()
    for kind, patterns in CAUSES:
        if any(pattern in text for pattern in patterns):
            return Cause(kind, {})
    return None


# --- the pump -----------------------------------------------------------------------------

# Why a pump was stopped.
CANCELLED, TIMED_OUT, TOO_BIG = "cancelled", "timeout", "too_big"
WATCH_SECONDS = 0.1


class PumpStopped(Exception):
    """The pump ended both commands before they were done: ``reason`` is CANCELLED, TIMED_OUT or
    TOO_BIG, and ``sent`` the bytes that had passed."""

    def __init__(self, reason: str, sent: int):
        self.reason, self.sent = reason, sent
        super().__init__(f"the copy was stopped ({reason}) after {sent} bytes")


@dataclass(frozen=True)
class Pumped:
    """How a pump ended: the bytes that passed, both commands' exit codes, what the source said
    on its error stream and the sink on its streams (the last SAID_BYTES of each)."""

    sent: int
    source_code: int
    sink_code: int
    source_err: bytes = field(default=b"", repr=False)
    sink_out: bytes = field(default=b"", repr=False)
    sink_err: bytes = field(default=b"", repr=False)


def _drain(stream, tail: bytearray) -> None:
    while chunk := stream.read1(65536):
        tail += chunk
        if len(tail) > SAID_BYTES:
            del tail[: len(tail) - SAID_BYTES]


def pump(
    source: Sequence[str],
    sink: Sequence[str],
    *,
    env: Mapping[str, str],
    limit: int | None,
    cancel: threading.Event | None,
    seconds: float,
    progress: Callable[[int], None] | None = None,
) -> Pumped:
    """Run ``source`` and ``sink`` with exactly ``env`` and copy the source's standard output
    into the sink's standard input, counting the bytes (``progress`` is told the count so far).
    A watchdog ends both commands when ``cancel`` is set, when ``seconds`` have passed, and the
    pump itself when more than ``limit`` bytes have passed: each is a :class:`PumpStopped`.
    Otherwise both commands' ends are returned, whatever their codes; a sink that ends early
    ends the source with it."""
    if cancel is not None and cancel.is_set():
        raise PumpStopped(CANCELLED, 0)
    source_proc = subprocess.Popen(
        list(source),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
    )
    try:
        sink_proc = subprocess.Popen(
            list(sink),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
        )
    except BaseException:
        source_proc.kill()
        source_proc.wait()
        raise
    tails = {name: bytearray() for name in ("source_err", "sink_out", "sink_err")}
    readers = [
        threading.Thread(
            target=_drain, args=(source_proc.stderr, tails["source_err"]), daemon=True
        ),
        threading.Thread(target=_drain, args=(sink_proc.stdout, tails["sink_out"]), daemon=True),
        threading.Thread(target=_drain, args=(sink_proc.stderr, tails["sink_err"]), daemon=True),
    ]
    for reader in readers:
        reader.start()
    stopped: list[str] = []
    finished = threading.Event()
    deadline = time.monotonic() + seconds

    def stop(reason: str) -> None:
        if not stopped:
            stopped.append(reason)
        for proc in (source_proc, sink_proc):
            with contextlib.suppress(OSError):
                proc.kill()

    def watch() -> None:
        while not finished.wait(WATCH_SECONDS):
            if cancel is not None and cancel.is_set():
                stop(CANCELLED)
            elif time.monotonic() > deadline:
                stop(TIMED_OUT)

    watchdog = threading.Thread(target=watch, daemon=True)
    watchdog.start()
    sent = 0
    try:
        while chunk := source_proc.stdout.read1(CHUNK_BYTES):
            try:
                sink_proc.stdin.write(chunk)
            except OSError:  # the sink has ended: what it said is why
                with contextlib.suppress(OSError):
                    source_proc.kill()
                break
            sent += len(chunk)
            if progress is not None:
                progress(sent)
            if limit is not None and sent > limit:
                stop(TOO_BIG)
                break
        with contextlib.suppress(OSError):
            sink_proc.stdin.close()
        source_code, sink_code = source_proc.wait(), sink_proc.wait()
    finally:
        finished.set()
        for proc in (source_proc, sink_proc):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        watchdog.join()
        for reader in readers:
            reader.join()
        for stream in (source_proc.stdout, source_proc.stderr, sink_proc.stdout, sink_proc.stderr):
            stream.close()
        with contextlib.suppress(OSError):
            sink_proc.stdin.close()
    if stopped:
        raise PumpStopped(stopped[0], sent)
    return Pumped(
        sent,
        source_code,
        sink_code,
        bytes(tails["source_err"]),
        bytes(tails["sink_out"]),
        bytes(tails["sink_err"]),
    )


# --- the archive of an image ---------------------------------------------------------------------


class ArchiveRefused(Exception):
    """The archive of an image is not the one image that was asked for, kept by its id and under
    no name; the message says what is wrong, in plain words."""


def is_image_id(value) -> bool:
    """Whether ``value`` is a whole image id."""
    return isinstance(value, str) and IMAGE_ID.match(value) is not None


def _json(raw: bytes, what: str):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ArchiveRefused(f"its {what} is not readable") from None


def _member_bytes(
    archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int, what: str
) -> bytes:
    if not member.isfile():
        raise ArchiveRefused(f"its {what} is not a file")
    if member.size > limit:
        raise ArchiveRefused(f"its {what} is too big")
    reader = archive.extractfile(member)  # a read of the archive's own bytes, not an extraction
    return reader.read(limit + 1) if reader is not None else b""


# The members of an OCI layout. Podman reads an archive as an OCI layout before it tries a
# docker-archive, and applies the names the layout's index carries, so an archive that has any
# of these is not the one image rcman asked to have kept by its id, whatever else it holds.
OCI_MEMBERS = ("index.json", "oci-layout", "blobs")
# How a member of the fresh archive is named, and the mode, owner and time it is given.
MANIFEST_NAME = "manifest.json"
FRESH_MODE = 0o644
# The fields of a tar header the size is read from (see ``_plain_size``), and its block.
SIZE_FIELD = slice(124, 136)
HEADER_BLOCK = 512


class Verified(NamedTuple):
    """What an archive that was accepted holds that is kept: the names, as the archive has
    them, of the configuration and of each layer in the manifest's order."""

    config: str
    layers: list[str]
    diff_ids: list[str]  # the digests of the layers' bytes, as the configuration lists them


def _plain_size(raw: bytes, member: tarfile.TarInfo) -> bool:
    """Whether the header ``raw`` of ``member`` has a size field every tar reads as Python did:
    octal digits and the padding that ends it, nothing before the digits but spaces. A leading
    NUL is read as no size by Python's tar and as the number by Go's, and a binary size is for
    the sizes the plain field cannot hold, which an extended header carries; either way what is
    taken for the end of the member differs."""
    field = raw[SIZE_FIELD]
    digits = field.rstrip(b"\0 ").lstrip(b" ")
    if not digits or not all(chr(c) in "01234567" for c in digits):
        return False
    return field[-1:] in (b"\0", b" ") and int(digits, 8) == member.size


def verify_archive(path, image_id: str) -> Verified:
    """Hold the docker-archive at ``path``, as ``podman save`` wrote it for the image ``image_id``,
    to what rcman accepts before it makes the archive that is loaded (``repack_archive``): no
    part of an OCI layout (``OCI_MEMBERS``); exactly one image, under no name at all - loading
    applies every name an archive carries, which could put a new image under a name of rcman's
    own - whose configuration, the bytes the id is the digest of, is in the archive, and whose
    layers are in it as files; the header of each of these members plain (``_plain_size``).
    Only the manifest and the configuration are read, within limits, and nothing is extracted,
    so the names of the other members, whatever they are, do nothing. :class:`ArchiveRefused`
    otherwise."""
    if not is_image_id(image_id):
        raise ArchiveRefused("the id of the image is not an id")
    try:
        with tarfile.open(path, "r:") as archive, open(path, "rb") as raw_file:
            members: dict[str, tarfile.TarInfo] = {}
            twice: set[str] = set()  # names of more than one member: which one is read is unclear
            for member in archive:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise ArchiveRefused("it has more members than an image has")
                name = posixpath.normpath(member.name)
                if name.split("/", 1)[0] in OCI_MEMBERS:
                    raise ArchiveRefused("it is also an OCI layout, which names its image")
                if name in members:
                    twice.add(name)
                members.setdefault(name, member)

            def plain(name: str, what: str) -> tarfile.TarInfo:
                member = members.get(name)
                if member is None or not member.isreg() or member.sparse is not None:
                    raise ArchiveRefused(f"its {what} is not in the archive as a file")
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ArchiveRefused(f"its {what} is not in the archive as a file")
                if not member.pax_headers:
                    raw_file.seek(member.offset)
                    if not _plain_size(raw_file.read(HEADER_BLOCK), member):
                        raise ArchiveRefused(f"the header of its {what} is not a plain one")
                return member

            found = members.get(MANIFEST_NAME)
            if found is None:
                raise ArchiveRefused("it has no manifest")
            if MANIFEST_NAME in twice:
                raise ArchiveRefused("it has two manifests")
            listed = _json(
                _member_bytes(archive, found, MAX_MANIFEST_BYTES, "manifest"), "manifest"
            )
            if not isinstance(listed, list) or len(listed) != 1 or not isinstance(listed[0], dict):
                raise ArchiveRefused("it does not hold exactly one image")
            entry = listed[0]
            if entry.get("RepoTags"):
                raise ArchiveRefused("the image has a name, which an image kept by its id has not")
            config = entry.get("Config")
            if not isinstance(config, str) or posixpath.normpath(config) not in members:
                raise ArchiveRefused("its configuration is not in the archive")
            config = posixpath.normpath(config)
            if config in twice:
                raise ArchiveRefused("it has two configurations")
            listed_layers = entry.get("Layers")
            if not isinstance(listed_layers, list) or not all(
                isinstance(layer, str) for layer in listed_layers
            ):
                raise ArchiveRefused("its manifest does not list its layers as names")
            layers = [posixpath.normpath(layer) for layer in listed_layers]
            if MANIFEST_NAME in layers or config in layers:
                raise ArchiveRefused("a layer is the manifest or the configuration")
            plain(MANIFEST_NAME, "manifest")
            plain(config, "configuration")
            for layer in layers:
                plain(layer, "layer")
            raw = _member_bytes(archive, members[config], MAX_CONFIG_BYTES, "configuration")
    except ArchiveRefused:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError):
        raise ArchiveRefused("it is not a readable archive") from None
    if hashlib.sha256(raw).hexdigest() != image_id:
        raise ArchiveRefused(
            "its configuration is not that of the image with the id it was saved by"
        )
    return Verified(config, layers, _diff_ids(raw, len(layers)))


def _diff_ids(raw: bytes, count: int) -> list[str]:
    """The ``rootfs.diff_ids`` of the configuration ``raw``, as many as the manifest lists
    layers; :class:`ArchiveRefused` for a configuration that lists others or none."""
    found = _json(raw, "configuration")
    rootfs = found.get("rootfs") if isinstance(found, dict) else None
    ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) != count:
        raise ArchiveRefused("its configuration does not list the layers its manifest does")
    return ids


# The storage takes a layer it already has by its digest, without reading the archive's bytes
# for it, so the bytes of each layer are held to the digest the configuration lists.
MISMATCHED_LAYER = "the bytes of a layer are not the ones its configuration lists"


class _Piecewise:
    """A source of exactly ``size`` bytes for a copy, read in pieces: asks ``stop`` before each,
    and refuses a source that ends before its size."""

    def __init__(
        self,
        reader,
        size: int,
        stop: Callable[[], bool] | None,
        so_far: Callable[[], int],
        digest=None,
    ):
        self._reader, self._left, self._stop, self._so_far = reader, size, stop, so_far
        self._digest = digest  # a hash fed every byte that passes, if one is given

    def read(self, count: int = CHUNK_BYTES) -> bytes:
        if self._stop is not None and self._stop():
            raise PumpStopped(CANCELLED, self._so_far())
        if self._left <= 0:
            return b""
        try:
            data = self._reader.read(min(count, self._left))
        except (tarfile.TarError, EOFError):
            data = b""
        if not data:
            raise ArchiveRefused("it is cut short")
        self._left -= len(data)
        if self._digest is not None:
            self._digest.update(data)
        return data


def repack_archive(path, image_id: str, target, stop: Callable[[], bool] | None = None) -> None:
    """Write, at ``target``, an archive of the image ``image_id`` that rcman makes itself from
    the parts of the archive at ``path`` that ``verify_archive`` accepted: a manifest of one
    image with no name, the configuration, and each layer once, in plain headers, each member a
    file of the same mode and no owner. Nothing else of ``path`` is carried over, so what Podman
    loads is only that, read the one way; the extra members the original may hold (legacy
    folders of layers, a repositories file, anything else) do nothing. ``stop``, if given, is
    asked between pieces of a layer, and :class:`PumpStopped` (``CANCELLED``) raised when it
    says so. ``target`` is not left behind when this raises."""
    verified = verify_archive(path, image_id)
    distinct = list(dict.fromkeys(verified.layers))
    fresh = {name: f"layer{n}.tar" for n, name in enumerate(distinct)}
    config_name = f"{image_id}.json"
    body = json.dumps(
        [{"Config": config_name, "RepoTags": None, "Layers": [fresh[n] for n in verified.layers]}]
    ).encode()
    expected: dict[str, str] = {}  # each layer's digest; a layer listed twice has one
    for layer, listed in zip(verified.layers, verified.diff_ids, strict=True):
        if expected.setdefault(layer, listed) != listed:
            raise ArchiveRefused(MISMATCHED_LAYER)
    copied = 0
    try:
        with (
            tarfile.open(path, "r:") as source,
            tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as out,
        ):
            found: dict[str, tarfile.TarInfo] = {}
            for member in source:
                found.setdefault(posixpath.normpath(member.name), member)

            def add(name: str, size: int, reader, digest=None) -> None:
                nonlocal copied
                info = tarfile.TarInfo(name)
                info.size, info.mode = size, FRESH_MODE
                piece = _Piecewise(reader, size, stop, lambda: copied, digest)
                out.addfile(info, piece)
                copied += size

            for name in (verified.config, *distinct):
                member = found[name]
                reader = source.extractfile(member)
                if reader is None:
                    raise ArchiveRefused("it is not a readable archive")
                if name == verified.config:
                    add(config_name, member.size, reader)
                    continue
                digest = hashlib.sha256()
                add(fresh[name], member.size, reader, digest)
                if f"sha256:{digest.hexdigest()}" != expected[name]:
                    raise ArchiveRefused(MISMATCHED_LAYER)
            add(MANIFEST_NAME, len(body), io.BytesIO(body))
    except BaseException:
        with contextlib.suppress(OSError):
            pathlib.Path(target).unlink(missing_ok=True)
        raise
