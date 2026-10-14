"""The exec channel: commands run one at a time through one long-lived shell.

Here the shell is the host's own ``sh``, with the host's tools, as the host runtime
runs it; tests/container/test_runtime_in_podman.py runs the same protocol through
``podman --remote exec`` and busybox in the podman host.
"""

import hashlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from rcman import channel as ch

SH = shutil.which("sh")


def tools(name: str) -> list[str]:
    return [shutil.which(name)]


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    return folder


@pytest.fixture
def channel(run_dir):
    c = ch.Channel([SH, "-s"], tools, str(run_dir))
    yield c
    c.close()


def test_a_command_answers_its_exit_code_and_its_two_streams_apart(channel):
    sh = shutil.which("sh")
    assert channel.run([sh, "-c", "echo out; echo err >&2; exit 7"]) == ch.Result(
        7, b"out\n", b"err\n"
    )
    assert channel.run([shutil.which("true")]).code == 0
    missing = channel.run(["/nonexistent/program"])
    assert missing.code == 127 and missing.out == b""


def test_arguments_reach_the_program_as_they_are(channel, tmp_path):
    planted = tmp_path / "planted"
    awkward = ["it's", 'a "quote"', "$(touch " + str(planted) + ")", "`id`", "a; b", "  ", "*", ""]
    found = channel.run([shutil.which("printf"), r"[%s]\0", *awkward])
    assert found.out.split(b"\0")[:-1] == [f"[{a}]".encode() for a in awkward]
    assert not planted.exists()  # nothing in an argument was run
    channel.run([shutil.which("touch"), str(planted)])
    assert planted.exists()  # positive control: a command that makes it does


def test_a_command_reads_nothing(channel):
    started = time.monotonic()
    assert channel.run([shutil.which("cat")]) == ch.Result(0, b"", b"")
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("bad", ["two\nlines", "nul\0char"])
def test_an_argument_that_is_not_one_line_is_refused_before_anything_runs(channel, bad, tmp_path):
    planted = tmp_path / "planted"
    with pytest.raises(ValueError):
        channel.run([shutil.which("touch"), str(planted), bad])
    assert not planted.exists()
    assert channel.run([shutil.which("true")]).code == 0  # the channel is still usable


def test_output_of_any_bytes_and_size_comes_through_intact(channel, tmp_path):
    data = bytes(range(256)) * 4096 + b"\n\0end"
    source = tmp_path / "data"
    source.write_bytes(data)
    found = channel.run([shutil.which("cat"), str(source)])
    assert hashlib.sha256(found.out).digest() == hashlib.sha256(data).digest()


def test_requests_after_one_another_stay_in_step(channel):
    for i in range(50):
        assert channel.run([shutil.which("echo"), str(i)]).out == f"{i}\n".encode()


def test_requests_from_threads_are_answered_each_its_own(channel):
    found = {}

    def ask(i):
        found[i] = channel.run([shutil.which("echo"), str(i)]).out

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert found == {i: f"{i}\n".encode() for i in range(20)}


def test_a_channel_that_died_while_idle_is_opened_again_for_the_next_request(channel):
    channel.run([shutil.which("true")])
    first = channel.process.pid
    channel.process.kill()
    channel.process.wait()
    assert channel.run([shutil.which("echo"), "again"]).out == b"again\n"
    assert channel.process.pid != first


def test_a_request_the_channel_died_running_is_not_run_again(channel, tmp_path):
    count = tmp_path / "count"
    script = f"echo once >> {count}; kill -9 $PPID"
    with pytest.raises(ch.ChannelBroken):
        channel.run([SH, "-c", script])
    assert count.read_text() == "once\n"
    assert channel.run([shutil.which("true")]).code == 0  # the next one opens it again
    assert count.read_text() == "once\n"


def test_a_request_that_outlasts_its_timeout_closes_the_channel(channel):
    started = time.monotonic()
    with pytest.raises(ch.ChannelTimeout):
        channel.run([shutil.which("sleep"), "30"], timeout=0.5)
    assert time.monotonic() - started < 10
    assert channel.run([shutil.which("echo"), "after"]).out == b"after\n"


