# Reference facts for Misty

Last verified: **2026-10-21**, against official product documentation.

## Core facts

GitHub Copilot is an AI coding assistant for suggestions, chat, explanations, debugging, tests and code review. GitHub Copilot also helped update Misty's functionality. [S1; local project fact]

GitHub Copilot CLI is a coding agent in your terminal: ask questions, edit files and debug code. Other interfaces include supported editors such as VS Code and Visual Studio, and GitHub.com. [S2, S4]

GitHub Copilot agents can plan changes, edit code and run tools. Cloud agents can prepare pull requests for human review. Custom instructions and MCP connections can provide project context and tools. [S1, S4]

GitHub Copilot can make mistakes. Review and test generated code. Features vary by plan, client and policy. Check GitHub for current prices, costs, usage limits, models and availability; do not promise flawless or secure code. [S1, S3]

## Short conversation examples

**What is it?** "Copilot helped upgrade what I can do! It helps with code, tests and reviews; you still check its work."

**What would I try first?** "Ask it to explain a small piece of code or draft a test, then review the result." [S1, S3]

**What are agents?** "Give an agent a task, and it can work through changes and prepare them for your review." [S1]

**What is the CLI?** "It's Copilot in your terminal, where it can help with project changes and GitHub tasks." [S4]

**Is it free?** GitHub offers a free option and paid plans. Exact allowances, prices and feature access can change; check the official access guidance rather than quoting numbers from memory. [S1]

**What about company code?** Follow your organization's policies and approved tools. Do not invent promises about privacy, training, retention, permissions or security. [S1, S3]

These are factual examples, not a greeting script. Keep answers brief and in
Misty's existing personality. Ask one question at a time and do not repeat
questions already answered. A visitor's unrelated question is not a sales pitch.

## Local project fact

The owner-approved backstory includes that GitHub Copilot helped update Misty's
functionality. Briefly acknowledge that contribution when answering a question
about it, then answer the actual question. This fact comes from project history,
not the external sources below. It does not mean Copilot runs her live
conversations; the existing local chat model is unchanged.

## Official sources

- [S1] Overview and access: https://docs.github.com/en/copilot/get-started/about-github-copilot
- [S2] Supported places to work: https://docs.github.com/en/copilot/get-started/where-to-use-github-copilot
- [S3] Responsible use and limitations: https://docs.github.com/en/copilot/responsible-use/chat
- [S4] Terminal capabilities and customization: https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-cli

## Maintenance and runtime use

The existing local grounding reader selects at most four matching lines and
clips each to 240 characters. Keep the four core facts self-contained, below
that limit, and before examples and source links. Keep this file first in
`GROUNDING_SOURCES` so general repository mentions do not displace the facts.
An explicit environment override replaces the default source list.

This file is a dated reference, not model training or a live web connection.
To update it, recheck the official sources, revise facts and the verification
date, and keep the core facts short. The reader picks up saved text on the next
matching request; source-list configuration changes require restarting the
orchestration service. No model download or retraining is needed.
