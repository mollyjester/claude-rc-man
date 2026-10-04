"""The journal: where rcman's own events go, one JSON line each."""

from __future__ import annotations

import logging
import sys


def install(log: logging.Logger) -> None:
    """Send a logger's events to standard error, one bare line each, which systemd sends
    to the journal.

    uvicorn configures only its own loggers, so without this the events go nowhere.
    """
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
