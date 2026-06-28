# Cloud Autonomous Issue Loop Limits

## Summary

The goal of an autonomous loop that works through every GitHub issue until completion is not realistic for this repository. It is still useful, but it needs a hybrid model: cloud agents should complete issues that are clearly scoped and cloud-verifiable, while Misty-specific runtime and hardware work should be off-ramped for local validation.

## What we learned

- A fully autonomous loop over all open issues is unsafe because many issues require resources that cloud agents cannot access.
- The user's laptop should not be the default execution environment for every issue because long-running local automation can create heat and resource pressure.
- Cloud agents are best for documentation, repository policy, prompt/agent workflow updates, pure Python checks, and other changes that can be verified without live hardware.
- Cloud agents are not enough for issues that require Misty hardware, Foundry Local, live model execution, Windows-only audio behavior, SAPI5 TTS, laptop microphone behavior, webcam validation, or local network access to the robot.
- Vague issues or feature ideas should be refined into acceptance criteria before implementation begins.

## Recommended operating model

Use a hybrid autonomous issue loop:

1. Label issues that are safe for cloud automation as `cloud-ready`.
2. Require clear acceptance or success criteria before an issue enters the run queue.
3. Require a credible verification path that avoids Misty hardware, Foundry Local, Windows-only audio/SAPI5 behavior, and unavailable local services.
4. Let cloud agents implement, verify, open PRs, request Copilot review, merge when allowed, tag `main`, and close only those cloud-safe issues.
5. Off-ramp hardware-dependent or runtime-dependent issues with a clear note that names the missing validation environment and the local check required.
6. Reserve the laptop for Misty-specific validation rather than broad issue-loop execution.

## Practical decision table

| Issue type | Automation fit | Expected handling |
|---|---|---|
| Documentation, lessons learned, prompts, agent instructions, repo policy | Good | Cloud agent can implement and verify with `git diff --check` or GitHub CLI/API inspection |
| Pure Python logic with narrow tests and no live service dependency | Good | Cloud agent can implement and run targeted compile/test checks |
| Misty REST/WebSocket behavior requiring robot validation | Limited | Implement only if a credible non-hardware test exists; otherwise off-ramp for local Misty validation |
| Foundry Local, STT, LLM, TTS, audio, SAPI5, webcam, or laptop microphone behavior | Poor | Off-ramp unless the issue defines a cloud-safe substitute test |
| Broad feature ideas without acceptance criteria | Poor | Convert to a planning issue before implementation |

## Key guardrail

The loop should optimize for completed, verified issues rather than raw throughput. If verification cannot be performed credibly in the cloud, the correct autonomous outcome is an off-ramp with evidence and next local validation steps, not a merged change that only appears complete.
