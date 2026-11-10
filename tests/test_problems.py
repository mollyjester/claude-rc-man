"""Every error is RFC 9457 problem details, and every refusal is in rcman's own words."""

import ast
import contextlib
import copy
import functools
import inspect
import json
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import pytest
from pydantic import BaseModel
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


def test_an_unknown_route_and_a_validation_error_are_problems(client):
    assert_problem(client.get("/api/nope", headers=as_user(ALICE)), 404, "not_found")
    body = assert_problem(
        client.post("/api/servers", json={}, headers=changing(ALICE)), 422, "validation_failed"
    )
    assert body["errors"] and body["errors"][0]["field"] == "workspace"
    assert body["errors"][0]["message"]


def unauthenticated_not_refused(client, found) -> list[str]:
    """The routes of ``found`` that do not answer a request with no identity with the problem
    ``not_authenticated``."""
    bad = []
    for route in found:
        method = sorted(route.methods)[0]
        path = route.path.replace("{person}", "1").replace("{workspace}", "1")
        response = client.request(method, path.replace("{slug}", "x"))
        if (
            response.status_code != 401
            or not response.headers["content-type"].startswith(problems.MEDIA_TYPE)
            or response.json().get("type") != "urn:rcman:problem:not_authenticated"
        ):
            bad.append(f"{method} {route.path}")
    return bad


def test_every_route_answers_a_missing_identity_with_a_problem(app, client):
    found = [c for c in routes.api_routes(app) if c.path not in problems.PUBLIC_PATHS]
    assert found  # non-vacuity
    assert unauthenticated_not_refused(client, found) == []


def test_the_missing_identity_check_finds_a_route_that_lets_anyone_in(app):
    """Planted twin: a route with no identity is found; one with it is not."""
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from rcman.identity import current_user

    @app.post("/api/planted-open")  # a GET would be the pages' catch-all's
    def open_():
        return {}

    @app.post("/api/planted-guarded")
    def guarded(identity=Depends(current_user)):
        return {}

    planted = [c for c in routes.api_routes(app) if c.path.startswith("/api/planted-")]
    assert len(planted) == 2  # the plant took effect
    with TestClient(app) as client:
        assert unauthenticated_not_refused(client, planted) == ["POST /api/planted-open"]


def missing_identity_accepted(client, paths) -> list[str]:
    """The WebSocket routes of ``paths`` that do not refuse an upgrade with no identity, before
    accepting it, with the problem ``not_authenticated``."""
    from starlette.testclient import WebSocketDenialResponse

    found = []
    for path in paths:
        try:
            with client.websocket_connect(path.replace("{workspace}", "1") + "?workspace=1"):
                pass
        except WebSocketDenialResponse as denied:
            body = denied.json()
            if (denied.status_code, body.get("code"), body.get("type")) == (
                401,
                "not_authenticated",
                "urn:rcman:problem:not_authenticated",
            ) and denied.headers["content-type"].startswith(problems.MEDIA_TYPE):
                continue
        except Exception:  # noqa: BLE001 - anything else is no refusal with a problem
            pass
        found.append(path)
    return found


def test_every_websocket_route_refuses_a_missing_identity_before_the_upgrade(app, client):
    paths = [r.path for r in routes.websocket_routes(app)]
    assert paths  # non-vacuity
    assert missing_identity_accepted(client, paths) == []


