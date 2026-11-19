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

## Subagents

- Every subagent starts cold and rereads the rules and the code it works on,
  so each one spawned costs tokens of its own; and every message to it carries
  everything it has read so far. Continue a subagent with a message for
  related work, but hand over to a new one, briefed with what the first
  reported, once it has used about 500k tokens.
- Every subagent you spawn is told to follow
  [CONTRIBUTING.md](../../../CONTRIBUTING.md) and
  [project-knowledge/working-rules.md](../../../project-knowledge/working-rules.md),
  and you hold its output to them. Every brief also names what it must not
  touch: the installed rcman, its account, its workspaces and its paths, and
  anything else live on the machine, and repeats the working rules' ban on
  loading the CPU on purpose.
- A brief for planning, implementation or review that touches the frontend's
  look also tells the subagent to load
  [frontend-design](../frontend-design/SKILL.md) and apply it within the
  frontend's design system, and to hold the result to Vercel's web interface
  guidelines
  (<https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md>).
- Subagents report briefly, in bullets: what was done, files changed, every
  test run with its totals, departures from the plan, tests not written first,
  what is lost or deferred, what is left behind, and blockers.
- Subagent output is a hint, not verified truth: confirm what matters
  yourself, totals above all, before acting on it or reporting it.
- Subagents run one at a time, and a subagent spawns nothing but its test
  runner: an implementation, review or documentation subagent does its work
  in its own context. Every brief says so.
