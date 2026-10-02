"""Every error is RFC 9457 problem details, and every refusal is in rcman's own words."""

import ast
import contextlib
import copy
import functools
import inspect
import json
import logging
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import pytest
from pydantic_core import PydanticKnownError
from pydantic_core.core_schema import ErrorType
from rcman import problems, routes

from tests.conftest import ALICE, CALLS, as_user, changing, concrete


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
        errors = [{"field": problems._field(e), "message": e["msg"]} for e in exc.errors()]
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
    from rcman.routers.claude import MAX_CODE_LENGTH

    code = "a" * (MAX_CODE_LENGTH + 1)
    r = client.post("/api/claude/code", json={"code": code}, headers=changing(ALICE))
    [error] = r.json()["errors"]
    assert error["field"] == "code"
    assert str(MAX_CODE_LENGTH) in error["message"]
    assert error["message"] == problems.FIELD_MESSAGES["string_too_long"].format(
        max_length=MAX_CODE_LENGTH
    )


def test_every_path_field_refuses_a_null_character_with_the_path_rule(app, client):
    from helpers import rc_helper

    rule = rc_helper.PATH
    checked = [f for f in request_fields(app) if f.schema.get("pattern") == rule.anchored]
    names = {(f.method, f.path, f.name) for f in checked}
    known = {
        ("POST", "/api/servers", "path"),
        ("POST", "/api/folders", "parent"),
        ("GET", "/api/folders", "path"),
        ("DELETE", "/api/folders", "path"),
        ("GET", "/api/servers/output", "path"),
    }
    assert known <= names, known - names  # non-vacuity
    for f in checked:
        r = send(client, f, "a\x00b", CALLS, changing(ALICE))
        assert r.json().get("errors") == [{"field": f.name, "message": rule.sentence}], f


def test_a_null_character_is_never_an_internal_error(app):
    """Driven over every text field: refused in rcman's words, or taken, never a crash."""
    from fastapi.testclient import TestClient

    from tests.test_rules import helpers_rules

    sentences = {rule.sentence for rule in helpers_rules().values()}
    texts = [f for f in request_fields(app) if f.schema.get("type") == "string"]
    assert len(texts) > 10  # non-vacuity
    found = []
    with TestClient(app, raise_server_exceptions=False) as client:
        for f in texts:
            r = send(client, f, "a\x00b", CALLS, changing(ALICE))
            what = f"{f.method} {f.path} {f.where} {f.name}"
            if r.status_code >= 500:
                found.append(f"{what}: {r.status_code} {r.text[:120]}")
            elif r.status_code == 422:
                for e in r.json()["errors"]:
                    if library_text(e["message"]) or not readable(e["message"], sentences):
                        found.append(f"{what}: {e['message']!r}")
    assert found == []


def test_a_body_that_is_not_utf8_is_refused_in_plain_words(app, client):
    found = bodies(app)
    assert ("POST", "/api/servers") in {(m, p) for m, p, _ in found}  # non-vacuity
    headers = {**changing(ALICE), "content-type": "application/json"}
    for method, path, _ in found:
        r = client.request(method, concrete(path), content=b'{"x": "\xff"}', headers=headers)
        assert_problem(r, 400, "body_unreadable")
        assert r.json()["title"] == problems.MESSAGES["body_unreadable"]


def test_half_a_character_is_refused_in_plain_words(client):
    headers = {**changing(ALICE), "content-type": "application/json"}
    r = client.post("/api/claude/code", content=b'{"code": "\\ud800"}', headers=headers)
    assert r.json()["errors"] == [
        {"field": "code", "message": problems.FIELD_MESSAGES["string_unicode"]}
    ]


def test_half_a_character_in_a_ruled_field_is_refused_with_its_rule(client):
    from helpers import rc_helper

    headers = {**changing(ALICE), "content-type": "application/json"}
    r = client.post("/api/folders", content=b'{"name": "\\ud800"}', headers=headers)
    assert r.json()["errors"] == [{"field": "name", "message": rc_helper.FOLDER_NAME.sentence}]


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


def test_an_error_without_the_context_its_message_needs_gets_a_readable_one(caplog):
    error = {"type": "string_too_long", "loc": ("body", "x"), "msg": "", "ctx": {}}
    with caplog.at_level(logging.WARNING, logger="rcman.problems"):
        message = problems._message(error)
    assert message == problems.FIELD_MESSAGES[problems.FIELD_INVALID]
    assert "string_too_long" in caplog.text


