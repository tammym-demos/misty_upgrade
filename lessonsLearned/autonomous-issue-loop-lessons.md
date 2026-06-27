# Autonomous Issue Loop Lessons - 2026-06-27

## Run summary

- Selected issue: #65, "Optimize laptop wake-word re-arm path by keeping WebSocket alive" (`priority: high`).
- Branch: `issue-65-laptop-fast-rearm`.
- PR: #80, merged to `main`.
- Main tag: `issue-65-pr-80-laptop-fast-rearm`.
- Issue closure: #65 was closed explicitly after merge, tag, cleanup, and AI usage comment.

## What worked

- Preflight caught the important repository gates: authenticated admin user, protected `main`, required CodeQL checks, auto-merge support, and delete-branch-on-merge.
- The PR body linked the issue without closing keywords, which kept issue closeout under agent control.
- Copilot review was useful: it identified two race/health-check problems before merge.
- Re-requesting Copilot review after each fix produced a final review with `0 new` comments.
- Review threads can be resolved via GraphQL after the code changes address them.

## Workflow lessons

- Agent claim comments should avoid escaped PowerShell variables; verify the rendered GitHub comment before continuing.
- For WebSocket fast paths, socket health should include both `ws.sock.connected` and the worker thread being alive.
- Resume wake-word detection only after the controller has transitioned back to `IDLE`, otherwise an immediate detection can be dropped.
- `git diff --check` can flag newly added CRLF lines as trailing whitespace in files already committed with CRLF; verify whitespace after patches.
- AI usage closeout needs a clear unavailable note when the telemetry source exposes tokens but not AIC/AI Credits or fully up-to-date closeout totals.

## Cost and safety guardrail lessons

- Do not start with a hard token or AIC budget. Record a pre-run prediction instead, then compare it with actual telemetry at closeout so future budgets can be based on observed data.
- Usage checkpoints should be recorded after selection/preflight, implementation, PR creation, each Copilot review round, before merge, and at closeout or off-ramp.
- Cached tokens need first-class reporting. The session telemetry exposes cache read/write token fields separately from input/output tokens, and cached tokens can still be paid usage.
- Repeated work is the practical cost danger. The workflow now stops before a third failed implementation attempt or a third Copilot review round with actionable findings.
- Prediction-vs-actual notes should explain why the estimate missed or held, including review churn, broad searches, subagent use, unavailable telemetry, or repeated verification failures.

## Verification used

- `python -m py_compile src/windows-orchestration/misty_controller.py tests/test_integration.py`
- `python -m pytest tests/test_integration.py -k "LaptopFastRearm or LaptopMistyRecordingConfig"` - 7 passed.
- `git diff --check -- src/windows-orchestration/misty_controller.py tests/test_integration.py`
- Required PR checks: `Analyze (python)`, `Analyze (javascript-typescript)`, and `CodeQL` all passed.
