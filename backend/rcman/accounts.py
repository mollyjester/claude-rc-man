"""Each person's Claude sign-in: one credentials file every workspace of theirs shares.

The file is the runtime's (runtime.py: ``ensure_account``, ``read_account``,
``clear_account``); this module signs a person in and out of it. A sign-in runs
``claude auth login`` in tmux in a short-lived sign-in container
(``start_sign_in``), which ends itself after CODE_WAIT_SECONDS should rcman not end
it first; the code Claude shows reaches it through stdin and a tmux buffer, never an
argv. On success the account Claude names (``oauthAccount``) is kept in the people
table, to be seeded into each workspace before its server starts
(workspaces.py). Signing out runs ``claude auth logout``, whose answer is only
advisory, and then empties the file in place; one that keeps sessions running waits
until the person's last running server stops (``settle``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass

from helpers import screens
from rcman import db, people
from rcman.channel import Channel
from rcman.claude_server import (
    POLL_SECONDS,
    SESSION,
    UNRECOGNISED_GRACE_SECONDS,
    Tmux,
    claude_command,
)
from rcman.problems import Problem
from rcman.workspaces import Workspaces, answered

log = logging.getLogger("rcman.accounts")
log.setLevel(logging.INFO)

# How long a sign-in waits for its code; its container ends itself then.
CODE_WAIT_SECONDS = 900.0
# How long Claude may take to show its sign-in link, and to take a code.
LOGIN_TIMEOUT = 60.0
# How long the container a sign-out runs `claude auth logout` in may live.
LOGOUT_SECONDS = 120.0
# The tmux buffer the code goes through.
CODE_BUFFER = "rcman-code"


@dataclass
class SignIn:
    name: str  # the sign-in container
    channel: Channel
    tmux: Tmux
    until: float  # monotonic: the last moment a code is taken


def _expiry(millis) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(millis) / 1000, dt.UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class Accounts:
    def __init__(self, workspaces: Workspaces):
        self.workspaces = workspaces
        self.runtime = workspaces.runtime
        self.settings = workspaces.settings
        self._lock = threading.Lock()
        self._sign_ins: dict[int, SignIn] = {}
        workspaces.on_stopped = self.settle

    def lock(self, person: int) -> threading.RLock:
        """The person's lock (Workspaces.person_lock), which a server's start holds too."""
        return self.workspaces.person_lock(person)

    def _person(self, person_id: int) -> people.Person:
        con = db.connect(self.settings.database)
        try:
            return people.by_id(con, person_id)
        finally:
            con.close()

    def _account(self, person_id: int) -> tuple[dict | None, bool]:
        con = db.connect(self.settings.database)
        try:
            return people.claude_account(con, person_id)
        finally:
            con.close()

    def _set(self, person_id: int, account: dict | None, signing_out: bool = False) -> None:
        con = db.connect(self.settings.database)
        try:
            people.set_claude_account(con, person_id, account, signing_out)
        finally:
            con.close()

    # --- status ---------------------------------------------------------------------------

    def status(self, person: people.Person) -> dict:
        """What the credentials file says, with the account the sign-in named:
        ``logged_in``, ``email``, ``org_name``, ``subscription_type`` and
        ``login_expires_at``, the refresh token's expiry, or None without one; and
        ``signing_out``. A sign-out that waits is finished first if it can be."""
        self.settle(person.id)
        return self._status(person)

    def _status(self, person: people.Person) -> dict:
        with answered():
            raw = self.runtime.read_account(person.id, person.slot)
        account, signing_out = self._account(person.id)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            return {"logged_in": False, "signing_out": signing_out}
        named = (account or {}).get("oauthAccount") or {}
        return {
            "logged_in": True,
            "email": named.get("emailAddress"),
            "org_name": named.get("organizationName"),
            "subscription_type": oauth.get("subscriptionType"),
            "login_expires_at": _expiry(oauth.get("refreshTokenExpiresAt")),
            "signing_out": signing_out,
        }

    # --- signing in -----------------------------------------------------------------------

    def _end(self, sign_in: SignIn | None) -> None:
        if sign_in is None:
            return
        sign_in.channel.close()
        with contextlib.suppress(Exception):  # it ends itself regardless
            self.runtime.end_sign_in(sign_in.name)

    def _wait(self, sign_in: SignIn, until) -> dict:
        """Poll until ``until`` holds. A screen being drawn matches nothing for a moment,
        so an unrecognised one counts only once it has lasted UNRECOGNISED_GRACE_SECONDS."""
        deadline = time.monotonic() + LOGIN_TIMEOUT
        unrecognised_since = None
        while True:
            found = screens.classify_login(sign_in.tmux.screen(), sign_in.tmux.exit_status())
            if found["state"] == "unrecognised":
                unrecognised_since = unrecognised_since or time.monotonic()
                if time.monotonic() - unrecognised_since >= UNRECOGNISED_GRACE_SECONDS:
                    return found
            elif until(found):
                return found
            else:
                unrecognised_since = None
            if time.monotonic() > deadline:
                raise Problem("timeout")
            time.sleep(POLL_SECONDS)

    def begin(self, person: people.Person, email: str) -> dict:
        """Start signing in as ``email``: the link to open; one begun before ends."""
        with self.lock(person.id):
            self._end(self._sign_ins.pop(person.id, None))
            # A code is taken only while there is time to use it before the container
            # ends itself: two waits of Claude's to spare.
            until = time.monotonic() + CODE_WAIT_SECONDS - 2 * LOGIN_TIMEOUT
            with answered():
                name = self.runtime.start_sign_in(person.id, person.slot, CODE_WAIT_SECONDS)
            layout = self.runtime.layout(name)
            channel = self.runtime.channel(name)
            sign_in = SignIn(name, channel, Tmux(layout, channel.run), until)
            try:
                with answered():
                    argv = claude_command(layout, "auth", "login", "--claudeai", "--email", email)
                    sign_in.tmux.new(layout.work, argv)
                    found = self._wait(sign_in, lambda s: s["state"] != "starting")
                    if found["state"] != "awaiting_code":
                        raise Problem("claude_unrecognised_output", screen=sign_in.tmux.screen())
            except BaseException:
                self._end(sign_in)
                raise
            self._sign_ins[person.id] = sign_in
            return {"url": found["url"]}

    def finish(self, person: people.Person, code: str) -> dict:
        """Give Claude the code; the account's status once it is signed in. The sign-in
        ends whatever happens."""
        with self.lock(person.id):
            sign_in = self._sign_ins.pop(person.id, None)
            if sign_in is None or time.monotonic() > sign_in.until:
                self._end(sign_in)
                raise Problem("no_login_in_progress")
            try:
                with answered():
                    named = self._give_code(sign_in, code)
            finally:
                self._end(sign_in)
            # Read before anything settles: a sign-out that waited is over now.
            if not self._status(person)["logged_in"]:
                raise Problem("login_failed")
            self._set(person.id, {"oauthAccount": named}, signing_out=False)
            return self._status(person)

    def _give_code(self, sign_in: SignIn, code: str) -> dict:
        """The code through stdin into a tmux buffer, pasted; the account Claude names."""
        tmux = sign_in.tmux
        if self._wait(sign_in, lambda s: True)["state"] != "awaiting_code":
            raise Problem("no_login_in_progress")
        loaded = self.runtime.exec(
            sign_in.name, [*tmux.base, "load-buffer", "-b", CODE_BUFFER, "-"], stdin=code.encode()
        )
        if loaded.code != 0:
            raise Problem("workspace_failed")
        tmux.run("paste-buffer", "-d", "-b", CODE_BUFFER, "-t", f"={SESSION}:")
        tmux.run("send-keys", "-t", f"={SESSION}:", "Enter")
        found = self._wait(
            sign_in, lambda s: s["exit_code"] is not None or s["state"] in ("failed", "succeeded")
        )
        if found["state"] != "succeeded":
            raise Problem("login_failed")
        if found["exit_code"] is None:
            tmux.type("")  # Claude Code waits for Enter after signing in
            self._wait(sign_in, lambda s: s["exit_code"] is not None)
        layout = tmux.layout
        config = sign_in.channel.run([*layout.tool("cat"), f"{layout.config}/.claude.json"])
        try:
            named = json.loads(config.out).get("oauthAccount")
        except (ValueError, AttributeError):
            named = None
        if not isinstance(named, dict):
            raise Problem("claude_unrecognised_output", screen=tmux.screen())
        return named

    # --- signing out ------------------------------------------------------------------------

    def sign_out(self, person: people.Person, keep_sessions: bool) -> dict:
        """Sign out of Claude. Keeping sessions, a sign-out waits for the person's last
        running server to stop, and no server starts meanwhile; otherwise their running
        servers are stopped and it is done at once."""
        with self.lock(person.id):
            self._end(self._sign_ins.pop(person.id, None))
            if not keep_sessions:
                self.workspaces.stop_running(person.id)
            account, _ = self._account(person.id)
            self._set(person.id, account, signing_out=True)
        self.settle(person.id)
        return self.status(person)

    def settle(self, person_id: int) -> None:
        """Finish a sign-out that waits, once none of the person's servers runs, and
        while no sign-in of theirs waits for its code: that one would end it."""
        with self.lock(person_id):
            _, signing_out = self._account(person_id)
            pending = self._sign_ins.get(person_id)
            if pending is not None and time.monotonic() <= pending.until:
                return
            if not signing_out or self.workspaces.running(person_id):
                return
            person = self._person(person_id)
            if person is not None:
                self._sign_out_now(person)

    def _sign_out_now(self, person: people.Person) -> None:
        with answered():
            name = self.runtime.start_sign_in(person.id, person.slot, LOGOUT_SECONDS)
            try:
                layout = self.runtime.layout(name)
                said = self.runtime.exec(name, claude_command(layout, "auth", "logout"))
                if said.code != 0:  # advisory: the file is emptied below regardless
                    log.warning(
                        "claude auth logout answered %s for person %s", said.code, person.id
                    )
            finally:
                self.runtime.end_sign_in(name)
            self.runtime.clear_account(person.id, person.slot)
        self._set(person.id, None, signing_out=False)

    def close(self) -> None:
        with self._lock:
            sign_ins, self._sign_ins = list(self._sign_ins.values()), {}
        for sign_in in sign_ins:
            self._end(sign_in)
