---
name: testbots-gen-from-requirements
description: Read a requirements document and write test scripts that cover it (PDF/DOCX/XLSX/CSV/TXT), grounded in the live app when the document names one — returned as a portable, self-contained bundle
tools:
  - mcp__testbots-mcp-server__extract_requirements
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__search_step_templates
  - mcp__testbots-mcp-server__get_step_template
  - mcp__testbots-mcp-server__crawl_url
---

## When to use this skill
The user has a requirements doc, user story export, or spec file and wants test scripts generated
from it. A live URL is not required — but when the document names one (or the user gives one),
ground the test cases in the real app instead of the document's wording alone.

This skill never writes into AHQ (no `create_website`/`create_page`/`add_locators`/
`create_test_script`/etc.) — it returns one self-contained JSON bundle with everything inlined.
That's a deliberate, current-state choice: every caller of this skill today is a testbots.ai
customer, who has no AHQ project to write into. If a caller that genuinely has one (and wants
scripts persisted directly) becomes real, that decision should come from the serving context
itself (e.g. this deployment's partner-branding config), not a question asked mid-conversation —
don't reintroduce a "where should this go" question to the user.

## What to collect before starting
- Absolute path to the requirements file (required)
- Target URL — look for one in the document first (a link, a "staging/prod/app URL" field, a
  header). Ask the user only if the document doesn't name one; don't skip testing a live app just
  because asking feels like friction
- If a URL is in play: does reaching the testable content require signing in? Look for a "test
  account"/"credentials" section in the document first. If it's genuinely unclear, ask — never
  guess a username or invent a password
- How many test cases the user wants, if they said a number ("just the critical ones", "5
  scenarios", "one per requirement"). Honor it exactly later — don't let the requirement count
  silently override an explicit ask

## Workflow

1. Call `get_context` — load whatever project snapshot is available, so generated scripts don't
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
     pages as if they were the full application** in that case; say the login failed and why.
   - No URL at all: skip to step 6 and derive test cases from the document alone, as this skill
     always has.

5. **Slice the application into Pages, Modules, Features, and Workflows** (only when step 4 ran):
   - **Pages** — each page `crawl_url` captured, by title and URL.
   - **Modules** — pages clustered by shared nav-label prefix or URL path segment (e.g. everything
     under `/admin/*`). `crawl_url`'s `menus_expanded` count is what makes a collapsed sidebar's
     modules visible at all — a page captured before menu expansion only shows what was already open.
   - **Features** — the concrete forms/CRUD affordances/interactive elements each module's pages
     actually have, grounded in the crawl, not inferred from the module's name.
   - **Workflows** — the multi-page sequences the requirements document actually describes (e.g.
     "sign up → verify email → complete profile"), matched against the pages/modules that realize
     each step. A workflow step the crawl can't place on a real page stays in the workflow, just
     flagged as not independently locatable yet — don't drop it or invent a page for it.
   - Show this breakdown to the user as a table before deriving test cases — the same review
     checkpoint this skill uses for the traceability matrix, one layer earlier. Confirm or take
     edits before step 7.

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

8. Build a **traceability matrix** before assembling anything: one row per requirement showing
   `requirement_id/heading → module/workflow (if step 5 ran) → test case name(s) → priority`. Mark
   any requirement a count cap excluded, so the omission is visible, not silent. Show this to the
   user for confirmation before writing scripts, since requirements → test case mapping is not
   always 1:1.

9. For each concrete UI action a test case needs, call `search_step_templates` to get the real
   `templateId` AND `templateTitle` — never a fixed list you can assume. Single-word searches work
   much better than full phrases — e.g. "Navigate" only matches back/forward history templates,
   use "Go to"/"Open" to find "Open Web Browser and go to page {{text}}"; "Enter Text" matches
   nothing, use "Enter"; "Assert Text" matches nothing, this platform uses "Verify". Call
   `get_step_template` if it's unclear which placeholder names (`{{...}}` in `templateTitle`) a
   template expects. **Never invent a templateId.**

10. Resolve every `ui-locator` a step needs from step 4's crawl result — never a fresh guess. Only
    leave a single descriptive placeholder step (flagged as needing manual locator/template work)
    when no concrete UI target or URL is known at all.

11. Assemble **one JSON document** for the whole run (not one message per script):

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
    - `data.type` is the real TypeValuePair code — `0` (literal) is the only one this skill should
      ever emit. There is no AHQ project here to hold a data column, config var, runtime var,
      parameter reference, or vault secret, so none of those apply; a password with no real value
      is the literal string `UPDATE_PASSWORD`, flagged plainly to the user, never a vault reference.
    - A built-in with **more than one** non-locator placeholder (rare) gets `data` as a list of
      `{"placeholder": "<name>", "type": 0, "value": "..."}` instead of a single object — name the
      placeholder so a later migration can still map it back to its `{{...}}` token.
    - `pageId`/`locatorId` are bundle-local identifiers, not real AHQ ids — there is no AHQ page or
      locator yet. Keep them **stable and reused** across every step/script in this same bundle that
      references the same crawled page/element, so a later migration can tell "these 5 steps share
      one locator" from "these are 5 different ones" without re-crawling. A human-readable slug
      (e.g. `"login-email-field"`) beats an opaque id — this bundle is meant to be read directly.
    - This shape carries exactly what a future migration into a real AHQ project would need to
      reconstruct the same thing: one website + one page/locator set per distinct `pageId`
      (building each locator's real `locationStrategies` from `locatorStrategies`) + one test
      script per script, remapping each step's bundle-local `locatorId` to whatever real id that
      migration mints. Nothing here is thrown away, just not persisted into AHQ.

12. Return the assembled JSON as your final answer (a fenced code block, or write it to a file if
    the user asked for one) — there is no tool call that "returns" it; this is the deliverable.

13. Return to the user:
    - The module/feature/workflow breakdown (if step 5 ran) and the login outcome
    - The traceability matrix (requirement → test case → priority), noting anything cut by a count cap
    - The bundle itself
    - Any requirements skipped and why (ambiguous, no clear UI target, duplicate of existing script)

## Rules
- **Never call a write tool** — this skill only ever reads (`extract_requirements`, `get_context`,
  `crawl_url`, `search_step_templates`, `get_step_template`). Nothing it does persists into AHQ.
- **Honor an explicit test-case count exactly** — rank candidates by priority and keep only the top
  N; state in the summary what was cut, never pad or silently generate a different count
- **Never present an unauthenticated crawl as the full application** when credentials were given —
  `crawl_url` only handles a single-step login form; a failed or skipped sign-in means only
  pre-login pages were seen, and that must be said plainly, not papered over
- Never fabricate UI locators that weren't derivable from context — flag those scripts instead of
  guessing
- Never write a raw guessed selector (e.g. `input[type='email']`) into a step instead of a real
  locator captured by `crawl_url`
- Never fabricate a templateId — always resolve it via `search_step_templates`/`get_step_template` first
- **Copy each step's `templateId` and `templateTitle` exactly as the search returned them, as a
  pair** — a mismatched pair is corrected to the template that has the title, or refused
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
  never the plain Enter step, so it is stored encrypted. Its value is the real password when the
  user gave one, otherwise the literal `UPDATE_PASSWORD`, told to the user plainly as something
  that must be updated before the script runs — never a made-up variable, a phrase like "the
  password you gave", or a row of dots.
- **Check text exactly as `crawl_url` captured it**, not as the user worded it. Icons (+, ×,
  arrows) are not text: a button showing "+ New Test Bot" has the text "New Test Bot", and a check
  for the user's wording fails on a page that is correct.
- **If a crawled page reports `overlays_on_arrival`**, add its `closes_with` step right after that
  page loads (after the login wait, for the first signed-in page) - a panel left open takes every
  click meant for the page behind it.
- Script names must be unique — append " (2)", " (3)" if duplicates arise
- Always show the module/feature/workflow breakdown and the traceability matrix before assembling
  the bundle, not just in the final summary
- If the file has an `error` from `extract_requirements`, do not retry — report it to the user
