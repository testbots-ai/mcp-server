# AHQ MCP Server — Project Context

## What This Is
A Python MCP server that wraps the Aviso-UTAP (AutomationHQ) test automation platform APIs.
Claude speaks plain language; the MCP server translates that into AHQ REST calls.
No AHQ web UI needed — everything happens through MCP tools.

## Versioning rule (for any change in this repo)

The plugin updater keys ONLY on `.claude-plugin/plugin.json`'s `version` — pushing code without
a bump means plugin users never receive it. Before pushing any user-visible change, bump BOTH
`plugin.json` and `pyproject.toml` (kept in sync) in the same batch: PATCH for fixes/doc
corrections, MINOR for new tools/skills/launcher changes, MAJOR for breaking tool contracts.
Internal-only changes (evals, CI, design docs) need no bump. Full policy + release checklist +
version history: `D:\MCP\AHQ_MCP_SERVER_MASTER_DESIGN.md` §14.

## Naming: what is `testbots-` and what is still `ahq-`

The rebrand moves in waves, split by what a rename actually breaks — an *install* (cheap, fixed by
reinstalling) or a *live connection / deploy* (expensive, breaks people mid-session).

Renamed: the MCP server id (`testbots-mcp-server` — `.mcp.json` key, `Server(...)`, `/status`'s
`service`, and the `mcp__testbots-mcp-server__*` prefix in all 16 skill files), the plugin
(`testbots-skills`), the marketplace (`testbots`), the 9 skills, and the stdio credentials:
`~/.testbots/.env` plus `TESTBOTS_API_TOKEN` / `TESTBOTS_PROJECT_ID` / `TESTBOTS_BASE_URL`.

**The credential rename is additive and must stay that way.** `_env_file_candidates()` still reads
`~/.ahq/.env` (lowest precedence) and `_either()` gives each of the three settings an
`AliasChoices("testbots_x", "ahq_x")`, so every pre-rebrand `.env` keeps working untouched.
Deleting either fallback strands those users *silently* — a missing token reads as "not
configured", which looks like a broken install rather than a renamed variable. Pinned by
`test_credentials_accept_both_the_testbots_and_ahq_variable_names` and
`test_both_credential_homes_are_searched_with_testbots_taking_precedence`.

## Configuration profiles

`.env.<profile>` layered over `.env`, selected by `TESTBOTS_ENV`/`AHQ_ENV` — Spring's
`application-<profile>.properties` and `spring.profiles.active`. `PROFILE_BASE_URLS`
(`config/credentials.py`) maps `dev`/`prod` to their gateways and `KNOWN_BASE_URLS` derives from
it, so adding an environment cannot leave the allowlist behind and reject its own tokens.

Three things here are load-bearing and easy to "simplify" wrongly:

- **`_env_file_candidates()` puts every base file before every profile file**, not each profile
  file above its own base file. The obvious per-directory interleaving lets `~/.testbots/.env`
  outrank `~/.ahq/.env.prod`, so what "prod is active" means would depend on which directory a
  value happened to live in.
- **`active_profile()` cannot read `settings`** — it decides which files `settings` loads. It
  re-reads the base `.env` itself. `settings.testbots_env` is declared only because
  `model_config` forbids extra keys, so an undeclared `TESTBOTS_ENV` line is a *hard startup
  failure*, not an ignored one. Read `ACTIVE_PROFILE`.
- **`--env` mints, `--use` switches; neither does the other.** Writing `.env.prod` does not
  activate it, so minting a prod token can never silently repoint a session at prod.

`--env` also picks the sign-in gateway, which is the reason profiles exist at all: a password
carries no environment (an API token names its own via `urlDetails.baseUrl`; a password has no
claim to read), so prod credentials sent to dev's gateway fail as *"your login details are
incorrect"* — indistinguishable from a wrong password. Hosted mode has the same limit and no fix:
a deployment only ever signs in against its own configured gateway.

Deliberately still `ahq-`: the gateway route prefix `/ahq-mcp-server` (in every live connector
URL), the `AHQ_MCP_*` deployment settings (DevOps ConfigMap/Secret), `TUNNEL_CLIENT_ID`, the
distribution name in `pyproject.toml` (paired with `http_server`'s `_pkg_version` call), the ECR
repository, and the k8s/ArgoCD resource names. These move as one batch with the connector URL —
same reasoning the connector cutover used, where `api-dev.testbots.ai/mcp-server` came up while
the `automationhq.ai` host kept answering rather than being switched off.

## Authentication
- Header depends on WHICH credential is in play (`AhqCredentials.auth_scheme`):
  - **API token** (stdio/plugin, and legacy hosted headers) → `X-API-AUTH-KEY: <token>`. The
    gateway resolves it by existence lookup only: no user attached, blanket org-wide reach.
  - **Login JWT** (hosted connector, since password sign-in replaced the token) →
    `Authorization: Bearer <jwt>`. This is the gateway's user-aware path, so the session carries
    the signed-in person's own roles.
  - `org-id` / `projectId` are omitted when empty, never sent blank — an empty `org-id` reads as
    "belongs to organization ''" and is rejected, which broke the pre-org `/users/me` call.
- Gateway URL: `https://api-dev.automationhq.ai` (NOT `https://dev.automationhq.ai`)
- Org token type: `ORGANIZATION` — signed with a different secret than user JWTs
- `.env` must never be committed
- **`AHQ_BASE_URL` is now optional in `.env` (2026-07-19)**: `AhqCredentials.from_settings`
  (stdio mode) resolves `base_url` from the token's own `urlDetails.baseUrl` claim first via
  `base_url_from_claims`, same as hosted mode's `from_headers` already did — `AHQ_BASE_URL` is
  only a fallback for a token issued before that claim existed. `org_id` was already
  token-derived; `base_url` now is too, `project_id` is the only credential left with no
  in-token equivalent. `_require_stdio_config`'s fail-fast check now reads the *resolved*
  `DEFAULT_BUNDLE.asset._credentials.base_url`, not the raw env var, so it only fails if
  neither the token nor `.env` produce one.

## Hosted mode authentication (Slice 9m, 2026-07-14)

`ahq-mcp-http` (`src/http_server.py`) is its own OAuth 2.1 authorization server — AHQ has NO
OAuth server anywhere (verified: no authorization-server dep in any pom; the gateway validates
API tokens by a Mongo `tokens.value` existence lookup, no signature/expiry/revocation check).
Key facts a future session must not rediscover:

- **Everything is stateless**: client_id, authorization code, access + refresh tokens are all
  Fernet blobs (`src/hosted/token_codec.py`) keyed by `AHQ_MCP_AUTH_SECRET` — required at
  startup, must be identical across replicas (a mismatch silently invalidates logins). The
  user's pasted AHQ ORGANIZATION token is sealed INSIDE the access token; `/consent` validates
  it live via `list_projects` (which is also the 9k org↔project check + project picker).
- **Stateless DCR rides on SDK mutation-echo**: `register_client` REPLACES the SDK-generated
  uuid `client_id` with an encrypted registration record and the SDK echoes the mutated model
  back (pinned by `test_register_client_replaces_uuid_client_id_with_blob` — check it after
  any `mcp` package upgrade).
- **Dual auth on /mcp** (`src/hosted/dual_auth.py`): `Authorization: Bearer <our blob>` →
  credentials into `scope["ahq_credentials"]`; legacy `X-API-AUTH-KEY`+`projectId` headers →
  pass-through to the original `from_headers` path. The SDK's `RequireAuthMiddleware` is NOT
  used (it would 401 the header clients).
- **The connection can never outlive the AHQ token sealed inside it.** `_capped_ttl` binds BOTH
  the access and the refresh token to the embedded AHQ token's `exp`, necessarily — every
  downstream call re-presents that token to the gateway, so an expired one is useless whatever we
  say about it. The consequence is a support question that looks like an OAuth bug: AHQ's token
  dialog offers an 8-hour expiry and it is popular (29% of live ORGANIZATION tokens use
  `expiryMinutes=480`), so those users must create a NEW AHQ token and re-paste it at consent
  roughly once a working day. Refresh is not broken — there is simply nothing left to refresh
  against. `/consent` now warns when a pasted token has under a week left, tells everyone to pick
  the longest expiry available, and records `short_lived_token` on the `auth.consent_ok` audit
  line so the question is answerable from logs.
- **"Works with account X's token, not account Y's" has two known causes, neither of them the
  account's role.** Investigated against the live dev DB + `TokenService` after a report that a
  Site-Admin-created ORGANIZATION token failed where a Support-created one worked.
  (1) `createOrgToken` lets a user mint an org token only for their OWN organization or one they
  created (`belongsToOrg || isOrgCreator`), while AHQ Support has created most customer orgs — so
  Support's tokens point at the CUSTOMER's org and a site admin's point at their own, which may
  hold no projects. (2) `generateOrgClaims` writes `urlDetails` only when the caller supplies it,
  and 30% of live ORGANIZATION tokens have no `baseUrl` claim at all; those fall back to this
  deployment's own gateway, so a perfectly valid token from the OTHER environment gets rejected by
  a server that was never asked about it. Both now produce a specific message instead of "the
  token may be expired or deleted", which sent people to re-check a token that was fine.
  Not reproducible from dev data — every ORGANIZATION token there was created by AHQ Support.
- **A token whose organization has no projects used to dead-end silently** — the picker rendered
  with zero options and a `required` radio, unsubmittable and unexplained, which reads as "this
  token is rejected" while a colleague's token works. Now a stated error. Reachable in live dev
  data (one org holds a valid ORGANIZATION token and zero projects), and a plausible explanation
  for any "works with account X's token, not account Y's" report — check the org's project list
  before assuming the token type or the creating account's role is what differs.
- **No token revocation by design** — no storage; the gateway re-validates the embedded AHQ
  token on every downstream call, so a revoked AHQ token dies at the next tool call anyway.
- **Public URLs vs app paths**: the gateway's `StripPrefix=1` removes `/ahq-mcp-server` before
  requests reach us, so routes are prefix-less but every ADVERTISED URL (metadata docs,
  consent redirect, `WWW-Authenticate: resource_metadata`) must be built from
  `AHQ_MCP_PUBLIC_BASE_URL` (which includes the prefix). Also: bare `/mcp` is path-rewritten
  to `/mcp/` in-process (`_ExactMcpPath`) — Starlette's Mount 307 would emit a prefix-less
  Location that 404s through the gateway.
- **Redirect-URI policy**: loopback http (any port) + `https://claude.ai|claude.com/api/mcp/auth_callback`
  + `AHQ_MCP_EXTRA_REDIRECT_URIS`. Anything else is refused at registration.
- Hosted hardening: per-org in-process rate limit (`AHQ_MCP_RATE_LIMIT_PER_MIN`, default 60),
  2 MB body cap, one JSON audit line per tool call (never log tool arguments — they contain
  credentials). `crawl_url` is hosted-ENABLED since 9j with an SSRF guard
  (`src/tools/url_guard.py`, `is_global` check on every navigation; DNS-rebinding TOCTOU is
  accepted residual risk); `extract_requirements`/`check_local_agent_status` stay stdio-only.
  `execute_bot` against a local grid is gated the same way (2026-07-16): the hosted pod's
  "localhost:9202" is its own loopback, not the caller's machine, and there is no reverse channel
  for the cloud to reach a specific user's agent (confirmed: `ahq-standalone-local-v2-services`'
  agent registry is a one-way heartbeat the agent pushes up, not a command channel) — a hosted
  session gets a clean error pointing at stdio instead of a silent hang/connection-refused.
