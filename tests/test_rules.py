"""A rule the helpers apply: a regular expression and the sentence that explains it."""

import re
import typing
from typing import Annotated

import pytest
from pydantic import AfterValidator, Field, TypeAdapter, ValidationError

from helpers.rules import Rule

DIGITS = Rule(re.compile(r"[0-9]+"), "Use digits only.")


def test_a_rule_matches_the_whole_text_only():
    assert DIGITS.fullmatch("123")
    assert not DIGITS.fullmatch("12a")
    assert not DIGITS.fullmatch("")
    assert DIGITS.pattern == "[0-9]+"


def accepts(anchored: str, text: str) -> bool:
    """Whether pydantic, the validator the anchored form is for, accepts ``text``."""
    try:
        TypeAdapter(Annotated[str, Field(pattern=anchored)]).validate_python(text)
    except ValidationError:
        return False
    return True


def test_the_anchored_pattern_matches_what_the_rule_matches():
    """The anchored form is what a validator that searches, such as pydantic's, is given."""
    assert DIGITS.anchored == "^(?:[0-9]+)$"
    for text in ("123", "12a", "a12", "", "1\n", "\n1"):
        assert accepts(DIGITS.anchored, text) is bool(DIGITS.fullmatch(text)), text


def test_an_alternation_stays_anchored_as_a_whole():
    either = Rule(re.compile(r"a|b"), "Say a or b.")
    assert accepts(either.anchored, "a")
    assert not accepts(either.anchored, "ab")
    assert not accepts(either.anchored, "xa")


def test_or_empty_also_accepts_nothing_and_keeps_the_sentence():
    optional = DIGITS.or_empty()
    assert optional.fullmatch("") and optional.fullmatch("42")
    assert not optional.fullmatch("4x")
    assert optional.sentence == DIGITS.sentence
    assert accepts(optional.anchored, "") and not accepts(optional.anchored, "x")


def test_a_rule_needs_a_sentence():
    with pytest.raises(ValueError):
        Rule(re.compile("x"), "")
    with pytest.raises(ValueError):
        Rule(re.compile("x"), "   ")


def test_a_rule_cannot_be_changed():
    with pytest.raises(AttributeError):
        DIGITS.sentence = "Anything."  # type: ignore[misc]


# --- the helpers' rules -------------------------------------------------------------


def held_rules(module) -> dict[str, Rule]:
    """The rules a module holds, by name, found among its own attributes."""
    return {name: value for name, value in vars(module).items() if isinstance(value, Rule)}


def helpers_rules() -> dict[str, Rule]:
    """Every rule the helpers define, by name."""
    from helpers import rc_admin, rc_helper, users_file

    found = {}
    for module in (users_file, rc_helper, rc_admin):
        found.update(held_rules(module))
    # Non-vacuity for every check that runs over them: the rules known to exist.
    names = {
        "PREFIX",
        "EMAIL",
        "LINUX_NAME",
        "FOLDER_NAME",
        "AUTHOR_NAME",
        "VERSION",
        "PATH",
        "FOLDER_PATH",
    }
    assert names <= set(found), names - set(found)
    return found


def test_the_helpers_rules_are_found():
    assert helpers_rules()


def test_held_rules_finds_a_rule_and_nothing_else():
    """Planted twin: a module with one rule among other things yields that rule only."""
    import types

    module = types.ModuleType("planted")
    module.RULE, module.REGEX, module.TEXT = DIGITS, re.compile("x"), "Use digits only."
    assert held_rules(module) == {"RULE": DIGITS}


def test_every_helpers_rule_sentence_is_a_sentence():
    for name, rule in helpers_rules().items():
        assert rule.sentence[0].isupper() and rule.sentence.endswith("."), name


def test_no_helpers_rule_sentence_is_in_a_librarys_words():
    from tests.test_problems import library_text

    for name, rule in helpers_rules().items():
        assert not library_text(rule.sentence), name


def _limits():
    from helpers import rc_helper, users_file

    return [
        (rc_helper.PREFIX, rc_helper.MAX_PREFIX_LENGTH),
        (users_file.LINUX_NAME, users_file.MAX_LINUX_NAME_LENGTH),
        (rc_helper.AUTHOR_NAME, rc_helper.MAX_AUTHOR_NAME_LENGTH),
        (rc_helper.PATH, rc_helper.MAX_PATH_LENGTH),
    ]


# A short value each rule accepts, to put every code point into; the first one a
# rule accepts is its example.
EXAMPLES = ("a", "a@b.c", "1.2.3")
CODE_POINTS = [chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]


def _contexts(example: str) -> list[str]:
    """Templates putting a code point alone, before, after and in place of each
    character of the example, each once."""
    found = ["{}", "{}" + example, example + "{}"]
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
            values = [context.format(c) for c in CODE_POINTS]
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


def test_the_backend_and_the_helpers_read_every_rule_alike():
    rules = [r for rule in helpers_rules().values() for r in (rule, rule.or_empty())]
    assert disagreements(*rules) == []


def test_a_rule_the_two_engines_read_differently_is_found():
    """Planted twin: Python's \\s holds the separators U+001C to U+001F; pydantic's does not."""
    found = disagreements(Rule(re.compile(r"a\s?"), "Say a."))
    assert "'a\\\\s?' 'a{}' U+001C" in found and "'a\\\\s?' 'a{}' U+0020" not in found


def test_a_sentence_that_allows_the_joiners_says_so():
    """A rule that refuses invisible characters but takes ZWNJ and ZWJ between letters
    says that; one that refuses them says nothing of joiners."""
    found = []
    for name, rule in helpers_rules().items():
        refuses_invisible = not rule.fullmatch(f"a{chr(0x200B)}b")
        joins = rule.fullmatch(f"a{chr(0x200C)}b") and rule.fullmatch(f"a{chr(0x200D)}b")
        if refuses_invisible and joins:
            found.append(name)
            assert "joiner" in rule.sentence, name
        else:
            assert "joiner" not in rule.sentence, name
    assert {"AUTHOR_NAME", "FOLDER_NAME"} <= set(found)  # non-vacuity


def test_a_sentence_about_letters_says_which_case():
    from helpers import rc_helper, users_file

    assert rc_helper.PREFIX.fullmatch("Ab") and "either case" in rc_helper.PREFIX.sentence
    assert not users_file.LINUX_NAME.fullmatch("Ab")
    assert "lowercase" in users_file.LINUX_NAME.sentence


def test_a_sentence_that_states_a_limit_states_the_real_one():
    for rule, limit in _limits():
        assert re.search(rf"\b{limit}\b", rule.sentence), rule.sentence
        assert rule.fullmatch("a" * limit)
        assert not rule.fullmatch("a" * (limit + 1))
