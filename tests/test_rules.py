"""The rules values are refused by: each a regular expression and the sentence that
explains it (helpers/rules.py)."""

import re
import subprocess
import typing
from typing import Annotated

import pytest
from pydantic import AfterValidator, Field, TypeAdapter, ValidationError

from helpers import rules
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
    return [
        (rules.PREFIX, rules.MAX_PREFIX_LENGTH),
        (rules.WORKSPACE_NAME, rules.MAX_WORKSPACE_NAME_LENGTH),
        (rules.AUTHOR_NAME, rules.MAX_AUTHOR_NAME_LENGTH),
        (rules.GITHUB_TOKEN, rules.MAX_GITHUB_TOKEN_LENGTH),
    ]


# A short value each rule accepts, to put every code point into; the first one a
# rule accepts is its example.
EXAMPLES = ("a", "a@b.c", "1.2.3", "https://github.com/o/r")
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


def _every_rule_in_both_forms():
    """Every helpers' rule, as it is and as or_empty() makes it, each named."""
    return [
        pytest.param(form, id=f"{name}{suffix}")
        for name, rule in helpers_rules().items()
        for form, suffix in ((rule, ""), (rule.or_empty(), ".or_empty()"))
    ]


@pytest.mark.parametrize("rule", _every_rule_in_both_forms())
def test_the_backend_and_the_helpers_read_the_rule_alike(rule):
    assert disagreements(rule) == []


def test_the_agreement_test_has_a_case_for_every_rule_in_both_forms():
    """Each rule is its own case, so parallel workers share the work; together the
    cases are every rule, as it is and as or_empty() makes it."""
    from tests.collected import collect

    test = "tests/test_rules.py::test_the_backend_and_the_helpers_read_the_rule_alike"
    cases = {n.removeprefix(test) for n in collect(["tests/test_rules.py"]) if n.startswith(test)}
    names = helpers_rules()
    assert names  # non-vacuity: helpers_rules checks the known ones are there
    assert cases == {f"[{n}]" for n in names} | {f"[{n}.or_empty()]" for n in names}


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
    assert {"AUTHOR_NAME"} <= set(found)  # non-vacuity


def test_a_sentence_about_letters_says_which_case():
    for rule in (rules.PREFIX, rules.WORKSPACE_NAME):
        assert rule.fullmatch("Ab") and "either case" in rule.sentence
    lower = Rule(re.compile("[a-z]+"), "Use lowercase letters a to z.")
    assert not lower.fullmatch("Ab") and "lowercase" in lower.sentence  # the words for the other


def test_a_sentence_that_states_a_limit_states_the_real_one():
    for rule, limit in _limits():
        assert re.search(rf"\b{limit}\b", rule.sentence), rule.sentence
        assert rule.fullmatch("a" * limit)
        assert not rule.fullmatch("a" * (limit + 1))


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
AUTHOR_NAMES_REFUSED = [
    "",
    " ",
    " Bob",
    "Bob ",
    "a\nb",
    "a\rb",
    "a\tb",
    "a\x00b",
    "a\x85b",
    f"a{chr(0x2028)}b",  # LINE SEPARATOR
    f"a{chr(0x2029)}b",  # PARAGRAPH SEPARATOR
    "a<b",
    "a>b",
    f"a{chr(0x200B)}b",  # ZERO WIDTH SPACE
    f"a{chr(0x202E)}b",  # RIGHT-TO-LEFT OVERRIDE
    f"a{chr(0x2066)}b",  # LEFT-TO-RIGHT ISOLATE
    f"a{chr(0xFEFF)}b",  # ZERO WIDTH NO-BREAK SPACE
    f"a{chr(0x00AD)}b",  # SOFT HYPHEN
    f"a{chr(0x1D173)}b",  # MUSICAL SYMBOL BEGIN BEAM, beyond the BMP
    f"a{chr(0xE0020)}b",  # TAG SPACE
    f"a{chr(0x3164)}b",  # HANGUL FILLER
    chr(0x3164),
    chr(0x200C),  # ZWNJ and ZWJ: not alone, not at either end
    chr(0x200C) + chr(0x200D),
    chr(0x200C) + "Bob",
    "Bob" + chr(0x200D),
    "'Bob'",
    '"Bob"',
    "Bob;",
    "Bob\\",
    ",Bob",
    ":Bob",
    "x" * (rules.MAX_AUTHOR_NAME_LENGTH + 1),
]
AUTHOR_NAMES = [(n, True) for n in AUTHOR_NAMES_ACCEPTED] + [
    (n, False) for n in AUTHOR_NAMES_REFUSED
]

# Format characters (Unicode category Cf) a name may hold, and why: Persian and
# Indic names are written with them.
ALLOWED_FORMAT = {0x200C: "ZERO WIDTH NON-JOINER", 0x200D: "ZERO WIDTH JOINER"}


def hangul_fillers() -> list[str]:
    """The Hangul fillers: letters by category, but they show as nothing."""
    import sys
    import unicodedata

    return [
        chr(c)
        for c in range(sys.maxunicode + 1)
        if {"HANGUL", "FILLER"} <= set(unicodedata.name(chr(c), "").split())
    ]


def format_characters() -> list[str]:
    """Every invisible format character this Python's Unicode database knows."""
    import sys
    import unicodedata

    return [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"]


@pytest.mark.parametrize(("name", "accepted"), AUTHOR_NAMES)
def test_author_name_rule(name, accepted):
    assert bool(rules.AUTHOR_NAME.fullmatch(name)) is accepted


# The rules that refuse invisible characters but the joiners: a commit author's name.
NAME_RULES = {"author name": rules.AUTHOR_NAME}


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_every_format_character_is_refused_but_zwnj_and_zwj_inside_a_name(rule):
    import unicodedata

    found = format_characters()
    assert chr(0x200B) in found and chr(0xE0020) in found  # positive control
    for code, name in ALLOWED_FORMAT.items():  # each exemption is still what it is said to be
        assert chr(code) in found and unicodedata.name(chr(code)) == name
        assert rule.fullmatch(f"a{chr(code)}b")
    let_through = [
        (f"U+{ord(c):04X}", name)
        for c in found
        for name in (f"a{c}b", c, f"{c}b", f"a{c}")
        if rule.fullmatch(name) and (ord(c) not in ALLOWED_FORMAT or name != f"a{c}b")
    ]
    assert let_through == []  # the allowed ones only inside a name, never alone or at an end


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_hangul_fillers_are_refused_in_a_name(rule):
    found = hangul_fillers()
    assert chr(0x3164) in found  # positive control: HANGUL FILLER
    assert {code for first, last in rules.HANGUL_FILLERS for code in range(first, last + 1)} == {
        ord(c) for c in found
    }
    for c in found:
        assert not rule.fullmatch(f"a{c}b"), f"U+{ord(c):04X}"
        assert not rule.fullmatch(c), f"U+{ord(c):04X}"


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
