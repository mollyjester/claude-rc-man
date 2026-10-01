"""Serving the built frontend: its files, and index.html for every other page."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from rcman.problems import Problem

CATCH_ALL = "/{path:path}"


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
            return FileResponse(candidate)
        return FileResponse(index)
