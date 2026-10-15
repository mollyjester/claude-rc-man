"""A repository's devcontainer.json: read, checked against rcman's policy, and made into the plan
a workspace is made from.

Nothing here starts anything. ``read`` asks the workspace (through the runner it is given) for
the file; ``parse`` reads JSON with comments; ``check`` holds the file to ``POLICY``, which
says for every key what rcman does about it, before anything is pulled; ``plan`` merges the
file with the metadata the image carries and makes the stored :class:`Plan`.

The file is a repository's text, so it is never echoed: a syntax error gives a line and a
column and nothing else, a link is not followed, and the only values shown are the names of
keys, which are cut short and stripped of control characters. Every refusal is a
:class:`Refused`, whose message is in ``problems.DEVCONTAINER_MESSAGES``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from rcman.channel import Result
from rcman.problems import DEVCONTAINER_MESSAGES
from rcman.runtime import RANGE, Layout

# The two places a devcontainer.json is looked for, in this order, and nowhere else.
FILES = (".devcontainer/devcontainer.json", ".devcontainer.json")
LOOKED_IN = f"devcontainer.json is looked for in {FILES[0]}, else {FILES[1]}, and nowhere else"
# The most of the file rcman reads, in bytes, and how long reading it may take.
MAX_BYTES = 256 * 1024
READ_SECONDS = 30.0
# The label by which an image carries the settings of its features and base image.
METADATA_LABEL = "devcontainer.metadata"
# What the file's own entry is called where an entry has to be named.
CONFIG_ORIGIN = "devcontainer.json"

# What rcman does about a key.
HONOUR, IGNORE, REFUSE = "honour", "ignore", "refuse"
# The longest name of a key that an error shows, the longest image name taken, how deeply values
# may nest, and the most digits a number in an id may have before it is no id of any range.
MAX_SHOWN_CHARS = 100
MAX_IMAGE_CHARS = 300
MAX_DEPTH = 64
MAX_ID_DIGITS = 12
# The environment variables rcman sets itself in a workspace (claude_server.child_env,
# claude_server.tmux_env and Repository.env) that a file may not set. PATH is not among them:
# a file may set it, with rcman's own folder kept in front of it. Nor are LANG, LC_ALL and
# BROWSER, which files commonly set. Each of those is set by rcman in only some of its programs
# (LANG for tmux and Claude, LC_ALL for git, BROWSER for Claude): there rcman's value comes
# after the file's and wins, and everywhere else, lifecycle commands among them, the file's is
# used.
RESERVED_ENV = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TERMINFO",
        "CLAUDE_CONFIG_DIR",
        "DISABLE_AUTOUPDATER",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
        "GIT_CEILING_DIRECTORIES",
    }
)
# The lifecycle commands rcman runs, in the order the specification runs them: the first
# three once, when the workspace is made, and the last each time its container starts.
CREATE_EVENTS = ("onCreateCommand", "updateContentCommand", "postCreateCommand")
START_EVENT = "postStartCommand"
EVENTS = (*CREATE_EVENTS, START_EVENT)
WAIT_FOR = (*EVENTS, "initializeCommand", "postAttachCommand")
ENV_PROBES = ("none", "loginInteractiveShell", "interactiveShell", "loginShell")
# What an environment variable may be called, and what an image may be called.
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# A plain registry reference, [host[:port]/]path[:tag][@digest], as the distribution
# specification writes one: no transport, no file, no path.
_ALNUM, _LOWER, _LETTER = "[A-Za-z0-9]", "[a-z0-9]", "[A-Za-z]"
_LABEL = f"{_ALNUM}(?:[A-Za-z0-9-]*{_ALNUM})?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*"
_COMPONENT = rf"{_LOWER}+(?:(?:\.|_|__|-+){_LOWER}+)*"
_ALGORITHM = rf"{_LETTER}[A-Za-z0-9]*(?:[-_+.]{_LETTER}[A-Za-z0-9]*)*"
IMAGE_NAME = re.compile(
    rf"(?:{_HOST}(?::[0-9]+)?/)?{_COMPONENT}(?:/{_COMPONENT})*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?"
    rf"(?:@{_ALGORITHM}:[0-9A-Fa-f]{{32,}})?\Z"
)
# What podman reads before a colon as how to get an image, which a name that looks like
# name:tag may be mistaken for.
TRANSPORTS = frozenset(
    {
        "docker",
        "docker-archive",
        "docker-daemon",
        "oci",
        "oci-archive",
        "dir",
        "containers-storage",
        "ostree",
        "sif",
        "tarball",
    }
)


@dataclass(frozen=True)
class Policy:
    action: str  # HONOUR, IGNORE or REFUSE
    reason: str  # in plain words: shown to the person, and on the administrators' page


def _honour(reason: str) -> Policy:
    return Policy(HONOUR, reason)


def _ignore(reason: str) -> Policy:
    return Policy(IGNORE, reason)


def _refuse(reason: str) -> Policy:
    return Policy(REFUSE, reason)


_RUN_ONCE = (
    "Run once, as the container's user in the work folder, before the workspace is ready. "
    "A string runs in a shell, an array as a program, and the entries of an object one "
    "after another. A command that takes too long is stopped."
)
_NO_PORTS = "rcman publishes no port of a workspace."
_NO_BUILD = "rcman cannot build images yet; name a ready-made image with image."

# Every key of devcontainer.json, and of an entry of an image's metadata, and what rcman does:
# honour it, ignore it (logged by name), or refuse it, which fails the workspace naming the
# key. A key not here is refused as unknown. A dotted key is a key inside another's object, and
# a key with =false is refused only when set to false. A refused key that asks for nothing -
# false, null, an empty list, object or string - is not refused. tests/test_devcontainer.py
# holds docs/devcontainer.md equal to this table.
POLICY: dict[str, Policy] = {
    "image": _honour(
        "The image is pulled by rcman, and the workspace's container is made from it."
    ),
    "remoteUser": _honour(
        "The user rcman's commands and Claude run as. It must be in the image, and it is "
        "mapped onto the same host user as root has in your other workspaces."
    ),
    "containerUser": _honour("Used as remoteUser when that is not set."),
    "updateRemoteUserUID": _honour(
        "Accepted. rcman always maps the user onto the same host user as root, so no file "
        "needs a new owner."
    ),
    "containerEnv": _honour(
        "Set for the container and for every command rcman runs in the workspace, Claude included."
    ),
    "remoteEnv": _honour(
        "Set for every command rcman runs in the workspace, Claude included; a null value "
        "removes a variable."
    ),
    "init": _honour("Accepted. Every workspace's container runs with an init process."),
    "waitFor": _honour(
        "Accepted. The workspace is ready only after all its create commands have run."
    ),
    "overrideCommand": _honour(
        "Accepted when absent or true. rcman runs its own process in the container."
    ),
    "onCreateCommand": _honour(_RUN_ONCE),
    "updateContentCommand": _honour(_RUN_ONCE),
    "postCreateCommand": _honour(_RUN_ONCE),
    "postStartCommand": _honour(
        "Run as the create commands are, and again each time the workspace's container is "
        "started, before its Claude server is."
    ),
    "userEnvProbe": _honour(
        "Accepted but never run: commands get the environment of the file and the image, "
        "not one probed from a shell."
    ),
    "forwardPorts": _ignore(_NO_PORTS),
    "portsAttributes": _ignore(_NO_PORTS),
    "otherPortsAttributes": _ignore(_NO_PORTS),
    "appPort": _ignore(_NO_PORTS),
    "postAttachCommand": _ignore(
        "Nothing attaches an editor to a workspace, so nothing runs on attach."
    ),
    "customizations": _ignore("Settings of editors and tools, none of which rcman runs."),
    "shutdownAction": _ignore("A workspace is stopped and removed only by rcman and its owner."),
    "secrets": _ignore("rcman gives a workspace no secret a repository asks for."),
    "hostRequirements": _ignore(
        "Requests for the size of the machine, which rcman does not check. A request for a "
        "GPU is refused."
    ),
    "name": _ignore("A label for editors."),
    "$schema": _ignore("Only describes the file."),
    "extensions": _ignore("Settings of editors, from an older form of the file."),
    "settings": _ignore("Settings of editors, from an older form of the file."),
    "id": _ignore("Names a feature in the metadata of an image."),
    "build": _refuse(_NO_BUILD),
    "build.options": _refuse("Options for building an image, which rcman does not do."),
    "dockerFile": _refuse(_NO_BUILD),
    "context": _refuse(_NO_BUILD),
    "features": _refuse(
        "Features need an image to be built, which rcman cannot do yet; name a ready-made "
        "image with image."
    ),
    "overrideFeatureInstallOrder": _refuse("It orders features, which rcman cannot install yet."),
    "mounts": _refuse(
        "A workspace's volume is its only storage; rcman mounts nothing else a repository asks for."
    ),
    "workspaceMount": _refuse("rcman decides where a workspace's files are."),
    "workspaceFolder": _refuse("rcman decides where a workspace's files are."),
    "runArgs": _refuse(
        "Arguments for the container engine could undo the limits rcman puts on a workspace."
    ),
    "initializeCommand": _refuse(
        "It would run on the machine rcman runs on, outside any workspace."
    ),
    "dockerComposeFile": _refuse("Workspaces of several containers are not supported."),
    "service": _refuse("Workspaces of several containers are not supported."),
    "runServices": _refuse("Workspaces of several containers are not supported."),
    "privileged": _refuse("A privileged container would not be kept apart from the machine."),
    "capAdd": _refuse("Extra capabilities would weaken the limits on a workspace."),
    "securityOpt": _refuse(
        "Security options of a repository's choosing would weaken the limits on a workspace."
    ),
    "hostRequirements.gpu": _refuse("A workspace has no GPU."),
    "overrideCommand=false": _refuse(
        "rcman runs its own process in the container, so the image's own command is not kept."
    ),
    "entrypoint": _refuse(
        "An entrypoint of an image or a feature is not run: rcman starts its own process in "
        "the container."
    ),
}


class Refused(Exception):
    """rcman refuses the repository's configuration or the image: ``kind`` is the key of
    ``problems.DEVCONTAINER_MESSAGES`` and ``fields`` fill it in. The message is the one a
    person is shown."""

    def __init__(self, kind: str, **fields):
        self.kind, self.fields = kind, fields
        super().__init__(DEVCONTAINER_MESSAGES[kind].format(**fields))


def _shown(text) -> str:
    """A key as an error shows it: printable, and cut short."""
    cleaned = "".join(c if c.isprintable() else "?" for c in str(text))
    return cleaned if len(cleaned) <= MAX_SHOWN_CHARS else cleaned[:MAX_SHOWN_CHARS] + "..."


# --- reading the file ---------------------------------------------------------------------

# Finds the first of the places that is a file and not a link, and says which. Shell builtins
# only, so it needs nothing of the image. $@ is FILES.
PROBE = (
    'for f in "$@"; do '
    'case "$f" in */*) if [ -L "${f%/*}" ]; then echo "link"; exit 0; fi;; esac; '
    'if [ -L "$f" ]; then echo "link"; exit 0; fi; '
    'if [ -f "$f" ]; then echo "file $f"; exit 0; fi; '
    "done; echo none"
)

Run = Callable[..., Result]
log = logging.getLogger("rcman.devcontainer")
# The most of what a failed step said that the journal gets.
MAX_LOGGED_CHARS = 300


def _unreadable(step: str, result: Result) -> Refused:
    """The refusal for a step of reading that failed, with what the step said on its error
    stream - never what it read - in the journal, since the person's message cannot hold it."""
    said = result.err.decode("utf-8", "replace").strip()[-MAX_LOGGED_CHARS:]
    log.warning("reading devcontainer.json failed at %s: exit code %s: %s", step, result.code, said)
    return Refused("unreadable")


