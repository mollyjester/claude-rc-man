"""The settings of port forwarding, for administrators (routers/forwarding_settings.py, the
Service settings page): whether ports are to be forwarded, how long a forward session is to last
with nothing going on, and how long in all. The forwarding server (forward.py) reads them on
every request: while forwarding is on, each port a ready workspace's plan lists is reached by its
owner's browser, and no one else's.

They are kept in the table ``forwarding_settings``, one row (``id = 1``) the first save makes;
no row is ``DEFAULTS``. The model speaks in the units the page shows, whole minutes and whole
hours, so that a value refused is refused with a bound in its own field's unit; the table keeps
seconds (``idle_seconds``, ``absolute_seconds``), which is what a session is counted in. An idle
timeout as long as the lifetime, or longer, would never end a session, and is refused at the idle
field.

``Store.on_off`` registers a listener that every save leaving forwarding off calls, in the
saving request's thread, once the settings are stored and taken up, so that whatever forwards
can end its sessions at once, in the process.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from rcman import db
from rcman.problems import RULE_REFUSED

IDLE_MINUTES_DEFAULT, IDLE_MINUTES_MIN, IDLE_MINUTES_MAX = 5, 1, 1440
ABSOLUTE_HOURS_DEFAULT, ABSOLUTE_HOURS_MIN, ABSOLUTE_HOURS_MAX = 12, 1, 168
MINUTE, HOUR = 60, 3600
IDLE_NOT_SHORTER = "The idle timeout must be shorter than the absolute lifetime."


class ForwardingSettings(BaseModel):
    """Whether ports are forwarded, and how long a session lasts. Every one is sent on a save."""

    enabled: Annotated[StrictBool, Field(description="Forward the ports workspaces list.")]
    # Before the idle timeout, which is held to it.
    absolute_hours: Annotated[
        StrictInt,
        Field(
            ge=ABSOLUTE_HOURS_MIN,
            le=ABSOLUTE_HOURS_MAX,
            description="Hours a session lasts in all.",
        ),
    ]
    idle_minutes: Annotated[
        StrictInt,
        Field(
            ge=IDLE_MINUTES_MIN,
            le=IDLE_MINUTES_MAX,
            description="Minutes a session lasts with nothing going on; less than its lifetime.",
        ),
    ]

    @field_validator("idle_minutes")
    @classmethod
    def _shorter_than_the_lifetime(cls, minutes: int, info: ValidationInfo) -> int:
        hours = info.data.get("absolute_hours")  # absent where it was refused itself
        if hours is not None and minutes * MINUTE >= hours * HOUR:
            raise PydanticCustomError(RULE_REFUSED, "{sentence}", {"sentence": IDLE_NOT_SHORTER})
        return minutes

    @property
    def idle_seconds(self) -> int:
        return self.idle_minutes * MINUTE

    @property
    def absolute_seconds(self) -> int:
        return self.absolute_hours * HOUR


DEFAULTS = ForwardingSettings(
    enabled=False, idle_minutes=IDLE_MINUTES_DEFAULT, absolute_hours=ABSOLUTE_HOURS_DEFAULT
)


def read_settings(con: sqlite3.Connection) -> ForwardingSettings:
    """The settings saved, or ``DEFAULTS`` where none are."""
    row = con.execute(
        "select enabled, idle_seconds, absolute_seconds from forwarding_settings where id = 1"
    ).fetchone()
    if row is None:
        return DEFAULTS
    return ForwardingSettings(
        enabled=bool(row[0]), idle_minutes=row[1] // MINUTE, absolute_hours=row[2] // HOUR
    )


def write_settings(con: sqlite3.Connection, settings: ForwardingSettings) -> None:
    """Save the whole set, over any saved before."""
    con.execute(
        "insert into forwarding_settings (id, enabled, idle_seconds, absolute_seconds) "
        "values (1, ?, ?, ?) on conflict (id) do update set enabled = excluded.enabled, "
        "idle_seconds = excluded.idle_seconds, absolute_seconds = excluded.absolute_seconds",
        (int(settings.enabled), settings.idle_seconds, settings.absolute_seconds),
    )
    con.commit()


class Store:
    """The settings as rcman holds them (``app.state.forwarding_settings``): read from the
    database once, then from memory, and taken up at once by a save."""

    def __init__(self, database: Path):
        self.database = database
        self._lock = threading.Lock()
        # Held across a save's write, its taking up and its listeners, so that two saves take
        # effect in the order they were stored.
        self._saving = threading.Lock()
        self._settings: ForwardingSettings | None = None
        self._off: list[Callable[[], None]] = []

    def settings(self) -> ForwardingSettings:
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

    def on_off(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` after every save that leaves forwarding off."""
        self._off.append(listener)

    def save(self, settings: ForwardingSettings) -> None:
        """Store the settings and take them up; with forwarding off, then call every listener,
        in the order they were registered. A listener that raises keeps none of the others from
        being called; the first exception is raised once all have been, failing the save's
        answer, and the settings stay stored."""
        with self._saving:
            con = db.connect(self.database)
            try:
                write_settings(con, settings)
            finally:
                con.close()
            with self._lock:
                self._settings = settings
            if settings.enabled:
                return
            failed: Exception | None = None
            for listener in list(self._off):
                try:
                    listener()
                except Exception as error:  # noqa: BLE001 - raised after the others are called
                    failed = failed or error
            if failed is not None:
                raise failed
