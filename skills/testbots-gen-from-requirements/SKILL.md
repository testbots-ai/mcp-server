---
name: testbots-gen-from-requirements
description: Read a requirements document and write test scripts that cover it (PDF/DOCX/XLSX/CSV/TXT), grounded in the live app when the document names one — into AHQ, or as a portable self-contained bundle
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
  - mcp__testbots-mcp-server__list_websites
  - mcp__testbots-mcp-server__create_website
  - mcp__testbots-mcp-server__create_page
  - mcp__testbots-mcp-server__add_locators
  - mcp__testbots-mcp-server__get_page_by_url
  - mcp__testbots-mcp-server__list_branches
  - mcp__testbots-mcp-server__create_branch
  - mcp__testbots-mcp-server__create_test_script
  - mcp__testbots-mcp-server__create_suite
  - mcp__testbots-mcp-server__add_scripts_to_suite
---

## When to use this skill
The user has a requirements doc, user story export, or spec file and wants TestBots test scripts
generated from it. A live URL is not required — but when the document names one (or the user gives
one), ground the test cases in the real app instead of the document's wording alone.

## What to collect before starting
- Absolute path to the requirements file (required)
- **Destination — ask before doing anything else, since it changes every step from here**: save
  the scripts into a real AHQ project (needs `website_id`/`story_id`/a branch, and persists real
  Website/Page/Locator/TestScript records), or return a **portable bundle** — one self-contained
  JSON document with no AHQ writes at all, built for a caller (e.g. a testbots.ai customer) who
  has no AHQ project to write into yet, but wants enough structure to migrate into one later if
  they choose to. Never guess this from context — a connected org token does not imply the user
  wants data persisted into it.
- Target URL — look for one in the document first (a link, a "staging/prod/app URL" field, a
  header). Ask the user only if the document doesn't name one; don't skip testing a live app just
  because asking feels like friction
- If a URL is in play: does reaching the testable content require signing in? Look for a "test
  account"/"credentials" section in the document first. If it's genuinely unclear, ask — never
  guess a username or invent a password. A vault secret reference (`{"vault": "<secret name>"}`) is
  a fine answer too
