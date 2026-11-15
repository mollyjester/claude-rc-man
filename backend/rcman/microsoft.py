"""Microsoft (Entra ID) sign-in, beside Google's: the confidential web app's authorization-code
flow, through MSAL for Python, and the session it ends in.

MSAL's ``ConfidentialClientApplication`` owns PKCE, the ``state`` round trip, the nonce and the
exchange of the code; of the ID token it checks the audience and the nonce. It does not check
the signature: the token comes straight from the configured tenant's token endpoint over TLS,
redeemed with the client secret, which OpenID Connect allows in place of a signature check.
A fresh application is made for each request from the settings as they are then
(microsoft_settings.py), so a replaced secret applies at once and MSAL's token cache never keeps
anyone's tokens past the request; every application shares ``HTTP_CACHE``, so the tenant's
discovery is fetched once, not on every request.

Between the authorize redirect and the callback, MSAL's flow is kept in the browser, sealed
(sealing.py) in ``FLOW_COOKIE`` for ``FLOW_SECONDS``, with the page to return to. SameSite=Lax
lets it through because Microsoft comes back by a top-level GET with the code in the query
(``response_mode=query``, MSAL's default for this flow).

A sign-in is refused, with a code the sign-in page words (problems.SIGN_IN_REFUSALS), unless
the ID token is of the configured tenant (``tid``, ``wrong_tenant``), names a member of it:
the ``acct`` claim, which the app registration must send as an optional claim
(``acct_missing``), exactly the integer 0 (``guest_account``), and gives an address as its UPN
(``preferred_username``, held to ``helpers.rules.EMAIL`` in lowercase; ``no_address``). The
``email`` claim is never read: it is the ``mail`` attribute an administrator of any tenant may
set, unverified. Only then is the address looked for among the people (``not_allowed``), so
no person is looked up for an account of another tenant.

A sign-in ends in ``SESSION_COOKIE``, sealed, naming the address and the generation of the
settings it was issued under, for ``SESSION_SECONDS``, as oauth2-proxy's lasts. identity.py
checks it on every request that does not come through oauth2-proxy.

Failures are codes, and nothing a token carried is logged: only a code, with an exception's
class, the token endpoint's own error code, or ``reason=busy`` for a wait for MSAL_LOCK that ran
out (docs/journal.md). MSAL writes the whole ID token's claims into its exceptions' text and
its own loggers' records, so those loggers are set above CRITICAL when this module is imported.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import msal
from msal.oauth2cli.oidc import (
    IdTokenAudienceError,
    IdTokenError,
    IdTokenIssuerError,
    IdTokenNonceError,
)
from starlette.responses import Response

from helpers import rules
from rcman import sealing
from rcman.microsoft_settings import Store

log = logging.getLogger("rcman.microsoft")
log.setLevel(logging.INFO)

AUTHORITY = "https://login.microsoftonline.com"
# The Graph scope the app registration has consented; MSAL adds the reserved OpenID Connect
# scopes, which are what bring the ID token.
SCOPES = ["User.Read"]
CALLBACK_PATH = "/api/auth/microsoft/callback"
LOGIN_PATH = "/api/auth/microsoft/login"
FLOW_COOKIE = "__Host-rcman-ms-flow"
SESSION_COOKIE = "__Host-rcman-session"
FLOW_SECONDS = 10 * 60
SESSION_SECONDS = 7 * 24 * 60 * 60
FLOW, SESSION = "flow", "session"  # the kinds of what is sealed in the two cookies
# Shared by every application made, never anyone's tokens: what MSAL fetches over HTTP, such as
# the tenant's discovery documents.
HTTP_CACHE: dict = {}
# Held while an application is made and used: MSAL locks HTTP_CACHE per application only, and the
# applications of requests at once share it.
MSAL_LOCK = threading.Lock()
# How long each of MSAL's requests to Microsoft may wait for an answer (requests' ``timeout``),
# so a stalled connection cannot hold MSAL_LOCK, and a thread of the server's, for ever.
MSAL_TIMEOUT_SECONDS = 10
# How long a request waits for MSAL_LOCK before it is refused ``idp_unreachable``: no longer
# than one of MSAL's requests may take, so an outage at Microsoft does not queue every sign-in
# on the server's shared threads.
LOCK_SECONDS = MSAL_TIMEOUT_SECONDS

# Above CRITICAL: no record of MSAL's is made at all, wherever a handler sits.
logging.getLogger("msal").setLevel(logging.CRITICAL + 1)


class SignInError(Exception):
    """A refused or failed sign-in, by its code alone: never any text it was given."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:
    client_id: str
    tenant_id: str
    secret: str
    redirect_uri: str
    generation: int

    @property
    def authority(self) -> str:
        return f"{AUTHORITY}/{self.tenant_id}"


