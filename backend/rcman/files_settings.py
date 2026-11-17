"""The settings of a workspace's files, for administrators (routers/files_settings.py, the Service
settings page): the largest folder a person may download as a zip from the Files tab
(routers/files.py), in decimal megabytes (10**6 bytes), 0 for no limit. A single file's download
is never limited.

They are kept in the table ``files_settings``, one row (``id = 1``) the first save makes; no row
is ``DEFAULTS``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StrictInt

from rcman import db

FOLDER_LIMIT_MB_MAX = 1_000_000
MEGABYTE = 10**6


class FilesSettings(BaseModel):
    """What may be downloaded from a workspace. Every one is sent on a save."""

    folder_download_limit_mb: Annotated[
        StrictInt,
        Field(
            ge=0,
            le=FOLDER_LIMIT_MB_MAX,
            description="The largest folder a person may download, in megabytes of a million "
            "bytes; 0 for no limit.",
        ),
    ]

    @property
    def folder_limit_bytes(self) -> int:
        """The limit in bytes, 0 for none."""
        return self.folder_download_limit_mb * MEGABYTE


DEFAULTS = FilesSettings(folder_download_limit_mb=0)


def read_settings(con: sqlite3.Connection) -> FilesSettings:
    """The settings saved, or ``DEFAULTS`` where none are."""
    row = con.execute("select folder_download_limit_mb from files_settings where id = 1").fetchone()
    return DEFAULTS if row is None else FilesSettings(folder_download_limit_mb=row[0])


def write_settings(con: sqlite3.Connection, settings: FilesSettings) -> None:
    """Save the whole set, over any saved before."""
    con.execute(
        "insert into files_settings (id, folder_download_limit_mb) values (1, ?) on conflict (id) "
        "do update set folder_download_limit_mb = excluded.folder_download_limit_mb",
        (settings.folder_download_limit_mb,),
    )
    con.commit()


class Store:
    """The settings as rcman holds them (``app.state.files_settings``): read from the database
    once, then from memory, and taken up at once by a save."""

    def __init__(self, database: Path):
        self.database = database
        self._lock = threading.Lock()
        self._settings: FilesSettings | None = None

    def settings(self) -> FilesSettings:
        with self._lock:
            if self._settings is None:
                con = db.connect(self.database)
                try:
                    self._settings = read_settings(con)
                finally:
                    con.close()
            return self._settings

    def save(self, settings: FilesSettings) -> None:
        with self._lock:
            con = db.connect(self.database)
            try:
                write_settings(con, settings)
            finally:
                con.close()
            self._settings = settings
