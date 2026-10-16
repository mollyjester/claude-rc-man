"""Limits (backend/rcman/limits.py): the values, how they are read and shown, and the names
and properties of the systemd slices that hold them."""

import re

import pytest
from rcman import limits
from rcman.limits import Limits, Refused

from helpers import rules

# --- memory --------------------------------------------------------------------------


def test_memory_is_read_as_mebibytes_from_a_count_and_a_unit():
    big = limits.MIN_MEMORY_MIB + 1
    assert limits.parse_memory(f"{big}M") == big
    assert limits.parse_memory(f"{big}m") == big
    assert limits.parse_memory("2G") == 2 * 1024
    assert limits.parse_memory("2g") == 2 * 1024
    assert limits.parse_memory(f"{limits.MIN_MEMORY_MIB}M") == limits.MIN_MEMORY_MIB


@pytest.mark.parametrize(
    "text", ["", "2", "2K", "2T", "G", "1.5G", "-2G", " 2G", "2G ", "2 G", "2GB"]
)
def test_memory_that_is_not_a_count_and_a_unit_is_refused_with_the_rules_words(text):
    with pytest.raises(Refused) as refused:
        limits.parse_memory(text)
    assert refused.value.field == "memory" and str(refused.value) == rules.MEMORY.sentence


def test_memory_below_the_minimum_is_refused_and_the_minimum_is_not():
    with pytest.raises(Refused):
        limits.parse_memory(f"{limits.MIN_MEMORY_MIB - 1}M")
    with pytest.raises(Refused):
        limits.parse_memory("0G")
    assert limits.parse_memory(f"{limits.MIN_MEMORY_MIB}M") == limits.MIN_MEMORY_MIB


def test_memory_with_too_many_digits_is_refused_before_it_can_overflow_anything():
    with pytest.raises(Refused):
        limits.parse_memory("9" * (rules.MAX_LIMIT_DIGITS + 1) + "G")
    assert limits.parse_memory("9" * rules.MAX_LIMIT_DIGITS + "M") > 0


def test_memory_is_shown_in_the_largest_unit_that_keeps_it_whole():
    assert limits.format_memory(2 * 1024) == "2G"
    assert limits.format_memory(1024 + 1) == f"{1024 + 1}M"
    assert limits.format_memory(limits.MIN_MEMORY_MIB) == f"{limits.MIN_MEMORY_MIB}M"
    for mib in (limits.MIN_MEMORY_MIB, 1500, 1024, 4096, 99999):
        assert limits.parse_memory(limits.format_memory(mib)) == mib


# --- CPU -----------------------------------------------------------------------------


def test_cpu_is_read_as_cores_with_up_to_the_decimals_and_stored_as_percent():
    assert limits.parse_cpu("1") == 100
    assert limits.parse_cpu("2") == 200
    assert limits.parse_cpu("1.5") == 150
    assert limits.parse_cpu("1.25") == 125
    assert limits.parse_cpu("0.5") == 50
    assert limits.parse_cpu("0.10") == limits.MIN_CPU_PERCENT == 10


@pytest.mark.parametrize(
    "text", ["", "x", ".5", "1.", "1.234", "1,5", "-1", "1e1", " 1", "1 ", "1.5c"]
)
def test_cpu_that_is_not_cores_with_the_decimals_is_refused_with_the_rules_words(text):
    with pytest.raises(Refused) as refused:
        limits.parse_cpu(text)
    assert refused.value.field == "cpu" and str(refused.value) == rules.CPU.sentence


def test_cpu_below_the_minimum_is_refused_and_the_minimum_is_not():
    below = f"0.{limits.MIN_CPU_PERCENT - 1:02d}"
    with pytest.raises(Refused):
        limits.parse_cpu(below)
    with pytest.raises(Refused):
        limits.parse_cpu("0")
    assert limits.parse_cpu(f"0.{limits.MIN_CPU_PERCENT:02d}") == limits.MIN_CPU_PERCENT


