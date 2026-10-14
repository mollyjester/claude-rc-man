"""What administrators are warned about, built fresh on every request: servers on a
screen rcman does not recognise, named by person and workspace but never with the
screen's text."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rcman import everyone
from rcman.identity import admin_user

router = APIRouter(prefix="/api/attention", tags=["attention"], dependencies=[Depends(admin_user)])


@router.get("")
def attention(request: Request) -> dict:
    return {
        "unrecognised": [
            {"email": person["email"], "workspace": workspace["name"]}
            for person in everyone.everyone(request)
            for workspace in person["workspaces"]
            if (workspace["server"] or {}).get("state") == "unrecognised"
        ]
    }