def test_a_ruled_field_refuses_a_length_with_its_rules_sentence(client):
    from helpers import rc_helper

    name = "a" * (rc_helper.MAX_PATH_LENGTH + 1)
    assert rc_helper.FOLDER_NAME.fullmatch(name)  # only its length is refused
    r = client.post("/api/folders", json={"name": name}, headers=changing(ALICE))
    assert r.json()["errors"] == [{"field": "name", "message": rc_helper.FOLDER_NAME.sentence}]


# --- every field of every route, as validated and as published --------------------


@dataclass(frozen=True)
class RequestField:
    """One thing a route takes: a body field, a query parameter or a path parameter."""

    method: str
    path: str
    where: str  # body, query or path
    name: str
    schema: dict = field(compare=False, hash=False)  # as published, without a null alternative
    required: bool = False


def _resolved(schema: dict, spec: dict) -> dict:
    """A published schema with its reference followed and a null alternative left out."""
    if "$ref" in schema:
        return _resolved(spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]], spec)
    if "anyOf" in schema:
        kept = [_resolved(s, spec) for s in schema["anyOf"] if s.get("type") != "null"]
        assert len(kept) == 1, schema
        return {**{k: v for k, v in schema.items() if k != "anyOf"}, **kept[0]}
    return schema


def _parameters(dependant) -> list[tuple[str, object]]:
    """Every parameter of a route, its dependencies' too, as (where, FastAPI's field)."""
    pending, found = [dependant], []
    while pending:
        current = pending.pop()
        for where in ("query", "path", "header", "cookie", "body"):
            found.extend((where, f) for f in getattr(current, f"{where}_params"))
        pending.extend(current.dependencies)
    return found


def _body_schema(spec: dict, operation: dict) -> dict:
    return _resolved(operation["requestBody"]["content"]["application/json"]["schema"], spec)


def request_fields(app) -> list[RequestField]:
    """What each route takes, from the route table, paired with what the API publishes.

    Fails where the two disagree: a field validated but not published, or the reverse.
    """
    spec = app.openapi()
    found = []
    for context in routes.api_routes(app):
        if not context.route.include_in_schema:
            # Outside the published API (the frontend's files): nothing it takes may be
            # refused, so it holds no field to drive.
            for where, param in _parameters(context.route.dependant):
                assert where != "body" and param.field_info.annotation is str, context.path
                assert not param.field_info.metadata, (context.path, param.field_info)
            continue
        for method in sorted(context.methods):
            operation = spec["paths"][context.path][method.lower()]
            published = {(p["in"], p["name"]): p for p in operation.get("parameters", [])}
            for where, param in _parameters(context.route.dependant):
                if where == "body":
                    model = param.field_info.annotation
                    body = _body_schema(spec, operation)
                    assert list(body["properties"]) == list(model.model_fields), context.path
                    found.extend(
                        RequestField(
                            method,
                            context.path,
                            "body",
                            name,
                            _resolved(body["properties"][name], spec),
                            name in body.get("required", []),
                        )
                        for name in model.model_fields
                    )
                else:
                    given = published.pop((where, param.alias))
                    found.append(
                        RequestField(
                            method,
                            context.path,
                            where,
                            param.alias,
                            _resolved(given["schema"], spec),
                            given.get("required", False),
                        )
                    )
            assert published == {}, (method, context.path, list(published))
    return found


def bodies(app) -> list[tuple[str, str, dict]]:
    """Each route that takes a body, with the body's published schema."""
    spec = app.openapi()
    return [
        (method.upper(), path, _body_schema(spec, operation))
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if "requestBody" in operation
    ]


def _calls(dependant):
    """The endpoint of a route and every dependency it calls."""
    pending, found = [dependant], []
    while pending:
        current = pending.pop()
        if current.call is not None:
            found.append(current.call)
        pending.extend(current.dependencies)
    return found


def _constrained(annotation) -> bool:
    """Whether an annotation carries constraints of its own (Annotated metadata)."""
    if typing.get_origin(annotation) is Annotated:
        return True
    return any(_constrained(a) for a in typing.get_args(annotation))


def dropped_constraints(app) -> list[str]:
    """Parameters whose constraints FastAPI leaves unchecked: a type carrying its own
    constraints, such as a problems.ruled one, with Path(), Query() or Body() as the
    default instead of inside an Annotated."""
    from fastapi import params

    found = []
    for context in routes.api_routes(app):
        for call in _calls(context.route.dependant):
            if not inspect.isroutine(call):  # a callable object, such as a Recorder
                call = type(call).__call__
            hints = typing.get_type_hints(call, include_extras=True)
            for name, parameter in inspect.signature(call).parameters.items():
                default = parameter.default
                if isinstance(default, (params.Param, params.Body)) and _constrained(
                    hints.get(name)
                ):
                    kind = type(default).__name__
                    found.append(
                        f"{context.path} {call.__name__}({name}): {kind}() as the default "
                        f"drops the constraints of its type; write "
                        f"{name}: Annotated[<type>, {kind}()] instead"
                    )
    return found