def test_cpu_is_shown_as_cores_without_a_trailing_zero_and_read_back_whole():
    assert limits.format_cpu(100) == "1"
    assert limits.format_cpu(150) == "1.5"
    assert limits.format_cpu(125) == "1.25"
    assert limits.format_cpu(25) == "0.25"
    assert limits.format_cpu(1000) == "10"
    for percent in (limits.MIN_CPU_PERCENT, 25, 50, 100, 150, 175, 333, 12345):
        assert limits.parse_cpu(limits.format_cpu(percent)) == percent


# --- processes -----------------------------------------------------------------------


def test_processes_are_a_whole_number_and_threads_count():
    assert limits.parse_pids(str(limits.MIN_PIDS)) == limits.MIN_PIDS
    assert limits.parse_pids("1000") == 1000
    assert "thread" in rules.PIDS.sentence


@pytest.mark.parametrize("text", ["", "x", "1.5", "-5", " 100", "100 ", "1e3", "100k"])
def test_processes_that_are_not_a_whole_number_are_refused_with_the_rules_words(text):
    with pytest.raises(Refused) as refused:
        limits.parse_pids(text)
    assert refused.value.field == "pids" and str(refused.value) == rules.PIDS.sentence


def test_processes_below_the_minimum_are_refused_and_the_minimum_is_not():
    with pytest.raises(Refused):
        limits.parse_pids(str(limits.MIN_PIDS - 1))
    with pytest.raises(Refused):
        limits.parse_pids("0")
    assert limits.parse_pids(str(limits.MIN_PIDS)) == limits.MIN_PIDS


# --- the three together ----------------------------------------------------------------


def test_the_minimums_are_the_rules_and_each_rules_sentence_names_its_own():
    assert limits.MIN_MEMORY_MIB == rules.MIN_MEMORY_MIB
    assert limits.MIN_CPU_PERCENT == rules.MIN_CPU_PERCENT
    assert limits.MIN_PIDS == rules.MIN_PIDS
    assert re.search(rf"\b{rules.MIN_MEMORY_MIB}M\b", rules.MEMORY.sentence)
    assert limits.format_cpu(rules.MIN_CPU_PERCENT) in rules.CPU.sentence
    assert re.search(rf"\b{rules.MIN_PIDS}\b", rules.PIDS.sentence)
    for rule in (rules.MEMORY, rules.CPU, rules.PIDS):
        assert str(rules.MAX_LIMIT_DIGITS) in rule.sentence


def test_nothing_sent_means_no_limit_and_a_set_of_three_is_read_whole():
    assert limits.parse(None, None, None) == Limits()
    assert Limits().memory_mib is None and Limits().cpu_percent is None and Limits().pids is None
    assert limits.parse("2G", "1.5", "500") == Limits(2048, 150, 500)
    assert limits.parse("2G", None, None) == Limits(memory_mib=2048)


def test_a_set_with_one_value_wrong_names_that_one():
    with pytest.raises(Refused) as refused:
        limits.parse("2G", "x", "500")
    assert refused.value.field == "cpu"


def test_none_is_the_word_the_command_line_uses_for_no_limit():
    assert limits.parse_option("none", limits.parse_pids) is None
    assert limits.parse_option("NONE", limits.parse_pids) is None
    assert limits.parse_option(str(limits.MIN_PIDS), limits.parse_pids) == limits.MIN_PIDS
    with pytest.raises(Refused):
        limits.parse_option("lots", limits.parse_pids)


def test_limits_are_shown_as_text_that_reads_back_the_same():
    held = Limits(2048, 150, 500)
    assert held.text() == {"memory": "2G", "cpu": "1.5", "pids": "500"}
    assert Limits().text() == {"memory": None, "cpu": None, "pids": None}
    assert limits.parse(*held.text().values()) == held


def test_limits_are_described_in_words_for_the_audit_and_the_command_line():
    assert Limits(2048, 150, 500).describe() == "memory=2G cpu=1.5 pids=500"
    assert Limits(2048).describe() == "memory=2G cpu=none pids=none"
    assert Limits().describe() == "memory=none cpu=none pids=none"


def test_a_limit_set_is_limited_when_any_of_it_is():
    assert not Limits().limited
    assert Limits(memory_mib=limits.MIN_MEMORY_MIB).limited
    assert Limits(cpu_percent=limits.MIN_CPU_PERCENT).limited
    assert Limits(pids=limits.MIN_PIDS).limited


