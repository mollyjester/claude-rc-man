# Working rules

The standing rules for anyone changing this repository, human or agent.
[CONTRIBUTING.md](../CONTRIBUTING.md) names the three that override everything
else; this file is the full list. Rules are referred to by their bold name.

---

## Knowing versus assuming

- **Robustness over speed.** Where the correct approach costs more than a
  workable one, take the correct one. A shortcut today is a defect tomorrow.
- **Verify every claim in the current code before depending on it** —
  including a claim made by an issue, a docstring, a comment, a previous
  message, or a test name.
- **A recorded decision is not a control.** A document or docstring asserting
  that a check exists is a claim to verify, never a fact to build on.
- **Prove every negative against a known positive.** An empty search, a zero
  count or an absent file is a finding only after the same search has been
  shown to find a case known to be present.
- **Tool, script and subagent output is a hint, not verified truth.** Confirm
  it independently before building on it or quoting it onward.
- **Ambiguity, contradiction or a missing prerequisite: stop and ask.** A
  question is a deliverable; a guess is a defect.
- **Verify after acting, not only before.** A change is finished when
  something independent shows it did what was intended.
- **Probe assumptions against reality, not against tests written alongside
  them.** Tests written with a mechanism encode its assumptions. Name the
  load-bearing assumptions; each one is a test case that must be able to
  contradict it. Reproduce through the product's own invocation path. A fake
  of an external program is recorded from the real one and checked against it
  by contract tests, never written from memory.

## Scope

- **No silent scope expansion.** Something wrong found nearby is reported, not
  fixed in passing — with one exception: a documentation error is fixed as
  soon as it is met, because a known-wrong document is a defect in its own
  right.
- **Recording a defect is not fixing it.** For every defect found, exactly one
  of: fix it (the default); get an explicit decision that it stays, recorded
  where the next reader will meet it; or show by measurement that it was never
  a defect. Handing back a list of known, undecided defects is not delivery.

## Tests and guards

- **Tests first.** A failing test that states the intended behaviour exists
  before the code that satisfies it. This applies to documentation guards as
  much as to features.
- **A guard that cannot fail is prose.** Every guard has a planted twin: a test
  that introduces the violation, confirms the plant took effect, and asserts
  the guard fires.
- **Non-vacuity.** Ask what would let a guard pass over an empty or wrong
  input, and close that path first. Every absence assertion is preceded by a
  positive that becomes true only once the thing it is absent from exists.
- **A guard whose input can shrink to empty must still be able to fail.**
  Drive it over a synthetic input with a known positive so *empty* and
  *clean* stay distinguishable.
- **Derive the candidate set from the code, never list it by hand.** Routes
  come from the application's route table, settings from the settings model,
  CLI options from the parser. A hand-kept list is one more thing to drift.
- **Exemptions are named, reasoned and stale-checked.** The first answer to a
  flagged site is to fix the site. Where an exemption is genuinely right it
  names its subject, states what makes that subject different, and fails when
  the subject no longer exists.
- **A green suite that proves nothing is worse than a missing test**, because
  it is credited as coverage.
- **A suite that did not run is not a green suite.** Read the totals and the
  collected count, not just the exit code; report failing test names.
