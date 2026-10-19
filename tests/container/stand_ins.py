#!/usr/bin/env python3
"""Stand-ins for ``systemctl``, ``loginctl`` and rcman's ``podman``, for where the container
suites run rcman with no systemd and no Podman of its own: on the tests' engine
(tests/container/test_restored_in_container.py), in the stubbed installation and in the whole
stack (tests/container/installed.py, tests/container/stack.py).

Not a test module, and the standard library only, so that a container can run it as it is from
the checkout's files at /src. Which program it stands in for is its own name (it is mounted over
the program) or, called by another name, its first argument. Every call is recorded, as a JSON
line of the time, the user, the arguments and the folder, in ``calls`` of its folder (STATE_DIR,
or ``RCMAN_STAND_IN_DIR`` where that is set), and answered as the real program would answer what
rcman and its installer ask of it:

- ``systemctl --user set-property --runtime <slice> <properties>``, ``show`` and ``revert`` keep
  each slice's properties in ``slices.json``, as the user manager keeps runtime properties until
  the machine restarts (``reboot`` below forgets them); ``show`` answers with them, nothing in
  use, the slice active;
- the installer's ``systemctl`` (``daemon-reload``, ``enable`` with or without ``--now``,
  ``is-active``, ``restart``, ``start``, ``show``, ``--user --machine``): a service of
  /etc/systemd/system (or ``RCMAN_STAND_IN_UNITS``) that is started is run as its ``User=`` with
  its environment, its folder and its ``ExecStart``, in a session of its own, its process noted,
  and stopped first when it is started again; a path unit started is only noted active, as
  nothing makes its path change; ``user@<uid>.service`` makes the
  user's runtime folder; ``podman.socket`` in a user's manager makes the socket Podman's would be;
- ``loginctl enable-linger <user>`` makes the user's linger file;
- ``podman`` answers what the installer asks of rcman's Podman: ``info`` with the OCI runtime it
  checks, ``image exists`` with whether the image was built, ``build`` noting it, ``run`` of the
  tmux build with the tmux it is given (``tmux`` in its folder, built by the suite with the
  installer's own argument list) and its sha256, and the rest with nothing.

``reboot``, run by name, forgets what a restart of the machine forgets: the slices' properties and
the units that ran.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/rcman-test-stand-ins")
NAMES = ("systemctl", "loginctl", "podman", "reboot")
# The system manager's units: /etc/systemd/system, or ``RCMAN_STAND_IN_UNITS`` where that is set.
UNITS = Path(os.environ.get("RCMAN_STAND_IN_UNITS") or "/etc/systemd/system")
LINGER = Path("/var/lib/systemd/linger")
OCI_RUNTIME = "crun"  # what rcman's installer checks rcman's Podman reports (OCI_RUNTIME)
# The properties ``show`` answers for a slice nothing set: no cap, nothing in use.
UNSET = {"MemoryMax": "infinity", "MemorySwapMax": "infinity", "TasksMax": "infinity",
         "CPUQuotaPerSecUSec": "infinity"}  # fmt: skip
IN_USE = {"ActiveState": "active", "MemoryCurrent": "0", "MemorySwapCurrent": "0",
          "TasksCurrent": "0"}  # fmt: skip


def state() -> Path:
    folder = Path(os.environ.get("RCMAN_STAND_IN_DIR") or STATE_DIR)
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o777)  # every account the installer runs it as records its calls
    return folder


def record(name: str, args: list[str]) -> None:
    line = json.dumps({"time": time.time(), "uid": os.getuid(), "program": name, "args": args,
                       "cwd": os.getcwd()})  # fmt: skip
    with open(state() / "calls", "a") as calls:
        calls.write(line + "\n")
    with __import__("contextlib").suppress(OSError):
        os.chmod(state() / "calls", 0o666)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    new = path.with_name(f".{path.name}.{os.getpid()}")
    new.write_text(json.dumps(data))
    os.chmod(new, 0o666)
    os.replace(new, path)


# --- systemctl -------------------------------------------------------------------------------


def _words(args: list[str]) -> list[str]:
    """The arguments that are not options, nor the values of ``-p``, ``--property`` and
    ``--machine``."""
    found, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-p", "--property", "--machine"):
            skip = True
        elif not arg.startswith("-"):
            found.append(arg)
    return found


def _properties(args: list[str]) -> list[str]:
    found = []
    for i, arg in enumerate(args):
        if arg in ("-p", "--property") and i + 1 < len(args):
            found += args[i + 1].split(",")
        elif arg.startswith("--property="):
            found += arg.split("=", 1)[1].split(",")
    return found


def user_manager(args: list[str]) -> int:
    """``systemctl --user``: the slices' properties."""
    words = _words(args)
    slices_file = state() / "slices.json"
    slices = _load(slices_file)
    verb = words[0] if words else ""
    if verb == "set-property":
        unit, *settings = words[1:]
        held = slices.setdefault(unit, {})
        for setting in settings:
            key, _, value = setting.partition("=")
            if key == "CPUQuota":
                key = "CPUQuotaPerSecUSec"
                value = "infinity" if not value else f"{float(value.rstrip('%')) / 100:g}s"
            held[key] = value
        _save(slices_file, slices)
        return 0
    if verb == "revert":
        for unit in words[1:]:
            slices.pop(unit, None)
        _save(slices_file, slices)
        return 0
    if verb == "show":
        wanted = _properties(args)
        value_only = "--value" in args
        for unit in words[1:] or [""]:
            known = {**IN_USE, **UNSET, **slices.get(unit, {})}
            for key in wanted or sorted(known):
                shown = known.get(key, "")
                print(shown if value_only else f"{key}={shown}")
        return 0
    if verb in ("is-enabled", "is-active"):
        return 0 if (state() / f"{verb}-{'-'.join(words[1:])}").exists() else 1
    if verb in ("enable", "start"):
        for unit in words[1:]:
            if unit == "podman.socket":
                _podman_socket()
            (state() / f"is-enabled-{unit}").touch()
            (state() / f"is-active-{unit}").touch()
        return 0
    return 0  # daemon-reexec, stop, and the rest: what the manager would do, nothing here


