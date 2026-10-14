"""rcman-admin: adding, listing and deleting people from a shell.

It is the People page's code (people.py, workspaces.py), run as rcman with the
settings the service runs with, read from its environment file. Run as root, it
runs itself again as rcman; anyone else may run it only where they can write
rcman's database. Deleting a person deletes their workspaces, so it reaches
Podman as rcman does. Answers are in words; a refusal is the problem's own words
on stderr, and exit status 1.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rcman import db, people
from rcman.problems import Problem
from rcman.runtime import Runtime
from rcman.settings import Settings

ACCOUNT = "rcman"
ENV_FILE = Path("/etc/rcman/rcman.env")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcman-admin", description="Add, list and delete the people who may use rcman."
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
                print(f"{person.email}{role}{state}  {counts.get(person.id, 0)} workspaces")
            return 0
        person = people.by_email(con, args.email)
    finally:
        con.close()
    if person is None:
        raise Problem("person_not_found")
    from rcman.main import podman_runtime
    from rcman.workspaces import Workspaces

    service = Workspaces(settings, runtime or podman_runtime(settings))
    try:
        service.delete_person(person)
    finally:
        service.close()
    print(f"Deleted {person.email} and every workspace of theirs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
