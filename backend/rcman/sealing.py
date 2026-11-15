"""Sealing what rcman keeps secret or hands to a browser to keep, with ``RCMAN_SECRET_KEY``
(settings.py), which the installer writes in ``secret.env``.

Each use has a key of its own, derived from the secret key with HKDF-SHA256 under its label
(``STORED_SECRET``, ``COOKIES``): the client secret stored for Microsoft sign-in
(microsoft_settings.py), and the cookies of Microsoft sign-in (microsoft.py). What is sealed is
AES-256-GCM, a fresh nonce (``NONCE_BYTES``) before the ciphertext and its tag, with the label
as the associated data, in URL-safe base64 without padding, which a cookie holds as it is. A payload
handed to a browser is JSON that names its ``kind`` and the time it expires (``exp``, seconds
since the epoch), both checked when it is read, so that one cookie can never stand in for
another, nor be used once it has expired.

Without a secret key nothing can be sealed or opened (``Sealer.available``): Microsoft sign-in
is then refused, and Google sign-in is all there is.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STORED_SECRET = b"rcman: the stored Microsoft client secret"
COOKIES = b"rcman: the cookies of Microsoft sign-in"
NONCE_BYTES = 12
KEY_BYTES = 32


class Unreadable(Exception):
    """What was given cannot be opened: no key, another key, or not something sealed."""


class Expired(Unreadable):
    """A payload that opened, but whose time is up."""


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decoded(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise Unreadable("not base64") from error


class Sealer:
    """Seals and opens with the keys derived from ``secret_key``, in the form
    helpers.rules.SECRET_KEY holds it to, or with none when it is empty or missing."""

    def __init__(self, secret_key: str | None):
        self._keys: dict[bytes, AESGCM] = {}
        if secret_key:
            for label in (STORED_SECRET, COOKIES):
                derived = HKDF(
                    algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=label
                ).derive(bytes.fromhex(secret_key))
                self._keys[label] = AESGCM(derived)

    @property
    def available(self) -> bool:
        """Whether there is a key to seal and open with."""
        return bool(self._keys)

    def seal(self, label: bytes, data: bytes) -> str:
        if not self.available:
            raise Unreadable("no secret key")
        nonce = os.urandom(NONCE_BYTES)
        return _encoded(nonce + self._keys[label].encrypt(nonce, data, label))

    def open(self, label: bytes, sealed: str) -> bytes:
        if not self.available:
            raise Unreadable("no secret key")
        raw = _decoded(sealed)
        if len(raw) <= NONCE_BYTES:
            raise Unreadable("too short")
        try:
            return self._keys[label].decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], label)
        except InvalidTag as error:
            raise Unreadable("not sealed with this key") from error

    def seal_payload(self, kind: str, payload: dict, seconds: int) -> str:
        """``payload`` as a cookie's value, naming ``kind`` and expiring in ``seconds``."""
        whole = {**payload, "kind": kind, "exp": int(time.time()) + seconds}
        return self.seal(COOKIES, json.dumps(whole, separators=(",", ":")).encode("utf-8"))

    def open_payload(self, kind: str, sealed: str) -> dict:
        """The payload of a cookie's value of ``kind``. Raises Unreadable for anything else,
        and Expired for one whose time is up."""
        try:
            payload = json.loads(self.open(COOKIES, sealed))
        except (UnicodeDecodeError, ValueError) as error:
            raise Unreadable("not a payload") from error
        if not isinstance(payload, dict) or payload.get("kind") != kind:
            raise Unreadable("another kind")
        expires = payload.get("exp")
        if type(expires) is not int:
            raise Unreadable("no expiry")
        if expires < int(time.time()):
            raise Expired(kind)
        return payload
