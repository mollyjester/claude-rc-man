"""A workspace's files, under ``/api/workspaces/{workspace}/files``: a folder listed, a download
checked, a file or a folder downloaded (a folder as a zip), and a file uploaded into a folder.
Every path is resolved in the workspace by files.py's one resolver before anything else, and a
transfer is streamed through the workspace (transfers.py), never held whole here.

A download and an upload are each recorded once, as they are let through, before their first
byte (``scope="function"``): the row says the transfer was let through, or why it was refused,
not that every byte arrived.
"""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from helpers import rules
from rcman import files
from rcman.audit import Record, Recorder
from rcman.channel import ChannelBroken, ChannelTimeout, Result
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled
from rcman.runtime import (
    TIMED_OUT,
    RuntimeFailed,
    RuntimeUnavailable,
    Stream,
    StreamFailed,
    TransferIdle,
)
from rcman.transfers import CHUNK_BYTES, TRANSFER_IDLE_SECONDS, Transfers, in_thread
from rcman.workspaces import Workspace

log = logging.getLogger("rcman.transfers")

router = APIRouter(prefix="/api/workspaces/{workspace}/files", tags=["files"])

WorkspaceId = Annotated[int, Path(ge=1, description="The workspace's id.")]
FilesPath = Annotated[
    ruled(rules.FILES_PATH.or_empty(), max_length=rules.MAX_FILES_PATH_LENGTH),
    Query(description="The path in the workspace's files, from their top; empty for the top."),
]
FileName = Annotated[
    ruled(rules.FILE_NAME, max_length=rules.MAX_FILE_NAME_LENGTH),
    Query(description="The file's name in the folder."),
]

# How long each program of a folder's scan, find and du, may take in the workspace before it is
# killed there, and how much longer the scan's answer is waited for here.
SCAN_SECONDS = 60
SCAN_MARGIN_SECONDS = 15
# How long an upload whose program has ended is waited for, to say why it ended.
ENDED_SECONDS = 5
# What the workspace cannot be reached through just now.
_UNREACHABLE = (RuntimeFailed, RuntimeUnavailable, ChannelBroken, ChannelTimeout)
# The problem each of the scripts' refusals is answered with.
REFUSALS = {
    files.REFUSED: "file_path_refused",
    files.NOT_FOUND: "file_not_found",
    files.NOT_A_FOLDER: "not_a_folder",
    files.EXISTS: "file_exists",
    files.TAKEN: "file_name_taken_by_folder",
    files.NOT_A_FILE: "not_a_file",
}


def disposition(name: str) -> str:
    """``Content-Disposition`` for a download saved as ``name``: as it is where it is plain
    ASCII, with its UTF-8 spelling beside an ASCII stand-in otherwise."""
    if name.isascii() and name.isprintable() and not any(c in name for c in '"\\'):
        return f'attachment; filename="{name}"'
    plain = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


class TransferCut(Exception):
    """A download that cannot end whole: its answer is left unfinished, which the browser takes
    for a failed download, never for a whole one."""


class TransferResponse(StreamingResponse):
    """A streamed answer whose body is always closed, which ends its stream in the workspace,
    also when the browser goes away: Starlette leaves an async body it stopped reading open. A
    body cut (TransferCut) leaves the answer unfinished, which the server closes."""

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        except TransferCut:
            pass
        finally:
            await self.body_iterator.aclose()


def _answered(result: Result) -> Result:
    """The script's answer: its refusal as its problem, any other failure as
    ``files_unreachable``."""
    if result.code in REFUSALS:
        raise Problem(REFUSALS[result.code])
    if result.code != 0:
        log.warning("a files script failed with %s: %r", result.code, result.err[-2000:])
        raise Problem("files_unreachable")
    return result


def _asked(request: Request, found: Workspace, argv: list[str]) -> Result:
    """``argv`` run through the workspace's channel, answered (``_answered``)."""
    try:
        result = request.app.state.workspaces.run_in(found)(argv)
    except _UNREACHABLE:
        log.warning("the files of %s could not be reached", found.name, exc_info=True)
        raise Problem("files_unreachable") from None
    return _answered(result)


def _kind(request: Request, found: Workspace, path: str) -> files.Kind:
    layout = request.app.state.workspaces.layout_of(found)
    return files.parse_kind(_asked(request, found, files.kind_argv(layout, path)).out)


