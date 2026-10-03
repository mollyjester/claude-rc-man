"""A rule the helpers apply to what they are given, with the sentence that explains it.

The helpers refuse a value that breaks a rule with the rule's sentence, and
the backend publishes the rule's pattern and refuses with the same sentence,
so the rule and its explanation are written once, side by side. Python
standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """A regular expression a whole value must match, and what it asks for, in words."""

    regex: re.Pattern[str]
    sentence: str

    def __post_init__(self) -> None:
        if not self.sentence.strip():
            raise ValueError("a rule needs a sentence that explains it")

    @property
    def pattern(self) -> str:
        return self.regex.pattern

    @property
    def anchored(self) -> str:
        """The pattern for a validator that searches rather than matches the whole value."""
        return f"^(?:{self.pattern})$"

    def fullmatch(self, text: str) -> re.Match[str] | None:
        return self.regex.fullmatch(text)

    def or_empty(self) -> Rule:
        """The same rule, also accepting nothing at all."""
        return Rule(re.compile(f"(?:{self.pattern})?"), self.sentence)
