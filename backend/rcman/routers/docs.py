"""The documentation pages in docs/, served as Markdown."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request

from rcman.identity import current_user
from rcman.problems import Problem

router = APIRouter(prefix="/api/docs", tags=["docs"], dependencies=[Depends(current_user)])
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def _title(markdown: str, slug: str) -> str:
    first = next((line for line in markdown.splitlines() if line.startswith("# ")), None)
    return first[2:].strip() if first else slug


@router.get("")
def pages(request: Request) -> dict:
    directory = request.app.state.settings.docs_dir
    found = []
    for path in sorted(directory.glob("*.md")):
        found.append(
            {"slug": path.stem, "title": _title(path.read_text(encoding="utf-8"), path.stem)}
        )
    return {"pages": found}


@router.get("/{slug}")
def page(slug: str, request: Request) -> dict:
    path = request.app.state.settings.docs_dir / f"{slug}.md"
    if not SLUG.fullmatch(slug) or not path.is_file():
        raise Problem("not_found")
    markdown = path.read_text(encoding="utf-8")
    return {"slug": slug, "title": _title(markdown, slug), "markdown": markdown}
