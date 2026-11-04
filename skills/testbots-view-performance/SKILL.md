---
name: testbots-view-performance
description: Read the duration and ROI metrics from a UI TestBot run (not Performance Bot load tests)
tools:
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__list_recent_runs
  - mcp__testbots-mcp-server__get_performance_report
---

## When to use this skill
The user wants duration or ROI/time-saved figures from a UI TestBot run.

Not for Performance Bots (JMeter load tests). Their runs and Smart Summary are
`testbots-performance-bot`.

## Workflow

1. Call `get_context` — get bots list
2. If no job ID provided, call `list_recent_runs(limit=1)` to get latest run
3. Call `get_performance_report(execution_id)` for metrics
4. Return human-readable summary:
   - Execution time per script
   - Pass rate trend vs previous run (if available)
   - ROI metrics if available

## Rules
- Never expose raw IDs
- If no performance data is available, say so clearly
