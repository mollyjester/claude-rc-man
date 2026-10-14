"""Sending rcman's own events to the journal."""

import logging
import re
import sys
import uuid

import pytest
from rcman import journal


@pytest.fixture
def planted():
    """A logger of its own, its handlers removed when the test ends."""
    log = logging.getLogger(f"rcman.planted.{uuid.uuid4().hex}")
    log.setLevel(logging.INFO)
    yield log
    for handler in list(log.handlers):
        log.removeHandler(handler)


def bare_stderr_handlers(log):
    """The handlers writing a bare message to standard error as it is now: install binds
    the stream it is given when it runs, so a test asks while that stream is current."""
    return [
        h
        for h in log.handlers
        if isinstance(h, logging.StreamHandler)
        and h.stream is sys.stderr
        and h.formatter is not None
        and h.formatter._fmt == "%(message)s"
    ]


def test_install_gives_one_handler_writing_the_bare_message(planted, capsys):
    journal.install(planted)
    journal.install(planted)
    assert len(planted.handlers) == 1 and len(bare_stderr_handlers(planted)) == 1
    planted.info("message")
    assert capsys.readouterr().err == "message\n"


def test_install_leaves_other_loggers_alone(planted):
    other = logging.getLogger(f"{planted.name}-other")
    root = list(logging.getLogger().handlers)
    journal.install(planted)
    assert planted.handlers  # positive control
    assert other.handlers == []
    assert logging.getLogger().handlers == root


def test_the_app_sends_each_of_its_journal_loggers_to_stderr(settings, runtime):
    from rcman import audit, unrecognised, workspaces
    from rcman.main import create_app
    from rcman.routers import client_errors

    logs = (audit.log, client_errors.log, unrecognised.log, workspaces.log)
    # An app made earlier in this process installed them with the standard error of
    # its time; they are taken off here, so that this app installs them afresh.
    earlier = {log: list(log.handlers) for log in logs}
    for log in logs:
        log.handlers.clear()
    try:
        create_app(settings, runtime=runtime)
        for log in logs:
            assert len(log.handlers) == 1, log.name
            assert len(bare_stderr_handlers(log)) == 1, log.name
            assert log.getEffectiveLevel() <= logging.INFO, log.name
    finally:
        for log in logs:
            log.handlers[:] = earlier[log]


# --- the journal guide's tables ------------------------------------------------------


def guide_fields(guide: str, heading: str) -> list[str]:
    """The first cell of each row of the table under ``heading`` in a guide, unwrapped
    from its backticks when it is one backticked field name, and marked as not one
    otherwise, so that it matches no name."""
    section = guide.partition(f"\n{heading}\n")[2].split("\n## ")[0]
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    fields = []
    for row in rows[2:]:  # below the header and its rule
        cell = row.split("|")[1].strip()
        name = re.fullmatch(r"`([a-z_]+)`", cell)
        fields.append(name.group(1) if name else f"{cell} (not a backticked name)")
    return fields


def guide_check_fails_on_a_drifted_table(heading: str, expected: set[str]) -> None:
    """Planted twin of a guide check: a table that lists ``expected`` passes, one with a
    row missing, a row too many or a name not in backticks does not."""
    rows = [f"| `{name}` | what it is |" for name in sorted(expected)]
    table = "| Field | What it is |\n|---|---|\n" + "\n".join(rows) + "\n"
    right = f"# Guide\n\n{heading}\n\n{table}\n## Next\n\n| `after` | not this table |\n"
    assert sorted(guide_fields(right, heading)) == sorted(expected)
    missing = right.replace(rows[0] + "\n", "")
    assert missing != right and sorted(guide_fields(missing, heading)) != sorted(expected)
    extra = right.replace(rows[0], rows[0] + "\n| `planted` | what it is |")
    assert extra != right and sorted(guide_fields(extra, heading)) != sorted(expected)
    bare = right.replace(rows[0], rows[0].replace("`", ""))
    assert bare != right and sorted(guide_fields(bare, heading)) != sorted(expected)
    assert guide_fields(right.replace(heading, "## Elsewhere"), heading) == []


def test_the_guide_reader_takes_only_the_table_under_its_heading():
    guide_check_fails_on_a_drifted_table("## Planted", {"one", "two_words"})
