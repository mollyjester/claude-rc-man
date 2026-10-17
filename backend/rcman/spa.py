"""Serving the built frontend: its files, and index.html for every other page.

index.html names the build's scripts and stylesheet by the hashes of their contents, which
Vite puts in their names under assets/; a browser asks again for every file outside assets/
before it uses the one it kept (``no-cache``), so that a page loaded after an upgrade names
the new build's files, and keeps those under assets/, which never change, for a year."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from rcman.problems import Problem

CATCH_ALL = "/{path:path}"
# The files whose names carry the hash of what they hold, as Vite builds them.
HASHED = "assets/"
REVALIDATE = {"Cache-Control": "no-cache"}
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def install(app: FastAPI, frontend_dir: Path) -> None:
    root = frontend_dir.resolve()

    @app.get(CATCH_ALL, include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise Problem("not_found")
        index = root / "index.html"
        if not index.is_file():
            raise Problem("not_found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            hashed = (root / HASHED) in candidate.parents
            return FileResponse(candidate, headers=IMMUTABLE if hashed else REVALIDATE)
        return FileResponse(index, headers=REVALIDATE)