- How many test cases the user wants, if they said a number ("just the critical ones", "5
  scenarios", "one per requirement"). Honor it exactly later — don't let the requirement count
  silently override an explicit ask
- Which epic/story to attach the scripts to, if any (optional — ask, don't guess)

## Workflow

1. Call `get_context` — load existing epics/bots/suites/websites so generated scripts don't
   duplicate something that already exists.

2. Call `extract_requirements` with the file path.
   - The tool only parses the file — it does not generate test cases. That reasoning happens here.
   - `.pdf`/`.docx`/`.txt`/`.md` return `raw_text` (and `.docx` also returns `sections` by heading).
   - `.csv` returns `rows` (list of dicts keyed by header) — treat each row as one requirement/story.
   - `.xlsx` returns `sheets` (sheet name → rows) — ask the user which sheet if more than one has content.
   - If the result has an `error` key, stop and report it to the user (bad path, unsupported type, or
     file too large) rather than guessing at a fix.

3. Read the extracted text for the collection items above: a URL, login/credential mentions, and
   an explicit test-case count. Fill in from the user only what the document leaves unclear.

4. **If a URL is in play, call `crawl_url(url, credentials)` before designing any test case** — this
   grounds every page, module, and locator that follows in the real app instead of the document's
   wording.
   - No credentials given: the crawl returns only what's reachable pre-login. Say so plainly if the
     document implies an authenticated area exists behind it.
   - Credentials given: check the returned `auth` block. `auth.succeeded: false` means the sign-in
     didn't go through — `crawl_url` only drives a single-step form (one page, email + password,
     one submit) and does not handle a multi-step or SSO-only login. **Never present the pre-login
     pages as if they were the full application** in that case; say the login failed and why, the
     same caution `testbots-test-architecture` uses for this exact limitation.
   - No URL at all: skip to step 6 and derive test cases from the document alone, as this skill
     always has.

5. **Slice the application into Pages, Modules, Features, and Workflows** (only when step 4 ran):
   - **Pages** — each page `crawl_url` captured, by title and URL.
   - **Modules** — pages clustered by shared nav-label prefix or URL path segment (e.g. everything
     under `/admin/*`), the same clustering `testbots-test-architecture` uses. `crawl_url`'s
     `menus_expanded` count is what makes a collapsed sidebar's modules visible at all — a page
     captured before menu expansion only shows what was already open.
   - **Features** — the concrete forms/CRUD affordances/interactive elements each module's pages
     actually have, grounded in the crawl, not inferred from the module's name.
   - **Workflows** — the multi-page sequences the requirements document actually describes (e.g.
     "sign up → verify email → complete profile"), matched against the pages/modules that realize
     each step. A workflow step the crawl can't place on a real page stays in the workflow, just
     flagged as not independently locatable yet — don't drop it or invent a page for it.
   - Show this breakdown to the user as a table before deriving test cases — the same review
     checkpoint this skill already uses for the traceability matrix, one layer earlier. Confirm or
     take edits before step 7.

6. Read the extracted content and identify discrete requirements — each row (CSV/XLSX), each heading
   section (DOCX), or each paragraph/numbered item (PDF/TXT) that describes a single piece of
   behavior. Where step 5 ran, read each requirement against the module/feature/workflow it belongs
   to rather than in isolation — a requirement naming "the dashboard" means whatever module the
   crawl actually clustered under that label, not a guess.

7. For each requirement, derive one or more Given/When/Then test cases:
   - **Given** — preconditions / starting state
   - **When** — the user action being tested
   - **Then** — the expected observable outcome
   - Assign a **priority**: `critical` (core flow, blocks release), `high` (common path), `medium`
     (edge case), `low` (cosmetic/rare). Base it on language cues in the requirement (e.g. "must",
     "shall" → critical/high; "may", "optional" → low) — do not invent a priority scheme per file.
   - **If the user asked for a specific number of test cases, generate exactly that many.** Rank
     every derived candidate by priority (critical > high > medium > low, ties broken by the order
     requirements appear in the document) and keep only the top N. Never pad with filler to reach
     a round number, and never substitute your own judgment for an explicit count the user gave.

8. Build a **traceability matrix** before creating anything: one row per requirement showing
   `requirement_id/heading → module/workflow (if step 5 ran) → test case name(s) → priority`. Mark
   any requirement a count cap excluded, so the omission is visible, not silent. Show this to the
   user for confirmation before writing scripts, since requirements → test case mapping is not
   always 1:1.

9. For each concrete UI action a test case needs, call `search_step_templates` to get the real
   `templateId` AND `templateTitle` — templates are live per-project data (built-ins plus
   org-defined Common Functions), never a fixed list you can assume. Single-word searches work
   much better than full phrases — e.g. "Navigate" only matches back/forward history templates,
   use "Go to"/"Open" to find "Open Web Browser and go to page {{text}}"; "Enter Text" matches
   nothing, use "Enter"; "Assert Text" matches nothing, this platform uses "Verify". Call
   `get_step_template` if it's unclear which placeholder names (`{{...}}` in `templateTitle`) a
   template expects. **Never invent a templateId.**

10. Build each script's steps, resolving every `ui-locator` first: check `get_page_by_url` —
    step 4 already crawled the app, so the real locator should already be on hand; only fall back
    to a fresh `crawl_url` call if the page in question wasn't covered (new page found mid-analysis,
    or step 4 never ran because no URL was known at the time). Only leave a single descriptive
    placeholder step (flagged as needing manual locator/template work) when no concrete UI target or
    URL is known at all — never guess a raw selector as a substitute. Then follow **Path A** or
    **Path B** below, per the Destination collected up front.

### Path A — persist into AHQ