def read(run: Run, layout: Layout) -> str | None:
    """The text of the workspace's devcontainer.json, or None where there is none. ``run`` runs
    an argv in the workspace, in its work folder: ``run(argv, seconds, cwd=...)``. A link, a
    file that is too big, one that cannot be read and one that is not text are :class:`Refused`,
    and nothing of what is in a file reaches a message."""
    probed = run([layout.shell, "-c", PROBE, "sh", *FILES], READ_SECONDS, cwd=layout.work)
    if probed.code != 0:
        raise _unreadable("looking", probed)
    answer = probed.out.decode("utf-8", "replace").strip()
    if answer == "link":
        raise Refused("symlink")
    kind, _, path = answer.partition(" ")
    if kind != "file":
        return None
    sized = run([*layout.tool("wc"), "-c", path], READ_SECONDS, cwd=layout.work)
    try:
        size = int(sized.out.split()[0])
    except (IndexError, ValueError):
        raise _unreadable("measuring", sized) from None
    if sized.code != 0:
        raise _unreadable("measuring", sized)
    if size > MAX_BYTES:
        raise Refused("too_big")
    content = run([*layout.tool("cat"), path], READ_SECONDS, cwd=layout.work)
    if content.code != 0:
        raise _unreadable("reading", content)
    return _text(content.out)


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as failed:
        line = raw.count(b"\n", 0, failed.start) + 1
        column = failed.start - (raw.rfind(b"\n", 0, failed.start) + 1) + 1
        raise Refused("invalid_file", line=line, column=column) from None


