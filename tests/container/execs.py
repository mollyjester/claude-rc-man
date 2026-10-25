"""The exec sessions of a container on the tests' engine (tests/podman_service.py), as Podman's
API tells them. Not a test module: tests/container/test_terminal_in_podman.py and
tests/container/test_forward_in_podman.py read with it which execs still run.
"""

from __future__ import annotations

import http.client
import json
import socket


def exec_ids(engine, name: str) -> list[str]:
    """The exec sessions of the container that are running, as Podman's API tells each one. A
    remote exec's record stays after it ends, until Podman's own cleanup removes it once the
    exit delay conmon is given has passed (``--exit-delay``), so the record alone is not a
    running exec."""
    found = engine.cli("container", "inspect", name, timeout=120)
    assert found.returncode == 0, found.stderr
    return [
        session
        for session in json.loads(found.stdout)[0].get("ExecIDs") or []
        if exec_session(engine, session).get("Running")
    ]


def exec_session(engine, session: str) -> dict:
    """What Podman's API says of an exec session: nothing for one already removed."""

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(30)
            self.sock.connect(str(engine.socket))

    connection = Connection("podman")
    try:
        connection.request("GET", f"/v4.0.0/libpod/exec/{session}/json")
        answer = connection.getresponse()
        body = answer.read()
    finally:
        connection.close()
    return json.loads(body) if answer.status == 200 else {}
