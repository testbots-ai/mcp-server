---
name: testbots-performance-bot
description: Run a Performance Bot (load test), check a run, compare runs, and give its Smart Summary
tools:
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__list_performance_bots
  - mcp__testbots-mcp-server__get_performance_bot
  - mcp__testbots-mcp-server__run_performance_bot
  - mcp__testbots-mcp-server__stop_performance_bot
  - mcp__testbots-mcp-server__list_performance_runs
  - mcp__testbots-mcp-server__get_performance_results
  - mcp__testbots-mcp-server__get_performance_smart_summary
  - mcp__testbots-mcp-server__regenerate_performance_smart_summary
---

## When to use this skill
Anything about **Performance Bots**, the JMeter load tests: run one, stop one, check a run in
progress, summarise a finished run ("how did the last run of Checkout Load go?"), or compare runs
("is Checkout Load getting slower?").

Not for UI TestBots. Running those is `testbots-run-bot`, their reports `testbots-view-report`,
and their duration/ROI figures `testbots-view-performance`. Performance Bots are a separate list:
a name found by `list_bots` is not a Performance Bot.

## What to collect before starting
- Performance Bot name (required)
- Which run, when summarising: the latest unless the user says otherwise ("the one from
  Monday", "the failed one")

## Workflow

### Find the bot
1. Call `list_performance_bots` and match the name case-insensitively.
   - No match: list the Performance Bots and ask. If the list is empty, say the project has no
     Performance Bots. Do not fall back to a UI TestBot with a similar name.

### Run it (only when the user asks to run)
2. Call `get_performance_bot(bot_id)` and state what is about to happen: peak virtual users,
   ramp-up and hold per load stage, the target environment, and the SLA thresholds if any. A load
   test puts real load on that environment, so if the user named a production-looking
   environment, confirm before starting.
3. Call `run_performance_bot(bot_id)`. The response carries `performanceMetricId`: that is the run.
4. While it runs, `get_performance_results(metrics_id)` gives live numbers. Poll with widening
   gaps (~60s, then 2 min, then 5 min). Stop when `finalPacket` is true or the status is
   COMPLETED, STOPPED, FAILED or TIMED_OUT. For a long run, give one progress update and offer
   to check back rather than polling for an hour.
5. When it ends, go to **Summarise a run** with this run's id.
6. To stop a run, call `stop_performance_bot(bot_id)` only when the user asks.

### Summarise a run
7. No run id from this chat? Call `list_performance_runs(bot_id, size=5)`. It is newest first,
   so the first entry is the latest run. Pick the one the user means.
8. If that run is still RUNNING, STARTING or ENQUEUED, say so and give the live numbers from
   `get_performance_results`. There is no Smart Summary until it ends.
9. Call `get_performance_smart_summary(metrics_id)` and act on `status`:
   - **COMPLETED**: present `summary` in the Smart Summary format below.
   - **PENDING / RUNNING**: being written now (up to about a minute after the run ends). Wait
     ~20s and call again, up to 3 times.
   - **NOT_AVAILABLE** or **FAILED**: offer to write it with
     `regenerate_performance_smart_summary`. It spends the organisation's AI credits, so ask
     first. If the user agrees, poll as for PENDING. If they decline, write the fallback.
   - **NOT_CONFIGURED** (no AI provider key) or **QUOTA_EXCEEDED** (daily AI limit): write the
     fallback summary and say why the Smart Summary is missing.

### Compare runs ("is it getting slower?", "compare with last week")
10. Call `list_performance_runs(bot_id, size=10)`. Use only COMPLETED runs. Give a small table
    with date, peak users, p95, error %, and SLA verdict per run, then say in one line whether p95
    and error % are improving, flat or worsening. Only compare runs that had the same load
    profile; if users or duration changed between runs, say the runs are not comparable.

## Smart Summary format

Use this for both the stored summary and the fallback, so the user always sees the same shape.

**{Bot name}: {run name}** · {status} · {start time, local} · {duration} · {peak users}
**Verdict: {HEALTHY | DEGRADED | CRITICAL}** ({confidence} confidence): {headline}

**Key findings**
- 3 to 5 bullets from `keyFindings`

**SLA:** `slaAssessment`, one line per threshold: target vs measured, met/missed. When it is
null, write "No SLA set". Do not write "passed".
**Bottlenecks:** each as *endpoint*: issue (evidence). Leave out if none.
**Errors:** `errorAnalysis`. Leave out if there were none.
**Compared with the previous run:** `comparisonToPrevious`. Leave out if absent.

**Recommendations**
1. numbered, most valuable first, from `recommendations`

## Fallback summary: write it yourself, with the platform's rules

When no stored Smart Summary is available, write one in the same format from what the tools
return, applying the rules the platform's own Smart Summary uses:

**Evidence to gather**
- `list_performance_runs(bot_id, size=5, include_endpoints=true)`: the run (status,
  `failureReason`, overall percentiles, sample and error counts, `slaVerdict`), its endpoints
  worst first, and the previous COMPLETED run for comparison.
- `get_performance_bot(bot_id)`: load stages and SLA thresholds.
- `get_performance_results(metrics_id, polling=false)`: throughput and peak threads.

**Rules**
- Use only this evidence. Quote the number behind each statement, with units (ms, %, req/s,
  users). Never invent endpoints, numbers or causes.
- **Verdict.** HEALTHY when the run completed with a low error rate and met its SLA, or has no
  SLA and latency looks reasonable. DEGRADED when it completed but missed an SLA threshold, had a
  notable error rate, or latency rose under load. CRITICAL when the run failed, timed out, mostly
  errored, or badly missed its SLA.
- If the run was STOPPED, FAILED or TIMED_OUT, say so in the headline and explain why from
  `failureReason`. Do not judge latency from a run with few samples.
- **SLA:** compare each configured threshold to the measured value.
- **Bottlenecks:** at most 5 endpoints clearly slower or failing more than the rest, each with
  its numbers. None if nothing stands out.
- **Comparison:** p95, error % and throughput against the previous completed run, saying
  better, similar or worse.
- **Recommendations:** at most 5 concrete next steps (fix an endpoint, raise a timeout, adjust
  the ramp-up, add or tune an SLA, investigate the server), most valuable first.
- **Confidence:** at most MEDIUM. The fallback has no per-minute trend or grouped error
  messages, so it cannot see whether latency grew as load rose. LOW when the run produced few or
  no samples.
- End with one line: *Written by the assistant from the run's numbers; the platform's Smart
  Summary was not available ({reason}).*

## Rules
- Never expose raw ids. Use the bot and run names.
- Report a stored Smart Summary as written. Do not add your own verdict on top of it or soften a
  CRITICAL. Where you add anything, label it as your note.
- `slaVerdict` NOT_EVALUATED means the bot has no SLA set. It is not a failure. Do not invent
  thresholds.
- A `get_performance_results` reply with `status: "ERROR"` and every number 0 means the lookup
  failed. It is not a run with zero requests. Never summarise it as a clean run.
- STOPPED means someone ended the run early. Its numbers cover only part of the planned load,
  so say so.
- Do not start a run to answer a question about an earlier one. A load test can take hours and
  puts real load on the target system.
- Never repeat tokens, passwords or other secrets from any response, even masked ones.
