"""A rule the helpers apply: a regular expression and the sentence that explains it."""

import re
from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

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
