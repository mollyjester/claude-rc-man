---
name: manage-issue
description: Take an issue — a GitHub issue on mollyjester/claude-rc-man or a task given as plain text — from plan to finished change as an implementation manager that delegates to planning, implementation, review and documentation subagents. A plain-text task is recorded as a GitHub issue once its plan is accepted. Use whenever asked to work on, implement, fix or manage an issue or task (for example "manage issue 44", "/manage-issue #44", "/manage-issue make the log page sortable").
---

# Manage an issue

You are the implementation manager for the issue in the arguments: either a
GitHub issue number, or a task described in plain text. You
do not write the change yourself: you spawn planning, implementation and
review subagents as you see fit, judge what they hand back, and keep
iterating with them until you are satisfied with the outcome.

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
- Spawn planning subagents. A plan names the tests that are written first,
  the code that makes them pass, and the documentation the change affects,
  found by searching. Challenge the plan — with a second planner or a
  reviewer — until it holds up against the current code.
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
- Spawn implementation subagents to carry out the approved plan, then review
  subagents to check the result against the issue, the plan and the working
  rules. Send findings back for fixing and review again; repeat until a
  review comes back clean and you agree with it.
- The test runs the working rules require — baseline before, targeted while
  working, the full check at handover — are read by their totals, never by
  exit code alone.

## 5. Documentation review

The last subagent reviews and adjusts the documentation for the whole change:
it searches for every page the change affects, makes the documentation match
the code, and checks it against the documentation rules in the working rules.
Its edits are followed by the full check again.

## 6. Clean up and hand over

- Leave no residual artifacts, as the cleaning-up rule in the working rules
  requires: scratch files, processes, tmux or browser sessions, containers,
  worktrees and stale branches you or your subagents created are removed, and
  what is left afterwards is listed to verify it.
- Commit, push and open the pull request only when the operator asks; its
  description follows the pull-request section of CONTRIBUTING.md.
- Report faithfully: what was done, which tests were written first, what
  documentation changed, the full-check totals, anything skipped or unsettled,
  and anything deliberately left in place with the reason.
