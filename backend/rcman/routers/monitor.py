"""The Monitor page, for administrators: what the host, each person and each workspace container
use now, and the monitor's settings (monitor.py)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from rcman.audit import Record, Recorder
from rcman.identity import admin_user
from rcman.monitor import MonitorSettings

router = APIRouter(prefix="/api/monitor", tags=["monitor"], dependencies=[Depends(admin_user)])


@router.get("")
def snapshot(request: Request) -> dict:
    """What is known now. ``scopes``: which of ``host``, ``people`` and ``containers`` are
    collected; ``usage`` and ``disk``: when each round last collected (``collected_at``, null
    before its first) and why its last run failed (``error``, null where it did not). ``host``,
    ``people`` and ``containers`` are null for a scope that is off; any figure not known is
    null, never 0. CPU is a percent of one core."""
    return request.app.state.monitor.snapshot()


@router.get("/settings")
def settings_of(request: Request) -> dict:
    """The monitor's settings: those saved, or the defaults where none are."""
    return {"settings": request.app.state.monitor.settings().model_dump()}


@router.put("/settings")
def save(
    body: MonitorSettings,
    request: Request,
    record: Record = Depends(Recorder("monitor.settings")),
) -> dict:
    """Save the whole set of settings. Each round takes them up at once: a scope switched off is
    hidden at once, and each interval counts from that round's last run."""
    record.target = json.dumps(body.model_dump())
    request.app.state.monitor.save(body)
    return {"settings": body.model_dump()}