# --- slices and their properties -------------------------------------------------------


def test_a_person_has_a_slice_by_their_id_and_a_workspace_one_beneath_it():
    assert limits.person_slice(7) == "rcman-p7.slice"
    assert limits.workspace_slice(7, 12) == "rcman-p7-w12.slice"
    assert limits.slice_of(7, None) == limits.person_slice(7)
    assert limits.slice_of(7, 12) == limits.workspace_slice(7, 12)


def test_the_dash_puts_the_workspace_slice_under_its_persons():
    """systemd reads a dash in a slice's name as 'below the slice named by the part before
    it'; the person's slice is the workspace's name up to its last dash."""
    child = limits.workspace_slice(7, 12)
    assert child.removesuffix(".slice").rsplit("-", 1)[0] + ".slice" == limits.person_slice(7)


def test_slice_names_are_by_id_and_never_collide():
    names = {limits.person_slice(p) for p in range(1, 30)}
    names |= {limits.workspace_slice(p, w) for p in range(1, 30) for w in range(1, 30)}
    assert len(names) == 29 + 29 * 29
    # a person's id 1 with workspace 11 is not person 11's slice or person 1's workspace 1
    assert limits.workspace_slice(1, 11) not in {limits.workspace_slice(11, 1)}
    assert limits.workspace_slice(1, 1) != limits.person_slice(11)


def test_every_slice_name_is_one_systemd_reads_as_a_slice_below_rcmans_own():
    for name in (limits.person_slice(3), limits.workspace_slice(3, 4)):
        assert re.fullmatch(r"rcman-p[0-9]+(-w[0-9]+)?\.slice", name)
        assert name.startswith(limits.ROOT_SLICE.removesuffix(".slice") + "-p")


def test_a_limited_set_is_applied_as_all_four_properties_with_swap_off_for_memory():
    held = Limits(2048, 150, 500)
    assert held.properties() == [
        f"MemoryMax={2048 * 1024 * 1024}",
        "MemorySwapMax=0",
        "CPUQuota=150%",
        "TasksMax=500",
    ]


def test_an_unlimited_set_is_applied_as_all_four_properties_too_each_reset():
    assert Limits().properties() == [
        "MemoryMax=infinity",
        "MemorySwapMax=infinity",
        "CPUQuota=",  # the empty value is what resets the CPU
        "TasksMax=infinity",
    ]


@pytest.mark.parametrize(
    "held",
    [Limits(), Limits(2048), Limits(cpu_percent=50), Limits(pids=100), Limits(2048, 150, 500)],
)
def test_the_four_properties_are_always_the_same_four_in_the_same_order(held):
    names = [p.split("=", 1)[0] for p in held.properties()]
    assert names == ["MemoryMax", "MemorySwapMax", "CPUQuota", "TasksMax"]
    assert names == list(limits.PROPERTIES)


def test_memory_is_real_memory_only_while_swap_is_off_with_it():
    assert "MemorySwapMax=0" in Limits(2048).properties()
    assert "MemorySwapMax=0" not in Limits(cpu_percent=50).properties()
    assert "MemorySwapMax=infinity" in Limits(cpu_percent=50).properties()


# --- what is in use --------------------------------------------------------------------

SHOW = "ActiveState=active\nMemoryCurrent=1073741824\nMemorySwapCurrent=0\nTasksCurrent=42\n"
NOT_ACTIVE = (
    "ActiveState=inactive\nMemoryCurrent=[not set]\nMemorySwapCurrent=[not set]\n"
    "TasksCurrent=[not set]\n"
)


def test_usage_is_read_from_what_systemctl_shows():
    assert limits.read_usage(SHOW) == limits.Usage(memory_bytes=1073741824, tasks=42)


def test_the_memory_in_use_is_memory_and_swap_together():
    shown = SHOW.replace("MemorySwapCurrent=0", "MemorySwapCurrent=1000")
    assert limits.read_usage(shown).memory_bytes == 1073741824 + 1000
    # swap not counted by the kernel is no swap in use
    shown = SHOW.replace("MemorySwapCurrent=0", "MemorySwapCurrent=[not set]")
    assert limits.read_usage(shown).memory_bytes == 1073741824
    assert limits.read_usage(SHOW.replace("MemorySwapCurrent=0\n", "")).memory_bytes == 1073741824


