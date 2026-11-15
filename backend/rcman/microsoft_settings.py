"""The settings of Microsoft sign-in, for administrators (routers/microsoft_settings.py, the
Service settings page): whether people may sign in with a Microsoft work account as well as with
Google, and the Microsoft Entra app registration rcman signs them in through, its application
(client) ID, its directory (tenant) ID and its client secret. Sign-in itself is microsoft.py's,
which reads them on every request, so a save applies at once and without a restart.

They are kept in the table ``microsoft_sign_in``, one row (``id = 1``) the first save makes; no
row is ``DEFAULTS``. The IDs are held to ``helpers.rules.GUID``, either empty, and kept in
lowercase. The client secret is never read back out of rcman: it is kept sealed
(sealing.py, ``STORED_SECRET``), without the spaces around it, a save that sends none keeps the
one stored, and the settings say only whether one is stored. Without rcman's secret key only a
save that leaves sign-in off and sends no secret is taken. Turning sign-in on needs both
IDs and a client secret, stored or sent (``MicrosoftSignIn.complete_with``).

``generation`` counts the saves that changed whether sign-in is on, or either ID. A Microsoft
session carries the generation it was issued under, and one that differs is refused
(identity.py), so such a save signs out everyone signed in with Microsoft.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictStr

from helpers import rules
from rcman import db, sealing
from rcman.problems import ruled

MOST_SECRET_LENGTH = 256
Guid = ruled(rules.GUID.or_empty())


class MicrosoftSettingsBody(BaseModel):
    """What a save sends: everything but the client secret, which is sent only to replace the
    one stored."""

    enabled: Annotated[StrictBool, Field(description="Let people sign in with Microsoft.")]
    client_id: Annotated[
        Guid, Field(description="The app registration's application (client) ID, or empty.")
    ]
    tenant_id: Annotated[
        Guid,
        Field(description="The directory (tenant) ID whose members may sign in, or empty."),
    ]
    client_secret: Annotated[
        StrictStr | None,
        Field(
            min_length=1,
            max_length=MOST_SECRET_LENGTH,
            description="A client secret's value, to replace the one stored; left out or null "
            "keeps it.",
        ),
    ] = None


@dataclass(frozen=True)
class MicrosoftSignIn:
    """The settings as they are kept: the client secret sealed, or None where none is."""

    enabled: bool
    client_id: str
    tenant_id: str
    secret: str | None
    generation: int

    def complete_with(self, secret_sent: bool) -> bool:
        """Whether sign-in may be on: both IDs, and a secret stored or sent."""
        return bool(self.client_id and self.tenant_id and (secret_sent or self.secret))


DEFAULTS = MicrosoftSignIn(enabled=False, client_id="", tenant_id="", secret=None, generation=0)


def read_settings(con: sqlite3.Connection) -> MicrosoftSignIn:
    """The settings saved, or ``DEFAULTS`` where none are."""
    row = con.execute(
        "select enabled, client_id, tenant_id, secret, generation from microsoft_sign_in "
        "where id = 1"
    ).fetchone()
    if row is None:
        return DEFAULTS
    return MicrosoftSignIn(
        enabled=bool(row[0]), client_id=row[1], tenant_id=row[2], secret=row[3], generation=row[4]
    )


def write_settings(con: sqlite3.Connection, settings: MicrosoftSignIn) -> None:
    """Save the whole set, over any saved before."""
    con.execute(
        "insert into microsoft_sign_in (id, enabled, client_id, tenant_id, secret, generation) "
        "values (1, ?, ?, ?, ?, ?) on conflict (id) do update set enabled = excluded.enabled, "
        "client_id = excluded.client_id, tenant_id = excluded.tenant_id, "
        "secret = excluded.secret, generation = excluded.generation",
        (
            int(settings.enabled),
            settings.client_id,
            settings.tenant_id,
            settings.secret,
            settings.generation,
        ),
    )
    con.commit()


def changed(before: MicrosoftSignIn, body: MicrosoftSettingsBody, sealer) -> MicrosoftSignIn:
    """The settings ``body`` saves over ``before``: its IDs in lowercase, its secret sealed
    with ``sealer`` or the stored one kept, and the generation increased where sign-in was
    turned on or off or either ID changed."""
    after = replace(
        before,
        enabled=body.enabled,
        client_id=body.client_id.lower(),
        tenant_id=body.tenant_id.lower(),
    )
    if body.client_secret is not None:
        sent = body.client_secret.strip()
        after = replace(after, secret=sealer.seal(sealing.STORED_SECRET, sent.encode("utf-8")))
    if (after.enabled, after.client_id, after.tenant_id) != (
        before.enabled,
        before.client_id,
        before.tenant_id,
    ):
        after = replace(after, generation=before.generation + 1)
    return after


class Store:
    """The settings as rcman holds them (``app.state.microsoft_settings``): read from the
    database once, then from memory, and taken up at once by a save."""

    def __init__(self, database: Path):
        self.database = database
        self._lock = threading.Lock()
        # Held across a save's read, write and taking up, so that two saves cannot both build
        # on the same settings, and take effect in the order they were stored.
        self._saving = threading.Lock()
        self._settings: MicrosoftSignIn | None = None

    def settings(self) -> MicrosoftSignIn:
        with self._lock:
            if self._settings is not None:
                return self._settings
        con = db.connect(self.database)
        try:
            read = read_settings(con)
        finally:
            con.close()
        with self._lock:
            if self._settings is None:
                self._settings = read
            return self._settings

    def save(self, body: MicrosoftSettingsBody, sealer, refuse) -> MicrosoftSignIn:
        """Store what ``body`` saves and take it up; ``refuse`` is called first, with the
        settings it would save over, and may raise to refuse the save."""
        with self._saving:
            before = self.settings()
            refuse(before)
            after = changed(before, body, sealer)
            con = db.connect(self.database)
            try:
                write_settings(con, after)
            finally:
                con.close()
            with self._lock:
                self._settings = after
            return after
