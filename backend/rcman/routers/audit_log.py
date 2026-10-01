"""Reading the audit trail, for administrators."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from rcman import db
from rcman.identity import admin_user

router = APIRouter(prefix="/api/audit", tags=["audit"], dependencies=[Depends(admin_user)])
MAX_PAGE = 500


@router.get("")
def events(
    request: Request,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    before: int | None = Query(None, description="Only events older than this id."),
) -> dict:
    con = db.connect(request.app.state.settings.database)
    try:
        rows = con.execute(
            "select * from audit where (? is null or id < ?) order by id desc limit ?",
            (before, before, limit),
        ).fetchall()
    finally:
        con.close()
    return {"events": [dict(r) for r in rows]}
