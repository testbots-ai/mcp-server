---
name: testbots-run-bot
description: Run a TestBot now and report how it went, step by step
tools:
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__list_bots
  - mcp__testbots-mcp-server__list_grids
  - mcp__testbots-mcp-server__get_grid_capabilities
  - mcp__testbots-mcp-server__list_environments
  - mcp__testbots-mcp-server__get_scripts_for_branch
  - mcp__testbots-mcp-server__execute_bot
  - mcp__testbots-mcp-server__get_job_status
  - mcp__testbots-mcp-server__list_recent_runs
  - mcp__testbots-mcp-server__get_execution_report
  - mcp__testbots-mcp-server__get_page_by_url
  - mcp__testbots-mcp-server__crawl_url
  - mcp__testbots-mcp-server__heal_locator
---

## When to use this skill
The user wants to run a test bot now and see the results.

## What to collect before starting
- Bot name (required) — e.g. "Regression Bot", "Smoke Bot"
- Environment name (optional — if not provided, use the first available environment)
- Branch, if the script under test lives on one (see Rules)

## Workflow

1. Call `get_context` — bots, environments and queue state
2. Find the bot by name from the context (case-insensitive match)
   - If not found: list available bots and ask the user to clarify
3. Call `list_grids` and `get_grid_capabilities` **now**, in this session. Do not reuse a gridId,
   browser or osType remembered from an earlier conversation — grids are per-project and get
   deleted, and a stale one is accepted at submit time and only fails minutes into the run.
4. Call `execute_bot(bot_id, execution_configuration)` where the configuration carries
   `baseUrl` (an ENVIRONMENT ID, not a URL), `browser`, `browserVersion`, `osType`, `gridId`, and
   `targetBranchName` if the script is on a branch
5. Poll `get_job_status(job_id)` — the response gives a **job** id, not an execution id. Widen the
   gaps: ~30s, then 60s, then 120s. A run typically sits ENQUEUED 2-3 minutes and then takes 2-3
   minutes more
6. Once the job leaves PROCESSING, call `list_recent_runs(bot_id)` to get the **executionId**, then
   `get_execution_report(execution_id)`
7. Return a human-readable summary:
   - Bot name, environment, start time, duration
   - Total scripts: X passed, Y failed
   - Each failed script by name, with the step it failed on

## Rules
- Never expose raw IDs to the user — always use names
- If the queue is busy (from context), warn the user before executing
- A job reporting SUCCEEDED means it *ran*, not that the tests passed — only the report says that
- **To run a script that lives on a branch, set `targetBranchName`. Never suggest merging the
  branch into main just to run or verify it** — the run dialog picks any branch directly, and a
  merge is only for making a version permanent
- `execute_bot` runs the last COMMITTED version. If the user just edited the script, confirm the
  edit was committed on that branch (`get_scripts_for_branch`) before running — otherwise the run
  silently tests the previous version and the report looks inexplicable
- **Diagnose before re-running.** Every re-run costs 2-6 minutes. When a run fails, read the
  report's `errorMessage` and the failing step first, and check the live page with
  `get_page_by_url`, `crawl_url` or `heal_locator` — those take seconds. Do not test a theory by
  firing another execution when a cheaper check would confirm or kill it
- When a step fails on timing, find out what the app actually does before picking a duration. A
  flat "wait N seconds" guessed against an unknown redirect chain is what produces a second failed
  run; prefer `Wait for visibility of {{ui-locator}} for {{number}} seconds` (template-id-36),
  which resolves as soon as the destination is ready
- For repeated debugging, reuse ONE debug bot and swap the scripts in its suite. Do not create a
  new suite+bot pair per script or per attempt — they stay in the project forever
