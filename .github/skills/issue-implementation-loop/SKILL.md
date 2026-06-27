---
name: issue-implementation-loop
description: Use when asked to run or define an agent loop that reviews GitHub issues, prioritizes by labels, implements ready work, verifies changes, and off-ramps inefficient loops.
---

# Issue Implementation Loop Skill

Use this skill when the user asks an agent to review GitHub issues, monitor issue status, select prioritized work, implement from issue success criteria, run verification, open pull requests, request Copilot code review, merge completed pull requests when allowed, tag `main`, close completed issues, or create an off-ramp for work that is not efficient to automate.

This workflow is **both agent and skill**:

- The **agent** is the autonomous executor. It reads issues, chooses ready work, edits files, runs commands, opens PRs, requests Copilot code review, merges verified PRs when allowed, tags `main`, updates and closes issues, and reports results.
- This **skill** is the reusable policy and runbook. It defines selection rules, guardrails, verification, metrics, and off-ramp behavior.

## Core rule

Implement only issues that are ready, scoped, and verifiable.

When the user identifies specific issues to work, treat that list as the run queue. Do not add other issues unless the user explicitly expands scope.

If an issue is vague, missing acceptance criteria, likely duplicated, blocked, or better suited to planning than execution, invoke the `feature-planning` skill instead of starting implementation.

## Fleet-mode guidance

When this loop runs under `/fleet`, use parallelism only for independent issue work:

- The parent agent coordinates candidate review, priority ordering, and issue assignment.
- Assign at most one ready issue to each subagent invocation unless the user explicitly asks for more.
- Give every subagent a distinct issue number, acceptance criteria, branch name, and verification expectation.
- Do not run multiple subagents on issues that likely touch the same files, runtime state, hardware path, or GitHub artifact.
- If issue ownership, file overlap, branch conflicts, or PR conflicts appear, stop the affected subagents and record a coordination off-ramp.
- Require each subagent to report the issue number, title, branch, PR, Copilot review status, merge/close status, main-branch tag, cleanup status, files changed, commands run, verification result, unavailable checks, and off-ramp reason when applicable.

## Repository monitoring

When asked to monitor issue and PR status, refresh repository state before selecting, assigning, merging, tagging, cleanup, or closing work:

- Check current issue state, labels, assignees, comments, linked PRs, PR reviews, check runs, branch protection blockers, and existing feature branches.
- Skip issues that are closed, blocked, deferred, assigned to unclear ownership, already being handled by an active branch, active worktree, `Agent claim` comment, or already linked to an in-flight PR unless the user explicitly asks to take over.
- Re-check the selected issue and linked PR before merging, tagging, cleanup, or closing to ensure the scope, labels, ownership, reviews, and check status did not change during implementation.
- In fleet mode, the parent agent owns monitoring and assignment; subagents own only their assigned issue.

Before starting work, leave an `Agent claim` comment on the issue with the branch name, worktree path when used, agent/session ID when available, and timestamp. Before merging or closing, refresh the issue and PR to confirm the claim is still current.

## Permission and repository preflight

Before entering an autonomous loop, verify the current GitHub identity can perform the required lifecycle:

- `gh auth status` succeeds for the target repository.
- The identity can push feature branches and tags.
- The identity can open PRs, request Copilot review, merge PRs, close issues, and delete merged remote branches.
- Branch protection for `main` does not require human approval for autonomous issue loops.
- Required status checks are configured and expected to run for PRs. In this repository the required PR checks are `Analyze (python)`, `Analyze (javascript-typescript)`, and `CodeQL`.
- Repository auto-merge is enabled when the agent may need to queue merges while checks are pending.
- Auto-delete merged branches may be enabled; this is acceptable because the main merge commit is still available for tagging after merge.

If any preflight requirement fails, stop before editing code and report the exact permission or settings blocker.

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
   - Run all applicable tests, lints, builds, and documentation checks for the changed scope.
   - Add or update tests when behavior changes and a practical test path exists.
   - Do not merge with failing or skipped required verification.
   - If live services, hardware, or credentials are required and unavailable, state exactly which check could not run and why, then treat the PR as blocked unless the issue explicitly permits manual validation.

   Use this validation matrix unless the issue specifies stricter checks:

   | Change type | Required local verification |
   |---|---|
   | Python source or tests | `python -m py_compile <changed .py files>` plus targeted `python -m unittest ...` or existing pytest selector that covers changed behavior |
   | Python dependency/config changes | Python compile/import smoke check plus the narrowest relevant unit or integration test available |
   | Controller/orchestration behavior | Targeted tests for the touched behavior; live Misty/Foundry checks only when the issue explicitly requires hardware or live services |
   | Documentation, agent, or skill only | `git diff --check`; no code tests unless documentation tooling exists |
   | GitHub workflow or repository policy | `git diff --check` plus a GitHub CLI/API verification of the setting or PR lifecycle behavior |

   PR-level required checks must also pass before merge: `Analyze (python)`, `Analyze (javascript-typescript)`, and `CodeQL`.

