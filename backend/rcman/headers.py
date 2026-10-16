"""The headers every answer of rcman carries.

No page of rcman is meant to be framed by another site, and nothing it sends is to be taken
for a kind other than the one it says: the policy that forbids framing (and nothing else, so
that the interactive description of the API, which loads its own scripts, works), the older
header the same for browsers that ignore the policy, the one that stops a browser from guessing
a content type, and the one that keeps an address of rcman's from going to other sites. Neither
Caddy nor oauth2-proxy adds them for what rcman serves. An answer that sets one of them itself
keeps its own. Python standard library only, and ASGI's own types.
"""

from __future__ import annotations

from fastapi import FastAPI

SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class SecurityHeaders:
    """An ASGI middleware adding SECURITY_HEADERS to each answer that lacks them."""

    def __init__(self, app):
        self.app = app
        self._added = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        *(h for h in self._added if h[0] not in present),
                    ],
                }
            await send(message)

        await self.app(scope, receive, with_headers)


def install(app: FastAPI) -> None:
    app.add_middleware(SecurityHeaders)