def _podman_socket() -> None:
    """The socket the user's Podman service would listen on, as a file: the installer waits for
    it to be there, and rcman's Podman is this stand-in, which needs no socket."""
    folder = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "podman"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "podman.sock"
    if not path.exists():
        with socket.socket(socket.AF_UNIX) as made:
            made.bind(str(path))


def _unit(name: str) -> dict[str, list[str]]:
    """A unit file's keys and their values, in order."""
    found: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def _environment(unit: dict[str, list[str]]) -> dict[str, str]:
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "LANG": "C.UTF-8"}  # fmt: skip
    for path in unit.get("EnvironmentFile", []):
        for line in Path(path.lstrip("-")).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    for value in unit.get("Environment", []):
        for item in shlex.split(value):
            key, _, held = item.partition("=")
            env[key] = held
    user = unit.get("User", ["root"])[0]
    account = pwd.getpwnam(user)
    env.setdefault("HOME", account.pw_dir)
    env["USER"] = env["LOGNAME"] = user
    return env


def _pid_file(name: str) -> Path:
    return state() / f"{name}.pid"


def _stop_unit(name: str) -> None:
    try:
        pid = int(_pid_file(name).read_text())
    except (OSError, ValueError):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(pid, signal.SIGKILL)
    _pid_file(name).unlink(missing_ok=True)
    (state() / f"is-active-{name}").unlink(missing_ok=True)


def _start_unit(name: str) -> None:
    """The unit's ``ExecStart`` as its ``User=`` and ``Group=``, with its environment, folder and
    umask, in a session of its own; what it writes is in ``<unit>.log``."""
    _stop_unit(name)
    unit = _unit(name)
    user = unit.get("User", ["root"])[0]
    group = unit.get("Group", [user])[0]
    argv = shlex.split(unit["ExecStart"][-1])
    umask = int(unit.get("UMask", ["0022"])[0], 8)
    log = open(state() / f"{name}.log", "ab")  # noqa: SIM115 - the child holds it
    # runuser gives the account its groups unless a group is named: only one that is not its own
    # is named, as systemd would make it the process's group with the account's groups beside it
    own = grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    as_group = [] if group == own else ["-g", group]
    child = subprocess.Popen(
        ["/usr/sbin/runuser", "-u", user, *as_group, "--", *argv],
        env=_environment(unit),
        cwd=unit.get("WorkingDirectory", ["/"])[0],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        preexec_fn=lambda: os.umask(umask),
    )
    _pid_file(name).write_text(str(child.pid))
    (state() / f"is-active-{name}").touch()


def _user_service(name: str) -> None:
    """``user@<uid>.service``: the user's runtime folder."""
    uid = int(name.removeprefix("user@").removesuffix(".service"))
    folder = Path(f"/run/user/{uid}")
    folder.mkdir(parents=True, exist_ok=True)
    os.chown(folder, uid, pwd.getpwuid(uid).pw_gid)
    os.chmod(folder, 0o700)


