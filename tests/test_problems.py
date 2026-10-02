"""Every error is RFC 9457 problem details, and every refusal is in rcman's own words."""

import ast
import functools
import logging
import re
import typing
from pathlib import Path
from typing import Annotated

import pytest
from pydantic_core import PydanticKnownError
from pydantic_core.core_schema import ErrorType
from rcman import problems, routes

from tests.conftest import ALICE, as_user, changing


def assert_problem(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"urn:rcman:problem:{code}"
    assert body["title"]
    return body


def test_unknown_route_is_a_problem(client):
    assert_problem(client.get("/api/nope", headers=as_user(ALICE)), 404, "not_found")


def test_wrong_method_is_a_problem(client):
    assert_problem(client.delete("/api/me", headers=changing(ALICE)), 405, "method_not_allowed")


def test_validation_errors_are_problems_with_field_errors(client):
    body = assert_problem(
        client.post("/api/servers", json={}, headers=changing(ALICE)), 422, "validation_failed"
    )
    assert body["errors"] and body["errors"][0]["field"] == "path"
    assert body["errors"][0]["message"]


def test_every_route_answers_a_missing_identity_with_a_problem(app, client):
    found = [c for c in routes.api_routes(app) if c.path not in problems.PUBLIC_PATHS]
    assert found  # non-vacuity
    for route in found:
        method = sorted(route.methods)[0]
        path = route.path.replace("{linux}", "x").replace("{slug}", "x")
        response = client.request(method, path)
        assert_problem(response, 401, "not_authenticated")


def test_every_problem_code_has_a_status_and_a_message():
    assert set(problems.STATUS) == set(problems.MESSAGES)


# --- what a refusal says ----------------------------------------------------------

ERROR_TYPES = typing.get_args(ErrorType)


def _template(error_type: str) -> str:
    """pydantic's message template for an error type, asked of pydantic itself."""
    context: dict = {}
    while True:
        try:
            return PydanticKnownError(error_type, context or None).message_template
        except (KeyError, TypeError) as error:
            needed = re.search(r"'(\w+)' required in context", str(error))
            text = re.search(r"'(\w+)' context value must be a String", str(error))
            if needed and needed.group(1) not in context:
                context[needed.group(1)] = 0
            elif text and context.get(text.group(1)) == 0:
                context[text.group(1)] = "x"
            else:
                raise


def _fastapi_messages() -> set[str]:
    """The messages FastAPI writes into validation errors itself: its "msg" literals."""
    import fastapi

    found = set()
    for path in Path(fastapi.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "msg"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        found.add(value.value)
    assert found  # non-vacuity: FastAPI has one for a body that is not JSON
    return found


@functools.cache
def library_phrases() -> frozenset[str]:
    """What the libraries' messages say: each run of two or more words of pydantic's
    templates between their placeholders, and FastAPI's own messages."""
    phrases = set(_fastapi_messages())
    for error_type in ERROR_TYPES:
        for piece in re.split(r"\{\w+\}", _template(error_type)):
            piece = piece.strip(" ,.:;'\"`")
            if len(piece.split()) >= 2:
                phrases.add(piece)
    assert "Field required" in phrases  # non-vacuity: a phrase known to be pydantic's
    return frozenset(phrases)


REGEX_SYNTAX = re.compile(r"\^|\$|\(\?|\[\^|\\[A-Za-z]|\{\d|\]\{|\]\*|\]\+")


def library_text(text: str) -> bool:
    """Whether a message holds a validation library's words or regular-expression syntax."""
    return bool(REGEX_SYNTAX.search(text)) or any(p in text for p in library_phrases())


def _passthrough_app():
    """An app whose validation errors send the library's own message, as rcman must not."""
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field

    class Body(BaseModel):
        name: str = Field(pattern="^a$", max_length=3)
        count: int = Field(ge=1)

    app = FastAPI()

    @app.post("/x")
    def x(body: Body) -> dict:
        return {}

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):
        errors = [{"field": problems._field(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"errors": errors}, status_code=422)

    return app


def test_library_text_is_recognised():
    """Planted twins: the library's own messages are flagged, rcman's own words are not."""
    from fastapi.testclient import TestClient
    from pydantic import Field, TypeAdapter, ValidationError

    from helpers import rc_admin, rc_helper
    from tests.test_rules import helpers_rules

    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Annotated[str, Field(pattern="^a$")]).validate_python("b")
    assert library_text(refused.value.errors()[0]["msg"])
    client = TestClient(_passthrough_app())
    sent = [
        {"json": {}},
        {"json": {"name": "b", "count": 0}},
        {"json": {"name": "aaaa", "count": "x"}},
        {"content": b"{bad", "headers": {"content-type": "application/json"}},
        {"json": []},
    ]
    passed_through = [e["message"] for kw in sent for e in client.post("/x", **kw).json()["errors"]]
    assert len(passed_through) >= len(sent)
    for message in passed_through:
        assert library_text(message), message
    ours = [
        *(rule.sentence for rule in helpers_rules().values()),
        *problems.MESSAGES.values(),
        *problems.FIELD_MESSAGES.values(),
        *rc_helper.MESSAGES.values(),
        *rc_admin.MESSAGES.values(),
    ]
    for message in ours:
        assert not library_text(message), message


def test_a_missing_field_says_it_is_required(client):
    r = client.post("/api/servers", json={}, headers=changing(ALICE))
    assert r.json()["errors"] == [{"field": "path", "message": problems.FIELD_MESSAGES["missing"]}]


def test_a_body_that_is_not_json_is_about_the_request(client):
    headers = {**changing(ALICE), "content-type": "application/json"}
    r = client.post("/api/servers", content=b"{bad", headers=headers)
    assert_problem(r, 422, "validation_failed")
    assert r.json()["errors"] == [
        {"field": "request", "message": problems.FIELD_MESSAGES["json_invalid"]}
    ]


def test_a_value_too_long_says_how_long_it_may_be(client):
    from helpers import rc_helper

    path = "a" * (rc_helper.MAX_PATH_LENGTH + 1)
    r = client.post("/api/servers", json={"path": path}, headers=changing(ALICE))
    [error] = r.json()["errors"]
    assert error["field"] == "path"
    assert str(rc_helper.MAX_PATH_LENGTH) in error["message"]
    assert error["message"] == problems.FIELD_MESSAGES["string_too_long"].format(
        max_length=rc_helper.MAX_PATH_LENGTH
    )


def test_an_empty_value_where_one_is_needed_says_so(client):
    r = client.post("/api/servers", json={"path": ""}, headers=changing(ALICE))
    assert r.json()["errors"] == [
        {"field": "path", "message": problems.FIELD_MESSAGES["string_empty"]}
    ]


def _float_app():
    """An app with a field of a kind rcman has no message for."""
    from fastapi import FastAPI
    from pydantic import BaseModel

    class Body(BaseModel):
        ratio: float

    app = FastAPI()
    problems.install(app)

    @app.post("/x")
    def x(body: Body) -> dict:
        return {}

    return app


def test_an_error_without_a_message_gets_a_readable_one_and_is_logged(caplog):
    from fastapi.testclient import TestClient

    with caplog.at_level(logging.WARNING, logger="rcman.problems"):
        r = TestClient(_float_app()).post("/x", json={"ratio": "x"})
    assert r.json()["errors"] == [
        {"field": "ratio", "message": problems.FIELD_MESSAGES["field_invalid"]}
    ]
    assert "float_parsing" in caplog.text