def parse_bytes(raw: bytes) -> dict:
    """:func:`parse` of file content that has not been decoded."""
    return parse(_text(raw))


def _blank(text: str) -> str:
    """``text`` as spaces, but for its line breaks: the same length, the same lines."""
    return "".join(c if c in "\r\n" else " " for c in text)


def _position(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    return line, index - (text.rfind("\n", 0, index) + 1) + 1


def strip_jsonc(text: str) -> str:
    """``text`` as JSON: comments blanked and a comma before a closing bracket blanked, outside
    strings only, with every other character where it was, so that a position in the answer is
    one in ``text``."""
    out: list[str] = []
    i, size = 0, len(text)
    in_string = False
    while i < size:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < size:
                out.append(text[i + 1])
                i += 2
                continue
            in_string = c != '"'
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = size if end == -1 else end
            out.append(_blank(text[i:end]))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                line, column = _position(text, i)
                raise Refused("invalid_file", line=line, column=column)
            out.append(_blank(text[i : end + 2]))
            i = end + 2
        else:
            out.append(c)
            i += 1
    return _without_trailing_commas("".join(out))


def _without_trailing_commas(text: str) -> str:
    chars = list(text)
    in_string = False
    i = 0
    while i < len(chars):
        c = chars[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == ",":
            j = i + 1
            while j < len(chars) and chars[j] in " \t\r\n":
                j += 1
            if j < len(chars) and chars[j] in "}]":
                chars[i] = " "
        i += 1
    return "".join(chars)


def parse(text: str) -> dict:
    """The object ``text`` holds, as JSON with comments and trailing commas. A syntax error is
    :class:`Refused` with its line and column and nothing of the text."""
    text = text.removeprefix("﻿")
    cleaned = strip_jsonc(text)
    try:
        found = json.loads(cleaned)
    except json.JSONDecodeError as failed:
        raise Refused("invalid_file", line=failed.lineno, column=failed.colno) from None
    except RecursionError:
        raise Refused("too_deep") from None
    except ValueError:  # a number with more digits than Python reads
        raise Refused("invalid_value") from None
    if not isinstance(found, dict):
        raise Refused("not_an_object")
    _shallow(found)
    return found


def _shallow(value) -> None:
    """:class:`Refused` if ``value`` nests lists and objects more deeply than MAX_DEPTH, found
    without recursion, so that nothing deep is ever walked."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict | list):
            if depth > MAX_DEPTH:
                raise Refused("too_deep")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)


# --- the policy ------------------------------------------------------------------------------


def _empty(value) -> bool:
    """A value that asks for nothing."""
    return value is None or value is False or value in ("", [], {})


def _key(policy_key: str) -> str:
    """A key as the person sees it: the policy's, but for the condition."""
    return policy_key.partition("=")[0]


def _children(key: str) -> list[str]:
    return [k for k in POLICY if k.startswith(f"{key}.")]


@dataclass(frozen=True)
class Command:
    """One lifecycle command: ``event`` is its key, ``origin`` the entry it came from, ``label``
    the name of its entry in an object. ``shell`` is for a shell, ``argv`` for a program."""

    event: str
    origin: str
    label: str | None = None
    shell: str | None = None
    argv: tuple[str, ...] | None = None

    @property
    def name(self) -> str:
        return f"{self.event} ({self.label})" if self.label else self.event


@dataclass
class _Part:
    """What one entry, the file's or an image's, asks for."""

    image: str | None = None
    remote_user: str | None = None
    container_user: str | None = None
    container_env: dict = field(default_factory=dict)
    remote_env: dict = field(default_factory=dict)
    init: bool = False
    commands: list = field(default_factory=list)
    ignored: list = field(default_factory=list)


@dataclass(frozen=True)
class Checked:
    """The file after the policy: the image it names and the keys it has that are ignored."""

    image: str
    ignored: tuple[str, ...]


@dataclass(frozen=True)
class _Context:
    work: str
    workspace: int
    env: dict


VARIABLE = re.compile(
    r"\$\{(?:(?P<scope>localEnv|env|containerEnv):(?P<name>[^}:]*)(?::(?P<default>[^}]*))?"
    r"|(?P<fixed>localWorkspaceFolderBasename|localWorkspaceFolder|containerWorkspaceFolderBasename"
    r"|containerWorkspaceFolder|devcontainerId))\}"
)


def devcontainer_id(workspace: int) -> str:
    """The workspace's devcontainerId: stable for the workspace, and unlike any other's."""
    digest = hashlib.sha256(f"rcman-workspace-{workspace}".encode()).digest()
    return base64.b32encode(digest).decode().lower().rstrip("=")


def _substituted(value, context: _Context, key: str):
    """``value`` with the devcontainer variables filled in. The local environment is empty,
    never rcman's own; a variable naming a folder of the host is refused; a ``${...}`` that is
    not one of the specification's is left for the shell."""
    if isinstance(value, str):
        return VARIABLE.sub(lambda m: _variable(m, context, key), value)
    if isinstance(value, list):
        return [_substituted(item, context, key) for item in value]
    if isinstance(value, dict):
        return {k: _substituted(v, context, key) for k, v in value.items()}
    return value


def _walked(key: str) -> bool:
    """Whether variables are filled in, and refused, in the value of ``key``: only in the keys
    rcman honours, and not in the image, whose name is what is pulled and is taken as it is."""
    policy = POLICY.get(key)
    return policy is not None and policy.action == HONOUR and key != "image"


def _variable(matched: re.Match, context: _Context, key: str) -> str:
    fixed = matched.group("fixed")
    if fixed is None:
        scope, default = matched.group("scope"), matched.group("default") or ""
        if scope == "containerEnv":
            return context.env.get(matched.group("name"), default)
        return default
    if fixed.startswith("local"):
        raise Refused("variable", variable=fixed, key=_shown(key))
    if fixed == "containerWorkspaceFolder":
        return context.work
    if fixed == "containerWorkspaceFolderBasename":
        return context.work.rstrip("/").rsplit("/", 1)[-1]
    return devcontainer_id(context.workspace)


def _bad(key: str, expected: str) -> Refused:
    return Refused("bad_value", key=_shown(key), expected=expected)


def _string(value, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(key, "a string")
    return value


def _image(value) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IMAGE_CHARS
        or not IMAGE_NAME.match(value)
        or value.partition(":")[0] in TRANSPORTS
    ):
        raise _bad(
            "image",
            "the name of an image in a registry, such as ubuntu:24.04, with no variable, "
            "file or transport in it",
        )
    return value


def _env(value, key: str, origin: str, nullable: bool) -> dict:
    if not isinstance(value, dict):
        raise _bad(key, "an object of variables and their values")
    for name, item in value.items():
        if not ENV_NAME.match(name):
            raise _bad(
                key, f"names of letters, digits and underscores, which {_shown(name)!r} is not"
            )
        if name in RESERVED_ENV:
            raise Refused("env_reserved", where=key, name=_shown(name), origin=_shown(origin))
        if not (isinstance(item, str) or (nullable and item is None)):
            raise _bad(key, "an object whose values are strings")
        if isinstance(item, str) and "\0" in item:
            raise _bad(key, "an object whose values are strings")
    return dict(value)


def _commands(value, event: str, origin: str) -> list[Command]:
    """A lifecycle command as the commands it makes: a string for a shell, an array for a
    program, an object's entries each of one of those, in order. Empty ones are nothing."""
    expected = "a string, an array of strings, or an object of those"

    def one(item, label: str | None) -> Command | None:
        if isinstance(item, str):
            return Command(event, origin, label, shell=item) if item.strip() else None
        if isinstance(item, list) and all(isinstance(a, str) for a in item):
            return Command(event, origin, label, argv=tuple(item)) if item else None
        raise _bad(event, expected)

    if isinstance(value, dict):
        made = [one(item, _shown(label)) for label, item in value.items()]
    else:
        made = [one(value, None)]
    return [c for c in made if c is not None]


def _entry(entry: dict, origin: str, in_image: bool) -> _Part:
    """The policy applied to one entry; ``origin`` names it, and ``in_image`` says it is an
    image's and not the file's own."""
    part = _Part()
    for key, value in entry.items():
        _apply_policy(key, value, entry, origin, in_image)
        policy = POLICY[key]
        if policy.action == IGNORE:
            if key not in part.ignored:
                part.ignored.append(key)
        elif policy.action == HONOUR:
            _honoured(part, key, value, origin)
    return part


def _refused(policy_key: str, origin: str, in_image: bool) -> Refused:
    key = _shown(_key(policy_key))
    reason = POLICY[policy_key].reason
    if in_image:
        return Refused("refused_by_image", key=key, entry=_shown(origin), reason=reason)
    return Refused("refused_key", key=key, reason=reason)


def _apply_policy(key: str, value, entry: dict, origin: str, in_image: bool) -> None:
    """Raise :class:`Refused` if ``key`` or something inside it is one the policy refuses."""
    if key not in POLICY:
        if in_image:
            raise Refused("unknown_key_in_image", key=_shown(key), entry=_shown(origin))
        raise Refused("unknown_key", key=_shown(key))
    for child in _children(key):
        inner = child.partition(".")[2]
        if (
            POLICY[child].action == REFUSE
            and isinstance(value, dict)
            and not _empty(value.get(inner))
        ):
            raise _refused(child, origin, in_image)
    if POLICY[key].action == REFUSE and not _empty(value):
        raise _refused(key, origin, in_image)
    negative = f"{key}=false"
    if negative in POLICY and value is False:
        raise _refused(negative, origin, in_image)


def _honoured(part: _Part, key: str, value, origin: str) -> None:
    if key == "image":
        part.image = _image(value)
    elif key == "remoteUser":
        part.remote_user = _string(value, key)
    elif key == "containerUser":
        part.container_user = _string(value, key)
    elif key == "containerEnv":
        part.container_env = _env(value, key, origin, nullable=False)
    elif key == "remoteEnv":
        part.remote_env = _env(value, key, origin, nullable=True)
    elif key == "init":
        if not isinstance(value, bool):
            raise _bad(key, "true or false")
        part.init = value
    elif key in ("updateRemoteUserUID", "overrideCommand"):
        if not isinstance(value, bool):
            raise _bad(key, "true or false")
    elif key == "waitFor":
        if value not in WAIT_FOR:
            raise _bad(key, "the name of a lifecycle command")
    elif key == "userEnvProbe":
        if value not in ENV_PROBES:
            raise _bad(key, "one of " + ", ".join(ENV_PROBES))
    elif key in EVENTS:
        part.commands.extend(_commands(value, key, origin))


def _context(workspace: int, work: str, env: dict | None = None) -> _Context:
    return _Context(work=work, workspace=workspace, env=env or {})


def _substituted_entry(entry: dict, context: _Context) -> dict:
    """The entry with its variables filled in. ``${containerEnv:...}`` sees the image's
    environment, the earlier entries' and the entry's own containerEnv."""
    found = {}
    own = entry.get("containerEnv")
    if isinstance(own, dict) and _walked("containerEnv"):
        env = dict(context.env)
        found["containerEnv"] = {}
        for name, value in own.items():  # each sees the ones before it
            value = _substituted(value, replace(context, env=env), "containerEnv")
            found["containerEnv"][name] = value
            if isinstance(value, str):
                env[name] = value
        context = replace(context, env=env)
    elif "containerEnv" in entry:
        found["containerEnv"] = entry["containerEnv"]
    for key, value in entry.items():
        if key != "containerEnv":
            found[key] = _substituted(value, context, key) if _walked(key) else value
    return {key: found[key] for key in entry}


def check(config: dict) -> Checked:
    """Hold the file to the policy, before anything is pulled: the first key it may not have,
    or a value of the wrong kind, is :class:`Refused`; else the image it names and the keys
    that are ignored. A file that names no image is refused once nothing else is."""
    _shallow(config)
    part = _entry(_substituted_entry(config, _context(0, "")), CONFIG_ORIGIN, in_image=False)
    if part.image is None:
        raise Refused("no_image")
    return Checked(part.image, tuple(part.ignored))


# --- passwd and group -------------------------------------------------------------------------


@dataclass(frozen=True)
class PasswdEntry:
    name: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True)
class GroupEntry:
    name: str
    gid: int
    members: tuple[str, ...]


def _is_number(text: str) -> bool:
    """Whether ``text`` is a number of ASCII digits, as an id is written; str.isdigit alone
    takes superscripts and the digits of other scripts, which int() then refuses."""
    return text.isascii() and text.isdigit()


def _number(text: str) -> int:
    """The id ``text`` writes, ASCII digits, or the size of the range for one with more digits
    than any id has, so that it is beyond the range and not a crash."""
    return int(text) if len(text) <= MAX_ID_DIGITS else RANGE


def _lines(text: str):
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            yield line.split(":")


def parse_passwd(text: str) -> list[PasswdEntry]:
    """The users of a passwd file; a line that is not one is left out."""
    found = []
    for fields in _lines(text):
        if len(fields) >= 6 and _is_number(fields[2]) and _is_number(fields[3]):
            found.append(PasswdEntry(fields[0], _number(fields[2]), _number(fields[3]), fields[5]))
    return found


def parse_group(text: str) -> list[GroupEntry]:
    """The groups of a group file; a line that is not one is left out."""
    found = []
    for fields in _lines(text):
        if len(fields) >= 3 and _is_number(fields[2]):
            members = tuple(m for m in (fields[3] if len(fields) > 3 else "").split(",") if m)
            found.append(GroupEntry(fields[0], _number(fields[2]), members))
    return found


@dataclass(frozen=True)
class Account:
    """The user a workspace's commands run as. ``run_as`` is what the container is told: a name
    or number, which keeps the user's groups, or ``uid:gid``, for which ``groups`` are the
    supplementary groups to add."""

    name: str | None
    uid: int
    gid: int
    run_as: str
    groups: tuple[int, ...]


def resolve_user(spec: str, passwd: str, group: str) -> Account:
    """The user ``spec`` names - a name, a number, either with ``:`` and a group - in an image's
    passwd and group files. A user the image does not have is :class:`Refused`, as an id
    beyond the range of ids a workspace has."""
    who, _, which = spec.partition(":")
    users, groups = parse_passwd(passwd), parse_group(group)
    if _is_number(who):
        found = next((u for u in users if u.uid == _number(who)), None)
        uid, gid, name = _number(who), (found.gid if found else 0), (found.name if found else None)
    else:
        found = next((u for u in users if u.name == who), None)
        if found is None and who == "root":
            uid, gid, name = 0, 0, "root"
        elif found is None:
            raise Refused("user_unknown", user=_shown(spec))
        else:
            uid, gid, name = found.uid, found.gid, found.name
    if which:
        if _is_number(which):
            gid = _number(which)
        else:
            named = next((g for g in groups if g.name == which), None)
            if named is None and which != "root":
                raise Refused("user_unknown", user=_shown(spec))
            gid = named.gid if named else 0
    extra = tuple(g.gid for g in groups if name in g.members and g.gid != gid)
    if uid >= RANGE or gid >= RANGE or any(g >= RANGE for g in extra):
        raise Refused("id_out_of_range", user=_shown(spec))
    if which:
        return Account(name, uid, gid, f"{uid}:{gid}", extra)
    return Account(name, uid, gid, who if _is_number(who) else (name or str(uid)), ())


# --- the plan -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What a workspace is made from, stored with its row: the image, the user and the ids of
    that user in it, the environment, the lifecycle commands in the order they run, and the
    keys of the file that rcman ignored."""

    image: str
    user_name: str
    run_as: str
    uid: int
    gid: int
    groups: tuple[int, ...] = ()
    container_env: dict = field(default_factory=dict)
    remote_env: dict = field(default_factory=dict)
    init: bool = False
    commands: tuple[Command, ...] = ()
    ignored: tuple[str, ...] = ()
    image_path: str = ""  # the PATH the image sets, which a PATH of the file replaces

    @property
    def path(self) -> str | None:
        """The PATH the workspace's programs get after rcman's own folder: the file's, else
        the image's, else None, for rcman's default."""
        return self.env.get("PATH") or self.image_path or None

    @property
    def env(self) -> dict[str, str]:
        """What every command rcman runs in the workspace gets: the container's variables, over
        which the remote ones, a null one removing the variable."""
        merged = {**self.container_env, **self.remote_env}
        return {k: v for k, v in merged.items() if v is not None}

    def create_commands(self) -> tuple[Command, ...]:
        """The commands run once, when the workspace is made, in order."""
        return self.commands

    def start_commands(self) -> tuple[Command, ...]:
        """The commands run each time the workspace's container is started."""
        return tuple(c for c in self.commands if c.event == START_EVENT)

    def to_json(self) -> str:
        data = {
            "image": self.image,
            "user_name": self.user_name,
            "run_as": self.run_as,
            "uid": self.uid,
            "gid": self.gid,
            "groups": list(self.groups),
            "container_env": self.container_env,
            "remote_env": self.remote_env,
            "init": self.init,
            "commands": [
                {
                    "event": c.event,
                    "origin": c.origin,
                    "label": c.label,
                    "shell": c.shell,
                    "argv": list(c.argv) if c.argv is not None else None,
                }
                for c in self.commands
            ],
            "ignored": list(self.ignored),
            "image_path": self.image_path,
        }
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Plan:
        """The plan ``to_json`` made; ValueError for anything else."""
        try:
            data = json.loads(text)
            commands = tuple(
                Command(
                    c["event"],
                    c["origin"],
                    c["label"],
                    c["shell"],
                    tuple(c["argv"]) if c["argv"] is not None else None,
                )
                for c in data["commands"]
            )
            made = cls(
                data["image"],
                data["user_name"],
                data["run_as"],
                data["uid"],
                data["gid"],
                tuple(data["groups"]),
                dict(data["container_env"]),
                dict(data["remote_env"]),
                bool(data["init"]),
                commands,
                tuple(data["ignored"]),
                data["image_path"],
            )
        except (TypeError, KeyError, AttributeError, ValueError) as failed:
            raise ValueError("the stored plan is not one") from failed
        if not (
            isinstance(made.image, str)
            and isinstance(made.run_as, str)
            and isinstance(made.user_name, str)
            and isinstance(made.uid, int)
            and isinstance(made.gid, int)
            and isinstance(made.image_path, str)
        ):
            raise ValueError("the stored plan is not one")
        return made


def _metadata(label: str | None) -> list[dict]:
    """The entries of an image's devcontainer.metadata label: a list of objects, or one."""
    if label is None:
        return []
    try:
        found = json.loads(label)
    except (ValueError, RecursionError):
        raise Refused("metadata_invalid") from None
    if isinstance(found, dict):
        found = [found]
    if not isinstance(found, list) or not all(isinstance(e, dict) for e in found):
        raise Refused("metadata_invalid")
    return found


def plan(config: dict, facts, workspace: int, work: str) -> Plan:
    """The plan for the workspace ``workspace``, whose work folder is ``work``, from the file
    ``config`` and the image's ``facts`` (runtime.ImageFacts): the entries of the image's
    metadata first, in order, then the file's own, once. Booleans are ORed, environments merged
    with the last winning, the user is the last one named, and the lifecycle commands are
    collected. Anything an entry asks for that the policy refuses is :class:`Refused`, naming
    the entry."""
    context = _context(workspace, work, dict(facts.env))
    entries = _metadata(facts.labels.get(METADATA_LABEL))
    _shallow(config)
    for entry in entries:
        _shallow(entry)
    parts = []
    for number, entry in enumerate(entries, start=1):
        origin = (
            entry["id"] if isinstance(entry.get("id"), str) and entry["id"] else f"entry {number}"
        )
        parts.append(_entry(_substituted_entry(entry, context), origin, in_image=True))
        context = replace(context, env={**context.env, **parts[-1].container_env})
    own = _entry(_substituted_entry(config, context), CONFIG_ORIGIN, in_image=False)
    if own.image is None:
        raise Refused("no_image")
    parts.append(own)
    return _merged(own.image, parts, facts)


def _merged(image: str, parts: list[_Part], facts) -> Plan:
    # The image's own variables come first, under everything else; its PATH is the plan's
    # image_path, and the names rcman sets itself are not taken.
    container_env: dict = {
        k: v for k, v in facts.env.items() if k != "PATH" and k not in RESERVED_ENV
    }
    remote_env: dict = {}
    remote_user = container_user = None
    init = False
    ignored: list[str] = []
    commands: list[Command] = []
    for part in parts:
        container_env.update(part.container_env)
        remote_env.update(part.remote_env)
        remote_user = part.remote_user or remote_user
        container_user = part.container_user or container_user
        init = init or part.init
        ignored.extend(k for k in part.ignored if k not in ignored)
        commands.extend(part.commands)
    ordered = sorted(commands, key=lambda c: EVENTS.index(c.event))  # stable
    who = remote_user or container_user or facts.user or "root"
    account = resolve_user(who, facts.passwd, facts.group)
    return Plan(
        image=image,
        user_name=account.name or str(account.uid),
        run_as=account.run_as,
        uid=account.uid,
        gid=account.gid,
        groups=account.groups,
        container_env=container_env,
        remote_env=remote_env,
        init=init,
        commands=tuple(ordered),
        ignored=tuple(ignored),
        image_path=facts.env.get("PATH", ""),
    )
