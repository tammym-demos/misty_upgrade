---
name: feature-planning
description: Use when asked to plan a future feature, create a GitHub issue from a plan, label or prioritize a feature request, or explicitly avoid immediate implementation.
---

# Feature Planning Skill

Use this skill when the user asks to plan a future feature, create a GitHub issue for later implementation, label or prioritize a feature request, or explicitly says not to implement yet.

Common trigger phrases include:

- "plan a feature"
- "new feature capability"
- "create an issue for later implementation"
- "generate a GitHub issue from this plan"
- "do not execute"
- "do not implement"
- "not immediately execute"

## Core rule

Separate planning from implementation.

If the user asks for a plan or issue only, do not change application source code, tests, docs, assets, or runtime behavior beyond the requested planning or GitHub issue artifact. Create the requested plan/issue, label it appropriately, report the result, and stop.

Only start implementation when the user explicitly asks to implement, start, execute, or get to work on the feature.

## Planning workflow

1. **Review context**
   - Read the relevant source files, docs, existing issues, and recent related user requests.
   - If the request references an external article, video, repo, or docs page, inspect it enough to understand the implementation pattern and constraints.
   - Check whether an existing issue already covers the feature.

2. **Clarify only when necessary**
   - Ask a question only when feature scope, behavior, or target architecture is genuinely ambiguous.
   - If the user has provided enough context, make a reasonable engineering decision and proceed.

3. **Draft a concise feature issue**
   - Include these sections when applicable:
     - `## Summary`
     - `## Current state`
     - `## Proposed approach`
     - `## Risks / constraints`
     - `## Acceptance criteria`
     - `## Related issues`
   - Keep the issue specific enough for later implementation.
   - If the feature was inspired by an external reference, describe what can be reused as a pattern and what should not be copied.

4. **Respect no-execution requests**
   - When the user says not to execute or not to implement, create only the issue or plan artifact requested.
   - Explicitly state in the final response that no implementation work was performed.

## Label and priority guidance

Use `enhancement` for new feature capability issues.

Apply exactly one priority label when possible:

- `priority: high` — demo-readiness, reliability, safety, wake-word/audio blockers, or major user-facing issues.
- `priority: medium` — meaningful product polish, maintainability, validation spikes, or useful feature work that is not blocking.
- `priority: low` — deferred, conditional, exploratory, or non-blocking improvements.

Preserve existing non-priority labels unless the request explicitly asks to reprioritize or relabel broader backlog items.

## GitHub issue discipline

- Before creating an issue, search for obvious duplicates by title and topic.
- If a duplicate exists, update or reference the existing issue instead of creating a new one.
- Link related current-repository issues using `#<number>`.
- For repositories outside this one, use fully qualified references like `owner/repo#123`.
- Keep issue titles action-oriented and concise.

## Final response

After creating or updating an issue, report:

- issue number and title
- issue URL when available
- labels applied
- whether any implementation work was intentionally not performed

Keep the final response concise.
