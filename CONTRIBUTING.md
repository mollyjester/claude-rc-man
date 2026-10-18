# Contributing

Three rules override everything else in this repository:

1. **Tests first.** Write the failing test that states the intended behaviour,
   then the code that makes it pass.
2. **Any inconsistency between documentation and code is a bug.** Fix it with
   the same priority as any other bug, and change docs in the same commit as
   the code they describe.
3. **Documentation describes the current state only.** No history ("used to",
   "was removed because"), and no numbers that go stale — point at where a
   value lives instead of copying it.

The full list of standing rules, and where each kind of documentation belongs,
is in [project-knowledge/working-rules.md](project-knowledge/working-rules.md).
Working in a workspace of rcman's: the repository's `.devcontainer` and what the tests need
there are in [project-knowledge/tests.md](project-knowledge/tests.md#the-container-suites).

## Plans and design

Plans, design decisions and their discussion live in
[GitHub issues](https://github.com/mollyjester/claude-rc-man/issues), not in
the repository. Each implementation step is its own issue and lists the tests
it starts with.

## Pull requests

- One issue per pull request where possible; link it.
- Before the pull request is opened, and again before it is merged, the
  work-in-progress commits the pre-test hook makes are rewritten into clean
  commits, each with the documentation for its code. The rewrite is
  force-pushed with `--force-with-lease` at once, before any further test
  run, to that feature branch only, never to `main`.
- The description says which tests were written first and which documentation
  changed. A change with no documentation impact says so explicitly.
- `make check` is the gating check. The on-demand runs (`make test-container`,
  `make check-ui`, together `make check-all`) are required for some changes;
  [the working rules](project-knowledge/working-rules.md#tests-and-guards)
  say which and when. The description says whether they were required and, if
  so, gives their totals, or says they were not required and why.
