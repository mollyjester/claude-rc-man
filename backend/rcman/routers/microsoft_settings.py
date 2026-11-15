"""The settings of Microsoft sign-in, for administrators (microsoft_settings.py), on the Service
settings page."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from rcman import microsoft
from rcman.audit import Record, Recorder
from rcman.identity import admin_user
from rcman.microsoft_settings import MicrosoftSettingsBody, MicrosoftSignIn
from rcman.problems import (
    FIELD_MESSAGES,
    MESSAGES,
    MICROSOFT_INCOMPLETE,
    STATUS,
    STRING_EMPTY,
    Problem,
)

router = APIRouter(prefix="/api/microsoft", tags=["microsoft"], dependencies=[Depends(admin_user)])


def _shown(request: Request, stored: MicrosoftSignIn) -> dict:
    """The settings as an administrator sees them: never the client secret, only whether one
    is stored."""
    return {
        "settings": {
            "enabled": stored.enabled,
            "client_id": stored.client_id,
            "tenant_id": stored.tenant_id,
            "secret_set": stored.secret is not None,
            "redirect_uri": request.app.state.settings.public_origin + microsoft.CALLBACK_PATH,
            "available": request.app.state.sealer.available,
        }
    }


@router.get("/settings")
def settings_of(request: Request) -> dict:
    """The settings of Microsoft sign-in: whether it is on (``enabled``), the app
    registration's ``client_id`` and ``tenant_id``, whether a client secret is stored
    (``secret_set``; the secret itself is never given), the ``redirect_uri`` to register, and
    whether rcman has the secret key it needs (``available``)."""
    return _shown(request, request.app.state.microsoft_settings.settings())


@router.put("/settings")
def save(
    body: MicrosoftSettingsBody,
    request: Request,
    record: Record = Depends(Recorder("microsoft.settings")),
) -> dict:
    """Save the settings. A ``client_secret`` replaces the one stored, and one left out keeps
    it, and one of spaces alone is refused. Turning sign-in on without both IDs and a client
    secret, stored or sent, is refused at ``enabled``; without rcman's secret key, a save that
    turns sign-in on or sends a secret is refused with ``microsoft_unavailable``. A save that
    turns sign-in on or off, or changes either ID, signs out everyone signed in with Microsoft.
    The settings as they are then."""
    record.target = json.dumps(
        {
            "enabled": body.enabled,
            "client_id": body.client_id.lower(),
            "tenant_id": body.tenant_id.lower(),
            "secret_changed": body.client_secret is not None,
        }
    )
    sealer = request.app.state.sealer
    if not sealer.available and (body.enabled or body.client_secret is not None):
        raise Problem("microsoft_unavailable")

    def refused_at(field: str, message: str) -> Problem:
        return Problem(
            "validation_failed",
            STATUS["validation_failed"],
            MESSAGES["validation_failed"],
            errors=[{"field": field, "message": message}],
        )

    def refuse(before: MicrosoftSignIn) -> None:
        if body.client_secret is not None and not body.client_secret.strip():
            raise refused_at("client_secret", FIELD_MESSAGES[STRING_EMPTY])
        after = MicrosoftSignIn(
            enabled=body.enabled,
            client_id=body.client_id,
            tenant_id=body.tenant_id,
            secret=before.secret,
            generation=before.generation,
        )
        if body.enabled and not after.complete_with(body.client_secret is not None):
            raise refused_at("enabled", MICROSOFT_INCOMPLETE)

    stored = request.app.state.microsoft_settings.save(body, sealer, refuse)
    return _shown(request, stored)