def test_the_missing_identity_check_finds_a_websocket_route_that_lets_anyone_in(app):
    """Planted twin: a WebSocket route with no identity is found; one with it is not."""
    from fastapi import Depends, WebSocket
    from fastapi.testclient import TestClient
    from rcman.identity import current_user

    @app.websocket("/api/planted-open")
    async def open_(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    @app.websocket("/api/planted-guarded")
    async def guarded(websocket: WebSocket, identity=Depends(current_user)):
        await websocket.accept()
        await websocket.close()

    with TestClient(app) as client:  # a catch-all for pages takes no WebSocket
        found = missing_identity_accepted(client, ["/api/planted-open", "/api/planted-guarded"])
    assert found == ["/api/planted-open"]


def unpaired_codes() -> set[str]:
    """The problem codes with a status but no message, or a message but no status."""
    return set(problems.STATUS) ^ set(problems.MESSAGES)


def test_every_problem_code_has_a_status_and_a_message():
    assert problems.STATUS  # non-vacuity
    assert unpaired_codes() == set()


def test_a_problem_code_without_a_message_is_found(monkeypatch):
    """Planted twin: a code given a status and no message is found."""
    monkeypatch.setattr(problems, "STATUS", {**problems.STATUS, "planted": 400})
    assert unpaired_codes() == {"planted"}


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


def test_library_text_is_recognised():
    """Planted twin: the library's own message is flagged, rcman's own words are not."""
    from pydantic import Field, TypeAdapter, ValidationError

    from tests.test_rules import helpers_rules

    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Annotated[str, Field(pattern="^a$")]).validate_python("b")
    assert library_text(refused.value.errors()[0]["msg"])
    ours = [
        *(rule.sentence for rule in helpers_rules().values()),
        *problems.MESSAGES.values(),
        *problems.CLONE_MESSAGES.values(),
        *problems.CLAUDE_INSTALL_MESSAGES.values(),
        *problems.DEVCONTAINER_MESSAGES.values(),
        *problems.REBUILD_MESSAGES.values(),
        *problems.POWER_MESSAGES.values(),
        *problems.UPGRADE_REFUSED_MESSAGES.values(),
        problems.PERSON_DELETION_LEFT,
        *problems.FIELD_MESSAGES.values(),
    ]
    for message in ours:
        assert not library_text(message), message


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
                model = param.field_info.annotation
                if where == "body" and not (
                    isinstance(model, type) and issubclass(model, BaseModel)
                ):
                    # An embedded Body(): one property of the body FastAPI publishes.
                    body = _body_schema(spec, operation)
                    assert param.alias in body.get("properties", {}), (
                        f"{method} {context.path}: request_fields reads a body that is a "
                        f"model or an embedded Body(), not {param.alias!r} as the whole body"
                    )
                    found.append(
                        RequestField(
                            method,
                            context.path,
                            "body",
                            param.alias,
                            _resolved(body["properties"][param.alias], spec),
                            param.alias in body.get("required", []),
                        )
                    )
                elif where == "body":
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


def _nodes(dependant):
    """A route's dependant and every dependency below it."""
    pending, found = [dependant], []
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(current.dependencies)
    return found


def _constraints_of(item) -> set[str]:
    """The constraints one piece of Annotated metadata adds: annotated_types' (lengths,
    bounds, a pattern) and pydantic's validators, inside a FieldInfo or alone. A
    parameter marker adds only what it was given; a plain note adds none."""
    import annotated_types
    from pydantic import AfterValidator, BeforeValidator, PlainValidator, WrapValidator
    from pydantic.fields import FieldInfo

    if isinstance(item, FieldInfo):  # Field(), and FastAPI's Query(), Path(), Body()...
        return set().union(*(_constraints_of(m) for m in item.metadata))
    validators = (AfterValidator, BeforeValidator, PlainValidator, WrapValidator)
    if isinstance(item, (annotated_types.BaseMetadata, *validators)):
        return {repr(item)}
    return set()


def _constraints(annotation) -> set[str]:
    """Every constraint an annotation carries, through Annotated and unions."""
    if typing.get_origin(annotation) is Annotated:
        base, *metadata = typing.get_args(annotation)
        return _constraints(base).union(*(_constraints_of(m) for m in metadata))
    return set().union(*(_constraints(a) for a in typing.get_args(annotation)))


def _call_name(call) -> str:
    call = getattr(call, "func", call)  # a functools.partial
    return getattr(call, "__name__", type(call).__name__)


