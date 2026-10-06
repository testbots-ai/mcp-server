---
name: testbots-gen-from-requirements
description: Read a requirements document and write test scripts that cover it (PDF/DOCX/XLSX/CSV/TXT)
tools:
  - mcp__testbots-mcp-server__extract_requirements
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__list_epics
  - mcp__testbots-mcp-server__create_epic
  - mcp__testbots-mcp-server__list_stories
  - mcp__testbots-mcp-server__create_story
  - mcp__testbots-mcp-server__search_step_templates
  - mcp__testbots-mcp-server__get_step_template
  - mcp__testbots-mcp-server__crawl_url
  - mcp__testbots-mcp-server__get_page_by_url
  - mcp__testbots-mcp-server__list_branches
  - mcp__testbots-mcp-server__create_branch
  - mcp__testbots-mcp-server__create_test_script
  - mcp__testbots-mcp-server__create_suite
  - mcp__testbots-mcp-server__add_scripts_to_suite
---

## When to use this skill
The user has a requirements doc, user story export, or spec file and wants TestBots test scripts
generated from it — no live app/URL involved.

## What to collect before starting
- Absolute path to the requirements file (required)
- Which epic/story to attach the scripts to, if any (optional — ask, don't guess)

## Workflow

1. Call `get_context` — load existing epics/bots/suites so generated scripts don't duplicate
   something that already exists.
2. Call `extract_requirements` with the file path.
   - The tool only parses the file — it does not generate test cases. That reasoning happens here.
   - `.pdf`/`.docx`/`.txt`/`.md` return `raw_text` (and `.docx` also returns `sections` by heading).
   - `.csv` returns `rows` (list of dicts keyed by header) — treat each row as one requirement/story.
   - `.xlsx` returns `sheets` (sheet name → rows) — ask the user which sheet if more than one has content.
   - If the result has an `error` key, stop and report it to the user (bad path, unsupported type, or
     file too large) rather than guessing at a fix.

3. Read the extracted content and identify discrete requirements — each row (CSV/XLSX), each heading
   section (DOCX), or each paragraph/numbered item (PDF/TXT) that describes a single piece of
   behavior.

4. For each requirement, derive one or more Given/When/Then test cases:
   - **Given** — preconditions / starting state
   - **When** — the user action being tested
   - **Then** — the expected observable outcome
   - Assign a **priority**: `critical` (core flow, blocks release), `high` (common path), `medium`
     (edge case), `low` (cosmetic/rare). Base it on language cues in the requirement (e.g. "must",
     "shall" → critical/high; "may", "optional" → low) — do not invent a priority scheme per file.

5. Build a **traceability matrix** before creating anything: one row per requirement showing
   `requirement_id/heading → test case name(s) → priority`. Show this to the user for confirmation
   before writing scripts, since requirements → test case mapping is not always 1:1.

6. For each concrete UI action a test case needs, call `search_step_templates` to get the real
   `templateId` AND `templateTitle` — templates are live per-project data (built-ins plus
   org-defined Common Functions), never a fixed list you can assume. Single-word searches work
   much better than full phrases — e.g. "Navigate" only matches back/forward history templates,
   use "Go to"/"Open" to find "Open Web Browser and go to page {{text}}"; "Enter Text" matches
   nothing, use "Enter"; "Assert Text" matches nothing, this platform uses "Verify". Call
   `get_step_template` if it's unclear which placeholder names (`{{...}}` in `templateTitle`) a
   template expects. **Never invent a templateId.**

7. Call `create_test_script` for each derived test case:
   - Name format: "<Requirement ref> — <Scenario>" (e.g. "REQ-12 — Login with invalid password")
   - Steps use the templateIds resolved above. If a requirement needs a `ui-locator` for a live
     page, check `get_page_by_url` for an existing locator first; if none exists and a target URL
     is known, call `crawl_url` on it to capture real locators rather than falling back to a
     placeholder. Only leave a single descriptive placeholder step (flagged as needing manual
     locator/template work) when no concrete UI target or URL is known at all — never guess a raw
     selector as a substitute.
   - Build each step in the exact shape below — it is the proven-working one, and the near-miss
     variants fail in ways that look like success. Each step needs `templateId` +
     `templateTitle` (built-ins only) + a `parameters` array (NOT
     `params`) with one entry per `{{placeholder}}` in `templateTitle` — `{"key": "ui-locator",
     "value": {"locatorId": "<real id>"}, "paramClass":
     "ai.automationhq.commons.entities.assets.UILocator"}` for element targets (never fabricate
     locateBy/locatorValue, the server enriches from the saved locator), and `{"key": "text",
     "value": {"type": 0, "value": "<literal>"}, "paramClass":
     "ai.automationhq.commons.entities.assets.TypeValuePair"}` for scalar values.
   - **For every built-in templateId (`"template-id-N"`), copy that template's `templateTitle`
     string verbatim into the step's `templateTitle` field** (placeholders intact) — the server
     does not look this up itself for built-ins and omitting it causes a 500 error. Not needed for
     Common-Function templateIds (real UUIDs).
   - **`website_id` and `story_id` are both REQUIRED** — `create_test_script` validates this locally
     and rejects the call with a clean error if either is missing, matching
     `automationhq-frontend-v2`'s own create-script form (this is a hard requirement now, not a
     "recommended" field). Pass `website_id` if the requirement maps to a known application.
   - Attach to an epic/story (`story_id`) if the user specified one, or if there's an obviously
     matching one from `get_context`/`list_epics`/`list_stories`. If nothing fits, call
     `create_epic` then `create_story` rather than skipping the field — there is always a way to
     satisfy this now, so don't flag-and-move-on the way this skill used to.
   - `status`/`type` default to `"Not Started"`/`"WEB"` in `create_test_script` — leave them unless
     you have a real reason to change them. If `status` is set to `"To Be Repaired"`, also pass
     `repair_comment` — required in that case only.

