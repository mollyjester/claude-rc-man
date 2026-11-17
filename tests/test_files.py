"""A workspace's files through the routes (backend/rcman/routers/files.py), on the host runtime.

A download and an upload stream: the test client holds a whole answer before it hands any of
it over, so these run through real uvicorn, in a thread of the test's own, serving the test's
app, whose service the ``client`` fixture has started.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest
import uvicorn
from rcman import audit
from rcman import runtime as rt

from tests.conftest import ALICE, as_user, changing, made_workspace
from tests.hang_guard import HANG_GUARD
from tests.until import until


@pytest.fixture
def served(app, client):
    """The app, as the ``client`` fixture started it, served by uvicorn on a port of its own:
    the port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    serving = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    serving.start()
    try:
        until(lambda: server.started)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        serving.join(HANG_GUARD)
        listener.close()
        assert not serving.is_alive(), "uvicorn did not stop"


@contextlib.contextmanager
def connected(port: int):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HANG_GUARD)
    try:
        yield conn
    finally:
        conn.close()


def rows(settings, action: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(settings.database)
    try:
        found = con.execute("select target, result from audit where action = ?", (action,))
        return list(found)
    finally:
        con.close()


def layout_of(app, made: dict):
    service = app.state.workspaces
    return service.layout_of(service.get(made["id"]))


def transfers_left(layout) -> list[str]:
    """The streams' pidfiles left in the workspace's runtime folder."""
    return sorted(p.name for p in Path(layout.run).glob("transfer-*.pid"))


def downloading(made: dict, path: str) -> str:
    return f"/api/workspaces/{made['id']}/files/download?path={path}"


def files_of(made: dict) -> str:
    return f"/api/workspaces/{made['id']}/files"


NAVIGATION = {**as_user(ALICE), "Sec-Fetch-Site": "same-origin"}


def test_a_folder_is_streamed_as_a_zip_and_audited_once_before_the_first_byte(
    app, client, served, settings, monkeypatch
):
    made = made_workspace(client, ALICE, "alpha")
    layout = layout_of(app, made)
    source = Path(layout.work, "src")
    (source / "b").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"a")
    (source / "b" / "c.txt").write_bytes(b"c")
    with connected(served) as conn:
        conn.request("GET", downloading(made, "work/src"), headers=NAVIGATION)
        answer = conn.getresponse()  # its head only
        assert answer.status == 200, answer.read()
        assert answer.getheader("Content-Disposition") == 'attachment; filename="src.zip"'
        assert rows(settings, "workspace.files.download") == [("alpha/work/src", "ok")]
        archive = zipfile.ZipFile(io.BytesIO(answer.read()))
    files = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
    assert files == {"src/a.txt": b"a", "src/b/c.txt": b"c"}
    assert rows(settings, "workspace.files.download") == [("alpha/work/src", "ok")]
    assert transfers_left(layout) == []

    # The top: what is there, but the hidden folders, each of which is there.
    for hidden in (rt.CONFIG, rt.HOME, rt.CONTAINERS):
        Path(layout.volume, hidden, "x").mkdir(parents=True, exist_ok=True)
    Path(layout.volume, "notes.txt").write_bytes(b"n")
    with connected(served) as conn:
        conn.request("GET", downloading(made, ""), headers=NAVIGATION)
        answer = conn.getresponse()
        assert answer.status == 200, answer.read()
        assert answer.getheader("Content-Disposition") == 'attachment; filename="rcman.zip"'
        top = zipfile.ZipFile(io.BytesIO(answer.read())).namelist()
    assert "notes.txt" in top and "work/src/b/c.txt" in top
    assert not [n for n in top if n.split("/")[0] in layout.hidden], top

    # A client that goes away after the first bytes: more than the sockets hold is left unsent,
    # so the zip's program is still writing, and is ended - killed, not left to finish - with
    # its pidfile.
    transfers, ended = app.state.transfers, []
    end = transfers.end

    def ending(workspace, stream):
        end(workspace, stream)
        ended.append(stream.process.returncode)

    monkeypatch.setattr(transfers, "end", ending)
    (source / "big").write_bytes(os.urandom(32 * 1024 * 1024))
    with connected(served) as conn:
        conn.request("GET", downloading(made, "work/src"), headers=NAVIGATION)
        answer = conn.getresponse()
        assert answer.status == 200 and answer.read(1)
        assert transfers_left(layout) != []  # the positive for its absence below
    until(lambda: transfers_left(layout) == [] and ended)
    assert ended != [0], "the zip ran to its end"
    assert transfers._all == 0


