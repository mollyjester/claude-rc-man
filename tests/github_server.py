"""A stand-in for github.com for the tests: real ``git http-backend`` behind a small server.

Not a test module. Reading — cloning, fetching — needs no credential, as for
a public repository on GitHub; pushing needs HTTP basic authentication with
any non-empty username and the server's token as the password, as GitHub
takes a personal access token. Each credential offered for a push is recorded
in ``attempts`` as (username, accepted), never with its password. A
repository is reached as ``/<owner>/<name>.git`` and as ``/<owner>/<name>``,
as on GitHub.

The request body is passed to git as it arrives, by its Content-Length: a
chunked body is not decoded, and git sends one only for a push larger than
its ``http.postBuffer``.

Python standard library only, so that the container suite can run it as it is
from the mounted checkout.
"""

from __future__ import annotations

import base64
import binascii
import ssl
import subprocess
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

GITHUB = "https://github.com/"
# An address nothing answers on, for RC_HELPER_GITHUB where a test needs no GitHub,
# so that no test can reach the real one.
UNREACHABLE = "http://127.0.0.1:1/"


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def _backend() -> str:
    exec_path = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return str(Path(exec_path) / "git-http-backend")


class GitHubServer:
    def __init__(self):
        self.attempts: list[tuple[str, bool]] = []
        self.url = ""
        self.root: Path | None = None
        self.token = ""
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        root: Path,
        token: str,
        ssl_context: ssl.SSLContext | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> str:
        """Serve the bare repositories under ``root``; the base URL, ending in a slash."""
        self.root, self.token = Path(root), token
        self.root.mkdir(parents=True, exist_ok=True)
        self._home = self.root / ".home"  # an empty HOME for git http-backend
        self._home.mkdir(exist_ok=True)
        self._backend = _backend()
        self._server = _Server((host, port), _Quiet)
        self._server.set_app(self._app)
        scheme = "http"
        if ssl_context is not None:
            self._server.socket = ssl_context.wrap_socket(self._server.socket, server_side=True)
            scheme = "https"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"{scheme}://{host}:{self._server.server_address[1]}/"
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            self._server = None

    def bare(self, owner: str, name: str) -> Path:
        """A new empty bare repository ``<owner>/<name>.git``, its default branch main."""
        path = self.root / owner / f"{name}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(path)],
            env=self._env(),
            check=True,
            capture_output=True,
        )
        return path

    @staticmethod
    def github_url(owner: str, name: str) -> str:
        """The repository's address on the real GitHub, which a test hook points here."""
        return f"{GITHUB}{owner}/{name}.git"

    def _env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self._home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C.UTF-8",
        }

    def _user(self, header: str | None) -> str | None:
        """The username of an accepted credential; None when there is none or it is wrong."""
        if not header or not header.startswith("Basic "):
            return None
        try:
            username, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        accepted = bool(username) and password == self.token
        self.attempts.append((username, accepted))
        return username if accepted else None

    def _app(self, environ, start_response):
        path, query = environ.get("PATH_INFO", ""), environ.get("QUERY_STRING", "")
        parts = path.lstrip("/").split("/", 2)
        if len(parts) == 3 and not parts[1].endswith(".git"):
            parts[1] += ".git"  # GitHub's address without .git reaches the same repository
        path = "/" + "/".join(parts)
        pushing = path.endswith("/git-receive-pack") or "service=git-receive-pack" in query
        user = self._user(environ.get("HTTP_AUTHORIZATION")) if pushing else None
        if pushing and user is None:
            start_response(
                "401 Unauthorized",
                [("WWW-Authenticate", 'Basic realm="GitHub"'), ("Content-Length", "0")],
            )
            return [b""]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        cgi = {
            **self._env(),
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": environ.get("REQUEST_METHOD", "GET"),
            "CONTENT_TYPE": environ.get("CONTENT_TYPE", ""),
            "CONTENT_LENGTH": str(len(body)),
        }
        for header, name in (
            ("HTTP_CONTENT_ENCODING", "HTTP_CONTENT_ENCODING"),
            ("HTTP_GIT_PROTOCOL", "GIT_PROTOCOL"),
        ):
            if environ.get(header):
                cgi[name] = environ[header]
        if user is not None:
            cgi["REMOTE_USER"] = user
        proc = subprocess.run([self._backend], input=body, env=cgi, capture_output=True)
        head, _, content = proc.stdout.partition(b"\r\n\r\n")
        status, headers = "200 OK", []
        for line in head.decode("latin-1").split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = value.strip()
            elif key:
                headers.append((key, value.strip()))
        if not any(k.lower() == "content-length" for k, _ in headers):
            headers.append(("Content-Length", str(len(content))))
        start_response(status, headers)
        return [content]
