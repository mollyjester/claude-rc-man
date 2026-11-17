"""The settings of a workspace's files, for administrators (files_settings.py), on the Service
settings page."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from rcman.audit import Record, Recorder
from rcman.files_settings import FilesSettings
from rcman.identity import admin_user

router = APIRouter(prefix="/api/files", tags=["files"], dependencies=[Depends(admin_user)])


@router.get("/settings")
def settings_of(request: Request) -> dict:
    """The settings of a workspace's files: those saved, or the defaults where none are."""
    return {"settings": request.app.state.files_settings.settings().model_dump()}


@router.put("/settings")
def save(
    body: FilesSettings,
    request: Request,
    record: Record = Depends(Recorder("files.settings")),
) -> dict:
    """Save the whole set of settings. They are taken up at once, by the next download's check."""
    record.target = json.dumps(body.model_dump())
    request.app.state.files_settings.save(body)
    return {"settings": body.model_dump()}
