---
description: Run the cloud-safe issue implementation loop for cloud-ready repository issues.
---

# Cloud issue implementation loop

Use `.github/agents/issue-implementation-loop.md`.

Work through open issues labeled `cloud-ready` that can be safely executed in the cloud. Select only issues with clear acceptance criteria and a credible non-hardware verification path.

Prefer documentation, repository policy/automation, and other work that can be verified entirely from the cloud runner.

Open PRs with GitHub closing keywords such as `Closes #123`, request Copilot review, wait for required checks, add AI usage closeout before merge or auto-merge, merge when allowed by branch protection, tag `main`, verify GitHub auto-closed linked issues, and clean up branches/worktrees.

Off-ramp issues that require Misty hardware, Foundry Local, Windows-only audio/SAPI5, product decisions, unclear criteria, or unavailable required verification.
