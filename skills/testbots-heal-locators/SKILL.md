---
name: testbots-heal-locators
description: Work out which locators a UI change broke, propose replacements, apply the ones you approve
tools:
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__list_websites
  - mcp__testbots-mcp-server__scan_broken_locators
  - mcp__testbots-mcp-server__heal_locator
  - mcp__testbots-mcp-server__apply_locator_fix
---

## When to use this skill
A test script is failing because a UI element's locator no longer matches anything (a class
name changed, an ID was regenerated, a button got restructured) — the user wants it diagnosed
and fixed instead of hand-editing a selector.

## What to collect before starting
- Nothing required up front — `scan_broken_locators` finds candidates on its own.
- `website_id` (needed by both `heal_locator` and `apply_locator_fix`) — get it from
  `get_context` or `list_websites` if not already known.
- Login credentials, only if the broken locator's page requires being signed in to view.

## Workflow

1. Call `scan_broken_locators` — lists every locator the platform has already flagged as broken.
   If empty, tell the user nothing is currently flagged and stop here.
2. For each broken locator (or the one the user asked about by name), call
   `heal_locator(locator_id, website_id)` — this re-crawls the live page and proposes ranked
   replacement selectors. It changes nothing.
3. If `found` is false or `candidates` is empty: say so plainly — don't guess a selector as a
   substitute. Suggest the page may require login (`credentials`) or the element may have been
   removed entirely.
4. If candidates exist, show the user a clear before/after:
   - Current (broken) strategy vs. the top candidate's `locateBy`/`locatorValue`
   - Its `confidence` score
   - Which element it matched against (`locator_name`)
5. On explicit confirmation, call `apply_locator_fix(locator_id, website_id, chosen_strategy)`
   with the strategy the user approved (default to the top-ranked candidate unless they pick a
   different one from the list).
6. Confirm what changed: the new strategy is now primary, and the old one is kept as a fallback
   — nothing was discarded.

## Rules
- Never call `apply_locator_fix` without the user having seen and approved a specific candidate
  first — this tool proposes, it does not auto-apply.
- Never fabricate a replacement selector outside of what `heal_locator` returns.
- If multiple locators are broken, handle them one at a time so the user reviews each fix
  individually rather than bulk-approving blind.
