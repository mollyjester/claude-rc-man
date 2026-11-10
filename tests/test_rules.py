"""The rules values are refused by: each a regular expression and the sentence that
explains it (helpers/rules.py)."""

import functools
import re
import subprocess
import typing
from typing import Annotated

import pytest
from pydantic import AfterValidator, Field, TypeAdapter, ValidationError

from helpers import rules
from helpers.rules import Rule

DIGITS = Rule(re.compile(r"[0-9]+"), "Use digits only.")


def accepts(anchored: str, text: str) -> bool:
    """Whether pydantic, the validator the anchored form is for, accepts ``text``."""
    try:
        TypeAdapter(Annotated[str, Field(pattern=anchored)]).validate_python(text)
    except ValidationError:
        return False
    return True


def test_a_rule_matches_the_whole_text_and_its_anchored_pattern_alike():
    """The anchored form is what a validator that searches, such as pydantic's, is given."""
    assert DIGITS.fullmatch("123") and not DIGITS.fullmatch("12a")
    assert DIGITS.anchored == "^(?:[0-9]+)$"
    for text in ("123", "12a", "a12", "", "1\n", "\n1"):
        assert accepts(DIGITS.anchored, text) is bool(DIGITS.fullmatch(text)), text


def test_or_empty_also_accepts_nothing_and_keeps_the_sentence():
    optional = DIGITS.or_empty()
    assert optional.fullmatch("") and optional.fullmatch("42")
    assert not optional.fullmatch("4x")
    assert optional.sentence == DIGITS.sentence
    assert accepts(optional.anchored, "") and not accepts(optional.anchored, "x")


# --- the rules ----------------------------------------------------------------------


def held_rules(module) -> dict[str, Rule]:
    """The rules a module holds, by name, found among its own attributes."""
    return {name: value for name, value in vars(module).items() if isinstance(value, Rule)}


def helpers_rules() -> dict[str, Rule]:
    """Every rule there is, by name."""
    found = held_rules(rules)
    # Non-vacuity for every check that runs over them: the rules known to exist.
    names = {
        "PREFIX",
        "EMAIL",
        "WORKSPACE_NAME",
        "AUTHOR_NAME",
        "VERSION",
        "COMMIT_MESSAGE",
        "BRANCH_NAME",
        "GITHUB_TOKEN",
        "GITHUB_URL",
        "MEMORY",
        "CPU",
        "PIDS",
        "FAILURE_REASON",
    }
    assert names <= set(found), names - set(found)
    return found


def test_no_helpers_rule_sentence_is_in_a_librarys_words():
    from tests.test_problems import library_text

    for name, rule in helpers_rules().items():
        assert not library_text(rule.sentence), name


# A short value each rule accepts, to put every code point into; the first one a
# rule accepts is its example.
EXAMPLES = ("a", "a@b.c", "1.2.3", "https://github.com/o/r", "1G", "1.5", "10")
CODE_POINTS = [chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]
# Examples too long to put a code point in place of each of their characters in the
# time a test should take, each with the places used instead: where an address's
# owner and repository name begin, and inside the owner; the ends are every
# example's.
LIMITED_CONTEXTS = {
    "https://github.com/o/r": [
        "https://github.com/{}/r",
        "https://github.com/o{}/r",
        "https://github.com/o/{}",
    ],
}


def _contexts(example: str) -> list[str]:
    """Templates putting a code point alone, before, after and in place of each
    character of the example, each once; for an example in LIMITED_CONTEXTS, in its
    places instead of in place of each character."""
    found = ["{}", "{}" + example, example + "{}"]
    if example in LIMITED_CONTEXTS:
        return found + LIMITED_CONTEXTS[example]
    found += [example[:i] + "{}" + example[i + 1 :] for i in range(len(example))]
    return list(dict.fromkeys(found))


