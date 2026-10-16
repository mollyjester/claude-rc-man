"""rcman-admin: adding, listing and deleting people, and setting limits on what they and their
workspaces may use, from a shell.

It is the People page's code (people.py, workspaces.py), run as rcman with the
settings the service runs with, read from its environment file. Run as root, it
runs itself again as rcman; anyone else may run it only where they can write
rcman's database. Deleting a person deletes their workspaces, and limits are
applied to the slices of rcman's user manager, so both reach Podman and systemd
as rcman does. Answers are in words; a refusal is the problem's own words
on stderr, and exit status 1.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from rcman import db, limits, people
from rcman.problems import Problem
from rcman.runtime import Runtime, RuntimeFailed, RuntimeUnavailable
from rcman.settings import Settings

ACCOUNT = "rcman"
# The reader of each limit's option, and the field of limits.Limits it sets, by the option's
# name (the fields are named for their units).
PARSERS = {"memory": limits.parse_memory, "cpu": limits.parse_cpu, "pids": limits.parse_pids}
FIELDS = {"memory": "memory_mib", "cpu": "cpu_percent", "pids": "pids"}
ENV_FILE = Path("/etc/rcman/rcman.env")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcman-admin",
        description="Add, list and delete the people who may use rcman, and set limits on "
        "what they and their workspaces may use.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ENV_FILE,
        help=f"the settings rcman runs with (default: {ENV_FILE})",
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    add = verbs.add_parser("add", help="let someone in, by their Google address")
    add.add_argument("email")
    add.add_argument("--admin", action="store_true", help="make them an administrator")
    verbs.add_parser("list", help="everyone, with their workspaces")
    delete = verbs.add_parser(
        "delete", help="delete someone and every workspace of theirs, at once"
    )
    delete.add_argument("email")
    held = verbs.add_parser(
        "limits",
        help="show or set the limits on what someone's workspaces may use together, or on "
        "one workspace",
        description="Without an option, show the limits and what is in use. With options, set "
        "those and leave the others as they are. A value is a number as the options say, or "
        f"{limits.NONE} for no limit. A memory or process limit below what is in use now is "
        "refused, so that no running work is ended.",
    )
    held.add_argument("email")
    held.add_argument("--workspace", metavar="NAME", help="their workspace, instead of them")
    held.add_argument(
        "--memory",
        metavar="V",
        help="memory: a whole number and M for mebibytes or G for gibibytes",
    )
    held.add_argument("--cpu", metavar="V", help="CPU: cores, with up to two decimals")
    held.add_argument(
        "--pids", metavar="V", help="processes: a whole number; threads count as processes"
    )
    return parser


def as_account(argv: list[str]) -> list[str]:
    """This command run as rcman, with rcman's own runtime folder and home: runuser keeps
    the caller's, and root's would never lead to rcman's Podman socket."""
    import pwd

    account = pwd.getpwnam(ACCOUNT)
    return [
        "runuser", "-u", ACCOUNT, "--", "/usr/bin/env",
        f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}", f"HOME={account.pw_dir}",
        sys.executable, "-m", "rcman.admin", *argv,
    ]  # fmt: skip


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(argv)
    if os.geteuid() == 0:
        os.execvp("runuser", as_account(argv))
    settings = Settings(_env_file=args.env_file)
    db.migrate(settings.database)
    try:
        return _run(args, settings, runtime)
    except Problem as refused:
        print(f"rcman-admin: {refused.title}", file=sys.stderr)
        return 1


def _run(args, settings: Settings, runtime: Runtime | None) -> int:
    con = db.connect(settings.database)
    try:
        if args.verb == "add":
            person = people.add(con, args.email, args.admin)
            people.write_allowed(con, settings.allowed_emails)
            role = " as an administrator" if person.admin else ""
            print(f"Added {person.email}{role}.")
            return 0
        if args.verb == "list":
            counts: dict[int, int] = {}
            for (person_id,) in con.execute("select person from workspaces"):
                counts[person_id] = counts.get(person_id, 0) + 1
            for person in people.everyone(con):
                role = "  administrator" if person.admin else ""
                state = "  being deleted" if person.state != people.ACTIVE else ""
                held = f"  {person.limits.describe()}" if person.limits.limited else ""
                print(f"{person.email}{role}{state}  {counts.get(person.id, 0)} workspaces{held}")
            return 0
        person = people.by_email(con, args.email)
    finally:
        con.close()
    if person is None:
        raise Problem("person_not_found")
    from rcman.main import podman_runtime
    from rcman.workspaces import Workspaces

    service = Workspaces(settings, runtime or podman_runtime(settings), prunes=False)
    try:
        if args.verb == "limits":
            return _limits(args, service, person)
        service.delete_person(person)
    finally:
        service.close()
    print(f"Deleted {person.email} and every workspace of theirs.")
    return 0


def _limits(args, service, person: people.Person) -> int:
    """Show the limits of the person, or of their workspace, and what is in use; or set the
    ones the options name, the others staying as they are."""
    workspace = None
    target = person.email
    if args.workspace is not None:
        workspace = next(
            (w for w in service.of_person(person.id) if w.name == args.workspace), None
        )
        if workspace is None:
            raise Problem("workspace_not_found")
        target = f"{person.email}/{workspace.name}"
    current = (workspace or person).limits
    asked = {"memory": args.memory, "cpu": args.cpu, "pids": args.pids}
    if all(value is None for value in asked.values()):
        print(f"Limits of {target}: {current.describe()}")
        print(_in_use(service, person, workspace))
        return 0
    changes = {}
    try:
        for name, value in asked.items():
            if value is not None:
                changes[FIELDS[name]] = limits.parse_option(value, PARSERS[name])
    except limits.Refused as refused:
        raise Problem(f"{refused.field}_invalid") from None
    held = dataclasses.replace(current, **changes)
    service.set_limits(person, workspace and workspace.id, held)
    print(f"Set the limits of {target}: {held.describe()}")
    return 0


def _in_use(service, person: people.Person, workspace) -> str:
    try:
        used = service.runtime.usage(person.id, workspace and workspace.id)
    except (RuntimeFailed, RuntimeUnavailable):
        return "In use: not known"
    return f"In use: memory={used.memory_text()} tasks={used.tasks}"


if __name__ == "__main__":
    sys.exit(main())
