"""RFC 9457 problem details for every error the API returns."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, WrapValidator
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException

from helpers import rules
from helpers.rules import Rule

log = logging.getLogger("rcman.problems")
MEDIA_TYPE = "application/problem+json"
# Validation error types of rcman's own.
RULE_REFUSED = "rule_refused"  # a helpers' rule refused the value
SETTINGS_NONE_SENT = "settings_none_sent"
STRING_EMPTY = "string_empty"  # string_too_short, where the shortest allowed is one
FIELD_INVALID = "field_invalid"  # any type FIELD_MESSAGES has no message for
PUBLIC_PATHS = {"/api/health", "/{path:path}"}  # the frontend's files need no identity

# Every problem rcman answers with: its words, and its HTTP status.
MESSAGES = {
    "not_authenticated": "Sign in first.",
    "not_allowed": "This account may not use rcman.",
    "admin_only": "Only administrators may do this.",
    "origin_refused": "The request did not come from rcman.",
    "not_found": "There is nothing here.",
    "method_not_allowed": "That is not allowed here.",
    "validation_failed": "Some of what was sent is not valid.",
    "body_unreadable": "What was sent could not be read as text.",
    "author_not_set": "Set your commit author name in Settings before committing.",
    "not_available_yet": "rcman cannot do this with workspaces yet.",
    "internal_error": "Something went wrong in rcman.",
    # people
    "email_invalid": rules.EMAIL.sentence,
    "email_taken": "Someone with that address is there already.",
    "people_full": "rcman has room for no more people.",
    "person_not_found": "There is no such person.",
    # workspaces
    "workspace_not_found": "There is no such workspace.",
    "workspace_name_taken": "You have a workspace with that name already.",
    "workspace_not_ready": "The workspace is not ready: it is being made, or deleted, or failed.",
    "runtime_unavailable": "rcman cannot reach its containers just now. Try again in a moment.",
    "workspace_failed": "The workspace did not do what rcman asked of it.",
    # servers
    "claude_not_installed": "Claude Code is not installed in the workspace.",
    "claude_not_logged_in": "Connect your Claude account on the Claude page.",
    "claude_signing_out": "Your Claude account is being signed out: connect it again on the "
    "Claude page to start servers.",
    "login_failed": "Signing in to Claude failed. Get a new link and try again.",
    "no_login_in_progress": "No sign-in to Claude is waiting for a code. Get a new link.",
    "claude_unrecognised_output": "Claude Code answered with output rcman does not recognise.",
    "already_running": "A server is already running in this workspace.",
    "not_running": "No server is running in this workspace.",
    "not_stopped": "There is no server in this workspace that stopped and can be resumed.",
    "still_running": "Stop the server before forgetting it.",
    "unknown_server": "There is no server in this workspace.",
    "nothing_to_confirm": "The server is not asking anything.",
    "confirm_not_accepted": "The server did not accept the answer.",
    "timeout": "The workspace did not answer in time.",
    "prefix_invalid": rules.PREFIX.sentence,
}
STATUS = {
    "not_authenticated": 401,
    "not_allowed": 403,
    "admin_only": 403,
    "origin_refused": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "validation_failed": 422,
    "body_unreadable": 400,
    "author_not_set": 409,
    "not_available_yet": 501,
    "internal_error": 500,
    "email_invalid": 400,
    "email_taken": 409,
    "people_full": 409,
    "person_not_found": 404,
    "workspace_not_found": 404,
    "workspace_name_taken": 409,
    "workspace_not_ready": 409,
    "runtime_unavailable": 503,
    "workspace_failed": 502,
    "claude_not_installed": 409,
    "claude_not_logged_in": 409,
    "claude_signing_out": 409,
    "login_failed": 409,
    "no_login_in_progress": 409,
    "claude_unrecognised_output": 502,
    "already_running": 409,
    "not_running": 409,
    "not_stopped": 409,
    "still_running": 409,
    "unknown_server": 404,
    "nothing_to_confirm": 409,
    "confirm_not_accepted": 409,
    "timeout": 504,
    "prefix_invalid": 400,
}


class Problem(Exception):
    def __init__(self, code: str, status: int | None = None, title: str | None = None, **extra):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS[code]
        self.title = title or MESSAGES[code]
        self.extra = extra


def body(code: str, status: int, title: str, **extra) -> dict:
    """A problem's details, as a response carries them or an answer reports one."""
    return {
        "type": f"urn:rcman:problem:{code}",
        "title": title,
        "status": status,
        "code": code,
        **extra,
    }


