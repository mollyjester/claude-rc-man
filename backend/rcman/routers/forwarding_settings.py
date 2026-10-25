"""The settings of port forwarding, for administrators (forwarding_settings.py), on the Service
settings page."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from rcman.audit import Record, Recorder
from rcman.forwarding_settings import ForwardingSettings
from rcman.identity import admin_user

router = APIRouter(
    prefix="/api/forwarding", tags=["forwarding"], dependencies=[Depends(admin_user)]
)


@router.get("/settings")
def settings_of(request: Request) -> dict:
    """The settings of port forwarding: those saved, or the defaults where none are."""
    return {"settings": request.app.state.forwarding_settings.settings().model_dump()}


@router.put("/settings")
def save(
    body: ForwardingSettings,
    request: Request,
    record: Record = Depends(Recorder("forwarding.settings")),
) -> dict:
    """Save the whole set of settings. They are taken up at once; a save that leaves forwarding
    off tells whatever forwards, in the process, before it answers."""
    record.target = json.dumps(body.model_dump())
    request.app.state.forwarding_settings.save(body)
    return {"settings": body.model_dump()}
