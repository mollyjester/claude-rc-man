"""tests/container/stack.py's relay of the browser tests' commands, on the machine: it runs only
``python3 tests/container/stack.py repository <digits>`` and ``serve-port <digits> <digits>``
and answers 2 to anything else, through its socket, which is its owner's alone; and the node
client the browser's container runs."""

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from devtools import held_folders as hf
from tests.container import stack as st

CLIENT = Path(__file__).resolve().parent / "container" / "stack_exec.mjs"
TAKEN = ["python3", "tests/container/stack.py", "repository", "12"]
SERVING = ["python3", "tests/container/stack.py", "serve-port", "12", "8080"]


@pytest.fixture
def relayed():
    ran = []

    def run(argv):
        ran.append(argv)
        return 0, "REPOSITORY\n"

    with hf.held("rcrelay-") as folder, st.command_relay(run, folder) as path:
        yield path, ran


def sent(path: Path, argv: list[str]) -> dict:
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(json.dumps(argv).encode())
        client.shutdown(socket.SHUT_WR)
        return json.loads(b"".join(iter(lambda: client.recv(65536), b"")))


def test_the_repository_verb_is_run_and_its_answer_returned(relayed):
    path, ran = relayed
    assert sent(path, TAKEN) == {"code": 0, "output": "REPOSITORY\n"}
    assert ran == [TAKEN]


def test_the_serve_port_verb_is_run(relayed):
    path, ran = relayed
    assert sent(path, SERVING)["code"] == 0 and ran == [SERVING]


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "tests/container/stack.py", "check"],
        [*TAKEN[:3], "12; rm -rf /"],
        [*TAKEN[:3], "12", "13"],
        ["sh", "-c", "id"],
        [],
        SERVING[:4],
        [*SERVING[:4], "80; id"],
        [*SERVING[:4], "٨٠"],
        TAKEN[:2],
    ],  # fmt: skip
    ids=[
        "another verb",
        "not digits",
        "more",
        "another program",
        "nothing",
        "serve-port short",
        "serve-port not digits",
        "serve-port other digits",
        "no verb",
    ],  # fmt: skip
)
def test_anything_else_is_answered_2_and_not_run(relayed, argv):
    path, ran = relayed
    answer = sent(path, argv)
    assert answer["code"] == 2 and ran == []


def test_the_socket_and_its_folder_are_the_owners_alone(relayed):
    path, _ = relayed
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_browsers_client_passes_on_the_answer_and_its_code(relayed):
    path, ran = relayed
    done = subprocess.run(["node", str(CLIENT), str(path), *TAKEN], capture_output=True, text=True)
    assert (done.returncode, done.stdout) == (0, "REPOSITORY\n") and ran == [TAKEN]
    refused = subprocess.run(["node", str(CLIENT), str(path), "sh"], capture_output=True, text=True)
    assert refused.returncode == 2 and ran == [TAKEN]


def test_the_playwright_image_is_the_lock_files_version():
    lock = json.loads((st.SRC / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert st.playwright_problem(st.PLAYWRIGHT_IMAGE, lock) is None


def test_a_playwright_image_of_another_version_is_found():
    """Planted twin: the image named at another version than the lock file's."""
    lock = json.loads((st.SRC / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    planted = st.PLAYWRIGHT_IMAGE.replace(f"v{st.PLAYWRIGHT_VERSION}-", "v1.0.0-")
    assert planted != st.PLAYWRIGHT_IMAGE  # the plant took effect
    assert st.playwright_problem(planted, lock)
    assert os.path.isfile(CLIENT)


def test_the_stacks_global_options_ask_what_they_are_given_or_nothing():
    """The gate that shows what Caddy does without the installer's on_demand_tls runs the
    stack's Caddy with no ``on_demand_tls``; every other start asks the address given."""
    block = "\ton_demand_tls {\n\t\task http://127.0.0.1:1/.rcman/ask\n\t}\n"
    asking = st.global_options("http://127.0.0.1:1/.rcman/ask")
    assert block in asking
    assert st.global_options() == st.global_options(st.ASK_URL)
    bare = st.global_options("")
    assert "on_demand_tls" not in bare and "ask" not in bare
    assert bare == asking.replace(block, "")
