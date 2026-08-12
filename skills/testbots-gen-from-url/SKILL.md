---
name: testbots-gen-from-url
description: Explore a live web app and write test scripts for the flows it finds
tools:
  - mcp__testbots-mcp-server__get_context
  - mcp__testbots-mcp-server__crawl_url
  - mcp__testbots-mcp-server__get_page_by_url
  - mcp__testbots-mcp-server__create_website
  - mcp__testbots-mcp-server__create_page
  - mcp__testbots-mcp-server__add_locators
  - mcp__testbots-mcp-server__search_step_templates
  - mcp__testbots-mcp-server__get_step_template
  - mcp__testbots-mcp-server__list_epics
  - mcp__testbots-mcp-server__create_epic
  - mcp__testbots-mcp-server__list_stories
  - mcp__testbots-mcp-server__create_story
  - mcp__testbots-mcp-server__list_branches
  - mcp__testbots-mcp-server__create_branch
  - mcp__testbots-mcp-server__create_test_script
---

## When to use this skill
The user has a deployed web application and wants test scripts generated automatically from a live URL.

## What to collect before starting
- Target URL (required)
- Login credentials: username + password (optional — ask only if the app requires login to reach testable content)

## Workflow

1. Call `get_context` — load full project snapshot (websites, envs, epics, bots, queue)
2. Call `crawl_url` with the provided URL and credentials (if any)
   - If it returns an error about a missing Playwright browser: relay the fix to the user
     verbatim (run `playwright install chromium` once, then retry — a one-time setup step),
     and OFFER the no-crawl fallback: if the user can paste the page's selectors (or you
     already know this page's structure), build the locators directly via
     `create_page` + `add_locators` and skip crawling entirely. Do not silently give up.

3. Review the crawl result:
   - If any page has `passes_threshold: false` (resolution_rate < 0.80), skip it and note it in the final summary
   - If ALL pages fail the threshold, stop and report to the user

4. For the first valid page, call `create_website` using the root domain as the name and the root URL

5. For each valid page:
   a. Call `create_page` with the page URL and title as the name, linked to the website
   b. Call `add_locators` with `page_url` set to that same page URL (locators are upserted by URL
      match, not by page_id — omitting it or getting it wrong creates a duplicate page instead of
      attaching locators to the one just created). Transform each crawled element into
      `{locatorName, locatorType, locationStrategies: [{locateBy, locatorValue, selected: true}]}` —
      `locateBy`/`locatorValue` come from the crawl result's `css` or `xpath` field (prefer `css`
      when present), `locatorType` from the crawl result's `tag`.

6. Analyze all pages together and identify testable flows:
   - Login / logout
   - Navigation flows between pages
   - Form submissions (create, update, search)
   - Validation (empty fields, invalid input, required fields)
   - Any visible CRUD operations

7. For each flow, before writing any steps, call `search_step_templates` for each action you need
   to get the real `templateId` AND `templateTitle` — there is no fixed/static list of action
   types, templates are live per-project data (built-ins plus org-defined Common Functions).
   Single-word searches work much better than full phrases — e.g. "Navigate" only matches
   back/forward history templates, use "Go to" or "Open" to find
   "Open Web Browser and go to page {{text}}"; "Enter Text" matches nothing, use "Enter" to find
   "Enter {{text}} for the {{ui-locator}}"; "Click" matches directly
   ("Click {{ui-locator}}"); "Assert" matches nothing, this platform uses "Verify" (e.g.
   "Verify current URL contains path {{expected}}"). Call `get_step_template` on the result if
   it's unclear which placeholder names (`{{...}}` in `templateTitle`) that template expects.
   **Never invent a templateId.**

