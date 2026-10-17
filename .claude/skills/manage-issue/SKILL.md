---
name: manage-issue
description: Take an issue — a GitHub issue on mollyjester/claude-rc-man or a task given as plain text — from plan to finished change as an implementation manager that delegates to planning, implementation, review and documentation subagents. A plain-text task is recorded as a GitHub issue once its plan is accepted. Use whenever asked to work on, implement, fix or manage an issue or task (for example "manage issue 44", "/manage-issue #44", "/manage-issue make the log page sortable").
---

# Manage an issue

You are the implementation manager for the issue in the arguments: either a
GitHub issue number, or a task described in plain text. You
do not write the change yourself: you spawn one planning subagent, and
implementation and review subagents as you see fit, judge what they hand back,
and keep iterating with them until you are satisfied with the outcome.

Every subagent rereads the rules and the code it works on, so each one spawned
costs tokens of its own. Spawn as few as the work allows: continue a subagent
you already have, with a message, rather than start a new one for related work.

Every subagent you spawn is told to follow
[CONTRIBUTING.md](../../../CONTRIBUTING.md) and
[project-knowledge/working-rules.md](../../../project-knowledge/working-rules.md),
and you hold its output to them. Subagent output is a hint, not verified
truth: confirm what matters yourself before acting on it or reporting it.

## 1. Plan

- For a GitHub issue, read it and its comments (`gh issue view <n> --comments`),
  and any issue it links to. For a plain-text task, dispatch a planning
  subagent first, before anything else; the task has no issue yet and gets one
  only in step 3.
- Spawn exactly one planning subagent for the whole issue, however many parts
  it has; for a plain-text task it is the one dispatched first. A plan names the tests that are written first, the code that makes
  them pass, and the documentation the change affects, found by searching.
  Challenge the plan with a reviewer until it holds up against the current
  code; the reviewer's findings, and the operator's answers, go back to that
  same planner by message for it to revise the plan. No second planner is
  spawned.
- Ambiguity, contradiction or a missing prerequisite in the issue is a
  question for the operator, not a guess.

## 2. Stop and report the plan

Stop before dispatching any implementation. Report to the operator a short
version of the plan in plain words: what will change, which tests come
first, which documentation moves, and any open question. Wait for the
operator's go-ahead; change the plan if they ask.

## 3. Record a plain-text task as an issue

Plans live in GitHub issues, not in the repository
(CONTRIBUTING.md, "Plans and design"). When the task came as plain text,
create the issue from the accepted plan before any implementation, with
`gh issue create`: the problem, the agreed approach, and the tests the change
starts with, labelled `build-step` (and `design` where it records a design
decision). A plan the operator split into several steps becomes one issue per
step. Check the created issue reads as intended, then continue with its
number as `<n>`. A task that came as a GitHub issue skips this step; where the
accepted plan departs from that issue, record the plan as a comment on it.

## 4. Implement and review

- Work on a branch named `issue-<n>/<short-slug>`, cut from an up-to-date
  `main`.
- Group the approved plan into as few implementation tasks as it allows, and
  give each to one implementation subagent: parts that touch the same code, or
  follow one another, go to the same subagent, and a new one is spawned only
  when a part needs a context of its own (a different area of the code, or
  more than the subagent already at work can hold). Review findings go back by message to the
  subagent that wrote the code, not to a new one.
- Review subagents check the result against the issue, the plan and the
  working rules; one review covers what one implementation subagent handed
  back, and a re-review goes to the reviewer that found the issues. Repeat
  until a review comes back clean and you agree with it.
- The test runs the working rules require — the `make check` baseline before,
  targeted while working, the gating `make check` at handover — are read by
  their totals, never by exit code alone.
- The on-demand runs (`make test-container`, `make check-ui`, together
  `make check-all`) happen once, at the end of the implementation, only when
  the working rules require them for this change, and have no baseline. If one
  is red, fix everything red together and repeat the run only after the
  operator confirms.

## 5. Documentation review

The last subagent reviews and adjusts the documentation for the whole change:
it searches for every page the change affects, makes the documentation match
the code, and checks it against the documentation rules in the working rules.
Its edits are followed by the gating `make check` again; an on-demand run
already green is not repeated for documentation edits.

## 6. Clean up and hand over

- Leave no residual artifacts, as the cleaning-up rule in the working rules
  requires: scratch files, processes, tmux or browser sessions, containers,
  worktrees and stale branches you or your subagents created are removed, and
  what is left afterwards is listed to verify it.
- Beyond the work-in-progress commits and pushes the pre-test hook makes
  before tests run, commit, push and open the pull request only when the
  operator asks. Before it is opened, and again before it is merged, the
  work-in-progress commits are rewritten into clean commits and force-pushed
  with lease to the feature branch at once, before any further test run, as
  the pull-request section of CONTRIBUTING.md says; the description follows
  that section too.
- Report faithfully: what was done, which tests were written first, what
  documentation changed, the `make check` totals, whether the on-demand runs
  were required and, if so, their totals (or why they were not), anything
  skipped or unsettled,
  and anything deliberately left in place with the reason.
