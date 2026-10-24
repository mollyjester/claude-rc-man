"""The rules values are refused by, each with the sentence that explains it.

The backend publishes a rule's pattern and refuses a value that breaks it with
the rule's sentence (``problems.ruled``), and the installer refuses a setting
the same way, so a rule and its explanation are written once, side by side.
Python standard library only: the installer runs under the system interpreter.
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


# Never beginning with a hyphen, so that no address can be read as an option.
EMAIL = Rule(
    re.compile(r"[A-Za-z0-9._%+][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"),
    "That is not a valid email address.",
)
MAX_PREFIX_LENGTH = 32
MAX_WORKSPACE_NAME_LENGTH = MAX_PREFIX_LENGTH
PREFIX = Rule(
    re.compile(rf"[A-Za-z0-9][A-Za-z0-9-]{{0,{MAX_PREFIX_LENGTH - 1}}}"),
    "The session name prefix may use letters a to z in either case, digits and hyphens, "
    f"start with a letter or digit, and be at most {MAX_PREFIX_LENGTH} characters long.",
)
# A workspace's name: the prefix of its sessions' names until the person sets one
# (routers/me.py), so it is a prefix too.
WORKSPACE_NAME = Rule(
    PREFIX.regex,
    "A workspace's name may use letters a to z in either case, digits and hyphens, "
    f"start with a letter or digit, and be at most {MAX_WORKSPACE_NAME_LENGTH} characters long.",
)
MAX_AUTHOR_NAME_LENGTH = 128
# Code points no name may hold anywhere, because they show as nothing or as
# something they are not, and make two names look alike; each as a tuple of
# (first, last).
CONTROLS = ((0x00, 0x1F), (0x7F, 0x9F))
SEPARATORS = ((0x2028, 0x2029),)  # line and paragraph separators
# Invisible format characters (Unicode category Cf); tests/test_rules.py
# derives them from the Unicode database. Not ZWNJ and ZWJ (JOINERS): Persian
# and Indic names are written with them.
INVISIBLE_FORMAT = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200B),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
# The Hangul fillers: letters that show as nothing.
HANGUL_FILLERS = ((0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0))
# ZWNJ and ZWJ, which join or part the characters beside them: a name may
# hold them, but not alone and not at either end.
JOINERS = (0x200C, 0x200D)


def _escape(code: int) -> str:
    """A code point as an escape both Python's re and the backend's validator read."""
    return f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}"


def _class(ranges=(), chars: str = "") -> str:
    """The inside of a character class holding ``ranges`` and ``chars``, as escapes."""
    spans = (_escape(a) if a == b else f"{_escape(a)}-{_escape(b)}" for a, b in ranges)
    return "".join(spans) + "".join(_escape(ord(c)) for c in chars)


# What no name may hold anywhere, and what it may besides not begin or end with,
# each with the words the rules' sentences say it in.
_UNSEEN = _class((*CONTROLS, *SEPARATORS, *INVISIBLE_FORMAT, *HANGUL_FILLERS))
_UNSEEN_WORDS = (
    "a control character or an invisible character other than the joiners some "
    "scripts need between letters"
)
_NOT_AT_AN_END = _class(chars="".join(map(chr, JOINERS))) + r"\s"
_NOT_AT_AN_END_WORDS = "white space"


def _either(words: list[str]) -> str:
    """Words as alternatives: "a, b or c"."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"