def response(code: str, status: int, title: str, **extra) -> JSONResponse:
    return JSONResponse(
        body(code, status, title, **extra), status_code=status, media_type=MEDIA_TYPE
    )


# What a refused field is told, by the type of the validation error, with the
# numbers from the error's own context, so they are the field's constraints.
# The library's own message is never sent.
FIELD_MESSAGES = {
    RULE_REFUSED: "{sentence}",
    "missing": "This is required.",
    "string_type": "This must be text.",
    "string_unicode": "This holds half of a character, which is not text.",
    STRING_EMPTY: "This cannot be empty.",
    "string_too_long": "This can be at most {max_length} characters long.",
    "list_type": "This must be a list.",
    "too_short": "This must hold at least {min_length} items.",
    "too_long": "This can hold at most {max_length} items.",
    "bool_type": "This must be true or false.",
    "bool_parsing": "This must be true or false.",
    "int_type": "This must be a whole number.",
    "int_parsing": "This must be a whole number.",
    "greater_than_equal": "This must be at least {ge}.",
    "less_than_equal": "This must be at most {le}.",
    "literal_error": "This must be one of {expected}.",
    "json_invalid": "What was sent is not valid JSON.",
    "model_attributes_type": "What was sent must be a JSON object.",
    SETTINGS_NONE_SENT: "Send at least one setting.",
    FIELD_INVALID: "This is not valid.",
}


# What a ruled field refuses with its rule's sentence: what the rule refuses, any
# length the field is given besides, and text holding half of a character.
_RULED = ("string_pattern_mismatch", "string_too_short", "string_too_long", "string_unicode")


def ruled(rule: Rule, **constraints):
    """Text that must match a helpers' rule, and any length ``constraints``.

    The rule's anchored pattern is validated and published; a value it or a
    length refuses is refused with the rule's sentence.
    """

    def explain(value, handler):
        try:
            return handler(value)
        except ValidationError as error:
            if all(e["type"] in _RULED for e in error.errors()):
                raise PydanticCustomError(
                    RULE_REFUSED, "{sentence}", {"sentence": rule.sentence}
                ) from None
            raise

    return Annotated[str, Field(pattern=rule.anchored, **constraints), WrapValidator(explain)]


def _field(error) -> str:
    """The field an error is about: its location without the leading part (body, query, ...).

    A body that is not JSON is about the request as a whole, not the place it broke.
    """
    loc = error["loc"]
    if error["type"] == "json_invalid":
        return "request"
    parts = (
        list(loc[1:])
        if loc and loc[0] in ("body", "query", "path", "header", "cookie")
        else list(loc)
    )
    return ".".join(str(p) for p in parts) or "request"


def _message(error) -> str:
    """What a field is told about an error, in rcman's words."""
    kind, context = error["type"], error.get("ctx") or {}
    if kind == "string_too_short" and context.get("min_length") == 1:
        kind = STRING_EMPTY
    if kind not in FIELD_MESSAGES:
        log.warning("validation error type %s has no message in FIELD_MESSAGES", kind)
        kind = FIELD_INVALID
    try:
        return FIELD_MESSAGES[kind].format(**context)
    except (KeyError, IndexError):
        log.warning("validation error type %s lacks the context its message needs", kind)
        return FIELD_MESSAGES[FIELD_INVALID]


# A body FastAPI cannot read as text is its 400; rcman sends no 400 of its own.
HTTP_CODES = {400: "body_unreadable", 404: "not_found", 405: "method_not_allowed"}


def code_of(exc: Exception) -> str:
    """The code of the problem an exception is answered with."""
    if isinstance(exc, Problem):
        return exc.code
    if isinstance(exc, RequestValidationError):
        return "validation_failed"
    if isinstance(exc, StarletteHTTPException):
        return HTTP_CODES.get(exc.status_code, "internal_error")
    return "internal_error"


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem):
        return response(exc.code, exc.status, exc.title, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": _field(e), "message": _message(e)} for e in exc.errors()]
        code = code_of(exc)
        return response(code, STATUS[code], MESSAGES[code], errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = code_of(exc)
        return response(code, exc.status_code, MESSAGES[code])

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        code = code_of(exc)
        return response(code, STATUS[code], MESSAGES[code])