def config(store: Store, sealer: sealing.Sealer, public_origin: str) -> Config | None:
    """The configuration to sign in with, or None while Microsoft sign-in is off. Raises
    SignInError ``secret_unreadable`` where the stored secret cannot be opened."""
    stored = store.settings()
    if not stored.enabled:
        return None
    if stored.secret is None:
        raise SignInError("secret_unreadable")
    try:
        secret = sealer.open(sealing.STORED_SECRET, stored.secret).decode("utf-8")
    except sealing.Unreadable, UnicodeDecodeError:
        raise SignInError("secret_unreadable") from None
    return Config(
        client_id=stored.client_id,
        tenant_id=stored.tenant_id,
        secret=secret,
        redirect_uri=public_origin + CALLBACK_PATH,
        generation=stored.generation,
    )


def _application(cfg: Config) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=cfg.client_id,
        client_credential=cfg.secret,
        authority=cfg.authority,
        http_cache=HTTP_CACHE,
        timeout=MSAL_TIMEOUT_SECONDS,
    )


@contextmanager
def _msal_held() -> Iterator[None]:
    """MSAL_LOCK, waited for at most LOCK_SECONDS. Raises SignInError ``idp_unreachable`` where
    it could not be had: the sign-ins ahead are still waiting on Microsoft."""
    if not MSAL_LOCK.acquire(timeout=LOCK_SECONDS):
        log.warning("Microsoft sign-in refused: code=idp_unreachable reason=busy")
        raise SignInError("idp_unreachable")
    try:
        yield
    finally:
        MSAL_LOCK.release()


def start(cfg: Config, sealer: sealing.Sealer, return_to: str) -> tuple[str, str]:
    """A sign-in begun: the flow cookie's value and the address of Microsoft's authorize page.
    The account picker is shown every time, whatever account the browser is signed in with."""
    with _msal_held():
        flow = _application(cfg).initiate_auth_code_flow(
            SCOPES, redirect_uri=cfg.redirect_uri, prompt="select_account"
        )
    if "auth_uri" not in flow:
        raise SignInError("sso_failed")
    sealed = sealer.seal_payload(FLOW, {"flow": flow, "rd": return_to}, FLOW_SECONDS)
    return sealed, flow["auth_uri"]


def read_state_cookie(sealer: sealing.Sealer, value: str | None) -> dict:
    """The flow cookie's payload (``flow``, ``rd``). Raises SignInError ``bad_state`` for one
    missing, forged or of another kind, and ``expired_state`` for one expired. That the state
    Microsoft sends back is the flow's is MSAL's to check, in ``redeem_code``."""
    if not value:
        raise SignInError("bad_state")
    try:
        payload = sealer.open_payload(FLOW, value)
    except sealing.Expired:
        raise SignInError("expired_state") from None
    except sealing.Unreadable:
        raise SignInError("bad_state") from None
    if not isinstance(payload.get("flow"), dict) or not isinstance(payload.get("rd"), str):
        raise SignInError("bad_state")
    return payload


# MSAL raises a bare RuntimeError for these, told apart only by its text, which is read here and
# never logged. Text no entry matches is token_invalid, so a rewording in a later MSAL still
# refuses, under the wider code.
_RUNTIME_ERROR_CODES = (("The nonce in id token", "nonce_mismatch"),)