- **Work is committed and pushed before a test command runs.** A test run
  can delete the checkout, `.git` with it, and work that exists only in the
  working tree or only in the local repository goes with it. Before a command
  that runs tests, the changes are committed on the working branch, untracked
  files included, and the branch is pushed to `origin`. In Claude Code
  sessions the pre-test hook does this; anyone running tests otherwise does
  it by hand. Work on `main` or on a detached HEAD is moved onto a branch
  first, because the hook commits on neither. Those work-in-progress commits
  are rewritten into clean ones, and force-pushed with lease at once, before
  the pull request is opened and again before it is merged, as
  [CONTRIBUTING.md](../CONTRIBUTING.md#pull-requests) says. The paired test
  the edit-time hook runs after an edit is not a command, and no commit
  precedes it; that exception, and what the hook does and does not catch,
  are in [tests.md](tests.md#pre-test-hook).
- **The full check runs at the edges of a change; targeted tests run in
  between.** Before a change starts, `make check` gives the baseline: what
  already fails is recorded and reported as found, not later taken for one's
  own. While working, run the tests the change touches — the test written
  first, its module, the paired test the edit-time hook runs — and
  `make test`, the tests that run on the host, when a step is done. Before
  the change is handed over, `make check`, the gating check, runs again, and
  again in full after any fix to a red run: a fix checked only by the tests
  it aimed at is not checked. A targeted run or `make test` is never reported
  as the full check; every run is read by its totals, its collected count and
  its failing test names. What each target runs is in
  [tests.md](tests.md#running).
- **The on-demand runs happen once, at the end, when required.** `make check`
  holds no container test. The container tests (`make test-container`) and
  the whole-stack browser test (`make check-ui`) are the on-demand runs, and
  `make check-all` runs `make check` and both. They are required when the
  change touches the runtime, the uid mapping, the installer, the restore
  after a restart or reboot, or the container-based stack, and before the live
  reinstall; otherwise they are not run. They run once, at the end of the
  implementation, after the gating check is green. They have no baseline: a
  red on-demand test is triaged by running that test on `main`. When an
  on-demand run is red, everything red is fixed together, and the run is
  repeated only after the operator confirms. The pull request says whether
  they were required and, if so, their totals
  ([CONTRIBUTING.md](../CONTRIBUTING.md#pull-requests)).
- **A test that patches a constant the service reads when it starts does so
  before the `client` fixture starts the service.** The service takes the
  value once, at start; a patch made afterwards changes nothing the running
  service uses, and the test passes or fails by timing. Such a patch goes in a
  fixture the test lists before `client`.
- **Throwaway probes go in a scratch directory** outside the repository and are
  deleted when done.

## Design rules that prevent drift

- **Derive counts; never hand-maintain a constant** that duplicates
  information the code already implies.
- **Never write a number in a document.** Counts, sizes, durations and
  defaults go stale the moment they are written. Point at where the value
  lives — the settings model, the parser, the route table — instead.
- **User-facing strings live in one message catalogue** per tier, never
  inline and never mirrored by hand.
- **Validation lives on the server only.** The frontend sends what was entered
  and displays the problem-details errors it receives; it holds no rules of
  its own.
- **Remove orphaned functionality; do not keep it as "legacy".** Dead or
  unreachable code is deleted, then the documentation is revisited.

## Documentation

- **Documentation describes the current state only.** No "previously",
  "used to", "was removed because", no migration narrative, no account of a
  superseded approach. Git and the issue tracker hold history. Where a past
  decision explains a present constraint, write the constraint.
- **Any inconsistency between documentation and code is a bug**, with the same
  priority as any other bug.
- **Docs move in the same commit as the code.** A change is not done until the
  documentation it affects matches it.
- **Knowledge lives in one place.** A second statement of the same thing is the
  one nobody updates. Where a subject has a home, link to it; where it has
  none, create one rather than adding a paragraph wherever you happen to be.
- **Find affected documentation by searching, never from memory.** One field
  can surface in its endpoint, the form that sets it and the page that shows
  it; missing one is a documentation bug.
- **A reference must point at something that ships.** Name only files that
  exist in the repository after merge. Plans and design discussion live in
  GitHub issues and are linked from there, never from code.
- **`install.sh` is part of the documentation contract.** A change to the
  installer and to the installation guide land together.
- **An illustration that enumerates is wrong the moment another case exists.**
  Repair it by quantifying ("wherever they appear"), not by extending the list.
- **Reference documentation is generated, not hand-written.** The HTTP API
  reference comes from the OpenAPI description, a CLI's reference from its
  `--help`, the configuration reference from the settings model. Hand-written
  prose that names any of them is checked against them by tests.

### Where documentation goes

| Location | Audience |
|---|---|
| `docs/` | Colleagues and admins using the service |
| `project-knowledge/` | Contributors |
| `CLAUDE.md` | A contents list for agent sessions; nothing in it is duplicated from what it points at |
| GitHub issues | Plans, design decisions, history |

## Code invariants

- **Never `shell=True`.** Child processes get a fixed argv and an allowlisted
  environment, never the service's full environment.
- **Credentials and one-time codes never appear in argv, URLs or logs.** They
  reach child processes through the environment or stdin and are held in
  memory only as long as needed.
- **A person reaches only their own workspaces.** Every route that names a
  workspace finds it among the signed-in person's, and answers as if it were
  not there otherwise; every workspace of a person runs in their own uid
  range, and no workspace mounts another's volume.
- **rcman drives containers through `podman --remote` only**, as its own
  account, with an argv and never a shell; the limits on what they may use are
  set through `systemctl --user` and nothing else, on slices and never by
  stopping one; a command in a workspace is an argv
  too, and no text a person sent through rcman's interface is ever run by a
  shell there. The lifecycle commands of a repository's devcontainer.json are
  not such text: the person chose them by choosing the repository, and a
  string command is run by a shell in the workspace, as the container's user,
  with a time limit.
- **No privilege boundary in product code.** What the service ships has no command
  that raises privilege, no root helper and no switching of accounts; rcman runs as
  its own account and drives everything else through it. The installer is the one
  program that makes accounts, and `rcman-admin`, started by root, runs itself again
  as rcman, which is a named exemption. The guard and its exemptions are in
  [tests.md](tests.md#repository-guards).
- **Every mutating endpoint is audited**, and a test over the route table
  checks it. An exemption carries a reason that also appears in `docs/`.
- **The interface offers only what the server will allow.** A control shown to
  someone the server will refuse is a defect.

## Cleaning up

- **Leave nothing behind when a session ends.** Before a working session is
  handed over, it removes everything it created that is no longer needed:
  files and directories made outside the repository — scratch probes,
  temporary homes, sockets; processes it started — servers, tmux servers,
  watchers, containers; sessions it opened — tmux, browser, agent sessions;
  git worktrees it added and local branches whose work is merged or
  abandoned; and remote branches whose pull request has been merged.
  Removal is verified, not assumed: what is left is listed afterwards.
  Anything that must stay — a branch with an open pull request, a process
  still in use — is named in the handover with the reason.

## Reporting

- **A negative result is a deliverable** — provided it says how it was looked
  for, so the reader can judge whether the method could have found something.
- **Name what is unsettled** alongside what was found, and what would settle it.
- **Rank findings by severity**, each with a reproduction someone else can run.
- **Report completion faithfully**: what was skipped, what failed, what was
  left out and why.
