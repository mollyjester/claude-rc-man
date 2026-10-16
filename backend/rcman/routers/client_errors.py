"""A page's reports of failures rcman could not see, written to the journal.

A page that cannot reach rcman, or cannot read its answer, reports the failure
once it can reach rcman again. Each report is one JSON line on the
``rcman.client`` logger, with who sent it; nothing is kept and nothing is
audited (``audit.EXEMPT``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, BeforeValidator, Field
from pydantic_core import PydanticKnownError

from rcman.identity import Identity, current_user
from rcman.problems import Problem
from rcman.ratelimit import TokenBuckets

log = logging.getLogger("rcman.client")
log.setLevel(logging.INFO)

router = APIRouter(prefix="/api/client-errors", tags=["client-errors"])

# How many reports one person may send at once, and how many more each minute: a page that
# fails again and again counts its failures (``count``) and sends them one at a time, so a
# page that keeps within this loses nothing, and one that does not cannot fill the journal.
REPORTS_BURST = 20
REPORTS_PER_MINUTE = 20
SECONDS_PER_MINUTE = 60


def new_limit() -> TokenBuckets:
    return TokenBuckets(REPORTS_BURST, REPORTS_PER_MINUTE / SECONDS_PER_MINUTE)


# How many characters each kind of text in a report is cut to.
SHORT_CHARS = 100
PATH_CHARS = 300
MESSAGE_CHARS = 500
# The largest whole number a page's JavaScript holds exactly: Number.MAX_SAFE_INTEGER.
JS_MAX_SAFE = 2**53 - 1


class Cut:
    """Text cut to ``chars`` characters; anything else is left for its type to refuse.

    Text holding half of a character is refused, as every other field refuses it.
    """

    def __init__(self, chars: int):
        self.chars = chars

    def __call__(self, value):
        if not isinstance(value, str):
            return value
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise PydanticKnownError("string_unicode") from None
        return value[: self.chars]


def Clipped(chars: int, description: str, nullable: bool = False):  # noqa: N802 - a type
    """A field of text of any length, cut to ``chars`` characters rather than refused, or
    null too when ``nullable``. Its description says what it is cut to."""
    text = Annotated[str, BeforeValidator(Cut(chars))]
    return Annotated[
        text | None if nullable else text,
        Field(description=f"{description} Cut to {chars} characters."),
    ]


Count = Annotated[int, Field(ge=0, le=JS_MAX_SAFE)]


class ClientErrorBody(BaseModel):
    """One failure the page saw, or several alike, counted."""

    kind: Clipped(SHORT_CHARS, "What kind of failure it was.")
    method: Clipped(SHORT_CHARS, "The request's method.", nullable=True)
    path: Clipped(PATH_CHARS, "The request's path, without its query.", nullable=True)
    status: Annotated[int, Field(ge=100, le=599)] | None = Field(
        description="The status of the answer, if one came."
    )
    name: Clipped(SHORT_CHARS, "The name of the error the page saw.", nullable=True)
    message: Clipped(MESSAGE_CHARS, "The error's message.", nullable=True)
    content_type: Clipped(SHORT_CHARS, "The answer's content type.", nullable=True)
    page: Clipped(PATH_CHARS, "The page's path, without its query.")
    visibility: Clipped(SHORT_CHARS, "Whether the page was visible.")
    online: bool = Field(description="Whether the browser thought it was online.")
    elapsed_ms: Count | None = Field(description="How long the request took, in milliseconds.")
    count: Annotated[int, Field(ge=1, le=JS_MAX_SAFE)] = Field(
        description="How many times the failure was seen."
    )
    first_at: Count = Field(description="When it was first seen, in milliseconds since 1970.")
    last_at: Count = Field(description="When it was last seen, in milliseconds since 1970.")


@router.post("")
def report(
    body: ClientErrorBody, request: Request, identity: Identity = Depends(current_user)
) -> dict:
    """Write a page's report of a failure to the journal; a person who sends more than
    REPORTS_BURST at once, or REPORTS_PER_MINUTE a minute, is refused with 429 and the seconds
    to wait, and the report is dropped."""
    limit = request.app.state.client_error_limit
    if not limit.take(identity.email):
        wait = limit.seconds_until_allowed(identity.email)
        raise Problem("too_many_reports", headers={"Retry-After": str(wait)})
    event = {
        "event": "client_error",
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "email": identity.email,
        **body.model_dump(),
    }
    log.info(json.dumps(event))
    return {}