8. For each flow, call `create_test_script` with:
   - Name format: "<PageName> — <FlowType>" (e.g. "Login Page — Happy Path")
   - Steps built from the locators captured and the resolved templateIds, in the exact shape
     below — it is the proven-working one, and the near-miss variants fail in ways that look
     like success. Each step needs `templateId` + `templateTitle` (built-ins only) + a
     `parameters` array (NOT
     `params`) with one entry per `{{placeholder}}` in `templateTitle` — `{"key": "ui-locator",
     "value": {"locatorId": "<real id from add_locators>"}, "paramClass":
     "ai.automationhq.commons.entities.assets.UILocator"}` for element targets (never fabricate
     locateBy/locatorValue here, the server enriches from the saved locator), and
     `{"key": "text", "value": {"type": 0, "value": "<literal>"}, "paramClass":
     "ai.automationhq.commons.entities.assets.TypeValuePair"}` for scalar values.
   - **For every built-in templateId (looks like `"template-id-N"`), copy that template's
     `templateTitle` string verbatim into the step's `templateTitle` field** (placeholders intact,
     e.g. `"Enter {{text}} for the {{ui-locator}}"`). The server does not look this up itself for
     built-ins — omitting it causes a 500 error. Not needed for Common-Function templateIds (real
     UUIDs), which the server resolves on its own.
   - **`website_id` and `story_id` are both REQUIRED** — `create_test_script` now validates this
     locally and rejects the call with a clean error if either is missing, matching
     `automationhq-frontend-v2`'s own create-script form. Pass `website_id` from `create_website`'s
     result. For `story_id`: call `list_epics` → `list_stories` and actually read the result before
     creating anything. If a fitting epic or story already exists, **say which one and ask the user
     whether to reuse it or start a fresh one for this work** — that is their structural decision,
     and quietly adopting an existing epic (or adding a near-duplicate story to one that already
     had a suitable story) leaves them with a test tree they did not choose. Only call
     `create_epic`/`create_story` once nothing fits or the user has said so.
   - `status`/`type` default to `"Not Started"`/`"WEB"` in `create_test_script` — leave them unless
     you have a real reason to change them (never send them as null/omit them entirely). If you do
     set `status` to `"To Be Repaired"`, also pass `repair_comment` — required in that case only.

9. Return a summary to the user:
   - Pages crawled (total, skipped)
   - Total locators captured
   - Scripts created (list each by name)

## Rules
- Maximum 20 pages per invocation
- Never fabricate a templateId — always resolve it via `search_step_templates`/`get_step_template` first
- Never omit `templateTitle` on a step whose templateId is a built-in (`"template-id-N"`) — causes a 500
- Never put step values in `params` — use `parameters` (a list); `params` does not drive step titles or execution
- Never fabricate `locateBy`/`locatorValue` on a `ui-locator` parameter — pass only `{"locatorId": "..."}` and let the server enrich it
- Never write a raw guessed selector (e.g. `input[type='email']`) into a step instead of a real `locatorId` — check `get_page_by_url` for an existing locator first, and if none exists, call `crawl_url` to capture real ones before writing the step
- Never call `create_test_script` without `website_id` and `story_id` — both are validated locally and rejected if missing; resolve or create a story rather than omitting it
- **Ask the user which branch the script should land on before creating it** — offer a new branch alongside the real ones from `list_branches`, and pass the answer as `branch_name`. Do not silently default to `main`: it is protected, so a later `commit_branch` returns 403, the edit stays an uncommitted version, and `execute_bot` keeps running the last committed one — the change appears saved but never executes. The same branch is what `execute_bot` needs as `targetBranchName`, so settle it before the bot runs, not after
- **After any step that can trigger navigation (a submit/sign-in/link click) and before the next
  step that verifies the result (URL check, element check on the new page), insert a wait step** —
  `search_step_templates("Wait")` for `template-id-36` ("Wait for visibility of {{ui-locator}} for
  {{number}} seconds", preferred when a locator on the destination page is already known — it
  resolves as soon as the page is ready) or `template-id-35` ("Wait for {{number}} seconds", plain
  fixed delay, ~5-10s, use when no destination-page locator is known yet). Skipping this causes the
  verify step to run against the pre-navigation page and fail: the URL/element check is a single
  immediate read with no retry loop, so it sees the page the click was meant to leave.
- Never create a script with 0 steps
- Script names must be unique — append " (2)", " (3)" if duplicates arise
- **Agree the granularity before generating at scale.** One script per test case and one script
  bundling several related cases are both legitimate, and the right answer depends on how the user
  intends to run and maintain them. When a run would produce more than a handful of scripts, state
  the split you are about to make and confirm it — restructuring afterwards means recreating every
  script, since a script's steps are its identity
- Locators from a credentialed crawl: `crawl_url` captures the sign-in page BEFORE authenticating
  and the authenticated pages after, so one call covers both. If a login form's fields are missing
  from the result, the credentials logged straight through — re-crawl without credentials rather
  than writing guessed selectors
- If crawl returns an error for a page, skip it silently and note it in the summary
- Do not call `create_website` more than once per invocation — reuse the websiteId for all pages
