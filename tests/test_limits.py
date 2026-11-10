"""Limits (backend/rcman/limits.py): the values, how they are read and shown, and the names
of the systemd slices that hold them, and a cap below what is in use."""

from rcman import limits
from rcman.limits import Limits

from helpers import rules

# --- the values ------------------------------------------------------------------------


def test_each_limit_is_read_in_its_unit_and_shown_so_that_it_reads_back_the_same():
    """Memory as mebibytes from a count and a unit, shown in the largest unit that keeps it
    whole; CPU as cores with up to two decimals, stored as percent; processes a whole number,
    threads counted."""
    assert limits.parse_memory("2G") == 2 * 1024
    assert limits.parse_memory(f"{limits.MIN_MEMORY_MIB + 1}m") == limits.MIN_MEMORY_MIB + 1
    assert limits.format_memory(2 * 1024) == "2G"
    assert limits.format_memory(1024 + 1) == f"{1024 + 1}M"
    assert limits.parse_cpu("1.5") == 150 and limits.parse_cpu("0.10") == limits.MIN_CPU_PERCENT
    assert limits.format_cpu(150) == "1.5" and limits.format_cpu(100) == "1"
    assert limits.parse_pids("1000") == 1000
    assert "thread" in rules.PIDS.sentence
    for mib in (limits.MIN_MEMORY_MIB, 1500, 4096):
        assert limits.parse_memory(limits.format_memory(mib)) == mib
    for percent in (limits.MIN_CPU_PERCENT, 125, 150):
        assert limits.parse_cpu(limits.format_cpu(percent)) == percent


def test_a_set_of_three_is_read_whole_shown_as_text_and_described_and_nothing_is_no_limit():
    held = Limits(2048, 150, 500)
    assert limits.parse("2G", "1.5", "500") == held and held.limited
    assert limits.parse(None, None, None) == Limits() and not Limits().limited
    assert held.text() == {"memory": "2G", "cpu": "1.5", "pids": "500"}
    assert limits.parse(*held.text().values()) == held
    assert Limits(2048).describe() == "memory=2G cpu=none pids=none"
    assert limits.parse_option("none", limits.parse_pids) is None  # the command line's word


# --- slices ----------------------------------------------------------------------------


def test_a_person_has_a_slice_by_their_id_and_a_workspace_one_beneath_it():
    """systemd reads a dash in a slice's name as 'below the slice named by the part before
    it'; the person's slice is the workspace's name up to its last dash."""
    assert limits.person_slice(7) == "rcman-p7.slice"
    assert limits.workspace_slice(7, 12) == "rcman-p7-w12.slice"
    assert limits.slice_of(7, None) == limits.person_slice(7)
    assert limits.slice_of(7, 12) == limits.workspace_slice(7, 12)
    child = limits.workspace_slice(7, 12)
    assert child.removesuffix(".slice").rsplit("-", 1)[0] + ".slice" == limits.person_slice(7)


# --- what is in use --------------------------------------------------------------------


def test_a_cap_below_what_is_in_use_is_told_and_use_is_named_in_a_unit_a_cap_can_be_typed_in():
    use = limits.Usage(memory_bytes=1000 * 1024 * 1024, tasks=300)
    assert limits.lower_than(5, 6) and not limits.lower_than(6, 6)
    assert limits.lower_than(5, None)  # no cap: any cap is lower
    assert limits.memory_below(999, use) and not limits.memory_below(1000, use)
    assert limits.pids_below(299, use) and not limits.pids_below(300, use)
    # one byte over a whole mebibyte is named as the next one up, so that a cap typed from the
    # name is never below what is used
    assert limits.Usage(1024 * 1024 + 1, 1).memory_text() == "2M"