- Repository files are edited with the Edit and Write tools, never through a
  shell command (`sed -i`, a heredoc, a script that writes): the edit-time
  hook runs a file's paired test only after those tools
  ([tests.md](../../../project-knowledge/tests.md#edit-time-hook)), and an
  edit made through the shell is checked by nothing until the next test
  run. A change no tool can make, a move or a generated file, is followed
  by its paired test on the runner. Every implementation and documentation
  brief says so.
- Every test run goes to the `test-runner` agent
  ([.claude/agents/test-runner.md](../../agents/test-runner.md), on a small
  model), dispatched by its type, never to you or to an implementation,
  review or documentation subagent: it is cheap, and it keeps a run's output
  and the wait for it out of a large context. You dispatch the `make check`
  baseline, every gating `make check`, and the on-demand run.
  Implementation, review and documentation subagents dispatch only the
  targeted tests of what they change, and read the report. Every brief says
  so, and every brief that would otherwise end in a gating or on-demand run
  says so too and names the runner instead, and you dispatch it when the
  subagent hands back.
- A runner's brief is the folder, the command and the log file, one of its
  own for each run, in the scratch directory; how a run is started, waited
  for and reported is the definition's, and no brief repeats it. The command
  carries its own `timeout -k`, with a limit beyond the hang guard of the
  tests it runs
  ([tests.md](../../../project-knowledge/tests.md#the-hang-guard)), so that
  the guard ends and names a hung test first; `make` is given `-k`, so that
  a part that fails does not hide the parts after it. One runner runs at a
  time in the working tree, since the pre-test hook commits and two at once
  collide on git's index. Check a report's headline numbers against the log
  with one grep.
- A run has ended when its marker, `<log>.exit`, exists; the runner hands
  back only then, or to say at once that the run was killed or outlived its
  limit. Whoever dispatched it, you or a subagent, reads nothing from the log
  before the report. When a report has not come by the command's limit and its grace,
  or comes without the marker, the run is treated as killed: one look for
  the marker file settles it, that runner is stopped, and a fresh one is
  dispatched. Nobody, you included, waits for a run with a loop over a log
  line or a command line (`until grep`, `pgrep -f`), which can match itself
  or never come, and never end. Every brief for a subagent that dispatches
  runners repeats this bullet.

## 1. Plan

- Before planning, confirm with the operator what outcome counts as done,
  when the issue leaves it open. A goal changed after planning costs every
  revision since.
- For a GitHub issue, read it and its comments (`gh issue view <n> --comments`),
  and any issue it links to. For a plain-text task, dispatch a planning
  subagent first, before anything else; the task has no issue yet and gets one
  only in step 3.
- Spawn exactly one planning subagent for the whole issue, however many parts
  it has; for a plain-text task it is the one dispatched first. A plan names
  the tests that are written first, the code that makes them pass, and the
  documentation the change affects, found by searching. Where the plan rests
  on something not yet proven, its first step is a gate: tests that prove it
  before anything depends on it.
- Tests come first, and cover the core only: the happy path of each behaviour
  the change adds, one test for it, extending a test that already covers the
  area rather than adding one. No variant, edge-case or refusal tests for
  every small piece. Each test is compact, fast and effective, and has no
  flaky shape: no sleeps or waits on real time, no races, nothing left
  running when it fails. Every brief for planning, implementation and review
  repeats this.
- A plan that touches the sandbox, the runtime, the installer, the uid mapping
  or anything security-related is challenged by a reviewer until it holds up
  against the current code; a smaller plan is challenged by its planner
  against the code. The reviewer's findings, and the operator's answers, go
  back to that same planner by message. No second planner is spawned.
- Ambiguity, contradiction or a missing prerequisite in the issue is a
  question for the operator, not a guess.

## 2. Stop and report the plan

Stop before dispatching any implementation. Report to the operator a short
version of the plan in plain words: what will change, which tests come
first, which documentation moves, what coverage or behaviour is given up, and
any open question. Wait for the operator's go-ahead; change the plan if they
ask. Ask with the plan whether the pull request may be opened once the final
gating check is green; a go-ahead that says so is the operator's asking of
step 6.

## 3. Record a plain-text task as an issue

Plans live in GitHub issues, not in the repository
(CONTRIBUTING.md, "Plans and design"). When the task came as plain text,
create the issue from the accepted plan before any implementation, with
`gh issue create`: the problem, the agreed approach, and the tests the change
starts with, labelled `build-step` (and `design` where it records a design
decision). A plan the operator split into several steps becomes one issue per
step. Check the created issue reads as intended, then continue with its
number as `<n>`. A task that came as a GitHub issue skips this step; where the
accepted plan departs from that issue, or a gate changes it, record the plan
as a comment on it.

## 4. Implement and review

- Work on a branch named `issue-<n>/<short-slug>`, cut from an up-to-date
  `main`.
- Group the approved plan into as few implementation tasks as it allows, and
  give each to one implementation subagent: parts that touch the same code, or
  follow one another, go to the same subagent, and a new one is spawned only
  when a part needs a context of its own (a different area of the code) or
  the subagent at work has reached the handover point above. A gate that
  fails stops the work: its subagent reports what failed and the options, and
  the choice is the operator's. Review findings go back by message to the
  subagent that wrote the code, not to a new one.
- Review subagents check the result against the issue, the plan and the
  working rules; one review covers what one implementation subagent handed
  back, and a re-review goes to the reviewer that found the issues. After a
  review and its re-review, findings marked minor are fixed with the next
  change or left with the reason, without another round; anything blocking
  or should-fix is still reviewed until it is clean. A round of review fixes
  gets no gating run of its own: the implementer has their targeted tests
  run, and the next gating `make check` on the tree covers them, the next
  implementation task's at its handover or the one that follows the
  documentation review; where the on-demand run is required, a gating
  `make check` is dispatched after the fixes and before it, and covers them.
- The test runs the working rules require — the `make check` baseline before,
  targeted while working, the gating `make check` at handover, every one of
  them on the `test-runner` agent of "Subagents" above — are read by
  their totals, collected count and failing names, never by exit code alone.
  Output goes to a file and is read by grep; the summary lines are read
  whether the run is green or red. Stopping at the first failure (`-x`,
  `--bail`) suits only the one test being fixed, never a module or a gating
  run, whose failures are all read and fixed together.
- The on-demand run (`make test-container`) happens once, at the end of the
  implementation, only when the working rules require it for this change, and
  has no baseline. A subagent has the modules it changes run directly, on its
  runner, not the whole on-demand run.
  If it is red, fix everything red together and repeat the run only after
  the operator confirms.

## 5. Documentation review

The last subagent reviews and adjusts the documentation for the whole change,
even where implementation passes already edited it: it searches for every
page the change affects and for terms the change made stale, makes the
documentation match the code, and checks it against the documentation rules
in the working rules. A mismatch where the code looks wrong is reported to
you, not fixed by it. Its edits, and the review fixes since the last gating
run, are followed by one gating `make check`, on the runner. If it is red,
the failing tests say whose edit it was: the fix goes to the subagent that
made the edit, and the gating `make check` runs again in full after every
fix to a red run, as the working rules require. An on-demand run already
green is not repeated for documentation edits.

## 6. Clean up and hand over

- Leave no residual artifacts, as the cleaning-up rule in the working rules
  requires: scratch files, processes, tmux or browser sessions, containers,
  worktrees and stale branches you or your subagents created are removed, and
  what is left afterwards is listed to verify it.
- Beyond the work-in-progress commits and pushes the pre-test hook makes
  before tests run, commit, push and open the pull request only when the
  operator asks, or has asked with the go-ahead of step 2: then it is opened
  once the final gating `make check` is green, and the on-demand run where
  the working rules require it, and not while anything is red, skipped or
  left for the operator to decide. Merging is the operator's. Before it is
  opened, and again before it is merged, the work-in-progress commits are
  rewritten into clean commits and force-pushed with lease to the feature
  branch at once, before any further test run, as the pull-request section
  of CONTRIBUTING.md says; the description follows that section too.
- Report faithfully: what was done, which tests were written first and which
  were not, what documentation changed, the `make check` totals, whether the
  on-demand run was required and, if so, its totals (or why it was
  not), what coverage was given up, anything skipped, deferred or unsettled,
  and anything deliberately left in place with the reason.
