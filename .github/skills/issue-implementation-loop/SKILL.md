---
name: issue-implementation-loop
description: Use when asked to run or define an agent loop that reviews GitHub issues, prioritizes by labels, implements ready work, verifies changes, and off-ramps inefficient loops.
---

# Issue Implementation Loop Skill

Use this skill when the user asks an agent to review GitHub issues, select prioritized work, implement from issue success criteria, run verification, monitor loop performance, or create an off-ramp for work that is not efficient to automate.

This workflow is **both agent and skill**:

- The **agent** is the autonomous executor. It reads issues, chooses ready work, edits files, runs commands, opens PRs, and reports results.
- This **skill** is the reusable policy and runbook. It defines selection rules, guardrails, verification, metrics, and off-ramp behavior.

## Core rule

Implement only issues that are ready, scoped, and verifiable.

If an issue is vague, missing acceptance criteria, likely duplicated, blocked, or better suited to planning than execution, invoke the `feature-planning` skill instead of starting implementation.

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
   - Create a feature branch; never work directly on `main`.
   - Keep the planned diff narrow and tied to the issue.

4. **Implement iteratively**
   - Make surgical code or documentation changes that satisfy the checklist.
   - Preserve repository conventions and existing behavior unless the issue explicitly changes behavior.
   - Do not silently skip invalid input, broad errors, failed checks, or unavailable dependencies.

5. **Verify**
   - Run the smallest targeted lint, test, build, or documentation check that covers the change.
   - Add or update tests when behavior changes and a practical test path exists.
   - If live services, hardware, or credentials are required and unavailable, state exactly which check could not run and why.

6. **Report and hand off**
   - Open or prepare a PR that links the issue.
   - Include the completed success-criteria checklist.
   - Include commands run and verification results.
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
- Failures and retries.
- Off-ramp reason, when applicable.

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
- Branch or PR, when available.
- Success criteria completed.
- Verification commands and results.
- Any off-ramp, unavailable check, or manual follow-up.

Keep the response concise and distinguish completed work from planned or blocked work.