- The 8 skills are also served as MCP prompts (`src/prompts.py`) so hosted clients get the
  workflows; the gateway needs `/ahq-mcp-server/**` permitAll (like `testbot-mcp-server`)
  before OAuth Bearer tokens can reach us.
- **Consent-page branding is per-deployment configurable** (`ahq_mcp_partner_display_name`,
  `ahq_mcp_partner_logo_url`, `ahq_mcp_partner_primary_color` in `src/config/ahq_services.py`,
  consumed in `src/hosted/consent.py`): empty settings render the product's own
  name/logo/color — **TestBots.ai, the inlined TestBots mark, and `#7c3aed`** — and a self-hosted
  client sets its own values (mirrors the `partner.display-name` / `partner.logo-url` pattern
  other services already use for PDF reports). Verified live: with
  `AHQ_MCP_PARTNER_DISPLAY_NAME`/`AHQ_MCP_PARTNER_LOGO_URL` set, the page's title, heading,
  logo and message text all switch to the configured partner. Because the defaults are the real
  brand, **no ConfigMap change is needed to make a deployment show TestBots.ai** — the three
  `AHQ_MCP_PARTNER_*` env vars are only for white-labelling to someone else.
- **VS Code's Copilot Chat MCP OAuth client skips Dynamic Client Registration entirely** —
  confirmed live 2026-07-16, reproduced in a brand-new temporary profile with zero cached state
  (ruling out caching). It goes straight to `/authorize` with a self-constructed, never-registered
  `client_id`: its own redirect URIs, space-joined verbatim (e.g.
  `"http://127.0.0.1:33418/ https://vscode.dev/redirect"`). The first symptom looked like a
  redirect-URI allowlist gap (`https://vscode.dev/redirect` 400'd at `/register`) — that's real and
  now fixed (`KNOWN_CLIENT_CALLBACKS` includes it), but fixing it alone didn't help, because
  `/register` was never actually being called. `StatelessAhqProvider.get_client()` now has an
  `_implicit_client()` fallback: if a client_id doesn't decode as one of our own blobs, treat it as
  an implicit public client IF every space-separated URI in it independently passes
  `redirect_uri_allowed()` — this doesn't loosen the real security boundary (the allowlist), it
  just stops requiring prior `/register` for a client whose "identity" already satisfies it. The
  SDK's authorize handler still independently validates the requested `redirect_uri` against the
  client's declared list either way. If a future client (Cursor, Windsurf, ...) hits a similar
  symptom, check with a direct curl repro of both `/register` AND `/authorize` before assuming
  which one is actually failing — don't guess from the error message alone, it can be a second-hand
  symptom of an earlier silent failure.
- **Microsoft 365 Copilot declarative agents** (v1.6.5): a third Microsoft path, distinct from
  both Copilot Chat and Copilot Studio. Built with the M365 Agents Toolkit, which supports
  "OAuth (with dynamic registration)" — so our DCR works and no static client id/secret is
  needed — and is NOT restricted to read-only tools (unlike M365 *federated connectors*, which
  are, and are a different feature). Two server-side requirements, both now met: the fixed Teams
  callback `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect` is allowlisted, and an
  unauthenticated **GET** on `/mcp` must answer 401 with `WWW-Authenticate` (the toolkit probes
  with GET to discover auth; verified live — `DualAuthMiddleware` already handles every method
  but OPTIONS). `CLIENT_TTL` was raised 90d → 365d because the toolkit persists the DCR result
  in the agent's plugin manifest, so a short client life expires the customer's *deployment*
  months later with no obvious cause; pinned by
  `test_client_registration_outlives_a_deployed_agent_manifest`.
- **Microsoft Copilot Studio needs two things VS Code's Copilot Chat did not** (v1.6.4). They are
  different clients despite the shared "Copilot" name: Copilot Chat is a plain MCP client (already
  handled by `_implicit_client`), while Copilot Studio goes through Power Platform's *connector*
  infrastructure. (1) It performs real RFC 7591 DCR with a per-connector, per-region callback
  `https://<region>.consent.azure-apim.net/redirect/<connector-id>` — the connector id doesn't
  exist until after the connector is created, so `AHQ_MCP_EXTRA_REDIRECT_URIS` can't hold a
  literal; `redirect_uri_allowed` matches the `consent.azure-apim.net` host + `/redirect` path
  instead (Microsoft-owned, same posture as the `vscode.dev` entry). (2) Its API-key auth sends
  only ONE header, but header auth here needs `X-API-AUTH-KEY` **and** `projectId` —
  `BaseAhqClient` puts `projectId` on every AHQ request, and an empty one returns 200 with an
  EMPTY RESULT SET, not an error (confirmed live: `list_websites` → `[]`). `_resolve_clients` now
  raises on a header-path request with no `projectId` rather than letting an agent report "you
  have no websites". OAuth is the documented path for Copilot Studio since `/consent` resolves
  the project itself. Copilot Studio is Streamable-HTTP only (SSE dropped Aug 2025) — already fine.
- **One consent URL now serves both dev and prod tokens (2026-07-17)**: every AHQ API call
  previously used this server's own fixed `AHQ_BASE_URL` setting (dev), so pasting a PROD
  organization token into the dev-hosted `/consent` flow validated it against DEV's gateway/DB
  and showed the wrong (or no) project list — confirmed live with a real prod org token. Fixed
  by resolving the gateway `base_url` from the TOKEN'S OWN `urlDetails` claim instead
  (`credentials.base_url_from_claims`) — every other AHQ client (standalone local agent, browser
  locator-spy extension, the frontend's token controller) already does exactly this; ahq-mcp-server
  was the one place still ignoring it. Restricted to an allowlist (`KNOWN_BASE_URLS` = dev + prod,
  extendable via `AHQ_MCP_EXTRA_BASE_URLS`) since `decode_ahq_token` does not verify the JWT
  signature — trusting an arbitrary claim value would make this server an open relay. The
  resolved base_url is sealed into the authorization code and both OAuth access/refresh token
  blobs (`AhqAuthorizationCode.base_url` / `AhqAccessToken.base_url` / `AhqRefreshToken.base_url`)
  so it survives for every subsequent `/mcp` tool call, not just the consent-time project list;
  `DualAuthMiddleware` uses `access.base_url or self.base_url` (the fallback only matters for
  tokens issued before this shipped). The legacy `X-API-AUTH-KEY` header path
  (`AhqCredentials.from_headers`) got the same fix.
- **Consent page stays in this repo, styled with AHQ branding**: `/consent`'s HTML GET/POST pair
  (`consent.py`) is the only consent UI — it is not delegated to a separate frontend app. The
  page uses the real AHQ palette (`#9c27b0` primary), IBM Plex Sans, and the AutomationHQ mark
  inlined as a base64 `<img>` so the page has no external asset dependency. Keep `consent.py`'s
  validation/issuance logic (`_validate_and_list`/`_issue_code`/`_redirect_url`) as the single
  source of truth if this page ever needs a second UI in front of it.
- **Gateway route prefixes are overridable per deployment target** (`ahq_gw_prefix_*` settings in
  `src/config/ahq_services.py`): every module-level `*_SVC` constant (`ASSET_SVC`, `TEST_MGMT_SVC`,
  etc.) now derives from a `Settings` field defaulting to the shared SaaS gateway's route-id
  convention. A self-hosted deployment whose gateway uses a different naming convention (e.g. a
  `${PARTNER_PREFIX}-<svc>-${SUFFIX}` scheme instead of `/ahq-<svc>-services`) overrides only the
  prefixes it needs via env vars — no code change. `LOCAL_EXEC_SVC` is intentionally excluded
  (never gateway-routed; targets the caller's own machine). This exists because this service is
  the only AHQ client that calls every peer service back out through its own gateway rather than
  via internal service DNS, so its gateway-prefix assumptions can't be hardcoded the way every
  other backend service's peer-URL config already isn't.

## AHQ Platform Knowledge (Aviso-UTAP)

### Module Map — where things live in the UI

| Module | Navigation Path |
|---|---|
| Test Scripts | Test Management → Test Scripts |
| AI Test Builder | Test Management → AI Test Builder |
| Recorded Scripts | Test Management → Recorded Scripts |
| Pull Requests | Test Management → Pull Requests |
| TestBots | Test Execution → TestBots |
| TestBot Folders | Test Execution → TestBots → Folder View |
| Execution Queue | Administration → Execution Queue |
| Scheduler Admin | Administration → Scheduler Admin |
| Integrations | Administration → Integrations |
| Tunnel | Administration → Tunnel |
| System Status | Administration → System Status |
| Archive Manager | Administration → Archive |
| Project Roles | Administration → Global Settings → Project Roles |
| UI Components (Websites/Pages) | UI Components |

### Test Script Hierarchy
Epics → Stories → Test Scripts (storyId links a script to a story)
A script with `storyId: null` is standalone — visible in "All Test Scripts" flat list, not in any epic/story view.

### Key Terminology
- **TestBot** — an execution configuration that runs a set of test scripts
- **Test Script** — a sequence of test steps (stored as `testSteps[]` in the API, field `name`)
- **Epic** — top-level grouping (e.g. a feature or module)
- **Story** — child of an epic (e.g. a user story or scenario)
- **Suite** — a flat collection of test scripts for batch execution
- **Environment** — execution target (URL + browser config); supplies `gridUrlForExecution` and `browser`
- **Website** — an application under test; fields: `name`, `websiteUrl`
- **Page** — a screen/view within a website; fields: `pageName`, `pageUrl`
- **Locator** — a UI element selector (XPath, CSS, aria-label) attached to a page
- **Recorded Script** — a browser session captured by the TestBot Recorder Chrome Extension
- **Tunnel** — secure bridge that exposes a private/local app to the cloud execution grid

### Project Roles & Permissions
Five permissions: VIEW (blue), EXECUTE (green), EDIT (orange), DELETE (red), SHARE (purple)
VIEW is independent — it is NOT automatically granted when other permissions are added.
Roles: Site Admin, Project Admin, QA Manager, QA Director, Tester, Business Sponsor

### AI Test Builder
Chat-based interface. Three quick-start modes:
- **Test Script from App** — generates steps from an existing UI Components website
- **Test Script from Repo** — generates from a connected source code repository
- **Test Script from Req** — generates from a requirements doc or plain-text description

### Integrations Available
Azure DevOps, Jira, Slack, Jenkins, GitLab, Bitbucket

### Tunnel Commands
`status`, `start`, `stop`, `execute` — these 4 ONLY. (This list previously also claimed
`restart`/`health`/`info`/`logs`/`version` — those are not implemented anywhere in
`ahq-gateway-services` and never were; corrected 2026-07-12.) Tunnel endpoints are served at the
gateway ROOT (`/tunnel`, `/tunnel-launcher`, no service prefix) and require a `ROLE_TUNNEL_CLIENT`
JWT — the org API token 403s, so `TunnelClient` mints one per call via `POST /token/tunnel` and
sends it as `Authorization: Bearer`.

**Known dev-environment gap (2026-07-12)**: the deployed dev gateway 403s even a freshly-minted
tunnel token (confirmed live, bearer-only). The source's `TunnelTokenController` adds
`ROLE_TUNNEL_CLIENT` to the token's `roles` claim with a `// <-- add this line` comment — the
deployed build evidently predates that fix and mints role-less tokens. The tunnel tools match
current source and will start working when the gateway is redeployed; a tunnel-tool 403 on dev is
this, not a client bug.

### Execution Queue — Status Values
`PROCESSING` (running), `ENQUEUED` (waiting), `PENDING` (scheduled)

### Version Control Concepts
- Branches per script (fork from `main`)
- Pull Request lifecycle: Open → Review → Merge (or Close / Resolve Conflicts)
- Conflict resolution: Accept Ours / Accept Theirs / Keep Both (per field and per step)
- Closing a PR does NOT delete the source branch

### Archive / Soft Delete
Deleted assets go to Administration → Archive. Can be restored or permanently deleted.
Asset types: Epics, Stories, Applications, Pages, Locators, Recorded Scripts, Test Scripts, Test Sets, TestBots, TestBot Folders

### Scheduler Admin
Centralized view of all scheduled TestBot runs. Schedules are created per-TestBot (clock icon) and managed here. Toggle Enabled without deleting to pause.

**One-time scheduling removed (2026-07-15)**: `schedule_bot_once` (background-v2-services'
`/background-jobs/execution-jobs/schedule-once-at`) is gone — the tool, its client method
(`BackgroundClient.schedule_bot_once`), and its validator (`ScheduleOnceArgs`) were all deleted.
It shared the same UI-invisible, unreliable dispatch mechanism already called out for the old
`schedule_bot_recurring` path: a call reports success and shows a PENDING job that never actually
executes, and the job later vanishes from status lookup (confirmed live). There is no known
reliable one-time-schedule endpoint on this platform — for a single future run, use `execute_bot`
at the right time instead (schedule an external trigger, or just call it when the moment arrives).

### System Status
Health check per microservice: Liveness, Readiness, Ping — each shows UP (green) or DOWN (red).

---

## API Field Reference (avoid wrong field names)

| Entity | Correct fields |
|---|---|
| Page | `pageName`, `pageUrl` |
| TestScript | `name`, `testSteps` (NOT `steps`) |
| Website | `name`, `websiteUrl` |
| TestBot execution | `gridUrlForExecution`, `browser`, `profileId` |

### TestStep shape — never fabricate a `templateId`, and use `parameters` NOT `params`

A `TestStep` is NOT `{action_type, locator, value}`, and its values do NOT go in the `Params params`
field (that typed field exists on the entity but is NOT what `TestScriptController` reads to build
step titles or execution values — confirmed live, twice, the hard way: a script created with only
`params` set produced literal `"(Pending) typeValuePair not found"`/`"(Pending) uiLocator not found"`
text in every step). The field that actually drives everything is the generic
**`List<Parameters> parameters`**, keyed by the placeholder name in `templateTitle`. Proven-working
shape (this exact payload produced correct, human-readable step titles against a live org):

```json
{
  "templateId": "template-id-3",
  "templateTitle": "Enter {{text}} for the {{ui-locator}}",
  "sequence": 1,
  "parameters": [
    {
      "key": "ui-locator",
      "value": {"locatorId": "2be39546-2861-4e02-92b6-ab8a2ff4c126"},
      "paramClass": "ai.automationhq.commons.entities.assets.UILocator"
    },
    {
      "key": "text",
      "value": {"type": 0, "value": "testuser@example.com"},
      "paramClass": "ai.automationhq.commons.entities.assets.TypeValuePair"
    }
  ]
}
```

- `templateId` references a live, per-project catalog (platform built-ins + org-defined
  "Common Functions") — there is no fixed enum of action types to hardcode. Always resolve it via
  `search_step_templates`/`list_step_templates` first; use `get_step_template` to see exactly which
  placeholder names (`{{...}}` tokens in `templateTitle`) a given template expects.
- **`templateTitle` is REQUIRED for built-in templates** (`templateId` like `"template-id-N"`) —
  copy the exact `templateTitle` string (placeholders intact) from the search/get result verbatim.
  Confirmed via `TestScriptController.updateIdForSteps()` →
  `getTestStepTitleFromTemplateTitleAndParameterList(step.getTemplateTitle(), ...)`: for built-ins
  the server does `templateTitle.contains("{{")` directly on whatever the CALLER sent — it does
  **not** re-fetch the Template document server-side. Omit it and every `create_test_script` call
  with a built-in `templateId` throws a 500 (`Cannot invoke "String.contains(...)" because
  "templateTitle" is null`). Common-Function templateIds (real UUIDs, not `template-id-N`) take a
  different code path (`generateCommonFunctionTitle`) that derives the title itself — `templateTitle`
  is not needed for those.
- **Each `parameters[]` entry's `key` must match a `{{placeholder}}` name in `templateTitle`**
  (e.g. `templateTitle: "Enter {{text}} for the {{ui-locator}}"` needs one entry keyed `"text"` and
  one keyed `"ui-locator"`). Confirmed via `TestScriptController.updatedValueFromParameterList()`'s
  switch statement, keyed on the placeholder name.
- **`ui-locator`/`uiLocator` key**: value is `{"locatorId": "<real id>"}` — just a reference to a
  locator already saved via `add_locators`/`pushSpyElements`. **Do not fabricate `locateBy`/
  `locatorValue`/`locatorType` here** — the server looks the real locator up by `locatorId` on the
  page and enriches those fields itself (`TestScriptController.enrichUILocatorsWithPageId` /
  `enrichUILocatorParameter`). A locatorId that doesn't exist on any page produces
  `"(Pending) uiLocator not found"` in the step title, not an error.
- **Any scalar key** (`text`, `number`, `expected`, ...): value is a `TypeValuePair`:
  `{"type": <int>, "value": "<literal>"}`. `type` codes (from `typeAwareDisplay()`): `0` = literal
  (the common case), `1` = data-driven column, `2` = configuration var, `3` = runtime variable,
  `5` = parameter reference, `6` = faker/random, `7` = vault secret.
- `testStepTitle` is optional — the server regenerates it from `templateTitle` + `parameters` on
  every save/read (`getTestScriptById` does the same rebuild), so don't rely on whatever value you
  send surviving as-is.
- `search_step_templates`'s project-scoped variant (`/rest/api/templates/{projectId}/search`) only
  returns this org's own saved custom templates and is often empty. **Built-in templates only
  surface through the root-level `/rest/api/templates/search?title=` endpoint** (merged with this
  org's Common Functions) — this is what `test_mgmt_client.search_templates()` actually calls.
  Single-word searches work far better than phrases: "Navigate" only matches history back/forward,
  use "Go to"/"Open" for URL navigation; "Enter Text" matches nothing, use "Enter"; "Assert Text"
  matches nothing, this platform's verb is "Verify".

### create_test_script — required fields, now validated before the API call (2026-07-10)

`ahq-data-commons`' `TestScript` entity carries zero Bean Validation annotations — "required" was
never enforced by the entity itself, only by `automationhq-frontend-v2`'s own create-script form
(zod schema). That frontend schema is the real, authoritative contract, confirmed field-by-field
this session, and `src/schema/asset_kinds.py`'s `TestScriptCreateArgs` now enforces the same rules
**before** `create_test_script` reaches the API — a missing field returns a clean `{"error": ...}`
immediately instead of a live 500 or a silently invisible/broken script:

- **`name`** — required, 1-120 characters.
- **`website_id`** — required (was previously only "recommended" here — that was wrong). Separate
  field from `page_id`. Drives the UI's "Application" column; a script with only `page_id` is
  invisible in the Table View.
- **`story_id`** — required (was previously only "recommended" here — that was wrong). A script
  with no story attached is excluded from the Table View's default listing entirely, even though
  the user-guide documents that view as "flat list of all scripts." Get a real one via
  `list_epics` → `list_stories`; if nothing fits, use `create_epic`/`create_story` (both now
  exposed as tools — previously dead code in `test_mgmt_client.py`) rather than skipping the field.
- **`status`** — required, one of: Not Started, In Progress, Ready, To Be Repaired, On Hold.
  Defaults to `"Not Started"` if omitted. Sending it as an unrecognized value is now rejected
  locally instead of tripping the UI editor's `"Expected string, received null"` validation later.
- **`repair_comment`** — required only when `status` is `"To Be Repaired"` (a conditional rule from
  the frontend form, not documented here before this session). Omit otherwise.
- **`type`** (`script_type` param) — plain `String`, no server-side default; Jackson treats an
  absent key the same as JSON `null`, which trips the UI editor's validation on open even though
  creation succeeds. Defaults to `"WEB"` — leave unless there's a real reason to change it.
- Each step's `templateId` referencing a built-in template (`"template-id-N"`) still requires
  `templateTitle` verbatim, exactly as before — now enforced by the same validator instead of only
  documented (search/get_step_template results copy the string; the server does not fetch it for
  built-ins, and omitting it 500s).

Also: **`main` can be a protected branch** — direct `PUT` edits to an existing script on `main` may
403 (`"Direct edits to protected branch 'main' are not allowed. Create a working branch and use a
Pull Request."`). If you need to fix a mistake on a script that's already on `main`, delete and
recreate rather than editing, unless a PR flow is explicitly wanted.

### Version Control tools (2026-07-12)

`ProjectBranchController` (`/rest/api/projects/{projectId}/branches`) + `PullRequestController`
(`/rest/api/projects/{projectId}/pull-requests`), both test-management, standard `org-id` header.
14 tools; full branch→commit→PR→diff→close cycle verified live (PR #16).

- **Branch names travel as QUERY params (`?branchName=`), never path segments** — names may
  contain slashes (`feature/login`).
- **`create_branch` is two-phase**: the server's preflight conflict check may return
  `status: NEEDS_CONFIRMATION` instead of creating. The client/tool surfaces this and never
  auto-retries — relay to the user, resend with `confirmed=true` only after they agree.
  `strategy`: `FROM_BRANCH` (default) or `FROM_CURRENT`. Lombok `boolean isProtected` binds as
  JSON `"protected"` (client sends both spellings).
- **PR lifecycle endpoints (`approve`/`merge`/`close`/`rebase`/`ready-for-review`) take NO
  request body** — do not invent one. Closing a PR does NOT delete the source branch.
- `get_scripts_for_branch` (GET `/branches/scripts?branchName=`) is the ONLY correct answer to
  "which scripts are on branch X" — `TestScript.currentBranchName` does not reflect real
  membership (confirmed live: 1 vs the true 7).
- **Deferred to 9d-ii**: conflict resolution (`/merge/continue` with resolvedSteps /
  keepBothSteps / resolvedFields, PR rebase) — `merge_pull_request` returning a CONFLICTS status
  means "send the user to the UI's Resolve Conflicts flow".

### Project Roles (2026-07-12)

`ProjectRoleController` lives in **`ahq-test-management-services`** (not user-management, despite
being an admin feature), base `/rest/api/projects/{projectId}/roles`, standard `org-id` header.
Tools: `list_project_roles` / `create_project_role` / `update_project_role_permissions` /
`delete_project_role` / `assign_project_role` / `list_project_members`. All verified live except
`assign_project_role` (unit-tested only — assigning would change a real user's access).

- Permissions are exactly the 5-value enum `VIEW`/`EXECUTE`/`EDIT`/`DELETE`/`SHARE` (validated
  locally). **VIEW is independent — never add it implicitly** when granting others.
- **Role name is immutable** — the update endpoint reads only `permissions`; there is
  deliberately no rename parameter on the tool. Rename = create new + delete old.
- System roles (SITE_ADMIN, TESTER, PROJECT_ADMIN, QA_MANAGER, QA_DIRECTOR) cannot be deleted.
- `CreateRoleRequest.isDefault` is a Lombok `boolean isDefault` — Jackson's property name is
  `"default"`, not `"isDefault"`; the client sends both spellings so the flag actually binds.

### Archive Manager (2026-07-12)

One generic tool set — `list_archived_assets` / `restore_asset` / `permanently_delete_asset` —
covers 10 entity types via an `entity_type` enum (epic, story, website, page, locator,
test_script, test_suite, test_bot, test_bot_folder, recorded_script). All verified live.

- All `*ArchiveController`s live in **`ahq-user-management-services`** (base path `/api/...`,
  NOT `/rest/api/...`), extending one generic `ArchiveController<T, ID>`:
  `GET {prefix}/archived`, `POST {prefix}/{id}/archive|restore`, `DELETE {prefix}/{id}/permanent`.
  The archived-list endpoint reads the **`organizationId`** header (third controller family found
  using that spelling instead of `org-id`).
- **Two route deviants**, handled inside the clients: `locator` lists at the prefix ROOT
  (`GET /api/archived-locators`, no `/archived` suffix) and hard-deletes at `DELETE /{id}` (no
  `/permanent`); `recorded_script`'s archive endpoints live on `RecordedScriptController` in
  **test-management** (`/rest/api/recorded-scripts/archived`, pages with `offset` not `page`) —
  the dispatcher routes that entity to `TestMgmtClient`.
- There is no archive *action* tool: archiving IS each module's own DELETE (soft-delete). E.g.
  `DELETE /rest/api/epics/{id}` archives the epic (and cascades stories/scripts).
- `permanently_delete_asset` is irreversible and only works on already-archived assets — confirm
  with the user first.

### Recorded Scripts (2026-07-12)

Recordings are created by the TestBot Recorder Chrome Extension, not by tools — the MCP surface is
read + promote: `list_recorded_scripts` / `get_recorded_script` / `promote_recorded_script`.

- **Header inconsistency INSIDE one service**: `RecordedScriptController` reads
  `@RequestHeader("organizationId")`, while every other controller in the same
  `ahq-test-management-services` (TestScript, Epic, TestBot, ...) reads `org-id`. The client adds
  `organizationId` per recorded-script call via `_recorded_headers()` — don't "simplify" it away.
- **`story_id` is required for a first-time promotion** — the server itself throws
  (`"storyId is required for first-time promotion"`), and it travels as a **query param**
  (`?storyId=...`), not in the body. Re-promoting an already-promoted recording updates the linked
  TestScript and ignores storyId. `get_recorded_script` returns `promotedTestScriptId` to tell the
  two cases apart.
- The promote body (`PromoteRecordedScript`) is all-optional overrides (name/websiteId/status/
  description/steps/keepStepIds/...) — but `currentBranchName` is always sent explicitly
  (default `"main"`) for the same reason `create_test_script` does: a blank branch falls back to
  the API token's ambient checked-out-branch ProjectState, which is not reliably `main`.

### Common Function (User Test Step) CRUD — the destructive-PUT trap (2026-07-12)

`PUT /rest/api/commonFunctions/{id}` is a **full-document replace, not a patch**:
`CommonFunctionController.updateCommonFunction` saves the request body directly, forcing only
`commonFunctionId` back onto it — it does NOT carry over `testSteps`, `parameters`, `returnType`,
`websiteId`, `status`, or even `organizationId`/`projectId` from the existing document. A partial
PUT body silently wipes every omitted field AND orphans the function from its org. This is the bug
class behind the real User Test Step rename incident (2026-07-09).

- **Never update a Common Function via `call_api` with a partial PUT.** Use
  `update_common_function` — it GETs the full document, merges the requested changes, and PUTs the
  merged whole back, so a rename is just `update_common_function(id, name="new name")`.
- **Encrypted-value mask trap**: `GET /{id}` masks encrypted-template literal values
  (`template-id-105`'s `password`, `template-id-98`'s `text`; type-0 values become all-asterisks).
  A blind GET→PUT would therefore overwrite the real stored value with `"********"`.
  `update_common_function` detects this and refuses unless the caller supplies replacement
  `steps` — vault references (type 7) are unaffected and are the better way to store credentials
  in a Common Function anyway.
- The **list endpoint only routes when `offset` is in the query string**
  (`@RequestMapping(params = "offset")`) — it's a routing key, not a paging default; the client
  always sends it.
- Create contract (from the frontend zod schema, enforced in `asset_kinds.py`): `name` 1-120 chars,
  **letters/digits/spaces/hyphens only** (no underscores/punctuation); `website_id`, `status`
  (e.g. `"READY"`), and `return_type` (`{"type": "String", "name": "", "array": false}` — only
  `type` required) are all required; `description` max 600. Nesting is rejected server-side: a
  step's `templateId` must not be another Common Function's ID.

### A vault secret protects the script document, NOT the execution report

`{"type": 7, "value": "<secretName>"}` (`create_config_vault_secret` + the "From Secrets" value
type) keeps the real credential out of the stored test script: the step renders as
`Enter [vault: gfg_password] for the element: "Password input"` and the plaintext appears nowhere
in the document. That part works.

The executor, however, resolves the secret and writes the **resolved value** into the execution
report — confirmed live on a local-agent run:

```
"testStepName":  "Enter Onkar@123 for the element: \"Password input\" on the page: Login",
"statusMessage": "Entered value Onkar@123"
```

So anyone who can read an execution report can read the credential, and the vault buys storage-level
protection only. Tell users this when they supply a real password rather than letting them assume
the vault covers the whole lifecycle. (Fixing it belongs in the executor's step-title/status
rendering, not here — the MCP server never sees the resolved value.)

### Ask which branch a new script lands on — `main` is protected and silently strands edits

`create_test_script`'s `branch_name` defaults to `"main"`, and taking that default is a trap
rather than a safe choice. `main` is a **protected** branch (`list_branches` -> `protected: true`),
so `commit_branch("main", ...)` returns
`403 Direct push to protected branch 'main' is restricted. Use a Pull Request.`

The failure this produces is silent, because the edit itself succeeds. `add_test_steps` returns
`"Test script updated successfully"`, a subsequent GET shows the new step in place, and
`versionCount` goes to 2 with `commitMessage: null` — but **`execute_bot` runs the last COMMITTED
version**, which is still version 1. Confirmed live: a `"Wait for 10 seconds"` step inserted
between a sign-in click and a `Verify current URL` step never appeared in the execution report at
all (6 step results for a 7-step script), and the run failed on precisely the step the wait was
meant to fix. Nothing in either the edit response or the report says a version was skipped.

So: **ask the user which branch the script should live on before creating it** — a new branch, or
one of the real ones from `list_branches` — and pass it as `branch_name`. That same branch is what
`execute_bot` needs as `targetBranchName`, so deciding it after the script exists means redoing the
work. To move an existing script onto a branch, `create_branch(..., strategy="FROM_CURRENT",
script_ids=[id])` forks it across, then `commit_branch` on the new (unprotected) branch.

Note that a commit's `scriptVersionIds` records the version each script was pinned at — check it
against the script's own `currentVersionId` to confirm the commit actually captured the edit you
expected rather than an earlier version.

### Post-navigation race: always wait between a navigating action and its verify step

`"Verify current URL contains path {{expected}}"` (`template-id-117`, `ActionLibraryServices.
verifyUrlContainsText`) does **one immediate, unretried check** — `getCurrentUrl().contains(text)`,
no polling loop, no timeout param. Confirmed live: a script with `Click {{ui-locator}}` (sign-in)
immediately followed by this verify step ran too fast for the browser to have navigated yet, so it
executed against the pre-click URL and failed; the same script passed reliably once a wait step
was inserted between the click and the verify. Any step that can trigger navigation (a submit
click, a link click, anything a `Verify current URL`/`Verify page title`/element-visibility check
runs right after) needs an explicit wait step in between — never assume the next step will find
the destination page already loaded.

Two built-in templates do this (`ActionLibraryServices.java`, seeded in
`ahq-user-management-services/src/main/resources/db/taf-db.template.json`):
- **`template-id-36`, `"Wait for visibility of {{ui-locator}} for {{number}} seconds"`** —
  polls until a known element on the destination page appears, up to `number` seconds. Prefer this
  whenever a locator on the destination page is already known (e.g. a dashboard element) — it
  resolves as soon as the page is actually ready instead of always burning the full duration.
- **`template-id-35`, `"Wait for {{number}} seconds"`** — plain fixed delay, no locator needed.
  Simpler but brittle: too short reintroduces the race under real network conditions, too long
  just slows every run down. Use only when no destination-page locator is known yet.

`template-id-37` (`"Wait for clickability of {{ui-locator}} for {{number}} seconds"`) is the same
idea for waiting on an element to become interactable rather than a navigation to complete.

**Script-generation skills insert a wait step automatically now** (see `testbots-gen-from-url`/
`testbots-gen-from-requirements` Rules) — this is not something to re-solve per script by hand.

### Execution path — create TestBot → execute → results (2026-07-13, proven live end-to-end)

The full conversational chain works: `create_test_script` → `create_suite` → `create_test_bot` →
`execute_bot` → `get_execution_status` → `get_execution_report`. Verified live: RealtyVista login
script, 9/9 steps PASSED on the AHQ Premium Grid.

- **A TestBot carries NO browser/grid/environment config.** It's only name + botType +
  `testSuites` (min 1). Browser/grid arrive at TRIGGER time as an `ExecutionConfiguration` on
  the execute call. Don't look for execution config on the bot — it isn't there.
- **Scripts attach to suites, suites to bots.** `testScripts` is EMBEDDED in the TestSuite
  document; there is NO `POST /suites/{id}/scripts` endpoint (the old client called one — it
  never existed). `add_scripts_to_suite` is a GET-merge-PUT on the whole suite document.
- **execute_bot contract** (POST `/rest/api/bots/{botId}/execute`, executor service; body is a
  `BotExecution`): `baseUrl`, `browser`, `browserVersion`, `osType`, `gridId` all required —
  `browserVersion` and `osType` are enforced server-side even though the UI's zod schema allows
  them empty. Valid values come from `get_grid_capabilities` (wraps the config-services
  `/rest/api/grids/provider/{gridId}/*` endpoints — they exist ONLY on config-services; calling
  them on other services 404s). Plain Selenium grids report platform `"Grid OS"` and version
  `"latest"`; TestingBot/BrowserStack report real OS/version lists.
- **`baseUrl` is an ENVIRONMENT ID, not a URL — the field name lies.** The backend resolves it
  via `environmentRepository.findById(baseUrl)`; the value-resolution path has a raw-URL
  fallback (so a URL sometimes appears to work) but `getBaseUrlName()` has none and kills the
  run at report time with "Environment not found for this id" — found live: the run enqueued
  fine, ran 6 minutes, then died. The validator now rejects raw URLs upfront. Get an ID from
  `list_environments` or create one with `create_environment(name, url)` (URL lives in the
  Environment's `value` field).
- **Config-services lookup lists route on `params = "offset"`** with offset/size/sortBy
  REQUIRED (grids, browsers, environments — same routing-key trap as CommonFunctions). This was
  the root cause of the long-standing "list_environments hits the wrong endpoint" gap: the bare
  GET matched no handler at all. The clients now always send paging.
- **Results endpoints** (executor): `GET /rest/api/bots/execution/{id}/status` (lightweight
  poll) and `/detailed-results` (full per-suite/script/iteration/step report —
  `get_execution_report`). The previously-used `/execution/{id}/results` path never existed.
  The lightweight status reports `UNKNOWN` once the run leaves the queue — the
  `get_execution_status` tool auto-falls-back to the detailed report's overall status; for
  queue progress use `get_job_status` with the jobId from execute_bot's response. Runs can sit
  ENQUEUED ~2-3 minutes before a browser starts.
- **`list_recent_runs` rewired (2026-07-13)**: `GET /background-jobs/execution-jobs` never
  existed (404 since day one — ExecutionJobController is POST-only). Recent runs come from
  test-management's `TestReportController`: `/rest/api/testreports/{botId}` (one bot's history)
  or `/rest/api/testreports/bots/list` (paged, across bots).
- **`get_context` is a slimmed discovery snapshot** (id/name/status per entity, lists
  capped at 100): the raw documents came back at ~137K characters in a real org. Fetch full
  documents with the dedicated get_* tools.
- **Duplicate-name guard + retry trap**: TestBot and TestSuite creates reject duplicate names.
  The base client retries timeouts, but POST create is not idempotent — a timed-out create that
  actually persisted resurfaces as a confusing "This name already exists" on the retry
  (happened live, twice). If a create 400s with name-exists right after a slow call, check
  `list_suites`/`list_bots` — the entity probably exists.
- **Dev grid health (2026-07-13)**: in-cluster `Selenium Hub` (ahq-selenium-hub.ahq-dev...) and
  `Selenium` (selenium-hub.automationhq.ai) both fail to open sessions — infra, not client.
  `AHQ Premium Grid` (TestingBot) works. A 1-2 second FAILED execution = grid session failure;
  read `iterations[].errorMessage`.
- Execution runs as cloud (`test-bot-executor-services`) for paid orgs; the UI falls back to the
  user's local agent (localhost:9202) otherwise — the MCP execute_bot tool is the cloud path.

### TypeValuePair type codes + friendly forms (2026-07-14)

Full code table (from `typeAwareDisplay()`), previously undocumented past type 0: `0` literal,
`1` data-driven column, `2` configuration/global parameter (value = param name), `3` runtime
variable, `5` parameter reference, `6` faker/random (value = generator name), `7` vault secret
(value = secret name). Script-writing tools (`create_test_script`, `add_test_steps`,
`update_test_script`) also accept friendly single-key forms that are translated client-side:
`{"literal": "x"}`, `{"configuration": "baseUrl"}`, `{"vault": "password"}`,
`{"variable": "v"}`, `{"data_column": "col"}`, `{"faker": "Email"}`, `{"parameter": "p"}`.

### Script editing + response hygiene (2026-07-14, from the second field report)

- **`add_test_steps` / `update_test_script`**: PUT `/rest/api/stories/scripts/{id}` is a
  full-document update — both tools are GET-merge-PUT (like update_common_function), with
  sequence renumbering on insert. Protected-branch (often `main`) edits 403 by platform policy.
- **`list_websites`** exists now — `search_websites("")` returns `[]`, making "list my websites"
  unanswerable before.
- **`add_locators` returns the created locator IDs** (`{locators: [{locatorName, locatorId}]}`)
  — no more get_page_by_url round-trip to build script steps.
- **List paging**: `-1/-1` offset/size is REJECTED on some deployments ("Page size must not be
  less than one") while accepted on others — all list calls now send `offset=0&size=500`.
- **ResponseObj slimming**: mutation responses arrive as a ~25-field mostly-null login-shaped
  envelope with an untrustworthy `success` flag (defaults false even on success). The MCP layer
  strips it to `{id, message, success}` and derives `success` from the message/status — don't
  re-add a "verify with a GET" step for creates that report success.
- `/users/me` 500s for ORGANIZATION tokens (no userId claim — server quirk); `get_context`
  falls back to identity from token claims. `list_projects` uses
  `/rest/api/projects/organizations/{orgId}/all` (the bare path has NO handler; 405'd forever).

## Eval harness (`evals/`, slice 9i, 2026-07-12)

Golden-task suite proving END-TO-END behavior against the live dev API — the runtime twin of the
planned 9h spec-drift CI. Run `./.venv/Scripts/python.exe -m evals.runner` after any CLAUDE.md/
skill/validator/client change or backend deploy; results append to `evals/results.jsonl` (the
trend history is the deliverable — a dropped check or a jump in calls/seconds vs earlier lines is
a regression). 5 tasks: login_script (full generation path), archive_restore, vc_pr_flow,
uts_rename (the destructive-PUT regression test), global_param. Tasks create `EVAL-9i …`-named
throwaways and clean up best-effort (`d.try_call`). NOT part of pytest — it hits the live API.

Trap found by the harness itself: **`DELETE /rest/api/epics/{id}` on an epic WITH stories returns
202 and silently deletes NOTHING** ("This Epic is associated with…") — pass `?force=true` (or use
`/deleteAnyway`) to actually cascade-archive. An empty epic deletes fine without force, which is
why the 9b live test never hit this.

## Gateway routing gotcha — mtaf-* services

The gateway routes the `managed-testing-*`/`mtaf-cdct-core` family on SHORT route ids, not the repo
name. `src/config/ahq_services.py` constants already reflect this (fixed 2026-07-08) — don't
"correct" them back to the repo name if it looks wrong at a glance:

| Repo name | Real gateway route |
|---|---|
| `managed-testing-service-core` | `/mtaf-core` |
| `managed-testing-virtualization-client` | `/mtaf-sv-client` |
| `managed-testing-virtualization-server` | `/mtaf-sv-server` |
| `mtaf-cdct-core` | `/mtaf-cdct` |

`test-local-execution-services` has NO gateway route at all — it runs on the user's own machine
(ports ~9200/9202), not in the cluster. Any tool targeting it needs a direct-to-localhost call with
a fast-fail liveness check (see `local_exec_client.get_agent_status()`), not a gateway call.

## Services intentionally NOT covered by hand-written tools

These are infra/session plumbing, not something a user would ask for in plain language — don't
treat their absence as a gap to "rediscover":
- `ahq-gateway-services` — pure ingress/auth/tunnel infra
- `ahq-auth-services` — login/SSO/JWT issuance (service-to-service, not conversational)
- Most of `ahq-standalone-local-v2-services` — internal proxy/cache layer for the local agent
  (exceptions: `FakeDataController.generate` and token validation have standalone user-facing value)
- Ops-only endpoints in any service: job queues, log-migration/cleanup/admin endpoints, SSE log streams

## API / Performance Testing (mtaf-core) — a separate flow from UI test scripts

`ahq-test-management-services`' TestBots run UI test scripts (browser automation). `mtaf-core`
(`managed-testing-service-core`) is a completely separate product surface for REST/GraphQL request
testing, chained-workflow testing, and JMeter-backed load testing. Don't conflate the two — a user
asking to "test my API" or "run a load test" means the mtaf-core tools below, not `create_test_script`.

- `list_api_collections` / `get_api_collection` / `create_api_collection` — Postman-style groupings
- `list_api_requests` / `get_api_request` / `create_api_request` / `test_api_request` — individual
  REST/GraphQL requests; `test_api_request` runs one immediately without saving it first
- `import_curl` / `import_postman_collection` — bulk-import existing requests instead of hand-building them
- `list_workflows` / `get_workflow` / `create_workflow` / `test_workflow` — chained multi-request flows
  (e.g. a full user journey); `test_workflow` runs a one-off test without saving
- `list_performance_bots` / `get_performance_bot` / `run_performance_bot` / `stop_performance_bot` /
  `get_performance_results` — JMeter load tests; `run_performance_bot` returns immediately with a
  metrics ID, poll `get_performance_results` for progress (test can run for hours)
- `list_vault_secrets` — metadata only (names/keys); deliberately does NOT expose the
  `/resolve/{key}` endpoint that returns decrypted plaintext, to keep secrets out of the conversation
  transcript by default

## Other niche tool groups (2026-07-08)

- **Local execution agent** (`test-local-execution-services`, runs on the user's own machine, no
  gateway route): `check_local_agent_status` (hits `TestExecutorController`'s `/ping` directly on
  localhost:9202 — NOT `/rest/api/agent/status`, which doesn't exist), `list_local_agents` (via
  `ahq-standalone-local-v2-services`' real path `/rest/api/local/agent/getAllAgents` — note the
  `orgId` header here, no hyphen, unlike every other service's `org-id`)
- **Fake/synthetic test data**: `list_fake_data_types` / `generate_fake_data` (also
  `ahq-standalone-local-v2-services`)
- **Email**: `send_email` (`ahq-email-v2-services`)
- **Pact contract testing** (`mtaf-cdct-core`, niche): `list_consumers`/`create_consumer`,
  `list_providers`/`create_provider`, `list_contracts`/`create_contract`, `run_pact_tests`. Uses
  lowercase `organizationid`/`projectid` headers — a THIRD header-naming convention alongside
  `org-id`/`projectId` (most services) and `orgId` (standalone-local).
- **Service virtualization / API mocking** (`mtaf-sv-server` only, not the near-duplicate
  `mtaf-sv-client`): `list_mock_mappings`, `get_mock_mapping`, `get_mock_mapping_template`,
  `create_mock_mapping`, `delete_mock_mapping`. The mapping body is a raw JSON string, not a typed
  object — sent as `text/plain` like `import_curl`, not `application/json`.

## MCP Tool Quick Reference

| Tool | When to use |
|---|---|
| `get_context` | Always call first — loads project snapshot, including API collections/workflows/performance bots (added 2026-07-10; previously mtaf-core was entirely missing from this snapshot) |
| `crawl_url` | Discover pages + locators from a live URL |
| `create_website` / `create_page` / `add_locators` | Build UI component library |
| `search_step_templates` / `list_step_templates` / `get_step_template` | Resolve a real `templateId` before writing any test step |
| `scan_broken_locators` / `heal_locator` / `apply_locator_fix` | Self-healing locators: find what's already flagged broken, propose a ranked replacement selector (propose-only, re-crawls with Playwright), then apply the approved one (kept as new primary, old strategies demoted not deleted) |
| `list_epics` / `create_epic` / `list_stories` / `create_story` | Resolve or create the `story_id` a test script requires (`create_epic`/`create_story` added 2026-07-10 — previously dead code with no tool wired up; `list_stories` previously had no tool at all) |
| `create_test_script` | Add a new test script — `website_id`/`story_id` are now hard-required and validated locally before the API call (see "API Field Reference" above) |
| `list_recorded_scripts` / `get_recorded_script` / `promote_recorded_script` | Recorder-extension captures; promote turns one into a real Test Script (`story_id` required first time — see Recorded Scripts section) |
| `list_common_functions` / `get_common_function` / `create_common_function` / `update_common_function` | Reusable User Test Steps; `update_common_function` is the ONLY safe way to rename/edit one (see destructive-PUT trap section) |
| `list_archived_assets` / `restore_asset` / `permanently_delete_asset` | Administration → Archive across 10 entity types (see Archive Manager section; permanent delete is irreversible) |
| `list_project_roles` / `create_project_role` / `update_project_role_permissions` / `delete_project_role` / `assign_project_role` / `list_project_members` | Project role definitions + user assignments (see Project Roles section; VIEW is never implicit, names immutable) |
| `list_branches` / `get_scripts_for_branch` / `create_branch` / `commit_branch` / `list_commits` | Version-control branches & commits (see Version Control section; create_branch is two-phase) |
| `create_pull_request` / `list_pull_requests` / `get_pull_request` / `get_pull_request_diff` / `approve_pull_request` / `request_pr_changes` / `merge_pull_request` / `close_pull_request` | PR lifecycle (no request bodies on lifecycle actions; conflicts → UI) |
| `get_tunnel_status` / `start_tunnel` / `stop_tunnel` / `execute_tunnel_command` | Tunnel control (see Tunnel Commands note — dev gateway currently 403s all tunnel tokens) |
| `list_bots` / `execute_bot` | Run a TestBot by name |
| `get_execution_report` | Per-step pass/fail for a finished run, by `execution_id` (not `job_id` — that's `get_job_status`, queue-level) |
| `schedule_bot_recurring` | Set up a cron schedule (one-time scheduling removed 2026-07-15 — see note below) |
| `test_api_request` / `test_workflow` / `run_performance_bot` | API/load testing — see mtaf-core section above |
| `get_service_spec` / `call_api` | Discover and call any AHQ endpoint not covered by a hand-written tool |

## Pre-flight validation (2026-07-10)

Every tool that creates/schedules something (`create_test_script`, `create_suite`,
`add_scripts_to_suite`, `schedule_bot_recurring`, `create_api_collection`,
`create_api_request`, `create_workflow`, `create_epic`, `create_story`,
`promote_recorded_script`, `create_common_function`/`update_common_function`) is now validated in
`src/schema/asset_kinds.py` before `_dispatch` calls the client/API — a bad payload returns a
clean `{"error": "..."}` immediately instead of a live 500 or a silently broken asset. These rules
were ported directly from `automationhq-frontend-v2`'s zod form schemas, not invented — that
frontend is the actual enforced contract in this platform, since `ahq-data-commons` entities carry
no validation of their own. See `src/schema/asset_kinds.py` for the full rule set per tool.
As of 2026-07-12 every §13 backlog slice is built (9a Recorded Script + Common Function,
9b Archive Manager, 9c Project Roles, 9d Version Control v1, 9e Tunnel, 9f plugin packaging).
TestBot creation (`create_test_bot`) and the execution-config source (`list_environments` /
`create_environment` / `get_grid_capabilities` / `list_grids`) were listed here as missing and
have since shipped — the two genuinely open items are **conflict resolution (9d-ii)** and
**drift-detection CI (9h, needs GitHub)**.

## Self-healing locators (`scan_broken_locators` / `heal_locator` / `apply_locator_fix`)

The platform already has a real self-healing subsystem that this repo previously ignored:
`Locator.locationStrategies` (ahq-data-commons) already models multiple ranked candidate
selectors per element, `ActionLibraryServices.getElementByWithFallback()` (ahq-actions-commons,
shared by both cloud and local-agent execution) already tries every strategy before failing, and
a failure already POSTs to `ahq-asset-services`' AI Brain (`/report-broken`), which already marks
the `Locator.broken = true` and creates a `MaintenanceAlert`. `GET /rest/api/locators/broken` was
already live and simply unused by any tool until now.

What was missing was real candidate *generation* — nothing re-scanned the live DOM. These three
tools close that gap without any backend changes:

- `scan_broken_locators` → `AssetClient.list_broken_locators()`, the existing endpoint above.
- `heal_locator` (`src/tools/heal_locator.py`) — propose-only. Re-crawls the locator's page with
  Playwright (same engine as `crawl_url`), extracts ranked candidate selectors per element
  (id → data-testid → aria-label → name → class fallback), scores them against the broken
  locator's own `locatorName` via string similarity, and keeps only candidates that resolve to
  **exactly one** element (`count() == 1` — a selector matching several elements is not a fix).
  Writes nothing.
- `apply_locator_fix` → `AssetClient.apply_locator_strategy()`. Adds the chosen candidate as the
  new primary strategy — **never deletes any existing strategy**. This deliberately does NOT
  reuse `update_locator`, which always collapses `locationStrategies` down to a single entry and
  would destroy the fallback history; a future session must not "simplify" the two back together.

**Confirmed live: the new strategy MUST be placed FIRST in `locationStrategies`, not appended.**
`PageService.normalizeStrategySelection` (`ahq-asset-services`) marks a strategy `selected` by
matching `locateBy` TYPE only ("css" == "css"), never the actual `locatorValue` — so two
same-type strategies (the overwhelmingly common case: an old broken CSS selector and a new
working CSS selector) both end up `selected: true` regardless of what this client sends.
`ActionLibraryServices.getElementByWithFallback` then breaks in a specific way:
`getSelectedLocationStrategy()` is `stream().filter(selected).findFirst()` (array order wins among
`selected=true` entries), while `getFallbackLocationStrategies()` excludes every `selected=true`
entry from the fallback pool. Net effect: whichever same-type strategy sits first in the array is
the *only* one ever tried at execution time — anything placed after it is neither the chosen
primary nor an eligible fallback, i.e. permanently unreachable. Verified live end-to-end against
`https://the-internet.herokuapp.com/login`: appending the new strategy last left BOTH entries
marked `selected: true` with the broken one still first (unreachable fix); putting the new one
first made it the entry `getSelectedLocationStrategy` actually returns. This is a real
`ahq-asset-services` limitation (it should key selection on `(locateBy, locatorValue)`, not
`locateBy` alone) — worth a small, separate PR there someday, but out of Track 1's "no backend
changes" scope, and worked around client-side by ordering alone for now.

This is the MVP track from the self-healing locators plan — the durable, license-gated version
(new `RemediationSuggestion`/`RemediationCategory` entities, DOM snapshot capture, an approval API
in `ahq-asset-services`) is scoped in `AI_SMART_SCANNER_REMEDIATION.md`
(`ahq-background-v2-services`) as a follow-up; once that API exists, these three tools should be
re-pointed at it instead of the direct-PUT approach used here.

**Known upstream gap — half the executions never feed this.** `scan_broken_locators` can only
return what the platform already flagged, and only the CLOUD execution path does the flagging:
`ActionLibraryServices.setBrokenLocatorReporter(...)` is wired in `ahq-background-v2-services`'
`TestRemoteExecutionRepository.java` and has **zero** occurrences in
`test-local-execution-services` (re-verified 2026-07-28). A locator that breaks during a
local-agent run therefore produces no `MaintenanceAlert` and never appears in
`GET /rest/api/locators/broken`. So an empty `scan_broken_locators` result does NOT mean "nothing
is broken" for a user who runs locally — say so rather than reporting all-clear. Fixing it is a
contained one-repo change (add the same wiring at each `ActionLibraryServices` construction site,
where `setSmartClickRetryEnabled` is already set) and should land before Track 2, which needs both
paths reporting symmetrically for its failure-category data to mean anything.

## Third field report — the "accepted, then wrong" class (v1.9.0)

Three separate reported failures turned out to be the same shape: the platform **accepts** the
call, so the tool returns success, and the consequence only appears minutes later or never. That
is why each one cost a full execution cycle before anyone noticed.

- **A script edit lands on the token's ambient branch, not the script's.** `add_test_steps` /
  `update_test_script` are GET-merge-PUT, and the GET returns `currentBranchName` set to *this
  request's* ambient checked-out branch (this was already documented on `get_test_script`'s own
  tool description) — PUTting it straight back is what moved a step onto an unrelated feature
  branch. Both tools now take `branch_name`, which pins `currentBranchName` on the outgoing
  document, and both always echo `branchName` in the response so an unpinned edit's destination is
  at least visible. Pass the branch the script was created on; do not rely on the default.
- **`execute_bot` accepted ids that no longer exist.** `_is_local_grid` swallowed a failed
  `get_grid` lookup and returned False ("cloud grid"), so a stale gridId enqueued a doomed run that
  died mid-flight on a null `gridUrlForExecution`. `_preflight_execution_configuration` now checks
  `gridId`, `baseUrl` (the environment id) and `targetBranchName` against their own list endpoints
  before submitting, on `execute_bot`, `schedule_bot_recurring` and `update_schedule` — the
  scheduler cases matter most, since a dead id there fails unattended on every fire. It is
  deliberately **fail-open**: a lookup that itself errors, or returns an empty list, is treated as
  unverifiable and lets the run through (same discipline as `create_test_script`'s branch check —
  the check must never become the failure).
- **A credentialed `crawl_url` never captured the login form.** It filled and closed the login page
  without extracting anything, then crawled from the original URL — by which point the context held
  a session, so that URL rendered the authenticated app. Result: the whole product and none of the
  email/password/submit locators, forcing a second credential-less crawl. `_capture_page` (extracted
  from the crawl loop, now shared) runs against the login page *before* submitting, and its URL is
  marked visited so the authenticated pass can't overwrite it.

The remaining items in that report were model-behaviour, not code, and are addressed where the
model actually reads them — tool descriptions and skill Rules, not here: run a branch via
`targetBranchName` instead of proposing a merge, re-fetch ids in the current session instead of
recalling them, poll with widening gaps, reuse one debug bot, diagnose with a cheap check before
spending another execution, and confirm epic/story reuse and script granularity with the user.

## Tool profiles — `src/tool_groups.py` (v1.7.0)

All 138 tool schemas are sent to the model on every request (MCP has no lazy schema loading):
~14.9k tokens of fixed overhead, and 138 near-neighbours to disambiguate. A wrong pick between
siblings like `list_bots`/`list_performance_bots` returns an empty list, not an error, so it
surfaces as a confidently wrong answer rather than a failure.

`list_tools()` now filters by an optional profile. Default is unchanged — omit it and all 138 are
advertised.

- **Hosted**: `?profile=core` on the MCP URL (the only lever connector clients have — they
  configure a URL and nothing else), or an `X-AHQ-Tool-Profile` header.
- **Stdio**: `AHQ_MCP_TOOL_PROFILE=core`. Deliberately NOT read on the hosted path — that process
  is multi-tenant, and one deployment-wide value must not truncate the list for clients that never
  asked (pinned by `test_hosted_request_ignores_the_stdio_env_setting`).
- A spec is comma-separated profiles and/or groups: `core`, `core,api`, `reporting`. Unknown
  tokens fall back to **all 138**, never to an empty list — a typo costs the reduction, not the
  tools, since an empty tool list reads as a broken server.
- `get_context` is injected into every profile regardless of spec.

**`core` (61 tools — roughly half of `full`) is defined as "every bundled skill keeps
working"**, not as a taste judgment. Two tests hold that definition, and both are needed:
`test_every_skill_works_under_the_core_profile` reads each `SKILL.md`'s own `tools:` frontmatter,
and `test_core_covers_every_tool_the_skill_bodies_name` reads the prose. The frontmatter is
hand-maintained and drifts from what the workflow actually instructs — checking only the
declaration is how `list_branches`/`commit_branch` sat outside `core` while the test stayed green,
leaving both generation skills telling the model to offer the real branches with no tool to list
them, so it fell back to the protected `main` that rule exists to avoid. A tool the body merely
*names* is still a dependency: the model stops at that sentence either way. If a skill gains a
dependency, bring the tool into `core` or move the skill out — do not delete either assertion.
A half-running skill is worse than a long tool list.

**Filtering is presentation, not authorization.** `_dispatch` stays permissive, so a hidden tool
still executes if a client calls it anyway; the security boundary is the AHQ token, which the
gateway re-validates on every call. Never use a profile to withhold a destructive tool.

`GROUPS` is an exact partition of `TOOLS`, enforced by `test_groups_are_an_exact_partition_of_the_tool_surface`
— a newly added tool that lands in no group fails CI, because otherwise it would silently never
appear in any profile. Kept as a name→group table rather than a field on each `Tool` so adding a
tool is a one-line edit and doesn't conflict with every branch in flight.

Measured: `full` 138 tools / ~65.7k chars / ~16.4k tokens; `core` 61 tools /
~38.2k chars / ~9.6k tokens. Measure with `model_dump(exclude_none=True, by_alias=True)` and compact JSON
separators — a naive `model_dump()` keeps every unset field and overstates the real payload by
~29%.

## Never guess a UI locator (2026-07-16)

Found live: when a fast path to real locators wasn't obviously available (e.g. a browser
extension the session expected wasn't connected), the model defaulted to hand-guessed generic
selectors (`input[type='email']`, `input[type='password']`) for a login step instead of using
`crawl_url`, which was sitting right there in the tool list the whole time. A guessed selector
"working" is an accident of the moment's markup and breaks the instant it differs even slightly.
The rule, now also stated directly in `crawl_url`/`get_page_by_url`'s own tool descriptions and
both generation skills: before writing any `ui-locator` step for a live page, call `get_page_by_url`
first to check for an existing locator; if none exists, call `crawl_url` to capture real ones.
Never fall back to a raw guessed selector as a substitute.

## MCP server bugs/gaps found building a UI-navigation regression script (2026-07-16, v1.0.1)

Building a script that logs in then drives Administration → Global Settings → Global Parameters →
Add Custom Property (no crawl_url path exists post-login) surfaced these:

- **Fixed: `execute_bot_locally` swallowed the local agent's error body.** It called a bare
  `r.raise_for_status()`, so an agent-side 400 reached the MCP caller as only
  `"Client error '400 '"` with the real reason (a validation message, a not-found id, whatever the
  agent actually said) discarded — confirmed live, the true cause was only visible by hitting
  `localhost:9202` directly with `httpx` outside the MCP layer and reading `r.text`. Every other
  client in `src/clients/` gets this for free via `BaseAhqClient._request`'s `AhqApiError`
  (status + reason + parsed `message` field) — `local_exec_client.py`'s hand-rolled local-agent
  call was the one path that bypassed it. Fixed by raising the same `AhqApiError` there instead;
  regression test: `test_execute_bot_locally_surfaces_agent_error_body_on_failure`.
- **Fixed: `crawl_url`'s "never reaches post-login pages" limitation (previously documented above
  as an accepted gap) had a root cause after all, and it's now fixed.** The post-submit wait was
  `login_page.wait_for_url(lambda u: "login" not in u.lower(), ...)` — trivially true from the very
  first instant whenever the crawl's own starting URL doesn't itself contain "login" (this app's
  bare root `/` also renders the login form), making the wait a same-tick no-op; `networkidle`
  alone doesn't catch the SPA's client-side-only `/checking` transitional redirect either (no
  further network activity during that hop). Fixed by capturing the URL before the click and
  waiting for it to *change* from that value instead. Confirmed live: `crawl_url` against
  `https://app.automationhq.ai/` now reaches the dashboard and discovers real authenticated pages
  (9 pages incl. support tickets and object-repository, vs. 4 pre-auth-only pages before). Residual,
  non-bug scope limit: crawl_url still only follows `<a href>` tags already in the DOM — pages only
  reachable by clicking a JS-driven menu (e.g. this app's Administration flyout, which AntD doesn't
  mount until clicked) still won't be found without a real click-driven crawl strategy.
- **Open gap: `execute_bot` against a cloud grid (TestingBot) failed a specific script's login
  step 100% of the time (2 runs, one bundled with another script, one fully isolated in its own
  suite+bot) while an unrelated script with an *identical* login+wait+verify sequence passed in
  the same batch.** Doubling the wait (10s → 20s) made zero difference — browser stayed on bare
  `/login` either way, ruling out simple slowness. Only local-agent execution (real Selenium logs)
  gave a concrete answer for the *next* failure in the same script (see below) — the cloud grid
  path returns no screenshot (`get_execution_screenshots` 404s) and no comparable log access, so
  this one is still unexplained. If it recurs: it's plausibly a click that registers in Selenium's
  eyes without the app's overlay/focus state actually accepting it (an actionability gap Selenium
  doesn't check the way stricter frameworks like Playwright do — this session hit exactly that
  class of issue, "element intercepts pointer events", on a different locator during manual
  Playwright verification of the same app).
- **Locator lesson: an AntD custom-dropdown option's class name is not safely reusable across
  different `<Select>` instances on the same page without seeing it render real data.** The
  "Custom Property Type" dropdown's options were verified live (`.ant-select-item-option-content`,
  confirmed by reading real rendered text via Playwright). The "Environment" dropdown's option
  locator was *pattern-matched* from that verified one, never actually seen against real data (the
  manual test account had zero environments configured, so it only ever showed "No data"). Running
  the finished script against the real target project's real "dev" environment produced
  `"All location strategies failed for locator 'Environment Option - dev'"` — a genuine
  local-agent Selenium log, not a timing issue. Generalizing a verified locator pattern to a
  same-looking-but-untested sibling element is the trap; there's no tool-level fix for this, just
  a reminder to flag "verified against real data" vs. "pattern-matched, unverified" explicitly when
  handing off locators.
- **Local-agent execution timing, for future reference**: a run through ~14 real UI steps
  (login + 4 levels of navigation + opening a dropdown) took about 3 minutes wall-clock on the
  local agent, matching the cloud grid's pace roughly 1:1 — local isn't inherently faster, it's
  just the only path with real debuggable logs.