def test_an_upload_is_streamed_into_the_folder_and_audited(app, client, served, settings):
    made = made_workspace(client, ALICE, "alpha")
    layout = layout_of(app, made)
    sent = [os.urandom(64 * 1024) for _ in range(5)]
    with connected(served) as conn:
        url = f"/api/workspaces/{made['id']}/files?path=work&name=n.bin"
        conn.request("PUT", url, body=iter(sent), headers=changing(ALICE))  # chunked, no length
        answer = conn.getresponse()
        assert answer.status == 200, answer.read()
    assert Path(layout.work, "n.bin").read_bytes() == b"".join(sent)
    assert rows(settings, "workspace.files.upload") == [("alpha/work/n.bin", "ok")]
    assert [p.name for p in Path(layout.work).iterdir() if p.name.startswith(".rcman")] == []
    assert transfers_left(layout) == []


def test_the_top_is_listed_folders_first_without_claude_home_and_containers(app, client):
    made = made_workspace(client, ALICE, "alpha")
    layout = layout_of(app, made)
    hidden = (rt.CONFIG, rt.HOME, rt.CONTAINERS)
    for name in hidden:  # each is there, for its absence below
        Path(layout.volume, name).mkdir(exist_ok=True)
    Path(layout.volume, "notes.txt").write_bytes(b"n")
    r = client.get(files_of(made), params={"path": ""}, headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    listed = r.json()
    names = [e["name"] for e in listed["entries"]]
    assert names[0] == rt.WORK and "notes.txt" in names
    assert not set(hidden) & set(names) and set(hidden) <= set(listed["reserved"])
    notes = next(e for e in listed["entries"] if e["name"] == "notes.txt")
    assert (notes["kind"], notes["size"], listed["more"]) == ("file", 1, 0)
    assert listed["now"] >= notes["changed"] > 0
    # The folder the workspace's own Podman keeps its storage in is the one hidden.
    storage = (Path(rt.__file__).parent / "podman" / "storage.conf").read_text()
    assert f'graphroot="{rt.VOLUME_MOUNT}/{rt.CONTAINERS}/storage"' in storage


def test_a_file_downloads_as_it_is(app, client):
    made = made_workspace(client, ALICE, "alpha")
    Path(layout_of(app, made).work, "a b.txt").write_bytes(b"bytes")
    path = {"path": "work/a b.txt"}
    check = client.get(f"{files_of(made)}/download/check", params=path, headers=as_user(ALICE))
    assert check.json() == {"kind": "file", "name": "a b.txt", "size": 5}
    r = client.get(f"{files_of(made)}/download", params=path, headers=NAVIGATION)
    assert (r.status_code, r.content) == (200, b"bytes")
    assert r.headers["content-disposition"] == 'attachment; filename="a b.txt"'


def test_the_resolver_refuses_escapes_and_hidden_folders_and_lets_a_link_inside_through(
    app, client
):
    made = made_workspace(client, ALICE, "alpha")
    work = Path(layout_of(app, made).work)
    (work / "src").mkdir()
    (work / "src" / "f").write_bytes(b"f")
    (work / "out").symlink_to(f"../{rt.CONFIG}")
    (work / "up").symlink_to("/")
    (work / "in").symlink_to("src")
    for path in (f"work/../{rt.CONFIG}", f"{rt.CONFIG}/x", "work/out", "work/up"):
        r = client.get(files_of(made), params={"path": path}, headers=as_user(ALICE))
        assert (r.status_code, r.json()["code"]) == (400, "file_path_refused"), path
    r = client.get(files_of(made), params={"path": "work/in"}, headers=as_user(ALICE))
    assert [e["name"] for e in r.json()["entries"]] == ["f"]
    # Every route resolves through it.
    up = {"path": "work/up"}
    answers = [
        client.get(f"{files_of(made)}/download/check", params=up, headers=as_user(ALICE)),
        client.get(f"{files_of(made)}/download", params=up, headers=NAVIGATION),
        client.put(
            files_of(made), params={**up, "name": "x"}, content=b"x", headers=changing(ALICE)
        ),
    ]
    assert [r.json()["code"] for r in answers] == ["file_path_refused"] * 3


def test_an_upload_over_an_existing_name_is_refused_without_overwrite(app, client):
    made = made_workspace(client, ALICE, "alpha")
    target = Path(layout_of(app, made).work, "n.txt")
    target.write_bytes(b"old")
    sent = {"path": "work", "name": "n.txt"}
    r = client.put(files_of(made), params=sent, content=b"new", headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "file_exists")
    assert target.read_bytes() == b"old"
    sent["overwrite"] = "true"
    r = client.put(files_of(made), params=sent, content=b"new", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert target.read_bytes() == b"new"


def test_an_upload_of_a_hidden_name_at_the_top_is_refused(app, client):
    made = made_workspace(client, ALICE, "alpha")
    volume = Path(layout_of(app, made).volume)
    r = client.put(
        files_of(made), params={"path": "", "name": "notes.txt"}, content=b"n",
        headers=changing(ALICE),
    )  # fmt: skip
    assert r.status_code == 200 and (volume / "notes.txt").read_bytes() == b"n"
    r = client.put(
        files_of(made), params={"path": "", "name": rt.CONFIG}, content=b"x",
        headers=changing(ALICE),
    )  # fmt: skip
    assert (r.status_code, r.json()["code"]) == (400, "file_path_refused")
    assert (volume / rt.CONFIG).is_dir()


SETTINGS = "/api/files/settings"


def test_a_folder_over_the_limit_is_refused_and_0_lets_it_through(app, client):
    made = made_workspace(client, ALICE, "alpha")
    big = Path(layout_of(app, made).work, "big")
    big.mkdir()
    (big / "f").write_bytes(bytes(2 * 10**6))
    path = {"path": "work/big"}
    check, download = f"{files_of(made)}/download/check", f"{files_of(made)}/download"
    limit = {"folder_download_limit_mb": 1}
    assert client.put(SETTINGS, json=limit, headers=changing(ALICE)).status_code == 200
    r = client.get(check, params=path, headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"], r.json()["limit_mb"]) == (413, "folder_too_big", 1)
    assert r.json()["size"] >= 2 * 10**6
    assert client.get(download, params=path, headers=NAVIGATION).json()["code"] == "folder_too_big"
    limit = {"folder_download_limit_mb": 0}
    assert client.put(SETTINGS, json=limit, headers=changing(ALICE)).status_code == 200
    r = client.get(check, params=path, headers=as_user(ALICE))
    assert r.json() == {"kind": "folder", "name": "big.zip", "size": None}
    r = client.get(download, params=path, headers=NAVIGATION)
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).read("big/f") == bytes(2 * 10**6)


def test_the_files_settings_are_saved_read_back_and_audited(client, settings):
    defaults = {"settings": {"folder_download_limit_mb": 0}}
    assert client.get(SETTINGS, headers=as_user(ALICE)).json() == defaults
    sent = {"folder_download_limit_mb": 500}
    r = client.put(SETTINGS, json=sent, headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"settings": sent})
    assert client.get(SETTINGS, headers=as_user(ALICE)).json() == {"settings": sent}
    assert rows(settings, "files.settings") == [('{"folder_download_limit_mb": 500}', "ok")]


def uploading(layout) -> list[str]:
    """The temporary files of uploads under way in the work folder."""
    return [p.name for p in Path(layout.work).iterdir() if p.name.startswith(".rcman-upload-")]


def test_a_download_whose_answer_is_dropped_holds_no_place_and_leaves_nothing_running(
    app, client, monkeypatch
):
    made = made_workspace(client, ALICE, "alpha")
    layout = layout_of(app, made)
    Path(layout.work, "f").write_bytes(b"f")
    written = audit.write

    def failing(con, record, result):
        if record.action == "workspace.files.download":
            raise sqlite3.OperationalError("disk I/O error")
        written(con, record, result)

    monkeypatch.setattr(audit, "write", failing)
    # The row cannot be written, so the answer is dropped before its body starts.
    with contextlib.suppress(sqlite3.OperationalError):
        r = client.get(f"{files_of(made)}/download", params={"path": "work/f"}, headers=NAVIGATION)
        assert r.status_code == 500
    transfers = app.state.transfers
    assert (transfers._all, transfers._live) == (0, {})
    assert transfers_left(layout) == []


def test_a_stop_during_an_upload_ends_it_with_its_code_and_removes_its_file(app, client, served):
    made = made_workspace(client, ALICE, "alpha")
    layout = layout_of(app, made)
    more, answered = threading.Event(), {}

    def body():
        yield b"first"
        more.wait(HANG_GUARD)
        yield b"second"

    def put():
        with connected(served) as conn:
            url = f"{files_of(made)}?path=work&name=n.bin"
            conn.request("PUT", url, body=body(), headers=changing(ALICE))
            answer = conn.getresponse()
            answered["answer"] = (answer.status, json.loads(answer.read())["code"])

    sending = threading.Thread(target=put, daemon=True)
    sending.start()
    try:
        until(lambda: uploading(layout))  # under way
        r = client.post(f"/api/workspaces/{made['id']}/stop", headers=changing(ALICE))
        assert r.status_code == 200, r.text
    finally:
        more.set()
        sending.join(HANG_GUARD)
    assert not sending.is_alive()
    assert answered["answer"] == (409, "workspace_stopped")
    assert uploading(layout) == [] and transfers_left(layout) == []
    assert not Path(layout.work, "n.bin").exists()
    assert app.state.transfers._all == 0