def _planted_path_app(annotated: bool):
    """An app with a ruled path parameter, as the trap or in the Annotated form."""
    from fastapi import FastAPI, Path

    from helpers.rules import Rule

    Digits = problems.ruled(Rule(re.compile(r"[0-9]+"), "Use digits only."))
    app = FastAPI()
    problems.install(app)
    if annotated:

        @app.get("/x/{n}")
        def right(n: Annotated[Digits, Path()]) -> dict:
            return {}

    else:

        @app.get("/x/{n}")
        def trap(n: Digits = Path()) -> dict:  # noqa: B008 - the trap itself
            return {}

    return app


def _published_path_schema(app) -> dict:
    return app.openapi()["paths"]["/x/{n}"]["get"]["parameters"][0]["schema"]


def test_no_parameter_has_its_constraints_dropped(app):
    hints = typing.get_type_hints(
        next(c.route.endpoint for c in routes.api_routes(app) if c.path == "/api/users/{linux}"),
        include_extras=True,
    )
    assert _constrained(hints["linux"])  # non-vacuity: the guard sees the users route's rule
    assert dropped_constraints(app) == []


def test_a_rule_given_with_path_as_the_default_is_found():
    """Planted twin: the trap drops the rule from what is checked and published."""
    from fastapi.testclient import TestClient

    trap = _planted_path_app(annotated=False)
    assert "pattern" not in _published_path_schema(trap)  # the plant really drops the rule
    assert TestClient(trap).get("/x/abc").status_code == 200  # and lets a refused value in
    [found] = dropped_constraints(trap)
    assert "trap(n)" in found and "Annotated[<type>, Path()]" in found
    right = _planted_path_app(annotated=True)
    assert "pattern" in _published_path_schema(right)  # positive control
    assert TestClient(right).get("/x/abc").status_code == 422
    assert dropped_constraints(right) == []


def test_the_fields_are_found_and_agree_with_the_published_api(app):
    found = {(f.method, f.path, f.where, f.name) for f in request_fields(app)}
    known = {
        ("PATCH", "/api/me/settings", "body", "prefix"),
        ("DELETE", "/api/users/{linux}", "path", "linux"),
        ("DELETE", "/api/users/{linux}", "query", "home"),
        ("POST", "/api/folders", "body", "name"),
        ("GET", "/api/audit", "query", "limit"),
    }
    assert known <= found, known - found  # non-vacuity: one of each kind
    with_body = {(f.method, f.path) for f in request_fields(app) if f.where == "body"}
    assert with_body == {(method, path) for method, path, _ in bodies(app)}


def test_every_route_that_takes_parameters_has_a_valid_request(app, client):
    taking = {(f.method, f.path) for f in request_fields(app)}
    assert ("GET", "/api/folders") in taking  # non-vacuity
    assert taking <= set(CALLS), taking - set(CALLS)
    for method, path in sorted(taking):
        r = client.request(method, concrete(path), headers=changing(ALICE), **CALLS[(method, path)])
        assert r.status_code != 422, (method, path, r.text)


OMIT = object()  # a violation that leaves the field out
LONE_SURROGATE = object()  # a violation only raw JSON can send: "\ud800", half a character


def refused_value(pattern: str) -> str:
    """A value the published pattern refuses, from a few that break most patterns."""
    from tests.test_rc_helper import REFUSAL_CANDIDATES

    for value in REFUSAL_CANDIDATES:
        if not re.search(pattern, value):
            return value
    raise AssertionError(f"no candidate value breaks {pattern!r}")