def _scanned(request: Request, found: Workspace, path: str) -> int | None:
    """Look through the folder ``path`` before it downloads, as an exec of its own whose find
    and du are each killed in the workspace at SCAN_SECONDS: ``folder_unreadable`` where
    something in it cannot be read, and ``folder_too_big`` where it is over the limit. Its size,
    where there is a limit."""
    service = request.app.state.workspaces
    settings = request.app.state.files_settings.settings()
    limit = settings.folder_limit_bytes
    argv = files.scan_argv(service.layout_of(found), path, limit, SCAN_SECONDS)
    wait = 2 * SCAN_SECONDS + SCAN_MARGIN_SECONDS
    try:
        result = service.runtime.exec(found.container, argv, timeout=wait, again=True)
    except _UNREACHABLE:
        log.warning("a folder of %s could not be looked through", found.name, exc_info=True)
        raise Problem("files_unreachable") from None
    if result.code in TIMED_OUT:
        raise Problem("folder_scan_timeout")
    try:
        unreadable, size = files.parse_scan(_answered(result).out, measured=bool(limit))
    except ValueError:
        log.warning("a folder of %s could not be measured: %r", found.name, result.out[-2000:])
        raise Problem("files_unreachable") from None
    if unreadable is not None:
        raise Problem("folder_unreadable", path=unreadable)
    if size is not None and size > limit:
        raise Problem("folder_too_big", size=size, limit_mb=settings.folder_download_limit_mb)
    return size


async def _downloadable(
    request: Request, found: Workspace, path: str, person: int
) -> tuple[files.Kind, int | None]:
    """What ``path`` is, once it may be downloaded: a file, or a folder looked through, which
    holds a transfer's place while it is; and its size, a folder's only where there is a
    limit."""
    kind = await anyio.to_thread.run_sync(_kind, request, found, path)
    if kind.kind == "file":
        return kind, kind.size
    if kind.kind != "folder":
        raise Problem("not_a_file")
    with request.app.state.transfers.admit(person):
        return kind, await in_thread(_scanned, request, found, path)


async def _ended(transfers: Transfers, found: Workspace, stream: Stream, admitted) -> None:
    """End the stream and give its place back, each whatever the others do, shielded."""
    with anyio.CancelScope(shield=True):
        try:
            await in_thread(transfers.end, found, stream)
        finally:
            try:
                transfers.forget(stream)
            finally:
                admitted.__exit__(None, None, None)


async def _streamed(transfers: Transfers, found: Workspace, person: int, argv: list[str]):
    """The body of a download: what ``argv`` writes, streamed. Its place is taken and its
    stream started only as the body starts, so an answer dropped before it holds nothing; one
    that cannot end whole is cut (TransferCut)."""
    with anyio.CancelScope(shield=True):  # what is taken here is always handed to the finally
        admitted = transfers.admit(person)
        try:
            admitted.__enter__()
        except Problem as refused:
            log.warning("a download from %s was refused as it started: %s", found.name, refused)
            raise TransferCut from None
        try:
            stream = await in_thread(transfers.start, found, argv)
        except BaseException as error:
            admitted.__exit__(None, None, None)
            log.warning("a download from %s did not start: %r", found.name, error)
            raise TransferCut from None
    try:
        while chunk := await in_thread(stream.read, CHUNK_BYTES, TRANSFER_IDLE_SECONDS):
            yield chunk
        code = await in_thread(stream.wait, TRANSFER_IDLE_SECONDS)
        if code != 0:
            raise StreamFailed(f"it ended with {code}")
    except (OSError, StreamFailed, TransferIdle) as error:
        ended = transfers.ended_code(stream)
        if ended is not None:
            log.info("a download from %s was cut: %s", found.name, ended)
        else:
            log.warning("a download from %s was cut: %s %s", found.name, error, stream.said)
        raise TransferCut from None
    finally:
        await _ended(transfers, found, stream, admitted)


@router.get("")
def listing(
    request: Request,
    workspace: WorkspaceId,
    path: FilesPath,
    identity: Identity = Depends(current_user),
) -> dict:
    """The folder ``path``: its entries, folders first, at most ``files.LISTING_CAP`` of them,
    and how many ``more`` there are; ``now`` and each entry's ``changed`` by the workspace's
    clock, in seconds since the epoch; and at the top only, the names rcman keeps there
    (``reserved``), which are never listed and cannot be uploaded."""
    service = request.app.state.workspaces
    found = service.ready(identity.person, workspace)
    layout = service.layout_of(found)
    listed = files.parse_listing(_asked(request, found, files.list_argv(layout, path)).out)
    answer = {
        "path": path,
        "entries": [
            {
                "name": e.name,
                "kind": e.kind,
                "size": e.size,
                "changed": e.changed,
                "target": e.target,
            }
            for e in listed.entries
        ],
        "more": listed.more,
        "now": listed.now,
    }
    if listed.top:
        answer["reserved"] = list(layout.hidden)
    return answer


@router.get("/download/check")
async def check(
    request: Request,
    workspace: WorkspaceId,
    path: FilesPath,
    identity: Identity = Depends(current_user),
) -> dict:
    """Whether ``path`` may be downloaded now, asked before the download itself, a navigation
    whose answer the page never sees: its ``kind`` (file or folder), the ``name`` it is saved
    as, and its ``size`` (a folder's only where there is a limit); or the refusal."""
    service = request.app.state.workspaces
    found = await anyio.to_thread.run_sync(service.ready, identity.person, workspace)
    kind, size = await _downloadable(request, found, path, identity.person)
    request.app.state.transfers.room(identity.person)
    return {"kind": kind.kind, "name": kind.download_name, "size": size}