def disagreements(*rules: Rule) -> list[str]:
    """Where the backend's validator for a rule, problems.ruled(rule), and the rule's
    own Python expression disagree, over every code point in several contexts.

    Rules with the same example share its contexts, so each list of values is
    built once and checked against each of them."""
    from pydantic import WrapValidator
    from rcman.problems import ruled

    by_example: dict[str, list[Rule]] = {}
    for rule in rules:
        example = next((e for e in EXAMPLES if rule.fullmatch(e)), None)
        assert example is not None, rule.pattern
        by_example.setdefault(example, []).append(rule)

    # The backend's own type for the rule, less the validator that only rewords a
    # refusal (tests/test_problems.py checks the words): run in Python for each of
    # a million values, it would make this test slower by minutes.
    def checked(rule: Rule):
        base, *metadata = typing.get_args(ruled(rule))
        kept = [m for m in metadata if not isinstance(m, WrapValidator)]
        assert len(kept) == len(metadata) - 1, metadata
        return Annotated[(base, *kept)]

    # Each value the backend refuses falls through to the second branch and
    # becomes None, so no error is built for it.
    refusal = Annotated[str, AfterValidator(lambda value: None)]
    adapters = {
        rule: TypeAdapter(
            list[Annotated[checked(rule) | refusal, Field(union_mode="left_to_right")]]
        )
        for rule in rules
    }
    found = []
    for example, group in by_example.items():
        for context in _contexts(example):
            before, after = context.split("{}")  # concatenating is quicker than format
            values = [before + c + after for c in CODE_POINTS]
            for rule in group:
                result = adapters[rule].validate_python(values)
                refused = {i for i, value in enumerate(result) if value is None}
                match = rule.regex.fullmatch
                mine = {i for i, value in enumerate(values) if not match(value)}
                found += [
                    f"{rule.pattern[:20]!r} {context!r} U+{ord(CODE_POINTS[i]):04X}"
                    for i in sorted(refused ^ mine)
                ]
    return found


@pytest.mark.parametrize("rule", list(helpers_rules().values()), ids=list(helpers_rules()))
def test_the_backend_and_the_helpers_read_the_rule_alike(rule):
    assert disagreements(rule) == []


def test_a_rule_the_two_engines_read_differently_is_found():
    """Planted twin: Python's \\s holds the separators U+001C to U+001F; pydantic's does not."""
    found = disagreements(Rule(re.compile(r"a\s?"), "Say a."))
    assert "'a\\\\s?' 'a{}' U+001C" in found and "'a\\\\s?' 'a{}' U+0020" not in found


# --- a workspace's or person's limits ------------------------------------------------

LIMIT_RULES = (rules.MEMORY, rules.CPU, rules.PIDS)


def sentence_numbers(sentence: str) -> set[str]:
    return set(re.findall(r"[0-9]+(?:\.[0-9]+)?", sentence))


def test_a_limit_rules_sentence_has_no_number_but_the_constants_it_is_made_from():
    allowed = {
        str(rules.MAX_LIMIT_DIGITS),
        str(rules.MAX_CPU_DECIMALS),
        str(rules.MIN_MEMORY_MIB),
        str(rules.MIN_PIDS),
        f"{rules.MIN_CPU_PERCENT / 100:g}",
    }
    for rule in LIMIT_RULES:
        assert sentence_numbers(rule.sentence)  # non-vacuity: each names one of its constants
        assert sentence_numbers(rule.sentence) <= allowed, rule.sentence


def test_a_sentence_with_a_number_of_its_own_is_found():
    """Planted twin: the check above fires for a number that is not one of the constants."""
    planted = Rule(re.compile("[0-9]+"), "Use up to 77 digits.")
    assert sentence_numbers(planted.sentence) == {"77"}
    assert not sentence_numbers(planted.sentence) <= {str(rules.MAX_LIMIT_DIGITS)}


# --- a commit author's name ----------------------------------------------------------

AUTHOR_NAMES_ACCEPTED = [
    "Bob B",
    "Сергей Лихарев",
    "O'Brien",
    "Bob Jr.",
    "(Bob)",
    "x",
    "x" * rules.MAX_AUTHOR_NAME_LENGTH,
    f"a{chr(0x200C)}b",  # ZERO WIDTH NON-JOINER, see ALLOWED_FORMAT
    f"a{chr(0x200D)}b",  # ZERO WIDTH JOINER
]

