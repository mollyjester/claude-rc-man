"""Signing in: the sign-in page, and Microsoft sign-in's start and callback (microsoft.py).

Caddy passes ``/oauth2/start`` and ``/api/auth/*`` to rcman without a sign-in, with the identity
header stripped (installer/templates/Caddyfile): every route here is public
(problems.PUBLIC_PATHS) and outside the published API. Caddy sends a request that is not signed
in to ``/oauth2/start?rd=<the page asked for>``. While Microsoft sign-in is off, that answers
303 to oauth2-proxy's ``/oauth2/sign_in``, which starts Google's sign-in at once; while it is
on, it is the sign-in page, a choice of Google or Microsoft, and the words of a refusal when it
is sent back with ``?error=<code>``. The Microsoft routes answer ``not_found`` while sign-in is
off, and share one limit on how often they are asked (``new_limit``): past it, they send the
browser back to the sign-in page with ``too_many_requests`` and a ``Retry-After``. Without
rcman's secret key, the page offers Google's sign-in alone, as while Microsoft sign-in is off.

The page is rcman's own, without a script: the built frontend's stylesheet, which index.html
names, inlined, and its two ``theme-color`` metas copied; with no frontend built it is unstyled.
The page to return to is held to ``forward.safe_return`` and encoded into every address it is
written in; a refusal's code is only looked up (problems.SIGN_IN_REFUSALS), never shown.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from rcman import db, forward, microsoft, people
from rcman.problems import SIGN_IN_PAGE, SIGN_IN_REFUSALS, SIGN_IN_UNFINISHED, Problem
from rcman.ratelimit import TokenBuckets

router = APIRouter(include_in_schema=False)

START_PATH = "/oauth2/start"
GOOGLE_PATH = "/oauth2/sign_in"
# Every Microsoft sign-in's start and callback together, from anyone: each makes rcman ask
# Microsoft, so a page cannot make it ask without end.
REQUESTS_BURST = 60
REQUESTS_PER_MINUTE = 120
LIMIT_KEY = "microsoft"
POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
# The app's problem icon, a cross in a circle (frontend/src/Icons.tsx, ProblemIcon), drawn as
# MessageBar draws it: inline, so the page's policy needs nothing more, and hidden from screen
# readers, which the alert's words are for.
PROBLEM_ICON = (
    '<span class="message-icon"><svg class="icon" viewBox="0 0 16 16" fill="none" '
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="6"/>'
    '<path d="m6 6 4 4M10 6l-4 4"/></svg></span>'
)
STYLESHEET = re.compile(r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="(/assets/[^"]+\.css)"')
THEME_COLOR = re.compile(r'<meta name="theme-color"[^>]*>')


def new_limit() -> TokenBuckets:
    """Microsoft sign-in's starts and callbacks: REQUESTS_BURST at once, then
    REQUESTS_PER_MINUTE, for everyone together."""
    return TokenBuckets(REQUESTS_BURST, REQUESTS_PER_MINUTE / 60)


def _limited(request: Request) -> Response | None:
    """The sign-in page with ``too_many_requests``, saying when to come back, where the limit is
    reached; None otherwise."""
    limit = request.app.state.microsoft_limit
    if limit.take(LIMIT_KEY):
        return None
    answer = _refused("too_many_requests")
    answer.headers["Retry-After"] = str(limit.seconds_until_allowed(LIMIT_KEY))
    return answer


def _with_return(base: str, return_to: str) -> str:
    """``base`` with the page to return to as its ``rd``, encoded."""
    return base + "?rd=" + urllib.parse.quote(return_to, safe="/")


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303)


def _refused(code: str) -> RedirectResponse:
    """Back to the sign-in page, which words ``code``, with the flow cookie deleted."""
    answer = _redirect(START_PATH + "?error=" + urllib.parse.quote(code, safe=""))
    microsoft.delete(answer, microsoft.FLOW_COOKIE)
    return answer


# The last look read, by the frontend's folder: index.html's time of change, and what was read.
_LOOKS: dict[Path, tuple[int, tuple[str, str]]] = {}


def _cached_looks(frontend_dir: Path) -> tuple[str, str]:
    """``_looks``, read again only when index.html changed, as a new build changes it."""
    try:
        changed = (frontend_dir / "index.html").stat().st_mtime_ns
    except OSError:
        return "", ""
    kept = _LOOKS.get(frontend_dir)
    if kept is not None and kept[0] == changed:
        return kept[1]
    looks = _looks(frontend_dir)
    _LOOKS[frontend_dir] = (changed, looks)
    return looks


def _looks(frontend_dir: Path) -> tuple[str, str]:
    """The built frontend's stylesheet, and its theme-color metas, from its index.html; empty
    where there is no build, or the stylesheet could not be put safely in a style element."""
    try:
        index = (frontend_dir / "index.html").read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return "", ""
    metas = "".join(THEME_COLOR.findall(index))
    found = STYLESHEET.search(index)
    if found is None:
        return "", metas
    root = frontend_dir.resolve()
    path = (root / found.group(1).lstrip("/")).resolve()
    if root not in path.parents:
        return "", metas
    try:
        css = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return "", metas
    if "</style" in css.lower():
        return "", metas
    return css, metas


def _page(frontend_dir: Path, return_to: str, refusal: str | None) -> str:
    css, metas = _cached_looks(frontend_dir)
    google = html.escape(_with_return(GOOGLE_PATH, return_to), quote=True)
    with_microsoft = html.escape(_with_return(microsoft.LOGIN_PATH, return_to), quote=True)
    words = {key: html.escape(value) for key, value in SIGN_IN_PAGE.items()}
    alert = ""
    if refusal is not None:
        alert = (
            f'<div role="alert" class="message message-problem">{PROBLEM_ICON}'
            f'<div class="message-body">{html.escape(refusal)}</div></div>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'{metas}<meta name="color-scheme" content="light dark">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        f"<title>{words['title']} - rcman</title><style>{css}</style></head><body>"
        # The app's own top bar: its mark and name, then the page's name, which alone stays on a
        # phone, where the page's heading is for screen readers only, as on the app's pages.
        '<header class="topbar"><span class="brand">'
        '<span class="mark" aria-hidden="true">rc</span>rcman</span>'
        '<span class="crumb-sep" aria-hidden="true">/</span>'
        f'<span class="crumb">{words["title"]}</span>'
        "</header><main>"
        f'<div class="page-head"><h1>{words["title"]}</h1></div>'
        f'<section class="card">{alert}<p>{words["lead"]}</p>'
        '<p class="actions sign-in-choices">'
        f'<a class="button" href="{google}">{words["google"]}</a>'
        f'<a class="button" href="{with_microsoft}">{words["microsoft"]}</a></p>'
        "</section></main></body></html>"
    )


@router.get(START_PATH)
def start(request: Request, rd: str = "/") -> Response:
    """The way into rcman for a browser that is not signed in: Google's sign-in at once while
    Microsoft sign-in is off, and otherwise the sign-in page, with the words of a refusal
    named by ``error``."""
    return_to = forward.safe_return(rd)
    state = request.app.state
    # Without the secret key no Microsoft sign-in can be made, so none is offered.
    if not (state.microsoft_settings.settings().enabled and state.sealer.available):
        return _redirect(_with_return(GOOGLE_PATH, return_to))
    error = request.query_params.get("error")
    refusal = None if error is None else SIGN_IN_REFUSALS.get(error, SIGN_IN_UNFINISHED)
    return HTMLResponse(
        _page(request.app.state.settings.frontend_dir, return_to, refusal),
        headers={"Content-Security-Policy": POLICY, "Cache-Control": "no-store"},
    )


def _config(request: Request) -> microsoft.Config:
    """The configuration to sign in with; ``not_found`` while Microsoft sign-in is off."""
    state = request.app.state
    cfg = microsoft.config(state.microsoft_settings, state.sealer, state.settings.public_origin)
    if cfg is None:
        raise Problem("not_found")
    return cfg


@router.get(microsoft.LOGIN_PATH)
def microsoft_login(request: Request, rd: str = "/") -> Response:
    """A Microsoft sign-in begun: 303 to Microsoft's authorize page, with the flow kept in its
    cookie."""
    limited = _limited(request)
    if limited is not None:
        return limited
    try:
        cfg = _config(request)
        value, address = microsoft.start(cfg, request.app.state.sealer, forward.safe_return(rd))
    except microsoft.SignInError as refused:
        return _refused(refused.code)
    except Problem:
        raise
    except Exception as exc:  # noqa: BLE001 - its class alone is logged: MSAL's text holds claims
        microsoft.log.warning("Microsoft sign-in did not start: exception=%s", type(exc).__name__)
        return _refused("sso_failed")
    answer = _redirect(address)
    microsoft.set_flow(answer, value)
    return answer


@router.get(microsoft.CALLBACK_PATH)
def microsoft_callback(request: Request) -> Response:
    """Where Microsoft sends the browser back: the code redeemed, the account checked, and a
    session issued, then 303 to the page asked for first; or, refused, 303 to the sign-in page
    with the refusal's code. Either way the flow cookie is deleted."""
    limited = _limited(request)
    if limited is not None:
        return limited
    state = request.app.state
    query = request.query_params
    try:
        cfg = _config(request)
        payload = microsoft.read_state_cookie(
            state.sealer, request.cookies.get(microsoft.FLOW_COOKIE)
        )
        code, error = query.get("code"), query.get("error")
        if error or not code:
            return _refused(microsoft.callback_error_code(error))
        claims = microsoft.redeem_code(
            cfg, payload["flow"], {"code": code, "state": query.get("state")}
        )
        email = microsoft.address_of(claims, cfg.tenant_id)
        con = db.connect(state.settings.database)
        try:
            person = people.by_email(con, email)
        finally:
            con.close()
        if person is None or person.state != people.ACTIVE:
            raise microsoft.SignInError("not_allowed")
    except microsoft.SignInError as refused:
        return _refused(refused.code)
    except Problem:
        raise
    except Exception as exc:  # noqa: BLE001 - its class alone is logged: MSAL's text holds claims
        microsoft.log.warning(
            "Microsoft sign-in refused: code=sso_failed exception=%s", type(exc).__name__
        )
        return _refused("sso_failed")
    answer = _redirect(forward.safe_return(payload["rd"]))
    microsoft.delete(answer, microsoft.FLOW_COOKIE)
    microsoft.set_session(
        answer, microsoft.session_value(state.sealer, person.email, cfg.generation)
    )
    return answer
