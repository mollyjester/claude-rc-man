"""The tests' GitHub: its certificates, the workspace image that trusts them and finds it, and a
service that serves it.

Not a test module, and the standard library only, so that the whole stack's container can run it
as it is from the checkout's files at /src. Shared by tests/container/test_github_in_podman.py,
which serves tests/github_server.py as https://github.com itself, in a network of its own
(tests/container/network.py), and by tests/container/stack.py, which serves it behind Caddy
(``serve`` below).

- ``mint`` makes a certificate authority and a certificate it signed for github.com;
- ``dockerfile`` derives a workspace image from a base image that trusts that authority and
  whose git finds github.com at the network's own loopback (RESOLVE: a workspace that joins
  another container's network gets no hosts file of that container's), and, for a base that is
  not rcman's own, has git and python3;
- run as a program, ``serve`` serves the bare repositories under a folder with the token,
  after making the public and the private repository the whole-stack browser tests clone.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

CA_NAME = "rcman-test-github-ca"
# The token the whole-stack browser tests give the Settings page, and the stand-in takes.
STACK_TOKEN = "github_pat_stack-test-token"
# The repositories the stand-in is made with for them: owner, name, and whether private.
STACK_OWNER = "octo"
STACK_PUBLIC = "stack-public"
STACK_PRIVATE = "stack-private"
STACK_FILE = "README.md"
# A public one whose devcontainer.json names no image and forwards STACK_PORT.
STACK_PORTS = "stack-ports"
STACK_PORT = 8080

PACKAGES = """\
RUN apt-get update -qq \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \\
      git ca-certificates python3 \\
 && rm -rf /var/lib/apt/lists/*
"""
TRUST = """\
COPY ca.crt /usr/local/share/ca-certificates/{ca}.crt
RUN update-ca-certificates
"""
# Where the stand-in listens in the network the workspaces join, and how their git finds it there.
ADDRESS = "127.0.0.1"
PORT = 443
RESOLVE = f"RUN git config --system http.curloptResolve github.com:{PORT}:{ADDRESS}\n"


def dockerfile(base: str, packages: bool = False, proxy: str | None = None) -> str:
    """The build file of the image derived from ``base``: with ``packages``, git, python3 and
    the certificates installed first; the authority ``mint`` makes (``ca.crt`` in the
    build context) trusted, and github.com found at ADDRESS by git, or, with ``proxy``, reached
    by git through that proxy (tests/container/network.py ``Relay``)."""
    text = f"FROM {base}\n" + (PACKAGES if packages else "") + TRUST.format(ca=CA_NAME) + RESOLVE
    if proxy is not None:
        text += f"RUN git config --system http.https://github.com.proxy {proxy}\n"
    return text


def openssl(*args: str, cwd: Path) -> None:
    proc = subprocess.run(
        ["openssl", *args], cwd=cwd, capture_output=True, text=True, errors="replace"
    )
    assert proc.returncode == 0, proc.stderr


def mint(folder: Path) -> Path:
    """A certificate authority, ``ca.crt`` and ``ca.key``, and a certificate it signed for
    github.com, ``leaf.crt`` and ``leaf.key``, in ``folder``, which is made."""
    folder.mkdir(parents=True, exist_ok=True)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
            f"/CN={CA_NAME}", "-keyout", "ca.key", "-out", "ca.crt", cwd=folder)  # fmt: skip
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=github.com",
            "-keyout", "leaf.key", "-out", "leaf.csr", cwd=folder)  # fmt: skip
    (folder / "san.ext").write_text("subjectAltName=DNS:github.com\n")
    openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "2", "-extfile", "san.ext", "-out", "leaf.crt",
            cwd=folder)  # fmt: skip
    return folder


def seed(server) -> None:
    """The repositories the whole-stack browser tests clone, on ``server``: a public one, a
    private one and a public one that forwards a port, each with one commit on main."""
    with tempfile.TemporaryDirectory(prefix="rcman-github-seed-") as scratch:
        for name, private in ((STACK_PUBLIC, False), (STACK_PRIVATE, True), (STACK_PORTS, False)):
            bare = server.bare(STACK_OWNER, name, private=private)
            work = Path(scratch) / name
            git = [
                "git",
                "-C",
                str(work),
                "-c",
                "user.name=Octo",
                "-c",
                "user.email=octo@example.com",
            ]
            subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
            (work / STACK_FILE).write_text(f"# {name}\n")
            subprocess.run([*git, "add", STACK_FILE], check=True)
            if name == STACK_PORTS:
                (work / ".devcontainer").mkdir()
                (work / ".devcontainer" / "devcontainer.json").write_text(
                    json.dumps({"name": "Ports", "forwardPorts": [STACK_PORT]}) + "\n"
                )
                subprocess.run([*git, "add", ".devcontainer"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", f"First commit of {name}"], check=True)
            subprocess.run([*git, "push", "-q", str(bare), "main"], check=True)


def serve(root: str, host: str, port: str) -> None:
    """Serve the stand-in with the stack's token, plain http, until the process ends: Caddy
    is what speaks TLS for it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.github_server import GitHubServer

    server = GitHubServer()
    server.start(Path(root), STACK_TOKEN, host=host, port=int(port))
    seed(server)
    print("SERVING", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    verb, *arguments = sys.argv[1:]
    run = {"serve": serve}[verb]
    run(*arguments)
