"""The helpers of tests/container/ that run on the host as they run in the suites' containers:
what they do with output that is not UTF-8, which a check must never fail on."""

import sys

from tests.container import clone


def test_a_command_that_prints_bytes_that_are_not_utf8_does_not_crash_the_helper():
    found = clone.sh(sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xacb\\n')")
    assert found.stdout == "a�b\n"