def test_a_slice_that_is_not_active_shows_not_set_and_uses_nothing():
    assert limits.read_usage(NOT_ACTIVE) == limits.Usage(0, 0)
    assert limits.read_usage(NOT_ACTIVE).memory_known and limits.read_usage(NOT_ACTIVE).tasks_known
    failed = NOT_ACTIVE.replace("inactive", "failed")
    assert limits.read_usage(failed) == limits.Usage(0, 0)


def test_an_active_slice_whose_use_is_not_counted_is_unknown_and_not_zero():
    """Accounting off: the kernel shows nothing, which is not none in use."""
    memory_off = SHOW.replace("MemoryCurrent=1073741824", "MemoryCurrent=[not set]")
    used = limits.read_usage(memory_off)
    assert not used.memory_known and used.tasks_known and used.tasks == 42
    tasks_off = SHOW.replace("TasksCurrent=42", "TasksCurrent=[not set]")
    used = limits.read_usage(tasks_off)
    assert used.memory_known and not used.tasks_known
    assert limits.read_usage(SHOW).memory_known and limits.read_usage(SHOW).tasks_known  # control


@pytest.mark.parametrize(
    "said",
    [
        "",
        "ActiveState=active\nMemoryCurrent=1\n",
        "ActiveState=active\nTasksCurrent=1\n",
        "MemoryCurrent=1\nTasksCurrent=1\n",  # no state: not known to be inactive either
        "x\n",
        "ActiveState=active\nMemoryCurrent=abc\nTasksCurrent=1\n",
    ],
)
def test_usage_that_cannot_be_read_is_an_error_and_not_zero(said):
    with pytest.raises(ValueError):
        limits.read_usage(said)


# --- the caps a slice has now -----------------------------------------------------------


def test_the_caps_are_what_systemctl_shows_and_infinity_or_not_set_is_none():
    assert limits.read_caps("MemoryMax=2147483648\nTasksMax=500\n") == limits.Caps(2147483648, 500)
    assert limits.read_caps("MemoryMax=infinity\nTasksMax=infinity\n") == limits.Caps(None, None)
    assert limits.read_caps("MemoryMax=[not set]\nTasksMax=[not set]\n") == limits.Caps()
    assert limits.Caps() == limits.Caps(None, None)  # no cap is the default


def test_systemds_largest_number_is_no_cap_either():
    top = 2**64 - 1
    assert limits.read_caps(f"MemoryMax={top}\nTasksMax={top}\n") == limits.Caps()


@pytest.mark.parametrize(
    "said", ["", "MemoryMax=1\n", "TasksMax=1\n", "MemoryMax=abc\nTasksMax=1\n", "x\n"]
)
def test_caps_that_cannot_be_read_are_an_error_and_not_none(said):
    with pytest.raises(ValueError):
        limits.read_caps(said)


def test_a_limit_is_lower_than_a_cap_when_less_and_none_is_the_highest():
    assert limits.lower_than(5, 6) and not limits.lower_than(6, 6) and not limits.lower_than(7, 6)
    assert limits.lower_than(5, None)  # no cap: any cap is lower


def test_a_cap_is_below_use_when_it_is_less_than_what_is_in_use():
    use = limits.Usage(memory_bytes=1000 * 1024 * 1024, tasks=300)
    assert limits.memory_below(999, use) and not limits.memory_below(1000, use)
    assert not limits.memory_below(1001, use)
    assert limits.pids_below(299, use) and not limits.pids_below(300, use)


def test_use_is_named_in_a_unit_a_cap_can_be_typed_in_and_never_rounded_down():
    # one byte over a whole mebibyte is named as the next one up, so that a cap typed from the
    # name is never below what is used
    assert limits.Usage(1024 * 1024 + 1, 1).memory_text() == "2M"
    assert limits.Usage(2048 * 1024 * 1024, 1).memory_text() == "2G"
    assert limits.Usage(0, 0).memory_text() == "0M"
