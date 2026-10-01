#!/usr/bin/python3
"""Stands in for Google sign-in in the full-stack test.

/authorize shows a page with an email field; submitting it sends the browser
back to oauth2-proxy with a code. /token exchanges the code for tokens whose
ID token names that address (unsigned: oauth2-proxy's Google provider reads
the token it fetched itself). /tokeninfo answers that a token is valid.
Usage: oidc_stub.py PORT
"""

import base64
import html
import http.server
import json
import secrets
import sys
import time
import urllib.parse

CODES: dict[str, str] = {}


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def id_token(email: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "sub": email,
        "aud": "test-client-id",
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."


class Stub(http.server.BaseHTTPRequestHandler):
    def _send(
        self, status: int, body: str, content_type: str = "text/html", headers: dict | None = None
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/authorize" and "email" not in query:
            fields = "".join(
                f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                for k, v in query.items()
            )
            page = (
                '<form method="get" action="/authorize">'
                f'{fields}<label>Email <input name="email"></label>'
                '<button type="submit">Sign in</button></form>'
            )
            return self._send(200, f"<!doctype html><title>Test sign-in</title>{page}")
        if url.path == "/authorize":
            code = secrets.token_urlsafe(12)
            CODES[code] = query["email"]
            target = (
                query["redirect_uri"]
                + "?"
                + urllib.parse.urlencode({"code": code, "state": query.get("state", "")})
            )
            return self._send(302, "", headers={"Location": target})
        if url.path.startswith("/tokeninfo"):
            return self._send(200, json.dumps({"expires_in": 3600}), "application/json")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        email = CODES.pop(form.get("code", ""), None)
        if self.path.startswith("/token") and email:
            tokens = {
                "access_token": secrets.token_urlsafe(16),
                "id_token": id_token(email),
                "expires_in": 3600,
                "token_type": "Bearer",
            }
            return self._send(200, json.dumps(tokens), "application/json")
        return self._send(400, json.dumps({"error": "invalid_grant"}), "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Stub).serve_forever()
