"""A page's reports of failures rcman could not see, written to the journal."""

import datetime as dt
import json
import typing
from pathlib import Path

from pydantic import BeforeValidator
from rcman.routers import client_errors
from rcman.routers.client_errors import ClientErrorBody

from tests.conftest import ALICE, CALLS, as_user, changing
from tests.test_audit import json_events, rows, service_output
from tests.test_journal import guide_check_fails_on_a_drifted_table, guide_fields

REPO = Path(__file__).resolve().parent.parent
BODY = CALLS[("POST", "/api/client-errors")]["json"]
LOGGER = "rcman.client"
HEADING = "## Errors reported by the page"
# What every line holds besides the report itself.
ADDED = {"event", "at", "email", "linux"}


def reported(caplog) -> list[dict]:
    """The lines logged on the client-error logger, as JSON."""
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]


def report(client, body=BODY, headers=None):
    return client.post("/api/client-errors", json=body, headers=headers or changing(ALICE))


def test_a_report_is_one_json_line_with_who_sent_it(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    r = report(client)
    assert (r.status_code, r.json()) == (200, {})
    [line] = reported(caplog)
    assert line["event"] == "client_error"
    assert (line["email"], line["linux"]) == (ALICE, "alice")
    assert dt.datetime.fromisoformat(line["at"]).tzinfo is not None
    assert {k: v for k, v in line.items() if k not in ADDED} == BODY


def test_a_report_is_not_in_the_audit_trail(client, settings, caplog):
    caplog.set_level("INFO")
    client.patch("/api/me/settings", json={"prefix": "pfx"}, headers=changing(ALICE))
    before = rows(settings)
    assert before  # positive control: a change adds a row
    assert any(r.name == "rcman.audit" for r in caplog.records)  # and is logged
    caplog.clear()
    assert report(client).status_code == 200
    assert reported(caplog)  # positive control: the report was logged
    assert rows(settings) == before
    assert [r for r in caplog.records if r.name == "rcman.audit"] == []


def cut_of(metadata, annotation) -> int | None:
    """How many characters a field cuts its text to, if it is one that cuts: from its
    metadata, or from its type, through Annotated and unions."""
    for item in metadata:
        if isinstance(item, BeforeValidator) and isinstance(item.func, client_errors.Cut):
            return item.func.chars
    for arg in typing.get_args(annotation):
        if (found := cut_of([arg], arg)) is not None:
            return found
    return None


def test_text_past_its_bound_is_cut_not_refused(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    cut = {
        name: chars
        for name, f in ClientErrorBody.model_fields.items()
        if (chars := cut_of(f.metadata, f.annotation)) is not None
    }
    assert {"kind", "path", "message", "page"} <= set(cut)  # non-vacuity
    for name, chars in cut.items():
        caplog.clear()
        r = report(client, {**BODY, name: "x" * (chars + 50)})
        assert r.status_code == 200, (name, r.text)
        [line] = reported(caplog)
        assert line[name] == "x" * chars, name


def test_the_published_schema_says_what_each_text_is_cut_to():
    properties = ClientErrorBody.model_json_schema()["properties"]
    cut = {
        name: chars
        for name, f in ClientErrorBody.model_fields.items()
        if (chars := cut_of(f.metadata, f.annotation)) is not None
    }
    assert {"kind", "method"} <= set(cut)  # non-vacuity: a required field and one that may be null
    for name, chars in cut.items():
        assert f"Cut to {chars} characters." in properties[name].get("description", ""), name


def test_a_field_rcman_does_not_take_is_accepted_and_not_logged(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    r = report(client, {**BODY, "planted": "PLANTED-EXTRA"})
    assert r.status_code == 200, r.text
    [line] = reported(caplog)  # positive control: the report was logged
    assert "planted" not in line
    assert "PLANTED-EXTRA" not in caplog.text


def test_a_report_cannot_add_a_line_or_a_field(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    message = 'one\ntwo", "email": "mallory@example.com'
    assert report(client, {**BODY, "message": message}).status_code == 200
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert "\n" not in record.getMessage()
    line = json.loads(record.getMessage())
    assert line["email"] == ALICE and line["message"] == message


def test_a_report_needs_the_public_origin(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    r = report(client, headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"]) == (403, "origin_refused")
    assert reported(caplog) == []
    assert report(client).status_code == 200  # positive control
    assert len(reported(caplog)) == 1


def test_reports_reach_the_service_output(tmp_path, settings):
    """Started as the unit starts it, a report is a line on the service's own output."""
    lines = service_output(tmp_path, settings, "POST", "/api/client-errors", BODY, changing(ALICE))
    events = [e for e in json_events(lines) if e.get("event") == "client_error"]
    assert len(events) == 1, lines
    assert events[0]["email"] == ALICE and events[0]["kind"] == BODY["kind"]


def test_the_journal_guide_lists_every_field_of_a_client_error():
    guide = (REPO / "docs" / "journal.md").read_text(encoding="utf-8")
    expected = set(ClientErrorBody.model_fields) | ADDED
    assert sorted(guide_fields(guide, HEADING)) == sorted(expected)


def test_the_client_error_guide_check_fails_on_a_drifted_table():
    """Planted twin of the check above."""
    guide_check_fails_on_a_drifted_table(HEADING, set(ClientErrorBody.model_fields) | ADDED)
