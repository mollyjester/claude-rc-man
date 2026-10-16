"""The helpers of tests/container/ that run on the host as they run in the test container:
what they do with output that is not UTF-8, which a check must never fail on."""

import sys

from tests.container import clone


def test_a_command_that_prints_bytes_that_are_not_utf8_does_not_crash_the_helper():
    found = clone.sh(sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xacb\\n')")
    assert found.stdout == "a�b\n"


def test_the_lines_around_the_first_bytes_that_are_not_utf8_are_shown_and_only_those():
    lines = [f"line {i}" for i in range(40)]
    lines[10] += "�"
    lines[30] = "x" * 500 + "�"
    shown = clone.around_replacements("\n".join(lines), context=1, most=1)
    assert shown == ["line 9", "line 10�", "line 11"]
    both = clone.around_replacements("\n".join(lines))
    assert both[3] == "--" and len(both[5]) == 300  # a long line is cut
    assert clone.around_replacements("clean\ntext\n") == []


def test_a_journal_with_such_bytes_is_reported_and_one_without_is_not(capsys, monkeypatch):
    seen = []

    def fake(*argv, **kw):
        seen.append(argv)
        return type("Done", (), {"stdout": "2026-10-16T10:00:00 host rcman[1]: a�b\n"})()

    monkeypatch.setattr(clone, "sh", fake)
    clone.report_undecodable("clean")
    assert capsys.readouterr().out == "" and seen == []
    clone.report_undecodable("a�b")  # never raises
    out = capsys.readouterr().out
    assert "rcman[1]: a�b" in out and "short-iso" in seen[0]