def _failure_code(exc: BaseException) -> str:
    """The code a failure of redemption is answered with."""
    if isinstance(exc, (IdTokenIssuerError, IdTokenAudienceError)):
        return "token_invalid"
    if isinstance(exc, IdTokenNonceError):
        return "nonce_mismatch"
    if isinstance(exc, IdTokenError):
        return "token_invalid"
    if isinstance(exc, OSError):  # every exception of requests' is an IOError
        return "idp_unreachable"
    if isinstance(exc, RuntimeError):
        text = str(exc)
        for prefix, code in _RUNTIME_ERROR_CODES:
            if text.startswith(prefix):
                return code
        return "token_invalid"
    if isinstance(exc, ValueError) and str(exc).startswith("state"):
        return "bad_state"  # MSAL's "state missing" and "state mismatch"
    return "token_invalid"


def _refused(code: str, exc: BaseException) -> SignInError:
    log.warning("Microsoft sign-in refused: code=%s exception=%s", code, type(exc).__name__)
    return SignInError(code)


def callback_error_code(error: str | None) -> str:
    """The code a callback carrying Microsoft's own ``error`` is refused with. Microsoft's
    value is never passed on: a cancelled sign-in keeps its own code, and anything else is
    ``idp_error``; a callback with neither an error nor a code is ``bad_state``."""
    if not error:
        return "bad_state"
    return "access_denied" if error == "access_denied" else "idp_error"


def redeem_code(cfg: Config, flow: dict, auth_response: dict) -> dict:
    """The ID token's claims, once MSAL has checked the state against the flow, exchanged the
    code (PKCE and the client secret) and checked the token's audience and nonce. Raises
    SignInError on any failure, whatever raised it."""
    with _msal_held():
        try:
            application = _application(cfg)
        except Exception as exc:  # the tenant's discovery: the network, or an answer refused
            raise _refused("idp_unreachable", exc) from None
        try:
            result = application.acquire_token_by_auth_code_flow(flow, auth_response, scopes=SCOPES)
        except Exception as exc:
            raise _refused(_failure_code(exc), exc) from None
    if "error" in result:
        # The token endpoint's own vocabulary (invalid_grant and the like), never a claim.
        log.warning(
            "Microsoft sign-in refused: code=exchange_failed error=%s",
            str(result.get("error"))[:64],
        )
        raise SignInError("exchange_failed")
    return result.get("id_token_claims") or {}


def address_of(claims: dict, tenant_id: str) -> str:
    """The address a verified ID token signs in, in lowercase, once it is shown to be a member
    of ``tenant_id``'s. Raises SignInError with the refusal's code otherwise."""
    if str(claims.get("tid", "")).lower() != tenant_id.lower():
        raise SignInError("wrong_tenant")
    if "acct" not in claims:
        raise SignInError("acct_missing")
    acct = claims["acct"]
    if not (type(acct) is int and acct == 0):
        raise SignInError("guest_account")
    upn = claims.get("preferred_username")
    if not isinstance(upn, str) or not rules.EMAIL.fullmatch(upn.lower()):
        raise SignInError("no_address")
    return upn.lower()


def session_value(sealer: sealing.Sealer, email: str, generation: int) -> str:
    return sealer.seal_payload(SESSION, {"email": email, "generation": generation}, SESSION_SECONDS)


def session_email(sealer: sealing.Sealer, store: Store, value: str) -> str | None:
    """The address a session cookie's value names, or None for one that is not a valid
    session now: forged, expired, of another kind, issued under another generation of the
    settings, or while Microsoft sign-in is off."""
    stored = store.settings()
    if not stored.enabled or not sealer.available:
        return None
    try:
        payload = sealer.open_payload(SESSION, value)
    except sealing.Unreadable:
        return None
    email, generation = payload.get("email"), payload.get("generation")
    if not isinstance(email, str) or type(generation) is not int:
        return None
    if generation != stored.generation:
        return None
    return email


def _cookie(response: Response, name: str, value: str, seconds: int) -> None:
    response.set_cookie(
        name, value, max_age=seconds, path="/", secure=True, httponly=True, samesite="lax"
    )


def set_flow(response: Response, value: str) -> None:
    _cookie(response, FLOW_COOKIE, value, FLOW_SECONDS)


def set_session(response: Response, value: str) -> None:
    _cookie(response, SESSION_COOKIE, value, SESSION_SECONDS)


def delete(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


def deletion(name: str) -> str:
    """The Set-Cookie header's value that deletes the cookie ``name``, for an answer that is
    built as a problem rather than a response (identity.py)."""
    answer = Response()
    delete(answer, name)
    return answer.headers["set-cookie"]
