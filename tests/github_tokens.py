"""GitHub tokens of many shapes the token rule (helpers/rules.py: GITHUB_TOKEN) lets in, for
the tests of what is written for git and gh (tests/test_git_account.py) and of what real gh
reads back (tests/container/test_github_in_podman.py).

Not a test module: the tests that need the tokens import it.
"""

# Each a character, or a word, that YAML or a shell would take for something other than text,
# and every visible character at once.
AWKWARD_TOKENS = [
    "ghp_an-ordinary-token",
    "it's",
    "''",
    "'leading",
    "trailing'",
    "#comment",
    "key:value",
    "-dash",
    '"double"',
    "back\\slash",
    "{flow}",
    "[seq]",
    "&anchor",
    "*alias",
    "!tag",
    "%percent",
    "@at",
    "`tick",
    "|pipe",
    ">fold",
    "?question",
    ",comma",
    "~",
    "null",
    "true",
    "0x1F",
    "1e3",
    "".join(chr(c) for c in range(0x21, 0x7F)),
]