def violations(f: RequestField) -> list[tuple[str, object]]:
    """Every violation the field's published schema implies: (what, the value to send)."""
    s, found = f.schema, []
    if "pattern" in s:
        found.append(("pattern", refused_value(s["pattern"])))
    if "maxLength" in s:
        found.append(("maxLength", "a" * (s["maxLength"] + 1)))
    if s.get("minLength", 0) >= 1:
        found.append(("minLength", ""))
    if "minimum" in s:
        found.append(("minimum", s["minimum"] - 1))
    if "maximum" in s:
        found.append(("maximum", s["maximum"] + 1))
    if "enum" in s or "const" in s:
        found.append(("enum", "zz"))
    if s.get("type") == "boolean":
        found.append(("type", "maybe"))
    if s.get("type") in ("integer", "number"):
        found.append(("type", "x"))
    if f.where == "body":
        found.append(("type", []))
    if f.where == "body" and s.get("type") == "string":
        found.append(("lone surrogate", LONE_SURROGATE))
    if f.required:
        found.append(("required", OMIT))
    if f.where == "path":  # a path cannot leave a part out or make it empty
        found = [(what, value) for what, value in found if value is not OMIT and value != ""]
    return found


def send(client, f: RequestField, value, calls, headers):
    """The route's valid request, with the field set to ``value`` or left out."""
    kwargs = copy.deepcopy(calls[(f.method, f.path)])
    path = f.path
    if f.where == "path":
        path = path.replace("{" + f.name + "}", quote(str(value), safe=""))
    else:
        part = "json" if f.where == "body" else "params"
        sent = dict(kwargs.get(part, {}))
        sent.pop(f.name, None)
        if value is not OMIT:
            sent[f.name] = value if f.where == "body" else str(value)
        kwargs[part] = sent
    if value is LONE_SURROGATE:
        mark = "planted-surrogate"
        kwargs["json"][f.name] = mark
        raw = json.dumps(kwargs.pop("json")).replace(json.dumps(mark), '"\\ud800"')
        kwargs["content"] = raw.encode()
        headers = {**headers, "content-type": "application/json"}
    return client.request(f.method, concrete(path), headers=headers, **kwargs)


# What is sent in place of a body: (what, the client's keyword arguments).
NOT_A_BODY = [
    ("not JSON", {"content": b"{bad", "headers": {"content-type": "application/json"}}),
    ("not an object", {"json": []}),
    ("no body", {}),
]


@contextlib.contextmanager
def message_types_asked():
    """The error types the validation handler looks up in FIELD_MESSAGES meanwhile."""

    class Recording(dict):
        def __contains__(self, key):
            asked.add(key)
            return super().__contains__(key)

        def __getitem__(self, key):
            asked.add(key)
            return super().__getitem__(key)

    asked: set = set()
    original = problems.FIELD_MESSAGES
    problems.FIELD_MESSAGES = Recording(original)
    try:
        yield asked
    finally:
        problems.FIELD_MESSAGES = original


def readable(message: str, sentences) -> bool:
    """Whether a message is a helpers' rule sentence or one of FIELD_MESSAGES' own texts."""
    if message in sentences:
        return True
    return any(
        re.fullmatch(re.sub(r"\\\{\w+\\\}", ".+", re.escape(text)), message)
        for kind, text in problems.FIELD_MESSAGES.items()
        if kind not in (problems.RULE_REFUSED, problems.FIELD_INVALID)
    )


def refusal_problems(client, calls, headers) -> tuple[list[str], set[str]]:
    """Drive every field of the client's app with every violation its schema implies,
    and every body route with what is not a body.

    Returns what was not refused at its field in rcman's own words, and the
    FIELD_MESSAGES entries the handler used.
    """
    from tests.test_rules import helpers_rules

    sentences = {rule.sentence for rule in helpers_rules().values()}
    fallback = problems.FIELD_MESSAGES[problems.FIELD_INVALID]
    found = []

    def check(what, response, expected):
        if response.status_code != 422 or response.json().get("code") != "validation_failed":
            found.append(f"{what}: not refused ({response.status_code})")
            return
        errors = response.json()["errors"]
        if [e["field"] for e in errors] != [expected]:
            found.append(f"{what}: refused at {[e['field'] for e in errors]}, not {expected}")
        for e in errors:
            if e["message"] == fallback:
                found.append(f"{what}: the fallback message")
            elif library_text(e["message"]) or not readable(e["message"], sentences):
                found.append(f"{what}: {e['message']!r}")

    with message_types_asked() as asked:
        for f in request_fields(client.app):
            for what, value in violations(f):
                response = send(client, f, value, calls, headers)
                check(f"{f.method} {f.path} {f.where} {f.name}: {what}", response, f.name)
        for method, path, schema in bodies(client.app):
            for what, kwargs in NOT_A_BODY:
                sent = {**kwargs, "headers": {**headers, **kwargs.get("headers", {})}}
                response = client.request(method, concrete(path), **sent)
                check(f"{method} {path}: {what}", response, "request")
            if schema.get("minProperties", 0) >= 1:
                response = client.request(method, concrete(path), json={}, headers=headers)
                check(f"{method} {path}: an empty object", response, "request")
    found.extend(f"no message for {kind}" for kind in sorted(asked - set(problems.FIELD_MESSAGES)))
    return found, asked & set(problems.FIELD_MESSAGES)


