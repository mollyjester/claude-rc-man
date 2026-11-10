"""A page's reports of failures rcman could not see, written to the journal."""

import datetime as dt
import json
from pathlib import Path

from rcman.routers.client_errors import ClientErrorBody

from tests.conftest import ALICE, CALLS, changing
from tests.test_journal import guide_check_fails_on_a_drifted_table, guide_fields

REPO = Path(__file__).resolve().parent.parent
BODY = CALLS[("POST", "/api/client-errors")]["json"]
LOGGER = "rcman.client"
HEADING = "## Errors reported by the page"
# What every line holds besides the report itself.
ADDED = {"event", "at", "email"}


def reported(caplog) -> list[dict]:
    """The lines logged on the client-error logger, as JSON."""
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]


def test_a_report_is_one_json_line_with_who_sent_it(client, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    r = client.post("/api/client-errors", json=BODY, headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {})
    [line] = reported(caplog)
    assert line["event"] == "client_error"
    assert line["email"] == ALICE
    assert dt.datetime.fromisoformat(line["at"]).tzinfo is not None
    assert {k: v for k, v in line.items() if k not in ADDED} == BODY


def test_the_journal_guide_lists_every_field_of_a_client_error():
    guide = (REPO / "docs" / "journal.md").read_text(encoding="utf-8")
    expected = set(ClientErrorBody.model_fields) | ADDED
    assert sorted(guide_fields(guide, HEADING)) == sorted(expected)


def test_the_client_error_guide_check_fails_on_a_drifted_table():
    """Planted twin of the check above."""
    guide_check_fails_on_a_drifted_table(HEADING, set(ClientErrorBody.model_fields) | ADDED)
