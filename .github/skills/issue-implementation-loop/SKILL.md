---
name: issue-implementation-loop
description: Use when asked to run or define an agent loop that reviews GitHub issues, prioritizes by labels, implements ready work, verifies changes, and off-ramps inefficient loops.
---

# Issue Implementation Loop Skill

Use this skill when the user asks an agent to review GitHub issues, monitor issue status, select prioritized work, implement from issue success criteria, run verification, merge completed feature branches when allowed, close completed issues, or create an off-ramp for work that is not efficient to automate.

This workflow is **both agent and skill**:

- The **agent** is the autonomous executor. It reads issues, chooses ready work, edits files, runs commands, merges verified feature branches when allowed, updates and closes issues, and reports results.
- This **skill** is the reusable policy and runbook. It defines selection rules, guardrails, verification, metrics, and off-ramp behavior.

## Core rule

Implement only issues that are ready, scoped, and verifiable.

If an issue is vague, missing acceptance criteria, likely duplicated, blocked, or better suited to planning than execution, invoke the `feature-planning` skill instead of starting implementation.

## Fleet-mode guidance

When this loop runs under `/fleet`, use parallelism only for independent issue work:

- The parent agent coordinates candidate review, priority ordering, and issue assignment.
- Assign at most one ready issue to each subagent invocation unless the user explicitly asks for more.
- Give every subagent a distinct issue number, acceptance criteria, branch name, and verification expectation.
- Do not run multiple subagents on issues that likely touch the same files, runtime state, hardware path, or GitHub artifact.
- If issue ownership, file overlap, branch conflicts, or PR conflicts appear, stop the affected subagents and record a coordination off-ramp.
- Require each subagent to report the issue number, title, branch, merge/close status, files changed, commands run, verification result, unavailable checks, and off-ramp reason when applicable.

## Repository monitoring

When asked to monitor issue status, refresh repository state before selecting, assigning, merging, or closing work:
- Check current issue state, labels, assignees, comments, and existing feature branches.
- Skip issues that are closed, blocked, deferred, assigned to unclear ownership, or already being handled by an active branch unless the user explicitly asks to take over.
- Re-check the selected issue before merging or closing it to ensure the scope, labels, or ownership did not change during implementation.
- In fleet mode, the parent agent owns monitoring and assignment; subagents own only their assigned issue.

## Loop workflow

1. **Review candidate issues**
   - List open issues in the current repository.
   - Exclude closed issues, issues assigned to someone else when ownership is unclear, and issues explicitly marked blocked or deferred.
   - Prefer issues with clear `## Acceptance criteria`, `## Success criteria`, or an equivalent checklist.

2. **Prioritize by labels**
   - Work in this order:
     1. `priority: high`
     2. `priority: medium`
     3. `priority: low`
   - Within the same priority, prefer issues with the clearest acceptance criteria, smallest safe diff, and strongest verification path.
   - Do not invent priority. If priority is missing and the issue is not urgent, use `feature-planning` to refine or label it before implementation.

3. **Plan the implementation**
   - Restate the acceptance criteria as an execution checklist.
   - Identify affected files, tests, docs, and runtime constraints.
   - Create an issue-scoped feature branch, using a worktree when useful for parallel isolation; never work directly on `main`.
   - Keep the planned diff narrow and tied to the issue.

4. **Implement iteratively**
   - Make surgical code or documentation changes that satisfy the checklist.
   - Preserve repository conventions and existing behavior unless the issue explicitly changes behavior.
   - Do not silently skip invalid input, broad errors, failed checks, or unavailable dependencies.

5. **Verify**
   - Run the smallest targeted lint, test, build, or documentation check that covers the change.
   - Add or update tests when behavior changes and a practical test path exists.
   - If live services, hardware, or credentials are required and unavailable, state exactly which check could not run and why.

