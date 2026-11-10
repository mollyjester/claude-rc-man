"""The exec channel: commands run one at a time through one long-lived shell.

Here the shell is the host's own ``sh``, with the host's tools, as the host runtime
runs it; no test runs the channel through ``podman --remote exec``.
"""

import hashlib
import shlex
import shutil
import threading
import time

import pytest
from rcman import channel as ch

from tests.hang_guard import HANG_GUARD
from tests.until import until

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


def test_a_command_answers_its_exit_code_and_its_two_streams_apart_intact(channel, tmp_path):
    sh = shutil.which("sh")
    assert channel.run([sh, "-c", "echo out; echo err >&2; exit 7"]) == ch.Result(
        7, b"out\n", b"err\n"
    )
    data = bytes(range(256)) * 4096 + b"\n\0end"  # any bytes, of any size
    source = tmp_path / "data"
    source.write_bytes(data)
    found = channel.run([shutil.which("cat"), str(source)])
    assert hashlib.sha256(found.out).digest() == hashlib.sha256(data).digest()


def test_arguments_reach_the_program_as_they_are(channel, tmp_path):
    planted = tmp_path / "planted"
    awkward = ["it's", 'a "quote"', "$(touch " + str(planted) + ")", "`id`", "a; b", "  ", "*", ""]
    found = channel.run([shutil.which("printf"), r"[%s]\0", *awkward])
    assert found.out.split(b"\0")[:-1] == [f"[{a}]".encode() for a in awkward]
    assert not planted.exists()  # nothing in an argument was run
    channel.run([shutil.which("touch"), str(planted)])
    assert planted.exists()  # positive control: a command that makes it does


def test_closing_ends_the_shell_and_removes_its_files(run_dir):
    c = ch.Channel([SH, "-s"], tools, str(run_dir))
    c.run([shutil.which("echo"), "x"])
    assert list(run_dir.iterdir())  # positive control: the channel's files are there
    process = c.process
    c.close()
    assert process.poll() is not None
    assert list(run_dir.iterdir()) == []


def p95_of_a_hundred(channel, true: str) -> float:
    times = []
    for _ in range(100):
        started = time.perf_counter()
        channel.run([true])
        times.append(time.perf_counter() - started)
    times.sort()
    return times[94]


# How many batches of a hundred requests the latency benchmark takes before it reports the
# bound as broken: noise on a busy host passes in one of them, a slow channel in none.
LATENCY_BATCHES = 3


def batches_over_the_bound(measure) -> list[float]:
    """The 95th percentiles ``measure()`` gave, each over the bound, until one was under it,
    which ends them; the list is empty then."""
    over = []
    for _ in range(LATENCY_BATCHES):
        p95 = measure()
        if p95 < ch.LATENCY_P95_SECONDS:
            return []
        over.append(p95)
    return over


@pytest.mark.benchmark
def test_the_channels_latency_on_the_host(channel, record_property):
    """Reports the 95th percentile of a command through the channel, a few milliseconds where
    a new exec costs hundreds, against ch.LATENCY_P95_SECONDS.
    It measures and reports, and decides nothing (make benchmark). On a busy host a few
    scheduler stalls can push one batch's 95th percentile past the bound, so a batch is taken
    again; a channel that is slow is over the bound in every batch."""
    true = shutil.which("true")
    channel.run([true])
    batches: list[float] = []

    def measured() -> float:
        batches.append(p95_of_a_hundred(channel, true))
        return batches[-1]

    over = batches_over_the_bound(measured)
    assert batches  # it measured
    record_property("p95_seconds", batches)
    record_property("bound_seconds", ch.LATENCY_P95_SECONDS)
    verdict = "over the bound in every batch" if over else "within the bound"
    figures = ", ".join(f"{p95 * 1000:.2f} ms" for p95 in batches)
    print(
        f"the channel's 95th percentile on the host: {figures}, against "
        f"{ch.LATENCY_P95_SECONDS * 1000:.0f} ms: {verdict}"
    )


def test_a_channel_running_a_request_is_not_idle(channel, tmp_path):
    """A request on a channel unused for long is no idleness to close it for: asked while
    the request's program says it runs, and holds until the test releases it. Once that
    request is done and the channel idle longer than asked, it is marked closed, and a request
    then is refused as sent nowhere."""
    running, release = tmp_path / "running", tmp_path / "release"
    script = (
        f"touch {shlex.quote(str(running))}; "
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done"
    )
    channel._last -= 2 * HANG_GUARD  # unused for long before the request
    asking = threading.Thread(target=channel.run, args=([SH, "-c", script],))
    asking.start()
    try:
        until(running.exists)  # the request is in flight
        assert not channel.mark_closed_if_idle(HANG_GUARD)
    finally:
        release.touch()
        asking.join(HANG_GUARD)
    assert channel.run([shutil.which("true")]).code == 0  # it was not marked
    channel._last -= 2 * HANG_GUARD
    assert channel.mark_closed_if_idle(HANG_GUARD)
    with pytest.raises(ch.ChannelClosed):
        channel.run([shutil.which("true")])