def _start(unit: str) -> None:
    """A unit started as systemd starts it: ``user@<uid>.service`` makes the runtime folder, a
    service of the system's is run, and anything else (a path unit, which starts watching, and
    runs its service only when the path changes) is only noted active."""
    if unit.startswith("user@"):
        _user_service(unit)
    elif unit.endswith(".service") and (UNITS / unit).exists():
        _start_unit(unit)
    (state() / f"is-active-{unit}").touch()


def system_manager(args: list[str]) -> int:
    if "--user" in args:
        if "--machine" in args:  # as root, for a user: run as that user, as systemctl does
            machine = args[args.index("--machine") + 1]
            user = machine.removesuffix("@")
            uid = pwd.getpwnam(user).pw_uid
            at = args.index("--machine")
            rest = args[:at] + args[at + 2 :]
            return subprocess.run(
                ["/usr/sbin/runuser", "-u", user, "--", "/usr/bin/env",
                 f"XDG_RUNTIME_DIR=/run/user/{uid}", sys.executable, os.path.abspath(__file__),
                 *([] if Path(__file__).name in NAMES else ["systemctl"]), *rest],
                cwd="/",
            ).returncode  # fmt: skip
        return user_manager(args)
    words = _words(args)
    verb = words[0] if words else ""
    units = words[1:]
    if verb in ("restart", "start"):
        for unit in units:
            _start(unit)
        return 0
    if verb == "stop":
        for unit in units:
            _stop_unit(unit)
        return 0
    if verb == "enable":
        for unit in units:
            (state() / f"is-enabled-{unit}").touch()
            if "--now" in args:
                _start(unit)
        return 0
    if verb in ("is-active", "is-enabled"):
        return 0 if all((state() / f"{verb}-{u}").exists() for u in units) else 3
    if verb == "show":
        wanted = _properties(args)
        value_only = "--value" in args
        for key in wanted:
            shown = ""
            if key == "MainPID":
                shown = "1"
            print(shown if value_only else f"{key}={shown}")
        return 0
    return 0  # daemon-reload and the rest


# --- loginctl and podman ----------------------------------------------------------------------


def login_manager(args: list[str]) -> int:
    words = _words(args)
    if words[:1] == ["enable-linger"]:
        LINGER.mkdir(parents=True, exist_ok=True)
        for user in words[1:]:
            (LINGER / user).touch()
    return 0


def podman(args: list[str]) -> int:
    """rcman's Podman, as the installer asks it."""
    given = list(args)
    for option in ("--url", "--format", "--filter", "--tag", "--file"):
        while option in given:
            at = given.index(option)
            del given[at : at + 2]
    words = [a for a in given if not a.startswith("-")]
    built = state() / "built"
    if "info" in args:
        print(OCI_RUNTIME if "--format" in args else json.dumps({"host": {"ociRuntime": {
            "name": OCI_RUNTIME}, "security": {"rootless": True}}}))  # fmt: skip
        return 0
    if words[:2] == ["image", "exists"]:
        return 0 if words[2] in _load(built) else 1
    if "build" in words:
        tag = args[args.index("--tag") + 1] if "--tag" in args else ""
        names = _load(built)
        names[tag] = time.time()
        _save(built, names)
        return 0
    if "run" in words and any("tmux" in a for a in args):
        program = (state() / "tmux").read_bytes()
        sys.stderr.write(f"sha256 {hashlib.sha256(program).hexdigest()}\n")
        sys.stdout.buffer.write(program)
        return 0
    if any("json" in a for a in args):
        print("[]")  # ps, volume ls, images as JSON: nothing runs and nothing is kept
    return 0  # rmi, system migrate and the rest


def reboot(args: list[str]) -> int:
    """What a restart of the machine forgets: the slices' runtime properties, and the units that
    ran (their processes are ended)."""
    folder = state()
    for pid_file in folder.glob("*.pid"):
        _stop_unit(pid_file.name.removesuffix(".pid"))
    (folder / "slices.json").unlink(missing_ok=True)
    for active in folder.glob("is-active-*"):
        active.unlink()
    return 0


def main(argv: list[str]) -> int:
    name = Path(argv[0]).name
    args = argv[1:]
    if name not in NAMES:
        name, args = args[0], args[1:]
    record(name, args)
    run = {"systemctl": system_manager, "loginctl": login_manager, "podman": podman,
           "reboot": reboot}[name]  # fmt: skip
    return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
