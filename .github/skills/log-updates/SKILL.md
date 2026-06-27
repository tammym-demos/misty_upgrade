---
name: log-updates
description: Use when asked to create or update repository daily logs, record request history, summarize AI/Copilot usage, or capture AIC/AI Credits usage.
---

# Log Updates Skill

Use this skill whenever the user asks to create, update, fix, or review repository logs, especially prompts mentioning logs, daily logs, request history, AI/Copilot usage, AIC, AI Credits, or usage metrics.

## Required output file

- Store logs in `logs\`.
- Use one Markdown file per local calendar day when repository updates are made.
- Name files with this exact pattern:

```text
logs\log_YYYY-MM-DD.md
```

- Use the current date from the user-provided `current_datetime` when available. Otherwise use the host date.
- If the day's file already exists, update it in place. Do not create multiple logs for the same date.

## Required sections

Every daily log must include:

1. `## Requests`
   - List the user's requests for the day in chronological order.
   - Include repo-affecting requests and related operational requests that influenced repo changes.

2. `## AI/Copilot usage`
   - Summarize the work Copilot performed.
   - Include concrete tool or service usage when relevant, such as file edits, GitHub issue creation, GitHub CLI installation, web/doc lookups, and tests.

3. `## AIC usage snapshot`
   - Record quantitative AI Credits/AIC usage, not just a prose summary.
   - Prefer current-session AIC usage if available from `/usage` or session history.
   - If current-session usage is not available, record the best available scope, such as day-to-date usage from session history.
   - Include:
     - timestamp of the snapshot
     - scope, for example `current session`, `day-to-date`, or `month-to-date`
     - source, for example `/usage` or `session_store_sql events.usage_cost`
     - total AICs
     - model-level breakdown when available
   - If AIC usage cannot be retrieved, explicitly write `AIC usage unavailable` and the reason. Do not omit this section.

4. `## Repository updates`
   - List files changed, issues/PRs created, labels created, or other persistent repo/GitHub changes.

5. `## Logging convention`
   - Keep or add the convention:

```text
logs\log_YYYY-MM-DD.md
```

## AIC usage guidance

- Do not invent AIC numbers.
- If a session store query tool is available, query usage data from events with non-null `usage_model` and sum `usage_cost`.
- When querying by date, respect the timestamp timezone. If the user's `current_datetime` includes an offset, convert the local day window to the timestamp storage timezone if needed.
- If only a day-to-date or month-to-date total is available, state that scope clearly.
- If the user asks specifically for "at the time the log update was requested," capture the closest available snapshot and state the timestamp used.

## Update discipline

- Update the daily log whenever repository files are changed in response to the user.
- If the user explicitly asks for a log update and no repo files changed, still update the daily log with the request and AIC snapshot.
- Keep entries concise and factual.