6. **Merge, close, or hand off**
   - Do not create a pull request by default. Create or prepare a PR only when the user asks for one or repository policy requires one.
   - After verification, merge all issue-scoped branch or worktree branches into `main` only when repository policy, branch protection, and run authorization permit it.
   - If merge is not permitted or cannot be completed, leave the issue open and report the ready branch or worktree plus the exact blocker.
   - Include the completed success-criteria checklist.
   - Include commands run and verification results.
   - Update the issue with an `AI usage` closeout note that reports tokens consumed and AIC/AI Credits consumed for the loop.
   - Use exact usage from available Copilot/session usage telemetry when available; if exact usage is unavailable, do not estimate. State which usage source was checked and that the metric was unavailable.
   - After a successful merge to `main`, create and push a corresponding issue tag so the merged code can be correlated to the issue.
   - Use a safe, lowercase tag name derived from the issue number and title, such as `issue-66-make-misty-recording-tally-light-fallback-configurable-in-laptop-mode`.
   - Create the issue tag before closing the issue.
   - After the issue tag is pushed, remove associated worktrees and delete merged feature branches locally and remotely when permissions allow.
   - Never delete a worktree or feature branch before its changes are merged and the corresponding issue tag exists.
   - After merge, tag, and cleanup steps are complete, close the issue.
   - Note any unavailable checks, residual risks, or manual validation needed.

## When to invoke `feature-planning`

Invoke `feature-planning` instead of implementing when:

- The issue is a new feature idea without acceptance criteria.
- The issue may duplicate existing work.
- The requested behavior has multiple reasonable designs and no clear choice.
- The work requires reprioritization or label cleanup before execution.
- The loop needs to create a human-review issue as an off-ramp.
- A failed implementation attempt reveals that the issue needs a smaller or clearer scope.

After `feature-planning` produces a ready issue with labels and acceptance criteria, the agent may return to this skill for implementation.

## Off-ramp behavior

Use an off-ramp when continuing the loop is less efficient or less safe than human review.

First try to correct agent behavior when the failure is local and recoverable:

- Fix an obviously wrong file target.
- Narrow an overly broad diff.
- Re-run a failed command only after addressing the cause.
- Replace a speculative approach with a repository pattern found in existing code.

Stop and create or comment a human-review issue when any of these occur:

- Acceptance criteria are absent or contradictory.
- The agent has made two unsuccessful implementation attempts for the same issue.
- Verification cannot be run and there is no credible substitute.
- The required change crosses unrelated subsystems.
- The diff is growing beyond the issue scope.
- Required credentials, services, hardware, or external access are unavailable.
- The task is blocked on product, safety, privacy, or architectural decisions.

The off-ramp record must include:

- Issue number and title.
- What was attempted.
- Why the loop stopped.
- Evidence from commands, errors, tests, or code review.
- AI usage: tokens consumed and AIC/AI Credits consumed, or an explicit unavailable note with the source checked.
- Recommended next human decision.

## Loop performance monitoring

Track these metrics for each loop run:

- Issues reviewed.
- Issue selected and priority label.
- Started and completed timestamps.
- Iteration count.
- Files changed.
- Commands run.
- Verification status.
- Merge and issue closure status.
- Issue tag name and push status after merge.
- Worktree removal and merged branch deletion status after the issue tag is pushed.
- Failures and retries.
- Off-ramp reason, when applicable.
- Tokens consumed and AIC/AI Credits consumed, or the usage source checked when exact values are unavailable.

Use these metrics to decide whether to continue, narrow scope, switch to `feature-planning`, or stop for human review.

## Verification guidance

- Prefer targeted tests over full suites when they cover the changed behavior.
- Escalate to broader tests only when targeted checks are insufficient or reveal shared-risk failures.
- Documentation-only changes do not need code tests unless documentation tests exist.
- Hardware-dependent Misty checks should be reported as manual validation when the robot or live services are unavailable.
- Never report success for skipped or unavailable checks; report them as not run with the reason.

## Final response

Report:

- Issue number and title.
- Branch or worktree, merge commit, issue tag, cleanup, or issue closure status, when available.
- Success criteria completed.
- Verification commands and results.
- AI usage issue update status, including tokens and AIC/AI Credits consumed when available.
- Any off-ramp, unavailable check, unavailable merge/tag/close/worktree-cleanup/branch-cleanup step, or manual follow-up.

Keep the response concise and distinguish completed work from planned or blocked work.