def flaky_opener(tmp_path: Path, failures: int, error: str) -> list[str]:
    """A channel command that fails to open ``failures`` times, saying ``error`` on stderr
    as podman does, and then runs a shell; how often it was started is counted."""
    counter = tmp_path / "opened"
    counter.write_text("")
    script = tmp_path / "opener"
    script.write_text(
        f"#!{SH}\n"
        f'echo x >> "{counter}"\n'
        f'if [ "$(wc -l < "{counter}")" -le {failures} ]; then echo "{error}" >&2; exit 126; fi\n'
        f"exec {SH} -s\n"
    )
    script.chmod(0o755)
    return [str(script)]


CONMON = 'Error: container create failed (no logs from conmon): conmon bytes "": readObjectStart'


def opened(tmp_path) -> int:
    return len((tmp_path / "opened").read_text().splitlines())


def test_opening_is_tried_once_more_on_conmons_error(tmp_path, run_dir):
    c = ch.Channel(flaky_opener(tmp_path, 1, CONMON), tools, str(run_dir))
    try:
        assert c.run([shutil.which("echo"), "ok"]).out == b"ok\n"
        assert opened(tmp_path) == 2
    finally:
        c.close()


def test_opening_is_not_tried_a_third_time(tmp_path, run_dir):
    c = ch.Channel(flaky_opener(tmp_path, 2, CONMON), tools, str(run_dir))
    try:
        with pytest.raises(ch.ChannelBroken):
            c.run([shutil.which("true")])
        assert opened(tmp_path) == 2
    finally:
        c.close()


def test_opening_is_not_tried_again_on_any_other_error(tmp_path, run_dir):
    c = ch.Channel(flaky_opener(tmp_path, 1, "Error: no such container"), tools, str(run_dir))
    try:
        with pytest.raises(ch.ChannelBroken) as raised:
            c.run([shutil.which("true")])
        assert "no such container" in str(raised.value)
        assert opened(tmp_path) == 1
    finally:
        c.close()


def test_closing_ends_the_shell_and_removes_its_files(run_dir):
    c = ch.Channel([SH, "-s"], tools, str(run_dir))
    c.run([shutil.which("echo"), "x"])
    assert list(run_dir.iterdir())  # positive control: the channel's files are there
    process = c.process
    c.close()
    assert process.poll() is not None
    assert list(run_dir.iterdir()) == []


def test_an_idle_channel_says_how_long_it_has_been_idle(channel):
    channel.run([shutil.which("true")])
    time.sleep(0.2)
    assert channel.idle_seconds() >= 0.2
    channel.run([shutil.which("true")])
    assert channel.idle_seconds() < 0.2


def test_the_channels_latency_on_the_host(channel):
    """A command through the channel costs a few milliseconds, not the hundreds a new exec
    costs; the podman host pins the same through podman (test_runtime_in_podman.py)."""
    true = shutil.which("true")
    channel.run([true])
    times = []
    for _ in range(100):
        started = time.perf_counter()
        channel.run([true])
        times.append(time.perf_counter() - started)
    times.sort()
    assert times[94] < ch.LATENCY_P95_SECONDS, times[94]


def test_closing_does_not_wait_for_a_request_in_flight(run_dir):
    c = ch.Channel([SH, "-s"], tools, str(run_dir))
    failed = []

    def ask():
        try:
            c.run([shutil.which("sleep"), "30"])
        except ch.ChannelBroken as error:
            failed.append(error)

    asking = threading.Thread(target=ask)
    asking.start()
    time.sleep(0.5)
    started = time.monotonic()
    c.close()
    assert time.monotonic() - started < 5
    asking.join(10)
    assert not asking.is_alive() and failed  # the request fails as broken


def test_a_channel_running_a_request_is_not_idle(channel):
    """A request on a channel unused for long is no idleness to close it for."""
    channel._last -= 1000  # unused for long before the request
    asking = threading.Thread(target=channel.run, args=([shutil.which("sleep"), "2"],))
    asking.start()
    time.sleep(0.5)
    try:
        assert channel.idle_seconds() < 100
    finally:
        asking.join(10)