11a. If step 4 crawled a live app, persist it once, before any script: `create_website` for the app
     (check `get_context`/`list_websites` first — don't duplicate), then `create_page` +
     `add_locators` per crawled page, so every script below references a real, saved `locatorId`
     rather than re-deriving one per step.

12a. Call `create_test_script` for each derived test case:
    - Name format: "<Requirement ref> — <Scenario>" (e.g. "REQ-12 — Login with invalid password")
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
      "recommended" field). Pass `website_id` from step 11a's `create_website` call.
    - Attach to an epic/story (`story_id`) if the user specified one, or if there's an obviously
      matching one from `get_context`/`list_epics`/`list_stories`. If nothing fits, call
      `create_epic` then `create_story` rather than skipping the field — there is always a way to
      satisfy this now, so don't flag-and-move-on the way this skill used to.
    - `status`/`type` default to `"Not Started"`/`"WEB"` in `create_test_script` — leave them unless
      you have a real reason to change them. If `status` is set to `"To Be Repaired"`, also pass
      `repair_comment` — required in that case only.

13a. If more than a few scripts were created, call `create_suite` and `add_scripts_to_suite` to
     group them under one suite named after the source file.

### Path B — portable bundle (no AHQ writes)

11b. **Never call `create_website`/`create_page`/`add_locators`/`create_test_script`/`create_epic`/
     `create_story`/`create_suite`/`add_scripts_to_suite`/`create_branch` in this path** — nothing
     gets persisted into AHQ. `crawl_url`, `search_step_templates`, and `get_step_template` are all
     still fair game: they only read, and the bundle still needs real `templateId`/`templateTitle`
     and real locator strategies, not invented ones — grounding discipline doesn't relax just
     because the result isn't being saved anywhere.