def test_no_refusal_the_api_sends_holds_library_text(client):
    found, used = refusal_problems(client, CALLS, changing(ALICE))
    assert used  # non-vacuity: refusals were made and their messages looked up
    assert found == []


def test_an_error_type_without_a_message_is_found():
    """Planted twin: a field of a kind rcman has no message for is reported, by its type."""
    from fastapi.testclient import TestClient

    app = _float_app()
    assert [f.name for f in request_fields(app)] == ["ratio"]  # the plant took effect
    calls = {("POST", "/x"): {"json": {"ratio": 1.5}}}
    found, _ = refusal_problems(TestClient(app), calls, {})
    assert "no message for float_parsing" in found
    assert any("the fallback message" in problem for problem in found)


def unreached_messages(client) -> set[str]:
    """The FIELD_MESSAGES entries, but the fallback, that driving every field never uses."""
    _, used = refusal_problems(client, CALLS, changing(ALICE))
    return set(problems.FIELD_MESSAGES) - used - {problems.FIELD_INVALID}


def test_every_field_message_is_reached(client):
    assert unreached_messages(client) == set()
    own = {
        problems.RULE_REFUSED,
        problems.SETTINGS_NONE_SENT,
        problems.STRING_EMPTY,
        problems.FIELD_INVALID,
    }
    assert set(problems.FIELD_MESSAGES) <= set(ERROR_TYPES) | own
    assert own.isdisjoint(ERROR_TYPES)  # rcman's own types are not pydantic's


def test_a_field_message_nothing_reaches_is_found(client, monkeypatch):
    """Planted twin: an entry no refusal can produce is reported, and only it."""
    planted = {**problems.FIELD_MESSAGES, "planted_never_sent": "Nothing sends this."}
    monkeypatch.setattr(problems, "FIELD_MESSAGES", planted)
    assert unreached_messages(client) == {"planted_never_sent"}


# --- every published pattern is a helpers' rule -----------------------------------


def _rules_and_their_empty_forms():
    from tests.test_rules import helpers_rules

    found = {}
    for name, rule in helpers_rules().items():
        found[name] = rule
        found[f"{name}.or_empty()"] = rule.or_empty()
    return found


def pattern_problems(client, calls, headers) -> tuple[list[str], set[str]]:
    """Every published pattern of the client's app: whether it is a helpers' rule, and
    whether a value it refuses is refused at its field with that rule's sentence.

    Returns the problems found and the names of the rules the patterns are."""
    rules = _rules_and_their_empty_forms()
    found, matched = [], set()
    for f in request_fields(client.app):
        if "pattern" not in f.schema:
            continue
        what = f"{f.method} {f.path} {f.where} {f.name}"
        named = [name for name, rule in rules.items() if rule.anchored == f.schema["pattern"]]
        if not named:
            found.append(f"{what}: its pattern is no helpers' rule")
            continue
        matched.update(named)
        rule = rules[named[0]]
        response = send(client, f, refused_value(f.schema["pattern"]), calls, headers)
        errors = response.json().get("errors") if response.status_code == 422 else None
        if errors != [{"field": f.name, "message": rule.sentence}]:
            found.append(f"{what}: {response.status_code} {errors}")
    return found, matched


def test_every_published_pattern_is_explained_by_a_helpers_rule(client):
    found, matched = pattern_problems(client, CALLS, changing(ALICE))
    names = {"PREFIX", "EMAIL", "LINUX_NAME", "FOLDER_NAME", "AUTHOR_NAME", "VERSION"}
    assert names <= {name.removesuffix(".or_empty()") for name in matched}  # non-vacuity
    assert found == []


def test_a_pattern_without_a_rule_is_found():
    """Planted twin: a field with a pattern of its own is reported."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel, Field

    class Body(BaseModel):
        code: str = Field(pattern="^a$")

    app = FastAPI()
    problems.install(app)

    @app.post("/x")
    def x(body: Body) -> dict:
        return {}

    assert [f.schema.get("pattern") for f in request_fields(app)] == ["^a$"]  # the plant
    found, _ = pattern_problems(TestClient(app), {("POST", "/x"): {"json": {"code": "a"}}}, {})
    assert found == ["POST /x body code: its pattern is no helpers' rule"]