# A commit author's name may not hold < and >, which git puts around the
# address, nor begin or end with what git strips from either end besides white
# space: crud() in git's ident.c. tests/test_rules.py asks real git.
_GIT_TRIMS = ",:;<>\"'\\"
_IN_ADDRESS_MARKS = "<>"
_CHARACTER_WORDS = {
    ",": "a comma",
    ":": "a colon",
    ";": "a semicolon",
    "<": "<",
    ">": ">",
    '"': "a quotation mark",
    "'": "an apostrophe",
    "\\": "a backslash",
}
_NOT_IN_AUTHOR_NAME = _UNSEEN + _class(chars=_IN_ADDRESS_MARKS)
_NOT_AT_AUTHOR_ENDS = _NOT_IN_AUTHOR_NAME + _NOT_AT_AN_END + _class(chars=_GIT_TRIMS)
# No lookaround, so the backend's validator reads it as this module does.
AUTHOR_NAME = Rule(
    re.compile(
        rf"[^{_NOT_AT_AUTHOR_ENDS}]"
        rf"(?:[^{_NOT_IN_AUTHOR_NAME}]{{0,{MAX_AUTHOR_NAME_LENGTH - 2}}}[^{_NOT_AT_AUTHOR_ENDS}])?"
    ),
    f"A commit author's name may not be empty or longer than {MAX_AUTHOR_NAME_LENGTH} "
    f"characters, hold {_either([_CHARACTER_WORDS[c] for c in _IN_ADDRESS_MARKS])}, "
    f"{_UNSEEN_WORDS}, or begin or end with "
    + _either(
        [_NOT_AT_AN_END_WORDS]
        + [_CHARACTER_WORDS[c] for c in _GIT_TRIMS if c not in _IN_ADDRESS_MARKS]
    )
    + ".",
)
MAX_COMMIT_MESSAGE_LENGTH = 10_000
# A commit message must hold something besides what git counts as white space,
# which git would strip to nothing, and no NUL, at which git would cut it.
_GIT_SPACE = _class(chars=" \t\n\v\f\r")
_NUL = _class(chars="\0")
COMMIT_MESSAGE = Rule(
    re.compile(rf"[{_GIT_SPACE}]*[^{_GIT_SPACE}{_NUL}][^{_NUL}]*"),
    "A commit message must hold something other than white space, no NUL character, "
    f"and be at most {MAX_COMMIT_MESSAGE_LENGTH} characters long.",
)
MAX_BRANCH_NAME_LENGTH = 255
# What any branch name must be before git is asked about it: git itself says
# what else it takes (git-branch-new), and an option is never a name.
_NO_HYPHEN = _class(chars="-")
BRANCH_NAME = Rule(
    re.compile(rf"[^{_NUL}{_NO_HYPHEN}][^{_NUL}]{{0,{MAX_BRANCH_NAME_LENGTH - 1}}}"),
    "A branch name must not be empty, begin with a hyphen or hold a null character, "
    f"and may be at most {MAX_BRANCH_NAME_LENGTH} characters long.",
)
# GitHub's tokens are much shorter; this leaves room for longer ones to come.
MAX_GITHUB_TOKEN_LENGTH = 255
# Not checked against GitHub's own shapes of token: GitHub says what it takes.
GITHUB_TOKEN = Rule(
    re.compile(rf"[\x21-\x7E]{{1,{MAX_GITHUB_TOKEN_LENGTH}}}"),
    "A GitHub token is one line of visible characters, with no spaces, "
    f"at most {MAX_GITHUB_TOKEN_LENGTH} characters long.",
)
# How long a GitHub owner's and repository's names may be in an address: longer than
# GitHub allows, so that they bound what is taken and GitHub refuses the rest.
MAX_GITHUB_OWNER_LENGTH = 100
MAX_GITHUB_REPOSITORY_LENGTH = 100
# A repository on GitHub, as git clone takes it. ASCII only, and no lookaround, so the
# backend's validator reads it as this module does. A name may not begin with a dot:
# the folder git clones it into would be hidden.
_GITHUB_OWNER = rf"[A-Za-z0-9_][A-Za-z0-9_-]{{0,{MAX_GITHUB_OWNER_LENGTH - 1}}}"
_GITHUB_REPOSITORY = rf"[A-Za-z0-9_-][A-Za-z0-9._-]{{0,{MAX_GITHUB_REPOSITORY_LENGTH - 1}}}"
GITHUB_URL = Rule(
    re.compile(rf"https://github\.com/{_GITHUB_OWNER}/{_GITHUB_REPOSITORY}(?:\.git)?/?"),
    "A repository's address must be https://github.com/, its owner, a slash and its "
    "name, as GitHub shows them, and may end in .git or a slash. The owner and the name "
    "may hold letters a to z in either case, digits, hyphens and underscores, and the "
    "name dots too; the owner may not begin with a hyphen, nor the name with a dot. "
    "Nothing else may be in it: no user name, password, port, query, fragment or "
    "percent sign.",
)
MAX_FAILURE_REASON_LENGTH = 200
# Why an administrator marked a Claude Code version as failed (backend/rcman/claude_versions.py):
# one line that shows as what it is, so nothing unseen; the route trims it, and a reason left
# blank is none.
FAILURE_REASON = Rule(
    re.compile(rf"[^{_UNSEEN}]{{1,{MAX_FAILURE_REASON_LENGTH}}}"),
    f"A reason is one line of at most {MAX_FAILURE_REASON_LENGTH} characters, and may not "
    f"hold {_UNSEEN_WORDS}.",
)
# A Claude Code version, as its releases are numbered.
VERSION = Rule(re.compile(r"[0-9]+\.[0-9]+\.[0-9]+"), "A Claude Code version looks like 1.2.3.")
# The limits an administrator sets on a person or a workspace (backend/rcman/limits.py). A
# regular expression cannot hold a minimum, so the rules take the shape of a value and
# limits.py refuses what is below the minimums; each sentence says its minimum, made from
# the constants below, so that the two cannot differ.
MIN_MEMORY_MIB = 256
MIN_CPU_PERCENT = 10  # of one core, where 100 is one whole core
MIN_PIDS = 64
# Bounds the number of digits so that no value can overflow a column or a unit manager.
MAX_LIMIT_DIGITS = 9
MAX_CPU_DECIMALS = 2
_MIN_CORES = f"{MIN_CPU_PERCENT / 100:g}"
# ASCII digits only, as a limit is read in every engine alike.
MEMORY = Rule(
    re.compile(rf"[0-9]{{1,{MAX_LIMIT_DIGITS}}}[MmGg]"),
    f"Memory must be a whole number of at most {MAX_LIMIT_DIGITS} digits followed by M for "
    "mebibytes or G for gibibytes, in either case, and be at least "
    f"{MIN_MEMORY_MIB}M.",
)
CPU = Rule(
    re.compile(rf"[0-9]{{1,{MAX_LIMIT_DIGITS}}}(?:\.[0-9]{{1,{MAX_CPU_DECIMALS}}})?"),
    f"CPU must be a number of cores, of at most {MAX_LIMIT_DIGITS} digits before the point "
    f"and at most {MAX_CPU_DECIMALS} decimals, and be at least {_MIN_CORES}.",
)
PIDS = Rule(
    re.compile(rf"[0-9]{{1,{MAX_LIMIT_DIGITS}}}"),
    "Processes must be a whole number of at most "
    f"{MAX_LIMIT_DIGITS} digits, in which threads count as processes, and be at least "
    f"{MIN_PIDS}.",
)