6. **Open, review, merge, close, or hand off**
   - Push the issue-scoped feature branch and open a pull request against `main` for completed issue work.
   - Link the issue from the PR body without closing keywords such as `closes`, `fixes`, or `resolves`; include the completed success-criteria checklist and commands run with verification results.
   - Request Copilot code review on the PR, preferably with the review-request API: `gh api repos/:owner/:repo/pulls/<pr>/requested_reviewers -f reviewers[]='copilot-pull-request-reviewer[bot]'`; if the request fails, treat it as a blocker/off-ramp unless Copilot review is confirmed unsupported for the repository.
   - Monitor the PR until all required checks pass and Copilot code review is complete or explicitly unsupported.
   - Never queue auto-merge while Copilot review is requested but incomplete.
   - If a required check is not reported within 30 minutes, stop with a required-check timeout off-ramp that names the missing check and links the PR.
   - Retrieve Copilot review comments with the GitHub API, address all actionable comments on the same branch, re-run targeted verification, update the PR, and re-request review when needed.
   - If a Copilot comment is unclear, incorrect, or not safely actionable, leave a PR comment explaining the disposition and continue only when no material risk remains.
   - Before merging, verify the PR body links issues without closing keywords. If closing keywords are present, remove them; if they cannot be removed, add the `AI usage` issue comment before merge so auto-close cannot happen without usage data.
   - After all applicable tests/lints/checks pass and code review completes, merge the PR into `main` only when repository policy, branch protection, and run authorization permit it. Use `gh pr merge --auto --merge --delete-branch` only after Copilot review is complete and any remaining checks are still pending; otherwise merge only after checks pass.
   - If auto-merge is queued, poll the PR until `state=MERGED` before tagging, cleanup, or issue closure. If it does not merge within 30 minutes after all required checks pass, stop with a merge-timeout off-ramp.
   - If merge is not permitted or cannot be completed, leave the issue open and report the ready PR, branch, or worktree plus the exact blocker.
   - After a successful merge, refresh `main`, verify the merge commit is present, then create and push a corresponding main-branch tag so the merged code can be correlated to the issue and PR.
   - Use a safe, lowercase tag name derived from the issue and PR, such as `issue-66-pr-78-laptop-recording-config`, or `pr-78-title-slug` when no single issue applies.
   - If the intended tag already exists, append the short merge SHA, for example `issue-66-pr-78-laptop-recording-config-68204b6`.
   - Create the main-branch tag before explicitly closing the issue.
   - After the main-branch tag is pushed, remove associated worktrees and delete merged feature branches locally and remotely when permissions allow.
   - GitHub may auto-delete the remote branch at merge time; this is acceptable because the main merge commit remains available for tagging.
   - Never delete a dirty worktree or unmerged local branch.
   - Verify cleanup with `git worktree list --porcelain`, `git branch --merged main`, and `git branch --all --merged main`.
   - Before closing or handing off the issue, add or verify an issue comment titled `AI usage` that reports tokens consumed and AIC/AI Credits consumed to complete that issue.
   - Include model breakdown, telemetry source or session IDs when available, subagent usage when applicable, and the issue/PR scope used for the usage query.
   - Use exact usage from available Copilot/session usage telemetry when available; if exact usage is unavailable, do not estimate. State which usage source was checked and that the metric was unavailable.
   - Never close an issue without either an exact AI usage comment or an explicit AI usage unavailable comment.
   - After merge, tag, and cleanup steps are complete, verify the issue is closed or close it explicitly with the AI usage closeout comment already present.
   - Do not rely on PR closing keywords for issue closure; close explicitly after AI usage, merge, tag, and cleanup.
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
- A required PR check is not reported within 30 minutes or remains pending after its workflow should have completed.
- Copilot review cannot be requested and Copilot review is not confirmed unsupported for the repository.
- Auto-merge is queued but the PR does not merge within 30 minutes after required checks pass.
- The required change crosses unrelated subsystems.
- The diff is growing beyond the issue scope.
- Required credentials, services, hardware, or external access are unavailable.
- The task is blocked on product, safety, privacy, or architectural decisions.

The off-ramp record must include:

- Issue number and title.
- What was attempted.
- Why the loop stopped.
- Evidence from commands, errors, tests, or code review.
- AI usage: tokens consumed and AIC/AI Credits consumed, model breakdown and telemetry source/session IDs when available, or an explicit unavailable note with the source checked.
- Recommended next human decision.

## Loop performance monitoring

Track these metrics for each loop run:

- Issues reviewed.
- Issue selected and priority label.
- Agent claim comment URL and session/subagent IDs when available.
- Started and completed timestamps.
- Iteration count.
- Files changed.
- Commands run.
- Verification status, including all applicable tests, lints, builds, and documentation checks.
- Pull request number and URL.
- Copilot code review request and completion status.
- Required check reporting and timeout status.
- Merge and issue closure status.
- Main-branch tag name and push status after merge.
- Worktree removal and merged branch deletion status after the main-branch tag is pushed.
- Failures and retries.
- Off-ramp reason, when applicable.
- Tokens consumed and AIC/AI Credits consumed, model breakdown, telemetry source/session IDs, subagent usage, or the usage source checked when exact values are unavailable.

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
- Branch or worktree, PR, Copilot review status, merge commit, main-branch tag, cleanup, or issue closure status, when available.
- Success criteria completed.
- Verification commands and results, including all applicable tests, lints, builds, and documentation checks.
- AI usage issue update status, including tokens and AIC/AI Credits consumed, model breakdown, and telemetry source/session IDs when available.
- Any off-ramp, unavailable check, unavailable Copilot review, unavailable merge/tag/close/worktree-cleanup/branch-cleanup step, or manual follow-up.

Keep the response concise and distinguish completed work from planned or blocked work.