12b. Assemble **one JSON document** for the whole run (not one message per script):

     ```json
     {
       "application": { "name": "<app/website name>", "url": "<root url, if step 4 ran>" },
       "scripts": [
         {
           "name": "<Requirement ref> — <Scenario>",
           "description": "<Given/When/Then summary>",
           "priority": "critical | high | medium | low",
           "labels": ["<module or workflow name from step 5, if it ran>"],
           "steps": [
             {
               "templateId": "template-id-N (built-in) or a real Common-Function UUID",
               "templateTitle": "Enter {{text}} for the {{ui-locator}}",
               "testStepTitle": "Enter test@example.com for the Email field",
               "locator": {
                 "pageId": "<stable slug for this page, e.g. derived from its URL path>",
                 "pageName": "<page title, exactly as crawl_url captured it>",
                 "locatorId": "<stable slug for this locator, e.g. page-slug + locator name>",
                 "locatorName": "<locator label, exactly as crawl_url captured it>",
                 "locatorStrategies": [
                   { "key": "xpath", "value": "//*[@id='email']" },
                   { "key": "css", "value": "#email" }
                 ]
               },
               "data": { "type": 0, "value": "test@example.com" }
             }
           ]
         }
       ]
     }
     ```

     - Omit `locator` on a step with no UI target (e.g. a plain `"Wait for {{number}} seconds"`).
     - `data.type` is the real TypeValuePair code (`0` literal — the only one this skill should ever
       emit; `1`/`2`/`3`/`5`/`6`/`7` are data-column/config/runtime/parameter/faker/vault references
       that only make sense inside a real AHQ project). A built-in with **more than one**
       non-locator placeholder (rare) gets `data` as a list of `{"placeholder": "<name>", "type":
       0, "value": "..."}` instead of a single object — name the placeholder so a later migration
       can still map it back to its `{{...}}` token.
     - `pageId`/`locatorId` are bundle-local identifiers, not real AHQ ids — there is no AHQ page or
       locator yet. Keep them **stable and reused** across every step/script in this same bundle that
       references the same crawled page/element, so a later migration can tell "these 5 steps share
       one locator" from "these are 5 different ones" without re-crawling. A human-readable slug
       (e.g. `"login-email-field"`) beats an opaque id — this bundle is meant to be read directly.
     - This shape carries exactly what a migration needs to reconstruct the real thing later: one
       `create_website` + one `create_page`/`add_locators` per distinct `pageId` (building each
       locator's real `locationStrategies` from `locatorStrategies`) + one `create_test_script` per
       script, swapping each step's bundle-local `locatorId` for the real one `add_locators` returns.
       Nothing here is thrown away, just not persisted yet.

13b. Return the assembled JSON as your final answer (a fenced code block, or write it to a file if
     the user asked for one) — there is no tool call that "returns" it; this is the deliverable.

14. Return to the user:
    - The module/feature/workflow breakdown (if step 5 ran) and the login outcome
    - The traceability matrix (requirement → test case → priority), noting anything cut by a count cap
    - Path A: scripts created (count + names). Path B: the bundle itself, plus a one-line note that
      nothing was written to AHQ
    - Any requirements skipped and why (ambiguous, no clear UI target, duplicate of existing script)

## Rules
- **Settle the Destination (AHQ vs. portable bundle) before anything else** — it decides whether
  `website_id`/`story_id`/branch questions apply at all (Path A) or whether no AHQ write happens in
  this run (Path B). Never infer it from whether an AHQ org token happens to be connected.
- **Path B never calls a write tool** — `create_website`, `create_page`, `add_locators`,
  `create_test_script`, `create_epic`, `create_story`, `create_suite`, `add_scripts_to_suite`,
  `create_branch` are all off the table. Only `crawl_url`/`search_step_templates`/`get_step_template`
  (read-only) are used to ground the bundle's content.
- **A portable bundle still needs real templateIds and real locator strategies** — grounding
  discipline is the same in both paths; the only thing Path B skips is persistence, not accuracy.
- **Honor an explicit test-case count exactly** — rank candidates by priority and keep only the top
  N; state in the summary what was cut, never pad or silently generate a different count
- **Never present an unauthenticated crawl as the full application** when credentials were given —
  `crawl_url` only handles a single-step login form; a failed or skipped sign-in means only
  pre-login pages were seen, and that must be said plainly, not papered over
- Never fabricate UI locators that weren't derivable from context — flag those scripts instead of
  guessing (this mirrors the grounding-rules discipline used for `crawl_url`/`testbots-gen-from-url`)
- Never write a raw guessed selector (e.g. `input[type='email']`) into a step instead of a real
  `locatorId` — check `get_page_by_url` first, and if none exists, call `crawl_url` (when a URL is
  known) before writing the step
- Never fabricate a templateId — always resolve it via `search_step_templates`/`get_step_template` first
- Never omit `templateTitle` on a step whose templateId is a built-in (`"template-id-N"`) — causes a 500
- Never put step values in `params` — use `parameters` (a list); `params` does not drive step titles or execution
- Never fabricate `locateBy`/`locatorValue` on a `ui-locator` parameter — pass only `{"locatorId": "..."}` and let the server enrich it
- (Path A only) Never call `create_test_script` without `website_id` and `story_id` — both are validated locally and rejected if missing; resolve or create an epic/story rather than omitting it
- (Path A only) **Ask the user which branch the scripts should land on before creating them** — offer a new branch alongside the real ones from `list_branches`, and pass the answer as `branch_name`. Do not silently default to `main`: it is protected, so a later `commit_branch` returns 403, the edit stays an uncommitted version, and `execute_bot` keeps running the last committed one — the change appears saved but never executes. The same branch is what `execute_bot` needs as `targetBranchName`, so settle it before the bot runs, not after
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
- **Type a password with "Enter encrypted-text {{text}} for {{ui-locator}}" (`template-id-98`)**,
  never the plain Enter step, so it is stored encrypted. Its value is a vault secret
  (`{"vault": "<secret name>"}`, from `list_config_vault_secrets`) when the user names one or one
  clearly fits, otherwise the password the user gave. If there is neither, type `UPDATE_PASSWORD`
  and tell them plainly that the password must be updated before the script runs - never a
  made-up variable, a phrase like "the password you gave", or a row of dots. **`list_config_vault_secrets`
  is an AHQ-project resource — Path B has no project to call it against, so a Path B password step
  is always the literal password or `UPDATE_PASSWORD`, never a `{"vault": ...}` reference.**
- **Copy each step's `templateId` and `templateTitle` exactly as the search returned them, as a
  pair.** A step runs by its templateId whatever its title says; a mismatched pair is corrected to
  the template that has the title, or refused. The script-writing tools return `password_warning` when a
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
- Always show the module/feature/workflow breakdown and the traceability matrix before writing
  scripts, not just in the final summary
- If the file has an `error` from `extract_requirements`, do not retry — report it to the user