# Format characters (Unicode category Cf) a name may hold, and why: Persian and
# Indic names are written with them.
ALLOWED_FORMAT = {0x200C: "ZERO WIDTH NON-JOINER", 0x200D: "ZERO WIDTH JOINER"}


def _hangul_fillers() -> tuple[str, ...]:
    """The Hangul fillers: letters by category, but they show as nothing."""
    import sys
    import unicodedata

    return tuple(
        chr(c)
        for c in range(sys.maxunicode + 1)
        if {"HANGUL", "FILLER"} <= set(unicodedata.name(chr(c), "").split())
    )


def recorded_by_git(tmp_path, name: str) -> str:
    """The author name real git records for ``name``."""
    proc = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": "bob@example.com",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rpartition(" <bob@example.com>")[0]


def test_git_records_every_accepted_name_as_given(tmp_path):
    assert recorded_by_git(tmp_path, ";Bob;") == "Bob"  # positive control: git does change some
    for name in AUTHOR_NAMES_ACCEPTED:
        assert rules.AUTHOR_NAME.fullmatch(name), name
        assert recorded_by_git(tmp_path, name) == name


def test_names_git_would_trim_are_refused(tmp_path):
    """The printable characters git strips from the ends of a name are refused there."""
    printable = [chr(c) for c in range(0x21, 0x7F)]
    trimmed = [c for c in printable if recorded_by_git(tmp_path, f"{c}Bob{c}") == "Bob"]
    assert ";" in trimmed and "." not in trimmed  # positive control, and git keeps a dot
    for c in printable:
        refused = not rules.AUTHOR_NAME.fullmatch(f"{c}Bob") and not rules.AUTHOR_NAME.fullmatch(
            f"Bob{c}"
        )
        assert refused is (c in trimmed), repr(c)


# --- why a Claude Code version is marked as failed --------------------------------------------


def unseen_characters() -> list[str]:
    """Every character no reason may hold, from the Unicode database: every control, the line
    and paragraph separators, every format character but the joiners, and the Hangul
    fillers."""
    return list(_unseen_characters())


@functools.cache
def _unseen_characters() -> tuple[str, ...]:
    """``unseen_characters``, from one scan of the Unicode database per test process."""
    import sys
    import unicodedata

    return (
        tuple(
            chr(c)
            for c in range(sys.maxunicode + 1)
            if not 0xD800 <= c <= 0xDFFF
            and (
                unicodedata.category(chr(c)) in ("Cc", "Zl", "Zp")
                or (unicodedata.category(chr(c)) == "Cf" and c not in ALLOWED_FORMAT)
            )
        )
        + _hangul_fillers()
    )


def lets_through(rule: Rule) -> list[str]:
    """The unseen characters ``rule`` takes alone or between letters."""
    return [
        f"U+{ord(c):04X}"
        for c in unseen_characters()
        if rule.fullmatch(f"a{c}b") or rule.fullmatch(c)
    ]


def test_a_reason_refuses_every_line_break_and_every_unseen_character():
    found = unseen_characters()
    for known in ("\n", "\r", "\t", "\x00", "\x85", chr(0x2028), chr(0x2029), chr(0x200B),
                  chr(0x202E), chr(0xFEFF), chr(0xE0020), chr(0x3164)):  # fmt: skip
        assert known in found, f"U+{ord(known):04X}"  # positive control: each kind is there
    assert lets_through(rules.FAILURE_REASON) == []


def test_the_reason_check_finds_a_rule_that_lets_a_line_break_through():
    """Planted twin: a rule that takes any character is found letting the unseen through."""
    planted = Rule(re.compile(r"[\s\S]{1,200}"), "Anything.")
    assert {"U+000A", "U+2028", "U+200B", "U+3164"} <= set(lets_through(planted))