8. If more than a few scripts were created, call `create_suite` and `add_scripts_to_suite` to group
   them under one suite named after the source file.

9. Return to the user:
   - The traceability matrix (requirement → test case → priority)
   - Scripts created (count + names)
   - Any requirements skipped and why (ambiguous, no clear UI target, duplicate of existing script)

## Rules
- Never fabricate UI locators that weren't derivable from context — flag those scripts instead of
  guessing (this mirrors the grounding-rules discipline used for `crawl_url`/`testbots-gen-from-url`)
- Never write a raw guessed selector (e.g. `input[type='email']`) into a step instead of a real
  `locatorId` — check `get_page_by_url` first, and if none exists, call `crawl_url` (when a URL is
  known) before writing the step
- Never fabricate a templateId — always resolve it via `search_step_templates`/`get_step_template` first
- Never omit `templateTitle` on a step whose templateId is a built-in (`"template-id-N"`) — causes a 500
- Never put step values in `params` — use `parameters` (a list); `params` does not drive step titles or execution
- Never fabricate `locateBy`/`locatorValue` on a `ui-locator` parameter — pass only `{"locatorId": "..."}` and let the server enrich it
- Never call `create_test_script` without `website_id` and `story_id` — both are validated locally and rejected if missing; resolve or create an epic/story rather than omitting it
- **Ask the user which branch the scripts should land on before creating them** — offer a new branch alongside the real ones from `list_branches`, and pass the answer as `branch_name`. Do not silently default to `main`: it is protected, so a later `commit_branch` returns 403, the edit stays an uncommitted version, and `execute_bot` keeps running the last committed one — the change appears saved but never executes. The same branch is what `execute_bot` needs as `targetBranchName`, so settle it before the bot runs, not after
- Never create a script with 0 steps
- **After any step that can trigger navigation (a submit/sign-in/link click) and before the next
  step that verifies the result, insert a wait step** — `template-id-36` ("Wait for visibility of
  {{ui-locator}} for {{number}} seconds", preferred when a destination-page locator is known) or
  `template-id-35` ("Wait for {{number}} seconds", plain fixed delay ~5-10s otherwise). Skipping
  this causes the verify step to run before the page has navigated and fail: the URL/element
  check is a single immediate read with no retry loop, so it sees the page the click was meant
  to leave.
- **After the step that submits a login, the wait is always 10 seconds** (`template-id-35` with 10,
  or `template-id-36` on a signed-in page element with 10) - many apps take that long to load
  after signing in, and a shorter wait fails the first check on a slow day.
- **A login step needs the real password or a vault secret** (`{"vault": "<secret name>"}`). If the
  user has given neither, type `UPDATE_PASSWORD` in the password step and tell them plainly that the
  password must be updated before the script runs - never a made-up variable, a phrase like "the
  password you gave", or a row of dots. The script-writing tools return `password_warning` when a
  password step has no real password: always pass it on to the user.
- **Check text exactly as `crawl_url` captured it**, not as the user worded it. Icons (+, ×,
  arrows) are not text: a button showing "+ New Test Bot" has the text "New Test Bot", and a check
  for the user's wording fails on a page that is correct.
- **If a crawled page reports `overlays_on_arrival`**, add its `closes_with` step right after that
  page loads (after the login wait, for the first signed-in page) - a panel left open takes every
  click meant for the page behind it.
- A `wait_warning` on a write means a check runs straight after a click: insert the wait before
  running the script, rather than finding it one failed run at a time.
- Script names must be unique — append " (2)", " (3)" if duplicates arise
- Always show the traceability matrix before writing scripts, not just in the final summary
- If the file has an `error` from `extract_requirements`, do not retry — report it to the user
