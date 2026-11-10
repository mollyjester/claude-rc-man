"""The certificates of the tests' GitHub, and the build file of a workspace image that trusts
them and finds it, for tests/container/test_builder_in_podman.py; its ``github_com`` fixture
serves tests/github_server.py, reached as https://github.com through network.Relay.

- ``mint`` makes a certificate authority and a certificate it signed for github.com;
- ``dockerfile`` derives a workspace image from a base image that trusts that authority and
  whose git finds github.com at the network's own loopback (RESOLVE: a workspace that joins
  another container's network gets no hosts file of that container's), and, for a base that is
  not rcman's own, has git and python3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CA_NAME = "rcman-test-github-ca"
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