def dropped_constraints(app) -> list[str]:
    """Parameters whose declared type carries a constraint, such as a problems.ruled
    type's pattern and validator, that the field FastAPI resolved for them lacks.

    FastAPI reads every endpoint and dependency (a function, a class, a bound
    functools.partial, a callable object) with inspect.signature; this compares
    each query, path, header, cookie and body parameter it made with the
    annotation it was declared with. A FastAPI parameter marker given as the
    default of a constrained type drops the type's constraints; inside an
    Annotated they are kept."""
    found = []
    for context in routes.api_routes(app):
        for node in _nodes(context.route.dependant):
            if node.call is None:
                continue
            declared = inspect.signature(node.call, eval_str=True).parameters
            for where in ("query", "path", "header", "cookie", "body"):
                for param in getattr(node, f"{where}_params"):
                    info = param.field_info
                    kept = _constraints(info.annotation).union(
                        *(_constraints_of(m) for m in info.metadata)
                    )
                    if _constraints(declared[param.name].annotation) - kept:
                        kind = type(info).__name__
                        found.append(
                            f"{context.path} {_call_name(node.call)}({param.name}): {kind}() "
                            f"as the default drops the constraints of its type; write "
                            f"{param.name}: Annotated[<type>, {kind}()] instead"
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
        next(c.route.endpoint for c in routes.api_routes(app) if c.path == "/api/people/{person}"),
        include_extras=True,
    )
    assert _constraints(hints["person"])  # non-vacuity: the guard sees the people route's bound
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


def without_a_valid_request(client, calls, headers) -> list[str]:
    """The routes of the client's app that take parameters and have no request in ``calls``
    that they accept."""
    found = []
    for method, path in sorted({(f.method, f.path) for f in request_fields(client.app)}):
        call = calls.get((method, path))
        if (
            call is None
            or client.request(method, concrete(path), headers=headers, **call).status_code == 422
        ):
            found.append(f"{method} {path}")
    return found


def test_every_route_that_takes_parameters_has_a_valid_request(app, client):
    assert ("GET", "/api/servers/output") in {(f.method, f.path) for f in request_fields(app)}
    assert without_a_valid_request(client, CALLS, changing(ALICE)) == []


def test_a_route_whose_request_is_refused_is_found():
    """Planted twin: a route whose request in ``calls`` is refused as invalid is found."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    problems.install(app)

    @app.get("/x")
    def x(count: int) -> dict:
        return {}

    calls = {("GET", "/x"): {"params": {"count": "many"}}}
    assert without_a_valid_request(TestClient(app), calls, {}) == ["GET /x"]


# A few values that break most patterns, tried in order.
REFUSAL_CANDIDATES = ("\x01", " ", "", "/")
OMIT = object()  # a violation that leaves the field out
LONE_SURROGATE = object()  # a violation only raw JSON can send: "\ud800", half a character


def refused_value(pattern: str) -> str:
    """A value the published pattern refuses, from a few that break most patterns."""
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
    if s.get("type") == "array":
        if s.get("minItems", 0) >= 1:
            found.append(("minItems", []))
        if "maxItems" in s:
            found.append(("maxItems", ["a"] * (s["maxItems"] + 1)))
        found.append(("type", "x"))
    elif f.where == "body":
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
        # Rules may share a pattern and differ in words: any of theirs is the field's.
        sentences = [[{"field": f.name, "message": rules[name].sentence}] for name in named]
        response = send(client, f, refused_value(f.schema["pattern"]), calls, headers)
        errors = response.json().get("errors") if response.status_code == 422 else None
        if errors not in sentences:
            found.append(f"{what}: {response.status_code} {errors}")
    return found, matched


def test_every_published_pattern_is_explained_by_a_helpers_rule(client):
    found, matched = pattern_problems(client, CALLS, changing(ALICE))
    names = {
        "PREFIX",
        "EMAIL",
        "WORKSPACE_NAME",
        "AUTHOR_NAME",
        "VERSION",
        "COMMIT_MESSAGE",
        "MEMORY",
        "CPU",
        "PIDS",
    }
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