@router.get("/download", response_class=TransferResponse)
async def download(
    request: Request,
    workspace: WorkspaceId,
    path: FilesPath,
    record: Record = Depends(Recorder("workspace.files.download"), scope="function"),
) -> TransferResponse:
    """The file ``path`` as it is, or the folder ``path`` as a zip, streamed as the workspace
    writes it; a folder is looked through again first. A navigation, so it carries no Origin:
    refused unless the browser says it comes from rcman's own pages
    (``Sec-Fetch-Site: same-origin``)."""
    service = request.app.state.workspaces
    transfers = request.app.state.transfers
    person = record.identity.person
    found = await anyio.to_thread.run_sync(service.ready, person, workspace)
    record.target = f"{found.name}/{path}"
    if request.headers.get("sec-fetch-site") != "same-origin":
        raise Problem("origin_refused")
    kind, _ = await _downloadable(request, found, path, person)
    transfers.room(person)
    layout = service.layout_of(found)
    argv = files.zip_argv(layout, path) if kind.kind == "folder" else files.cat_argv(layout, path)
    media = "application/zip" if kind.kind == "folder" else "application/octet-stream"
    headers = {"Content-Disposition": disposition(kind.download_name), "Cache-Control": "no-store"}
    body = _streamed(transfers, found, person, argv)
    return TransferResponse(body, media_type=media, headers=headers)


async def _refusal(stream: Stream) -> str:
    """Why an upload's program ended before it took all its input: its refusal's code, where it
    refused as it checked again, or ``transfer_failed``."""
    try:
        code = await in_thread(stream.wait, ENDED_SECONDS)
    except StreamFailed:
        return "transfer_failed"
    return REFUSALS.get(code, "transfer_failed")


@router.put("")
async def upload(
    request: Request,
    workspace: WorkspaceId,
    path: FilesPath,
    name: FileName,
    overwrite: Annotated[
        bool, Query(description="Replace a file of that name, which is refused without it.")
    ] = False,
    record: Record = Depends(Recorder("workspace.files.upload"), scope="function"),
) -> dict:
    """The request's body, streamed into the file ``name`` of the folder ``path``. A name a file
    has is refused without ``overwrite`` (``file_exists``), and a name a folder or a link has is
    always refused; each checked before the body is read, and again as it is written. Recorded
    as it is let through, before its first byte."""
    service = request.app.state.workspaces
    transfers = request.app.state.transfers
    person = record.identity.person
    found = await anyio.to_thread.run_sync(service.ready, person, workspace)
    record.target = f"{found.name}/{path}/{name}" if path else f"{found.name}/{name}"
    layout = service.layout_of(found)
    temporary = files.temporary_name()
    checking = files.upload_argv(layout, path, name, temporary, overwrite, only_check=True)
    await anyio.to_thread.run_sync(_asked, request, found, checking)
    argv = files.upload_argv(layout, path, name, temporary, overwrite, only_check=False)
    # Removed by the stream's end where it is left: by the path as given, which holds no line
    # break, as the channel that removes it carries none.
    left = f"{layout.volume}/{path}/{temporary}" if path else f"{layout.volume}/{temporary}"
    admitted = transfers.admit(person)
    admitted.__enter__()
    try:
        with anyio.CancelScope(shield=True):
            stream = await in_thread(transfers.start, found, argv, left)
    except BaseException:
        admitted.__exit__(None, None, None)
        raise
    try:
        await anyio.to_thread.run_sync(record.let_through)
        chunks = request.stream().__aiter__()
        while True:
            try:
                with anyio.fail_after(TRANSFER_IDLE_SECONDS):
                    chunk = await chunks.__anext__()
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise Problem("transfer_idle") from None
            if chunk:
                await in_thread(stream.write, chunk, TRANSFER_IDLE_SECONDS)
        stream.close_input()
        code = await in_thread(stream.wait, TRANSFER_IDLE_SECONDS)
    except ClientDisconnect:
        raise Problem("transfer_failed") from None  # nobody is there to be answered
    except (OSError, StreamFailed, TransferIdle) as error:
        ended = transfers.ended_code(stream)
        if ended is not None:
            raise Problem(ended) from None
        if isinstance(error, TransferIdle):
            raise Problem("transfer_idle") from None
        raise Problem(await _refusal(stream)) from None
    finally:
        await _ended(transfers, found, stream, admitted)
    if code in REFUSALS:
        raise Problem(REFUSALS[code])
    if code != 0:
        log.warning("an upload to %s ended with %s: %s", found.name, code, stream.said)
        raise Problem("transfer_failed")
    return {"name": name}
