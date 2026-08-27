import asyncio
import json
import sys
import time
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import ValidationError

from src.config.ahq_services import settings
from src.config.credentials import AhqCredentials
from src.clients.base_client import AhqApiError
from src.clients.bundle import ClientBundle, DEFAULT_BUNDLE
from src.clients.generic_client import SERVICE_MAP
from src.hosted.audit import audit_log
from src.hosted.rate_limit import OrgRateLimiter
from src.prompts import get_skill_prompt, list_skill_prompts
from src.schema.asset_kinds import VALIDATORS, format_validation_error, RunExecutionConfiguration
from src.tool_groups import resolve_tool_names
from src.tools.crawl_url import crawl_url as _crawl_url
from src.tools.extract_requirements import extract_requirements as _extract_requirements
from src.tools.heal_locator import heal_locator as _heal_locator
from src.tools.script_inspection import (
    inspect_script as _inspect_script,
    validate_script_locators as _validate_script_locators,
    check_script_for_credential_exposure as _check_script_for_credential_exposure,
)
from src.tools.scoped_execution import (
    execute_script_only as _execute_script_only,
    execute_script_step_by_step as _execute_script_step_by_step,
)
from src.tools.locator_usage import find_locator_usage as _find_locator_usage
from src.tools.response_paging import paginate_script as _paginate_script, summarize_report as _summarize_report

server = Server("testbots-mcp-server")


class _HttpClientHolder:
    """
    Plain mutable holder (NOT a contextvars.ContextVar) for the one shared, long-lived
    httpx.AsyncClient used in hosted HTTP mode. A ContextVar was tried first and doesn't work
    here: it's set once in the Starlette lifespan task, but uvicorn spawns each incoming
    request in a separate, unrelated task — contextvars only propagate to child tasks created
    from a context where the var is already set, so the lifespan's value never reaches
    call_tool's task. A plain attribute, set once at startup and read on every call, is exactly
    what's needed since the client itself is intentionally shared across every tenant/request
    (only credentials vary per-request, and those already flow correctly through the SDK's own
    request_ctx contextvar since it's set inside the request-handling task itself).
    """

    client = None


app_http_client = _HttpClientHolder()

# Tools that are unsafe or meaningless to run from a centrally-hosted server:
# check_local_agent_status probes the SERVER's own localhost (not the caller's machine);
# extract_requirements reads an arbitrary path off the SERVER's disk (arbitrary-file-read
# surface once "local" isn't the caller's laptop). crawl_url left this set in Slice 9j
# (2026-07-14): hosted crawls are SSRF-guarded per navigation (src/tools/url_guard.py) and
# Chromium is baked into the Docker image.
_HOSTED_UNSUPPORTED = {"check_local_agent_status", "extract_requirements"}

# The one Grid record execute_bot's caller can pick that means "run on my own machine" — the
# same two URL forms test-local-execution-services' own BotExecutionRepository checks for.
_LOCAL_GRID_URLS = {"http://localhost:4455/wd/hub", "http://127.0.0.1:4455/wd/hub"}


async def _is_local_grid(clients: ClientBundle, grid_id: str) -> bool:
    """
    True if gridId resolves to the local-agent grid. Confirmed via a real browser HAR capture:
    for this grid, the Run TestBot dialog never calls the cloud executor at all — it POSTs
    straight to localhost:9202 (same machine as the browser). Routing through the cloud instead
    enqueues a job the cloud has no way to ever deliver to this developer's own machine, so it
    just sits ENQUEUED forever — a different, earlier-stage failure than the grid
    misclassification bug that only matters once a request already reached the agent.
    """
    try:
        grid = await clients.config.get_grid(grid_id)
    except Exception:
        return False
    return isinstance(grid, dict) and grid.get("url") in _LOCAL_GRID_URLS


def _named_ids(items, id_keys, name_keys) -> dict[str, str]:
    """{id: display name} out of a platform list response, tolerating its field-name variants."""
    out: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ident = next((str(item[k]) for k in id_keys if item.get(k)), None)
        if ident:
            out[ident] = next((str(item[k]) for k in name_keys if item.get(k)), ident)
    return out


async def _unknown_id_error(fetch, id_keys, name_keys, value: str, label: str, source: str):
    """
    Pre-flight one id in an execution configuration against the list it must come from.

    Every one of these is silently accepted by the platform and only surfaces minutes later, at
    the far end of a real browser session: an id that no longer exists produced a null
    gridUrlForExecution mid-run, a raw URL in baseUrl killed a run at report time after six
    minutes, and a mistyped branch ran a script version nobody edited. A list call up front turns
    all of that into an immediate, correctable answer.

    Returns an error dict, or None if the value is fine OR unverifiable. Fail-open is deliberate
    and matches create_test_script's branch check: a lookup that itself fails must never become
    the reason an otherwise-valid run can't start.
    """
    try:
        known = _named_ids(await fetch(), id_keys, name_keys)
    except Exception:
        return None
    if not known or value in known:
        return None
    return {"error": (
        f"{label} '{value}' does not exist in this project — the execution would be accepted and "
        f"then fail minutes into the run. Available (from {source}): "
        + ", ".join(f"{name} ({ident})" for ident, name in sorted(known.items(), key=lambda kv: kv[1]))
    )}


def _name_key(value: str) -> str:
    """Comparison form for a human-typed name: case and punctuation carry no meaning here."""
    return "".join(c for c in str(value).casefold() if c.isalnum())


async def _existing_match(fetch, name: str, confirmed: bool, id_key: str = "_id"):
    """Refuse to silently create a second epic/story that duplicates an existing one.

    Whether to reuse an existing epic/story or add another alongside it is the user's structural
    decision — a field report traced duplicated hierarchies to this being taken as a default. A
    near-name match returns the candidates instead of creating, and only `confirmed` proceeds,
    mirroring create_branch's NEEDS_CONFIRMATION handshake.

    Fail-open on a lookup error, like create_test_script's branch check: not being able to check
    for duplicates must never be the reason a legitimate create cannot happen.
    """
    if confirmed:
        return None
    try:
        existing = await fetch()
    except Exception:
        return None
    wanted = _name_key(name)
    if not wanted:
        return None
    matches = [
        item for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, dict) and (
            _name_key(item.get("name") or item.get("title") or "") == wanted
            or wanted in _name_key(item.get("name") or item.get("title") or "")
        )
    ]
    if not matches:
        return None
    listed = ", ".join(
        f"{m.get('name') or m.get('title')} ({m.get(id_key) or m.get('id')})" for m in matches
    )
    return {"status": "NEEDS_CONFIRMATION", "existing": matches, "error": (
        f"Something very like '{name}' already exists: {listed}. Reusing it or creating another "
        "alongside it is the user's call, not a default — show them what exists and ask. "
        "Resend with confirmed=true only if they want a new one anyway."
    )}


async def _preflight_execution_configuration(clients: ClientBundle, config: dict):
    """Check the three ids in an execution configuration that fail late and opaquely."""
    checks = (
        (clients.config.list_grids, ("gridId", "id", "_id"), ("name", "gridName"),
         config.get("gridId"), "Grid id", "list_grids"),
        (clients.config.list_environments, ("environmentId", "id", "_id"), ("name", "environmentName"),
         config.get("baseUrl"), "Environment id (baseUrl)", "list_environments"),
        (clients.test_mgmt.list_branches, ("branchName",), ("branchName",),
         config.get("targetBranchName"), "Branch", "list_branches"),
    )
    for fetch, id_keys, name_keys, value, label, source in checks:
        if not value:
            continue
        error = await _unknown_id_error(fetch, id_keys, name_keys, str(value), label, source)
        if error:
            return error
    return None


def _fill_resolution_default(execution_configuration: dict, is_local: bool) -> dict:
    """
    "Local Machine Resolution" only means something for the local-agent grid ("use whatever
    resolution my own machine has") — a real cloud grid like TestingBot rejects it outright
    (confirmed live: 500, "Invalid screen-resolution specified: Local Machine Resolution"). Only
    fill it in once we've confirmed the target actually is the local grid; leave it unset
    otherwise, matching the pre-existing (working) behavior for cloud grids.
    """
    if is_local and not execution_configuration.get("resolution"):
        execution_configuration["resolution"] = "Local Machine Resolution"
    return execution_configuration


async def _fill_custom_properties(clients: ClientBundle, execution_configuration: dict) -> dict:
    """
    The Run TestBot / Scheduler dialogs always populate execution_configuration.customProperties
    from the project's stored Global Parameters before submitting (confirmed via HAR: the UI
    calls GET .../globalParameters, then sends those exact customPropertyId/name/value triplets
    on the execute call) — this is what resolves {{username}}-style type-2 "configuration"
    variables in test steps at runtime. Our schema defaults this to an empty list, and no caller
    can reasonably be expected to already know the project's customPropertyId GUIDs — omitting it
    leaves those variables unresolved, and a step ends up literally typing the placeholder name
    instead of a real value (confirmed live: "username" typed verbatim into a login form instead
    of the configured email). Only fills in when the caller didn't already supply an override.
    """
    if execution_configuration.get("customProperties"):
        return execution_configuration
    try:
        params = await clients.config.list_global_parameters()
        execution_configuration["customProperties"] = params.get("customProperties") or []
    except Exception:
        pass  # best-effort — an empty list here is the same as today's behavior, not worse
    return execution_configuration


_rate_limiter = OrgRateLimiter(settings.ahq_mcp_rate_limit_per_min)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

# Per-collection field allowlists for the context snapshot. The raw documents are huge (a bot
# embeds its suites which embed script views; scripts embed steps) — an unslimmed snapshot came
# back at ~137K characters in a real org and overflowed the tool-result budget. The snapshot is
# for DISCOVERY (what exists + ids to use); fetch full documents with the dedicated get_* tools.
_CONTEXT_FIELDS = {
    "projects": ("projectId", "id", "_id", "name", "projectName"),
    "websites": ("websiteId", "id", "name", "websiteUrl"),
    "environments": ("environmentId", "name", "value", "type", "isDefault"),
    "epics": ("epicId", "id", "name"),
    "bots": ("testBotId", "name", "lastExecutionStatus"),
    "suites": ("testSuiteId", "name", "numberOfTestScripts"),
    "api_collections": ("apiCollectionId", "id", "name"),
    "workflows": ("workflowId", "id", "name"),
    "performance_bots": ("performanceBotId", "id", "name"),
}
_CONTEXT_LIST_CAP = 25
# Environments accumulate faster than anything else in a long-lived project — 153 in a real dev
# org, nearly all of them abandoned one-off URLs — and at the old flat cap of 100 they alone
# cost this snapshot ~8K characters before any work began. This tool runs first in essentially
# every session, so its budget is spent on breadth, not depth.
_CONTEXT_LIST_CAPS = {"environments": 10}
# Where a caller goes for the entries the snapshot didn't show.
_CONTEXT_FULL_LIST_TOOL = {
    "projects": "list_projects",
    "websites": "list_websites",
    "environments": "list_environments",
    "epics": "list_epics",
    "bots": "list_bots",
    "suites": "list_suites",
    "api_collections": "list_api_collections",
    "workflows": "list_workflows",
    "performance_bots": "list_performance_bots",
}


def _slim_context_list(items, fields, cap=_CONTEXT_LIST_CAP, more_tool=None):
    if not isinstance(items, list):
        return items

    shown = list(items[:cap])
    if len(items) > cap:
        # The default-flagged entry is the one a caller most often actually wants and it is not
        # reliably near the front (the real dev org's default environment sorted ~90th), so it
        # would otherwise be the single most useful row the truncation hides.
        default_entry = next(
            (it for it in items[cap:] if isinstance(it, dict) and it.get("isDefault")), None
        )
        if default_entry is not None and shown:
            shown[-1] = default_entry

    slimmed = [
        {f: it[f] for f in fields if isinstance(it, dict) and it.get(f) is not None}
        for it in shown
    ]
    if len(items) > cap:
        truncated = {"total": len(items), "showing": len(slimmed), "items": slimmed}
        if more_tool:
            truncated["more"] = f"truncated for brevity — call {more_tool} for the full list"
        return truncated
    return slimmed


async def _get_context(clients: ClientBundle) -> dict:
    results = await asyncio.gather(
        clients.user.get_current_user(),
        clients.user.list_projects(),
        clients.asset.list_websites(),
        clients.config.list_environments(),
        clients.test_mgmt.list_epics(),
        clients.test_mgmt.list_bots(),
        clients.test_mgmt.list_suites(),
        clients.background.get_queue_status(),
        # mtaf-core (API/performance testing) — a separate flow from UI test scripts, previously
        # missing from this snapshot entirely: an agent working on API/load-testing tasks had no
        # context-loading path here and had to call list_* blind.
        clients.managed_testing.list_api_collections(),
        clients.managed_testing.list_workflows(),
        clients.managed_testing.list_performance_bots(),
        return_exceptions=True,
    )
    keys = [
        "user", "projects", "websites", "environments", "epics", "bots", "suites", "queue",
        "api_collections", "workflows", "performance_bots",
    ]
    out = {}
    for k, v in zip(keys, results):
        if isinstance(v, Exception):
            out[k] = str(v)
        elif k in _CONTEXT_FIELDS:
            out[k] = _slim_context_list(
                v,
                _CONTEXT_FIELDS[k],
                cap=_CONTEXT_LIST_CAPS.get(k, _CONTEXT_LIST_CAP),
                more_tool=_CONTEXT_FULL_LIST_TOOL.get(k),
            )
        else:
            out[k] = v
    if isinstance(results[0], Exception):
        # /users/me 500s for ORGANIZATION tokens (no userId claim, server-side quirk) — fall
        # back to the identity we can always derive: the token's own org claims.
        from src.config.credentials import decode_ahq_token

        try:
            claims = decode_ahq_token(clients.asset._credentials.api_token)
            out["user"] = {
                "organizationId": claims.get("organizationId"),
                "organizationName": claims.get("organizationName"),
                "tokenType": claims.get("tokenType"),
                "note": "identity derived from token claims; /users/me is unavailable for ORGANIZATION tokens",
            }
        except Exception:
            pass  # keep the raw error string
    return out


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# Shared by add_test_steps/update_test_script: the same trap, worth stating identically on both.
_BRANCH_PIN_HINT = (
    "The branch this edit must land on — pass the branch the script was created on (from "
    "get_scripts_for_branch, or whatever branch_name create_test_script used). Omitting it does "
    "NOT mean 'the script's own branch': the server applies the edit to the token's ambient "
    "checked-out branch, which drifts. Confirmed live — a step added to a script created on main "
    "landed on an unrelated feature branch, the next run silently executed the old version, and "
    "neither the edit response nor the report mentioned it. The response echoes branchName so you "
    "can check where the edit actually went."
)

TOOLS = [
    # Context
    Tool(
        name="list_my_projects",
        description=(
            "Projects the signed-in user personally has a role in, across every organization they "
            "belong to. Call this before creating anything when the user has not named a project: "
            "115 of ~1800 live users hold roles in more than one, and there is no way to tell from "
            "a later error which one they meant. Ask them to choose, then pass the chosen id as "
            "project_id on subsequent calls — the server keeps no per-session project, so the "
            "choice only persists if you carry it."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(name="get_context", description="Load full TestBots project snapshot from all services in parallel. Call this first before any other action.", inputSchema={"type": "object", "properties": {}}),

    # Asset — websites
    Tool(name="list_websites", description="List ALL websites in the project. Use this for 'show me the websites' — search_websites needs a name and returns [] for an empty query.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="search_websites", description="Search for an existing website by name in TestBots. For the full list use list_websites.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    Tool(name="create_website", description="Create a new website record in TestBots.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string"}}, "required": ["name", "url"]}),

    # Asset — pages
    Tool(name="list_pages", description="List all pages under a website.", inputSchema={"type": "object", "properties": {"website_id": {"type": "string"}}, "required": ["website_id"]}),
    Tool(name="create_page", description="Create a page under an existing website.", inputSchema={"type": "object", "properties": {"website_id": {"type": "string"}, "name": {"type": "string"}, "url": {"type": "string"}}, "required": ["website_id", "name", "url"]}),
    Tool(name="get_page_by_url", description="Check if a page already exists at a given URL, and if so, what locators it already has. Call this BEFORE writing any ui-locator test step for a live URL — if the locator you need isn't in the result, call crawl_url on that URL to capture it (never guess a raw selector instead). Copy every locatorId straight from this result; do not retype one or recall it from earlier in the conversation. Both failure modes are silent — a locatorId belonging to a different element renders a plausible-looking step that drives the wrong control, and a mistyped id renders '(Pending) uiLocator not found' rather than an error.", inputSchema={"type": "object", "properties": {"website_id": {"type": "string"}, "url": {"type": "string"}}, "required": ["website_id", "url"]}),
    Tool(
        name="add_locators",
        description="Batch-create NEW locators on a page. Silently no-ops for any locator whose strategy value already exists — to change an existing one use update_locator, and to repair one broken by a UI change use heal_locator.",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "website_id": {"type": "string"},
                "page_url": {"type": "string", "description": "Must match the page's real URL — locators are upserted by URL match, not by page_id"},
                "page_name": {"type": "string"},
                "locators": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "locatorName": {"type": "string", "description": "Human-readable label, e.g. 'Email input'"},
                            "locatorType": {
                                "type": "string",
                                "description": "Element type — one of the platform's real values (case-sensitive, all-caps): TEXTBOX, BUTTON, HYPERLINK, STATIC_TEXT, TABLE, DROP_DOWN, RADIO_BUTTON, CHECK_BOX, OTHER. A value outside this set (e.g. lowercase 'input'/'button') is stored as-is but the UI's Type dropdown won't recognize it and falls back to a plain, icon-less text display instead of a real selection.",
                                "enum": ["TEXTBOX", "BUTTON", "HYPERLINK", "STATIC_TEXT", "TABLE", "DROP_DOWN", "RADIO_BUTTON", "CHECK_BOX", "OTHER"],
                            },
                            "locationStrategies": {
                                "type": "array",
                                "description": "One or more locating strategies for this element, in priority order",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "locateBy": {"type": "string", "description": "css, xpath, id, ..."},
                                        "locatorValue": {"type": "string"},
                                        "selected": {"type": "boolean", "description": "True for the primary strategy to use at execution time"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "required": ["page_id", "website_id", "page_url", "locators"]
        }
    ),
    Tool(
        name="update_locator",
        description=(
            "Fix an already-created locator's locateBy/locatorValue. add_locators only ADDS new "
            "locators — it silently no-ops for anything that already matches an existing one by "
            "locationStrategies value, never updating it. Use this to correct a locator instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "website_id": {"type": "string"},
                "page_id": {"type": "string"},
                "locator_id": {"type": "string"},
                "locator_name": {"type": "string"},
                "locator_type": {
                    "type": "string",
                    "description": "One of the platform's real values (case-sensitive, all-caps): TEXTBOX, BUTTON, HYPERLINK, STATIC_TEXT, TABLE, DROP_DOWN, RADIO_BUTTON, CHECK_BOX, OTHER. Any other value is stored as-is but the UI's Type dropdown won't recognize it.",
                    "enum": ["TEXTBOX", "BUTTON", "HYPERLINK", "STATIC_TEXT", "TABLE", "DROP_DOWN", "RADIO_BUTTON", "CHECK_BOX", "OTHER"],
                },
                "locate_by": {"type": "string", "description": "css, xpath, id, ..."},
                "locator_value": {"type": "string"},
            },
            "required": ["website_id", "page_id", "locator_id", "locator_name", "locator_type", "locate_by", "locator_value"]
        }
    ),

    # Self-healing locators — detect, propose a fix for, and apply a fix to a locator whose
    # selectors stopped resolving during a real execution.
    Tool(name="scan_broken_locators", description="List locators the platform has already flagged as broken — every location strategy failed during a real execution (the same signal behind AI Brain's maintenance alerts). Call this before heal_locator, rather than guessing which locator needs attention.", inputSchema={"type": "object", "properties": {}}),
    Tool(
        name="heal_locator",
        description=(
            "Re-crawl a broken locator's live page with a headless browser and propose ranked "
            "replacement selector candidates — PROPOSE ONLY, nothing is changed yet. This is "
            "crawl_url's discovery mechanism aimed at one specific stale locator instead of a "
            "whole site. Only candidates that resolve to EXACTLY ONE element are returned. Follow "
            "up with apply_locator_fix once the user picks a candidate (or none, if the results "
            "look wrong)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locator_id": {"type": "string"},
                "website_id": {"type": "string"},
                "credentials": {"type": "object", "properties": {"username": {"type": "string"}, "password": {"type": "string"}}, "description": "Optional — supply if the page requires login to view the element"},
            },
            "required": ["locator_id", "website_id"],
        },
    ),
    Tool(
        name="apply_locator_fix",
        description=(
            "Apply a candidate strategy from heal_locator's output to a locator. The new strategy "
            "becomes primary; every existing strategy is KEPT as a fallback, never discarded — "
            "this never silently overwrites a locator's history, so a bad fix can always fall "
            "back to what worked before."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locator_id": {"type": "string"},
                "website_id": {"type": "string"},
                "chosen_strategy": {
                    "type": "object",
                    "properties": {
                        "locateBy": {"type": "string", "description": "css, xpath, id, ..."},
                        "locatorValue": {"type": "string"},
                    },
                    "required": ["locateBy", "locatorValue"],
                },
            },
            "required": ["locator_id", "website_id", "chosen_strategy"],
        },
    ),

    # Test scripts
    Tool(name="list_test_scripts", description="List or search test scripts by name. Returns a summary per script (id, name, status, type, stepCount) — call get_test_script for a script's actual steps. The `name` filter is a plain case-insensitive substring match. Results cover the configured project only; use get_scripts_for_branch to ask which scripts are on a specific branch.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Optional case-insensitive substring filter"}}}),
    Tool(name="get_test_script", description="Get full details of a test script by ID, including every step. A 60+ step script can exceed the result token cap: summary=true gives one line per step, steps_from/steps_to (inclusive, 1-based) one range in full. NOTE: the returned currentBranchName reflects this request's ambient branch, NOT the script's real branch membership — use get_scripts_for_branch for that. Its versionCount is what the editing tools' expected_version takes.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "summary": {"type": "boolean", "description": "Step titles only"}, "steps_from": {"type": "integer"}, "steps_to": {"type": "integer"}}, "required": ["script_id"]}),
    Tool(name="delete_test_script", description="Delete a test script. This is the SAME soft delete the UI performs — the script is archived (isArchived=true), appears under Administration -> Archive, and can be brought back with restore_asset; it is not destroyed. TWO-PHASE: if the script is still referenced by any Test Set or TestBot, the first call deletes NOTHING and returns status NEEDS_CONFIRMATION listing them (the raw API signals this with a 202 that is easily misread as success). Relay that list to the user and only call again with confirmed=true if they agree — that detaches the script from each one as it deletes.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "confirmed": {"type": "boolean", "default": False, "description": "Set true ONLY to confirm a prior NEEDS_CONFIRMATION response, after the user has agreed"}}, "required": ["script_id"]}),
    Tool(name="add_test_steps", description="Append (or insert) steps into an EXISTING test script in one call — no manual PUT assembly needed. Steps use the same shape as create_test_script (templateId + templateTitle verbatim for built-ins + parameters). Scalar parameter values accept friendly forms: {\"literal\": \"text\"}, {\"configuration\": \"paramName\"}, {\"vault\": \"secretName\"}, {\"variable\": \"varName\"}, {\"data_column\": \"col\"}, {\"faker\": \"Email\"}, {\"parameter\": \"name\"} — or the raw {\"type\": <code>, \"value\": ...}. Sequences renumber automatically. ALWAYS pass branch_name — omitting it lets the edit land on whatever branch the token is ambiently pointed at, not the script's own. NOTE: scripts on a protected branch (often 'main') reject direct edits — create a branch or delete+recreate.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "expected_version": {"type": "integer", "description": "versionCount you last read; edit is refused if it moved, instead of overwriting a concurrent edit"}, "steps": {"type": "array", "items": {"type": "object"}, "description": "Steps to add"}, "position": {"type": "integer", "description": "0-based insert position; omit to append at the end"}, "branch_name": {"type": "string", "description": _BRANCH_PIN_HINT}}, "required": ["script_id", "steps", "branch_name"]}),
    Tool(name="update_test_script", description="Update fields of an existing test script (name, status, story_id, testSteps, ...) — GET-merge-PUT, so unspecified fields are preserved. Pass branch_name for the same reason as add_test_steps. Same protected-branch caveat.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "expected_version": {"type": "integer", "description": "versionCount you last read; edit is refused if it moved, instead of overwriting a concurrent edit"}, "changes": {"type": "object", "description": "Fields to change, using the entity's own field names (e.g. name, status, storyId, testSteps)"}, "branch_name": {"type": "string", "description": _BRANCH_PIN_HINT}}, "required": ["script_id", "changes", "branch_name"]}),
    Tool(name="delete_test_steps", description="Remove steps by 1-based sequence and/or testStepId, renumbering the rest. Use this rather than rebuilding the whole testSteps array via update_test_script — resending every unrelated step to drop one is how unrelated steps get corrupted. All-or-nothing: nothing is deleted unless every sequence/id matches. branch_name rule as add_test_steps.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "sequences": {"type": "array", "items": {"type": "integer"}}, "step_ids": {"type": "array", "items": {"type": "string"}}, "branch_name": {"type": "string"}, "expected_version": {"type": "integer"}}, "required": ["script_id"]}),
    Tool(name="reorder_test_steps", description="Reorder steps. `order` is every step's CURRENT 1-based sequence in its new order, and must be a permutation of all of them — a partial list is refused, not guessed at.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "order": {"type": "array", "items": {"type": "integer"}}, "branch_name": {"type": "string"}, "expected_version": {"type": "integer"}}, "required": ["script_id", "order"]}),
    Tool(name="find_locator_usage", description="Which scripts and Common Functions reference a locatorId — impact analysis before update_locator/apply_locator_fix. The same locatorId is routinely shared by a Common Function and a standalone script, so a fix in one place reads as complete while another caller still fails, and an edit to suit one script changes them all. Fetches every script, so use it per decision, not per edit.", inputSchema={"type": "object", "properties": {"locator_id": {"type": "string"}, "include_common_functions": {"type": "boolean", "default": True}}, "required": ["locator_id"]}),
    Tool(
        name="create_test_script",
        description=(
            "Create a test script in TestBots. Each step's templateId MUST come from "
            "list_step_templates/search_step_templates — never invent one. Call get_step_template "
            "on the chosen template first to see which parameter keys it actually uses. "
            "For built-in templates (templateId like 'template-id-N'), also copy that template's "
            "templateTitle string into the step verbatim — the server does not look it up itself "
            "and omitting it causes a 500 error. Step values go in `parameters` (a list), NOT a "
            "`params` object — confirmed live against TestScriptController: `params` is not what "
            "drives step titles or execution values."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "page_id": {"type": "string"},
                "website_id": {"type": "string", "description": "REQUIRED (validated before the API call) — matches automationhq-frontend-v2's own create-script form. A script created with only page_id and no website_id is invisible in the UI's Table View and Application filter, even though it's created correctly. Get this from create_website/list_websites."},
                "story_id": {"type": "string", "description": "REQUIRED (validated before the API call) — matches automationhq-frontend-v2's own create-script form. A script with no story_id was excluded from the UI's default Table View listing entirely in live testing. Get this from list_epics -> list_stories, or use create_epic/create_story if nothing fits."},
                "status": {"type": "string", "default": "Not Started", "description": "One of: Not Started, In Progress, Ready, To Be Repaired, On Hold. Sending this as null/absent triggers a UI validation error when the script is opened."},
                "repair_comment": {"type": "string", "description": "REQUIRED only when status is 'To Be Repaired' (matches the frontend form's conditional rule) — otherwise omit."},
                "script_type": {"type": "string", "default": "WEB", "description": "e.g. 'WEB'. Sending this as null/absent triggers a UI validation error when the script is opened."},
                "branch_name": {"type": "string", "description": "ASK THE USER which branch this script should live on before creating it — offer a new branch alongside the real ones from list_branches. Do NOT quietly accept the 'main' default: main is protected, so a later commit_branch on it returns 403, the edit stays an uncommitted version, and execute_bot goes on running the last committed one — the change looks saved and never executes (confirmed live: an inserted wait step silently never ran). Whatever branch is chosen is also what execute_bot needs as targetBranchName, so it has to be settled before the bot runs. Always send this field explicitly either way; omitting it falls back to the token's ambient checked-out branch, which is not reliably main."},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "templateId": {"type": "string", "description": "Real template ID from list_step_templates/search_step_templates — required, never fabricate"},
                            "templateTitle": {"type": "string", "description": "REQUIRED when templateId is a built-in ('template-id-N') template — copy the exact templateTitle string (with {{placeholder}} tokens intact, e.g. 'Enter {{text}} for the {{ui-locator}}') from the search_step_templates/get_step_template result verbatim. The server does NOT look this up itself for built-ins; omitting it causes a 500 error. Not needed for Common-Function templateIds (real UUIDs) — the server derives the title itself for those."},
                            "testStepTitle": {"type": "string", "description": "Optional — the server regenerates this from templateTitle + parameters anyway"},
                            "sequence": {"type": "integer", "description": "0-based order of this step within the script"},
                            "parameters": {
                                "type": "array",
                                "description": "Values for the placeholder names in templateTitle (e.g. {{text}}, {{ui-locator}}) — one entry per placeholder actually used by the template.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "key": {"type": "string", "description": "Must match a {{placeholder}} name in templateTitle, e.g. 'text' or 'ui-locator'"},
                                        "value": {
                                            "type": "object",
                                            "description": "For key='ui-locator': {\"locatorId\": \"<real id from add_locators>\"} — the server auto-enriches name/locateBy/locatorValue from the saved page locator, do not fabricate the rest. NEVER invent a raw CSS/XPath selector here as a substitute (e.g. \"input[type='email']\") — check get_page_by_url first for an existing locator, and if none exists, call crawl_url to capture real ones before writing this step. For any scalar key (text, number, expected, ...): {\"type\": 0, \"value\": \"<literal>\"} — type 0 means literal; other type codes exist for variables/data-driven/vault/etc. but 0 covers the common case.",
                                        },
                                        "paramClass": {"type": "string", "description": "Fully-qualified class name matching value's shape: 'ai.automationhq.commons.entities.assets.UILocator' or 'ai.automationhq.commons.entities.assets.TypeValuePair'"}
                                    },
                                    "required": ["key", "value", "paramClass"]
                                }
                            }
                        },
                        "required": ["templateId"]
                    }
                }
            },
            "required": ["name", "steps", "website_id", "story_id", "branch_name"]
        }
    ),

    # Step templates — resolve real templateIds before writing any test step
    Tool(name="list_step_templates", description="List available step templates (built-in action types + org-defined Common Functions) for the current project. Use this or search_step_templates before writing any test step — templateId is never invented.", inputSchema={"type": "object", "properties": {"offset": {"type": "integer", "default": 0}}}),
    Tool(name="search_step_templates", description="Search step templates by title (e.g. 'Click', 'Navigate', 'Assert Text') to find the real templateId for an action. Matching is a substring search over the platform's own titles, which often differ from the obvious word — common synonyms are expanded automatically (searching 'Navigate' also returns 'Open Web Browser and go to page', 'assert' also returns the 'Verify ...' family), so search by intent rather than guessing the platform's phrasing.", inputSchema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}),
    Tool(name="get_step_template", description="Get full detail of a step template by ID, including which params sub-fields it expects.", inputSchema={"type": "object", "properties": {"template_id": {"type": "string"}}, "required": ["template_id"]}),

    # Recorded Scripts — browser sessions captured by the TestBot Recorder Chrome Extension.
    # Read + promote only; recordings themselves are created by the extension, not by tools.
    Tool(name="list_recorded_scripts", description="List recorded scripts (browser sessions captured by the TestBot Recorder extension), optionally filtered by name or branch.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Optional name filter"}, "branch_name": {"type": "string", "description": "Optional branch filter"}}}),
    Tool(name="get_recorded_script", description="Get a browser RECORDING (captured by the Recorder extension) by ID, with its steps and promotedTestScriptId. A recording is not yet a test script — for a real test script use get_test_script, and promote_recorded_script to convert one.", inputSchema={"type": "object", "properties": {"recorded_script_id": {"type": "string"}}, "required": ["recorded_script_id"]}),
    Tool(name="promote_recorded_script", description="Promote a recorded script into a real Test Script. story_id is REQUIRED for a first-time promotion (the server rejects it otherwise, and a script without a story is invisible in the UI's Table View) — resolve via list_epics/list_stories or create_epic/create_story. Re-promoting an already-promoted recording updates the linked Test Script instead.", inputSchema={"type": "object", "properties": {"recorded_script_id": {"type": "string"}, "story_id": {"type": "string"}, "name": {"type": "string", "description": "Optional name override for the resulting Test Script"}, "website_id": {"type": "string", "description": "Optional application override"}, "status": {"type": "string", "description": "Optional status for the resulting Test Script"}, "description": {"type": "string"}, "branch_name": {"type": "string", "default": "main", "description": "Branch for the resulting Test Script — always sent explicitly (defaults to main) because omitting it falls back to the API token's ambient checked-out branch, which is unstable"}}, "required": ["recorded_script_id", "story_id"]}),

    # Common Functions (aka User Test Steps) — reusable multi-step components usable as a single
    # step inside test scripts.
    Tool(name="list_common_functions", description="List Common Functions / User Test Steps (reusable step components), optionally filtered by name.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Optional name filter"}}}),
    Tool(name="get_common_function", description="Get a Common Function by ID including its testSteps, parameters, and returnType. Note: encrypted-text step values are masked (asterisks) by the server on read.", inputSchema={"type": "object", "properties": {"common_function_id": {"type": "string"}}, "required": ["common_function_id"]}),
    Tool(name="create_common_function", description="Create a Common Function / User Test Step. Steps use the same TestStep shape as create_test_script (templateId + verbatim templateTitle + parameters). Nesting is rejected server-side: a step's templateId must not be another Common Function's ID.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "1-120 chars; letters, digits, spaces, hyphens only"}, "website_id": {"type": "string"}, "status": {"type": "string", "description": "e.g. 'READY'"}, "return_type": {"type": "object", "properties": {"type": {"type": "string", "description": "e.g. 'String'"}, "name": {"type": "string"}, "array": {"type": "boolean"}}, "required": ["type"]}, "steps": {"type": "array", "items": {"type": "object"}, "description": "TestStep objects — same shape as create_test_script's steps"}, "parameters": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "array": {"type": "boolean"}}}, "description": "Input parameters the function exposes to calling scripts"}, "description": {"type": "string", "description": "Max 600 chars"}}, "required": ["name", "website_id", "status", "return_type"]}),
    Tool(name="update_common_function", description="Update a Common Function — including a safe rename: update_common_function(common_function_id, name='new name'). Internally does GET-merge-PUT because the server's PUT is a full-document replace that would otherwise wipe every omitted field (testSteps, parameters, returnType, even org/project linkage). Never update a Common Function via call_api with a partial PUT body.", inputSchema={"type": "object", "properties": {"common_function_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}, "status": {"type": "string"}, "website_id": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object"}, "description": "Full replacement testSteps list (omit to keep existing steps)"}, "parameters": {"type": "array", "items": {"type": "object"}, "description": "Full replacement parameters list (omit to keep existing)"}, "return_type": {"type": "object"}}, "required": ["common_function_id"]}),

    # Organization
    Tool(name="list_epics", description="List all epics in the project.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_epic", description="Create a new epic. Use this when no existing epic fits a test script you're about to create — create_test_script requires a story_id, which requires a parent epic.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "confirmed": {"type": "boolean", "default": False, "description": "Set true ONLY after the user has seen the existing epics and still wants a new one"}}, "required": ["name"]}),
    Tool(name="list_stories", description="List all stories under an epic. ALWAYS call this before create_story — most epics already contain a story that fits, and adding a near-duplicate fragments the user's test organisation.", inputSchema={"type": "object", "properties": {"epic_id": {"type": "string"}}, "required": ["epic_id"]}),
    Tool(name="create_story", description="Create a new story under an epic. Only after list_stories has shown that nothing existing fits. Reusing or creating an epic/story is the user's structural decision, not a default to take silently: say which existing one you found and ask before either reusing it or adding a new one alongside it.", inputSchema={"type": "object", "properties": {"epic_id": {"type": "string"}, "name": {"type": "string"}, "confirmed": {"type": "boolean", "default": False, "description": "Set true ONLY after the user has seen the existing stories and still wants a new one"}}, "required": ["epic_id", "name"]}),
    Tool(name="list_bots", description="List the project's UI/functional TestBots. This is the tool for 'show me my bots' — list_performance_bots is a SEPARATE JMeter list (a project can have bots here and none there), and list_bot_types returns type metadata for create_test_bot, not bots.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Optional name filter"}}}),
    Tool(name="list_suites", description="List all test suites in the project.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_environments", description="List all configured environments.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_suite", description="Create a new test suite (Test Set). Scripts can be attached now (script_ids) or later via add_scripts_to_suite; a suite feeds create_test_bot.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "script_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional test script ids to attach immediately"}}, "required": ["name"]}),
    Tool(name="add_scripts_to_suite", description="Add test scripts to an existing test suite. (Scripts are embedded in the suite document — this fetches the suite, merges, and saves it back; already-attached scripts are skipped.)", inputSchema={"type": "object", "properties": {"suite_id": {"type": "string"}, "script_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["suite_id", "script_ids"]}),
    Tool(name="remove_scripts_from_suite", description="Detach test scripts from a test suite without deleting the scripts themselves. Also the way to clear the association that makes delete_test_script ask for confirmation. Remaining scripts are renumbered 1..n.", inputSchema={"type": "object", "properties": {"suite_id": {"type": "string"}, "script_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["suite_id", "script_ids"]}),

    # Version Control — branches, commits, and Pull Requests over test scripts.
    Tool(name="list_branches", description="List version-control branches in the project, optionally filtered by name.", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Optional name filter"}}}),
    Tool(name="get_scripts_for_branch", description="List the test scripts that are members of a branch. This is the ONLY correct way to answer 'which scripts are on branch X' — TestScript.currentBranchName does NOT reflect real branch membership.", inputSchema={"type": "object", "properties": {"branch_name": {"type": "string"}}, "required": ["branch_name"]}),
    Tool(name="delete_branch", description="Delete a version-control branch. Scripts that were on it are moved back to main and a project state pointing at it is reset to main, so the work survives — but the branch's own history does not. Refused with 400 for the default branch and for protected branches. REQUIRES CONFIRMATION: pass confirmed=true only after explicitly asking the user to confirm the deletion.", inputSchema={"type": "object", "properties": {"branch_name": {"type": "string"}, "confirmed": {"type": "boolean", "default": False, "description": "Confirmation gate — pass true only after confirming with the user"}}, "required": ["branch_name"]}),
    Tool(name="create_branch", description="Create a branch (fork from from_branch, default main). TWO-PHASE: the server runs a preflight conflict check and may return status NEEDS_CONFIRMATION with details instead of creating — relay that to the user and only resend with confirmed=true after they agree. strategy: FROM_BRANCH (default, fork from from_branch HEAD) or FROM_CURRENT (include scripts' individual branch work).", inputSchema={"type": "object", "properties": {"branch_name": {"type": "string"}, "from_branch": {"type": "string", "default": "main"}, "strategy": {"type": "string", "enum": ["FROM_BRANCH", "FROM_CURRENT"]}, "confirmed": {"type": "boolean", "default": False, "description": "Set true ONLY to confirm a NEEDS_CONFIRMATION response"}, "script_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional — branch only these scripts (default: all)"}, "is_protected": {"type": "boolean", "default": False, "description": "Require an approved PR to merge into this branch; also blocks deletion"}}, "required": ["branch_name"]}),
    Tool(name="commit_branch", description="Commit all current work on a branch with a message (and optional tag like 'v2.0').", inputSchema={"type": "object", "properties": {"branch_name": {"type": "string"}, "message": {"type": "string"}, "tag": {"type": "string"}}, "required": ["branch_name", "message"]}),
    Tool(name="list_commits", description="List commit history for a branch (paged).", inputSchema={"type": "object", "properties": {"branch_name": {"type": "string"}, "page": {"type": "integer", "default": 0}, "size": {"type": "integer", "default": 20}}, "required": ["branch_name"]}),
    Tool(name="create_pull_request", description="Open a Pull Request from source_branch into target_branch. Optional: reviewer_ids (from list_users), script_ids (single-script PR), delete_source_branch_after_merge.", inputSchema={"type": "object", "properties": {"source_branch": {"type": "string"}, "target_branch": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}, "reviewer_ids": {"type": "array", "items": {"type": "string"}}, "script_ids": {"type": "array", "items": {"type": "string"}}, "delete_source_branch_after_merge": {"type": "boolean", "default": False}}, "required": ["source_branch", "target_branch", "title"]}),
    Tool(name="list_pull_requests", description="List Pull Requests (paged, newest first), optionally filtered by status (e.g. OPEN, MERGED, CLOSED) and/or a search string.", inputSchema={"type": "object", "properties": {"status": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"}, "page": {"type": "integer", "default": 0}, "size": {"type": "integer", "default": 20}}}),
    Tool(name="get_pull_request", description="Get a Pull Request by ID (status, branches, reviewers, comments).", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}}, "required": ["pr_id"]}),
    Tool(name="get_pull_request_diff", description="Get a Pull Request's diff (added/removed/modified steps per script). Optionally scope to one script.", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}, "script_id": {"type": "string"}}, "required": ["pr_id"]}),
    Tool(name="approve_pull_request", description="Approve a Pull Request (review action; does not merge).", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}}, "required": ["pr_id"]}),
    Tool(name="request_pr_changes", description="Request changes on a Pull Request, with an optional review comment.", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}, "comment": {"type": "string"}}, "required": ["pr_id"]}),
    Tool(name="merge_pull_request", description="Merge an approved Pull Request into its target branch. May return a CONFLICTS status — conflict resolution is not yet supported through MCP tools; direct the user to the UI's Resolve Conflicts flow in that case.", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}}, "required": ["pr_id"]}),
    Tool(name="close_pull_request", description="Close a Pull Request WITHOUT merging. The source branch is NOT deleted.", inputSchema={"type": "object", "properties": {"pr_id": {"type": "string"}}, "required": ["pr_id"]}),

    # Project Roles (Administration → Global Settings → Project Roles) — role definitions and
    # user-role assignments for the current project.
    Tool(name="list_project_roles", description="List all roles defined for the current project (system roles like Site Admin/Tester plus custom ones), with each role's permission set.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_project_role", description="Create a custom project role. permissions must be a subset of VIEW/EXECUTE/EDIT/DELETE/SHARE. VIEW is independent — never add it implicitly just because other permissions are granted; include it only when the user asked for it.", inputSchema={"type": "object", "properties": {"role_name": {"type": "string"}, "permissions": {"type": "array", "items": {"type": "string", "enum": ["VIEW", "EXECUTE", "EDIT", "DELETE", "SHARE"]}}, "is_default": {"type": "boolean", "default": False, "description": "Make this the project's default role for new members"}}, "required": ["role_name", "permissions"]}),
    Tool(name="update_project_role_permissions", description="Replace a role's permission set. Role NAME is immutable server-side — there is deliberately no rename parameter; to rename, create a new role and delete the old one. VIEW is independent — never add it implicitly.", inputSchema={"type": "object", "properties": {"role_id": {"type": "string"}, "permissions": {"type": "array", "items": {"type": "string", "enum": ["VIEW", "EXECUTE", "EDIT", "DELETE", "SHARE"]}}}, "required": ["role_id", "permissions"]}),
    Tool(name="delete_project_role", description="Delete a custom project role. System roles cannot be deleted (server rejects it).", inputSchema={"type": "object", "properties": {"role_id": {"type": "string"}}, "required": ["role_id"]}),
    Tool(name="assign_project_role", description="Assign a role to a user in the current project. Get user IDs from list_users, role IDs from list_project_roles.", inputSchema={"type": "object", "properties": {"role_id": {"type": "string"}, "user_id": {"type": "string"}}, "required": ["role_id", "user_id"]}),
    Tool(name="list_project_members", description="List all user-role assignments for the current project.", inputSchema={"type": "object", "properties": {}}),

    # Archive Manager (Administration → Archive) — soft-deleted assets across all modules.
    # One generic tool set instead of 10 per-entity ones; entity_type picks the route.
    Tool(name="list_archived_assets", description="List soft-deleted (archived) assets of one type — what the UI shows under Administration → Archive. Deleting an asset in any module archives it rather than destroying it.", inputSchema={"type": "object", "properties": {"entity_type": {"type": "string", "enum": ["epic", "story", "website", "page", "locator", "test_script", "test_suite", "test_bot", "test_bot_folder", "recorded_script"]}, "search": {"type": "string", "description": "Optional name filter"}, "page": {"type": "integer", "default": 0}, "size": {"type": "integer", "default": 50}}, "required": ["entity_type"]}),
    Tool(name="restore_asset", description="Restore an archived asset back to its module (un-delete). Find the asset_id via list_archived_assets first.", inputSchema={"type": "object", "properties": {"entity_type": {"type": "string", "enum": ["epic", "story", "website", "page", "locator", "test_script", "test_suite", "test_bot", "test_bot_folder", "recorded_script"]}, "asset_id": {"type": "string"}}, "required": ["entity_type", "asset_id"]}),
    Tool(name="permanently_delete_asset", description="PERMANENTLY delete an archived asset — irreversible, the 'Delete forever' action in the Archive Manager. Only works on assets that are already archived. REQUIRES CONFIRMATION: pass confirmed=true only after explicitly asking the user and getting their approval for the irreversible deletion.", inputSchema={"type": "object", "properties": {"entity_type": {"type": "string", "enum": ["epic", "story", "website", "page", "locator", "test_script", "test_suite", "test_bot", "test_bot_folder", "recorded_script"]}, "asset_id": {"type": "string"}, "asset_name": {"type": "string", "description": "Human-readable name (for error messages)"}, "confirmed": {"type": "boolean", "default": False, "description": "Confirmation gate — pass true only after confirming with the user"}}, "required": ["entity_type", "asset_id"]}),

    # Tunnel (Administration → Tunnel) — secure bridge exposing a private/local app to the cloud
    # execution grid. Only these 4 operations exist server-side.
    Tool(name="get_tunnel_status", description="Get the tunnel process status (Administration → Tunnel). The tunnel bridges a private/local application to the cloud execution grid.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="start_tunnel", description="Start the tunnel process on the gateway.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="stop_tunnel", description="Stop the running tunnel process.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="execute_tunnel_command", description="Execute a command through the tunnel into the client network (raw string payload).", inputSchema={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),

    # Global Parameters — project-wide named values usable as a test step's "Configuration" value
    # type. NOT for secrets — passwords/API keys belong in the vault tools below instead.
    Tool(name="list_global_parameters", description="Get the project's Global Parameters (name/value pairs usable as a test step's 'Configuration' value type). Use this before referencing one by name in a step, and before add_global_parameter to avoid duplicate names.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="search_global_parameters", description="Search Global Parameters by name (also returns the 3 built-in system defaults: baseUrl, timeout, waitForElementTimeout).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}}}),
    Tool(name="add_global_parameter", description="Add a new Global Parameter. Never use this for passwords/API keys/secrets — use create_config_vault_secret instead, since Global Parameters are stored in plain text and visible in the UI.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "description": {"type": "string"}}, "required": ["name", "value"]}),
    Tool(name="check_global_parameter_usage", description="Check whether a Global Parameter (by its customPropertyId) is referenced in any test script, before deleting it.", inputSchema={"type": "object", "properties": {"custom_property_id": {"type": "string"}}, "required": ["custom_property_id"]}),
    Tool(name="flatten_and_delete_global_parameter", description="Delete a Global Parameter. This first converts every test step that references it into a literal value (matching its current value), then removes the parameter — call check_global_parameter_usage first so the caller/user knows how many scripts will be affected.", inputSchema={"type": "object", "properties": {"custom_property_id": {"type": "string"}}, "required": ["custom_property_id"]}),

    # Vault (config-services) — for secrets referenced by a test step's 'From Secrets' value type.
    # This is a DIFFERENT vault from list_vault_secrets (that one is mtaf-core's, for API/performance
    # testing only). Values are write-only from here — there is no tool to read a decrypted value
    # back, by design, so a real secret never appears in this conversation.
    Tool(name="list_config_vault_secrets", description="List secrets in the UI test-script vault (config-services) — the usual one, backing a test step's 'From Secrets' value type. Metadata only, never decrypted values. Use before create_config_vault_secret to avoid duplicates. The API/perf-testing vault is a DIFFERENT store: list_vault_secrets.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_config_vault_secret", description="Get vault secret metadata by ID (never returns the decrypted value).", inputSchema={"type": "object", "properties": {"secret_id": {"type": "string"}}, "required": ["secret_id"]}),
    Tool(name="create_config_vault_secret", description="Store a new secret (e.g. a real login password) for use in test steps via the 'From Secrets' value type. Use this instead of a literal text value for any real credential — never hardcode a real password into a test step.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "description": {"type": "string"}}, "required": ["name", "value"]}),
    Tool(name="update_config_vault_secret", description="Update an existing vault secret's value and/or description (e.g. after a password rotation) — existing test steps referencing it by name pick up the new value automatically, no script changes needed.", inputSchema={"type": "object", "properties": {"secret_id": {"type": "string"}, "value": {"type": "string"}, "description": {"type": "string"}}, "required": ["secret_id"]}),
    Tool(name="delete_config_vault_secret", description="Permanently delete a vault secret.", inputSchema={"type": "object", "properties": {"secret_id": {"type": "string"}}, "required": ["secret_id"]}),

    # Execution
    Tool(name="get_bot_notifications", description="Read a TestBot's email report settings: recipients, pass/fail triggers, PDF attachment, report template. Returns defaults for a never-configured bot, so a bare result is not an error.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}}, "required": ["bot_id"]}),
    Tool(name="configure_bot_notifications", description="Change a TestBot's email report settings; pass only what changes (merged for you — the server would otherwise switch the untouched booleans off). template_type: TEST_EXECUTION_REPORT_SUMMARY (charts+totals), TEST_EXECUTION_REPORT_WITH_FAILURES (default, the only one carrying failure reasons), TEST_EXECUTION_REPORT_EXECUTIVE (pass rate only). Never invent one from a UI label — an unrecognised value is stored without complaint, then ignored at send time.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}, "recipients": {"type": "array", "items": {"type": "string"}, "description": "Replaces the existing list."}, "notify_on_pass": {"type": "boolean"}, "notify_on_fail": {"type": "boolean"}, "attach_pdf": {"type": "boolean"}, "template_type": {"type": "string", "enum": ["TEST_EXECUTION_REPORT_SUMMARY", "TEST_EXECUTION_REPORT_WITH_FAILURES", "TEST_EXECUTION_REPORT_EXECUTIVE"]}, "schedule_on_completion": {"type": "boolean"}}, "required": ["bot_id"]}),
    Tool(name="create_test_bot", description="Create a TestBot (execution configuration for a set of Test Suites). A TestBot carries NO browser/grid settings — those are supplied at run time via execute_bot. Attach scripts by first creating a suite (create_suite) and passing its id+name in test_suites (min 1). Bot names must be unique in the project. For DEBUGGING, do not create a bot per script or per attempt: check list_bots for a debug bot that already exists and swap its suite's scripts with add_scripts_to_suite/remove_scripts_from_suite. One-off bots accumulate permanently in the user's project and are never used again.", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "1-120 chars, unique"}, "test_suites": {"type": "array", "items": {"type": "object", "properties": {"testSuiteId": {"type": "string"}, "name": {"type": "string"}}, "required": ["testSuiteId"]}, "description": "At least one suite from create_suite/list_suites"}, "description": {"type": "string"}, "bot_type": {"type": "object", "description": "Optional {type, value} from list_bot_types; server defaults to REGRESSION_TEST"}, "folder_id": {"type": "string"}, "profile_id": {"type": "string"}, "number_of_retries": {"type": "integer", "default": 0}}, "required": ["name", "test_suites"]}),
    Tool(name="list_bot_types", description="List the available TestBot TYPES ({type, value, color}) for create_test_bot's bot_type. Type metadata, not bots — use list_bots for the project's actual bots.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_grids", description="List execution grids (gridId + url). An execute_bot execution_configuration's gridId MUST come from a call made in THIS session — grids differ per project and are deleted over time, so a gridId carried over from an earlier conversation is a common cause of a run that fails minutes in.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_browsers", description="List available browsers for execution. An execute_bot execution_configuration's browser MUST come from here.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_execution_types", description="List execution types (e.g. Web/Mobile) and platform options.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_environment", description="Create an execution Environment (name + app-under-test URL). execute_bot's executionConfiguration.baseUrl must reference an Environment ID — if list_environments has nothing for the target app, create one here first.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string", "description": "The app-under-test URL this environment points at"}, "env_type": {"type": "string", "default": "Web"}, "description": {"type": "string"}}, "required": ["name", "url"]}),
    Tool(name="get_grid_capabilities", description="One call returns everything an execute_bot config needs for a grid: valid platforms (osType values), browsers, resolutions, and browser versions (pass browser to get its versions). Use this instead of guessing — values differ per grid ('Grid OS'/'latest' on plain Selenium, real OS/version lists on TestingBot/BrowserStack).", inputSchema={"type": "object", "properties": {"grid_id": {"type": "string"}, "testing_type": {"type": "string", "default": "Web"}, "browser": {"type": "string", "description": "Optional — include to also get this browser's valid versions"}}, "required": ["grid_id"]}),
    Tool(name="execute_bot", description="Run a TestBot — on the cloud grid pool, or on this machine's own local agent if gridId resolves to it (detected automatically; routed directly to localhost:9202, bypassing the cloud, since the cloud has no way to deliver a job to a specific developer's machine). execution_configuration is validated locally before submission. REQUIRED: baseUrl (an ENVIRONMENT ID from list_environments/create_environment — NOT a URL, despite the name; the backend resolves it via environment lookup and a raw URL kills the run at report time), browser + browserVersion + osType (from get_grid_capabilities), gridId (from list_grids). Returns the background JOB id. Poll get_job_status(jobId) with widening gaps (~30s, 60s, 120s), then pass that SAME id to get_execution_report — it resolves a job id to its execution, so no matching on names or timestamps. SUCCEEDED means the job ran, NOT that the tests passed; only the report says that. TO RUN A SCRIPT THAT LIVES ON A BRANCH, set execution_configuration.targetBranchName to that branch — this is the run-time branch selector (the same one the UI's run dialog offers). NEVER propose merging a branch into main just to run or verify a script; a merge is only for making a version permanent, and suggesting one as a prerequisite to testing sends the user through a PR they did not need.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}, "execution_configuration": {"type": "object", "description": "Required: baseUrl (Environment ID), browser, browserVersion, osType, gridId. Optional: resolution, type ('Web'), timeout (1-300, default 60), waitForElementTimeout (1-300, default 30), delayBetweenSteps (0-30), numberOfRetries (0-3), screenshot flags, targetBranchName, profileId.", "properties": {"baseUrl": {"type": "string", "description": "Environment ID (NOT a URL)"}, "browser": {"type": "string"}, "browserVersion": {"type": "string"}, "osType": {"type": "string"}, "gridId": {"type": "string", "description": "From list_grids, fetched THIS session — a grid id remembered from an earlier conversation may have been deleted since, and the run fails minutes in with a null gridUrlForExecution."}, "resolution": {"type": "string"}, "timeout": {"type": "integer"}, "takeScreenshots": {"type": "boolean"}, "screenshotOnError": {"type": "boolean", "description": "Capture on a failed step (default true) — this is your failure evidence."}, "screenshotAfterEachStep": {"type": "boolean", "description": "Every step, not just failures."}, "screenshotOnFinish": {"type": "boolean", "description": "Final state (default true)."}, "targetBranchName": {"type": "string", "description": "Which branch's committed version to run. This is how you test a script on a branch — no merge required. Confirm it with get_scripts_for_branch when a recent edit is supposed to be included; execute_bot runs the last COMMITTED version, so an uncommitted edit will not appear."}}, "required": ["baseUrl", "browser", "browserVersion", "osType", "gridId"]}, "name": {"type": "string", "description": "Execution display name (defaults to the bot's name)"}, "profile_id": {"type": "string"}, "partial_execution": {"type": "boolean", "default": False}}, "required": ["bot_id", "execution_configuration"]}),
    Tool(name="get_execution_status", description="Progress/status poll for a running execution (by executionId from execute_bot). The lightweight endpoint reports UNKNOWN for finished runs — this tool automatically falls back to the detailed report's overall status in that case. For queue-position detail, use get_job_status with the jobId from execute_bot's response.", inputSchema={"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}),

    # Script inspection & scoped execution (improvements from retrospective)
    Tool(name="inspect_script", description="Pre-execution inspection: step count, locator coverage, vault secret usage, branch info. Call this BEFORE executing a script to catch issues early rather than discovering them at runtime. Shows which steps reference vault secrets (resolved at runtime, appear in reports) and which use UI locators that may be stale.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]}),
    Tool(name="validate_script_locators", description="Check all UI locators in a script — which ones exist, which may be flagged as broken. Returns the locator status and recommends scan_broken_locators/heal_locator before executing if any issues are found.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]}),
    Tool(name="check_script_for_credential_exposure", description="Scan a script for plaintext credentials accidentally baked into step values. DETECTION ONLY — it tells you what's there, not a filter. Reports email addresses, passwords, tokens, API keys that may be exposed. Recommends vault secrets (type 7) as the fix: create_config_vault_secret + reference it instead of the literal value.", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]}),
    Tool(name="execute_script_only", description="Run a SINGLE test script in isolation, WITHOUT creating a full suite/bot structure. The fast path for targeting a specific script when debugging — avoids 40-90min full regression cycles. Internally creates temporary suite+bot, runs the script, and returns cleanup instructions. REQUIRED: execution_configuration (same as execute_bot: baseUrl/browser/browserVersion/osType/gridId). confirm_script_id is a safety parameter — must equal script_id to proceed (prevents accidental wrong-script execution).", inputSchema={"type": "object", "properties": {"script_id": {"type": "string"}, "execution_configuration": {"type": "object", "description": "Browser/grid/environment config (same shape as execute_bot)", "properties": {"baseUrl": {"type": "string"}, "browser": {"type": "string"}, "browserVersion": {"type": "string"}, "osType": {"type": "string"}, "gridId": {"type": "string"}}, "required": ["baseUrl", "browser", "browserVersion", "osType", "gridId"]}, "confirm_script_id": {"type": "string", "description": "Must equal script_id — safety check to prevent accidental wrong-script execution"}}, "required": ["script_id", "execution_configuration", "confirm_script_id"]}),

    Tool(name="schedule_bot_recurring", description="Create a recurring schedule for a TestBot — the real scheduler backing both the Scheduler Admin page and each TestBot's own clock-icon dialog (test-management-services). REQUIRED: name (1-120 chars, the schedule's own name — ask the user if not given), cron (a real cron expression — use convert_text_to_cron first if the user described it in plain language, e.g. 'every day at 9am'), execution_configuration (same shape as execute_bot's: baseUrl/browser/browserVersion/osType/gridId required). emails (result-recipient list) is optional but should be asked for — check list_scheduler_recipient_emails for previously-used addresses first.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}, "name": {"type": "string", "description": "The schedule's own name, 1-120 chars"}, "emails": {"type": "array", "items": {"type": "string"}, "description": "Result-notification recipients"}, "cron": {"type": "string", "description": "Cron expression, e.g. '0 9 * * *'. Use convert_text_to_cron to derive one from plain language."}, "execution_configuration": {"type": "object", "properties": {"baseUrl": {"type": "string", "description": "Environment ID (NOT a URL)"}, "browser": {"type": "string"}, "browserVersion": {"type": "string"}, "osType": {"type": "string"}, "gridId": {"type": "string"}}, "required": ["baseUrl", "browser", "browserVersion", "osType", "gridId"]}}, "required": ["bot_id", "name", "cron", "execution_configuration"]}),
    Tool(name="cancel_schedule", description="Delete a recurring schedule created by schedule_bot_recurring (test-management-services' real scheduler).", inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]}),
    Tool(name="update_schedule", description="Update an existing recurring schedule (name, emails, cron, and/or execution_configuration) — only supply the fields you want changed, everything else is preserved from the current schedule (fetched first, merged, then saved as a whole — the real endpoint has no partial-patch mode).", inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}, "bot_id": {"type": "string"}, "name": {"type": "string"}, "emails": {"type": "array", "items": {"type": "string"}}, "cron": {"type": "string"}, "execution_configuration": {"type": "object"}}, "required": ["schedule_id"]}),
    Tool(name="toggle_schedule", description="Enable/disable a recurring schedule without deleting it (flips its current state).", inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]}),
    Tool(name="list_schedulers", description="List recurring schedules (the same ones shown in Scheduler Admin). Pass bot_id to reproduce the exact filtered view a TestBot's own scheduler dialog shows — useful to confirm a schedule actually landed against the bot you expected.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string", "description": "Optional — filter to one TestBot's schedules"}, "offset": {"type": "integer", "default": 0}, "size": {"type": "integer", "default": 100}}}),
    Tool(name="list_scheduler_recipient_emails", description="Previously-used schedule result-notification email addresses, for suggesting values instead of guessing one.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="convert_text_to_cron", description="Convert a plain-language schedule description (e.g. 'every day at 9am', 'every Monday at noon') into a cron expression for schedule_bot_recurring — use this instead of hand-writing cron syntax.", inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    Tool(name="get_job_status", description="Queue-level status for a job, by the job_id from execute_bot — answers 'is it still waiting to start' (runs can sit ENQUEUED 2-3 minutes). Once running, use get_execution_status with the executionId instead; the two take different ids. Poll with WIDENING gaps — check at ~30s, then 60s, then 120s — rather than repeating a long fixed sleep: most runs finish well inside a fixed 150-180s wait, so a fixed interval spends the whole budget on a run that was already done.", inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
    Tool(name="list_recent_runs", description="List recent execution reports. With bot_id: that bot's execution history; without: the report list across bots. Start here to find the execution_id that get_execution_report needs.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}, "limit": {"type": "integer", "default": 10}}}),

    # Reporting
    Tool(name="get_execution_report", description="Screenshots ride in this report as screenshotUrl per iteration — there is no separate screenshots call. On by default for failed steps; use execute_bot screenshotAfterEachStep for passing ones. Full per-step pass/fail report for a FINISHED execution, by execution_id. This is 'what did the last run do' / 'why did it fail'. Siblings: get_execution_status for a run still in progress, get_performance_report for timing/ROI on this same execution.", inputSchema={"type": "object", "properties": {"execution_id": {"type": "string"}, "summary": {"type": "boolean", "description": "One row per script (status + first failing step). Use first on a multi-script run — the full report routinely exceeds the token cap"}, "failed_only": {"type": "boolean", "description": "Summary, failed scripts only"}}, "required": ["execution_id"]}),
    Tool(name="get_performance_report", description="Duration and ROI/time-saved metrics for an ordinary UI execution, by execution_id. Pass/fail detail is get_execution_report on the same id. Unrelated to get_performance_results, which polls a JMeter load test.", inputSchema={"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}),

    # Application context
    Tool(name="crawl_url", description="Crawl a live web application and capture real locators (XPath, CSS, aria-label) for test script generation. Run this whenever a test step needs a ui-locator for a page you haven't already captured locators for — never write a step against a hand-guessed selector (e.g. \"input[type='email']\") instead of calling this first.", inputSchema={"type": "object", "properties": {"url": {"type": "string"}, "credentials": {"type": "object", "properties": {"username": {"type": "string"}, "password": {"type": "string"}}}, "max_pages": {"type": "integer", "default": 20}}, "required": ["url"]}),
    Tool(
        name="extract_requirements",
        description=(
            "Parse a local requirements file (PDF/DOCX/XLSX/CSV/TXT/MD) into structured text or rows, "
            "for generating Given/When/Then test cases. Does not generate test cases itself — "
            "returns extracted content for the caller to reason over."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the requirements file"},
            },
            "required": ["file_path"],
        },
    ),

    # API / Performance Testing (mtaf-core) — a separate flow from UI bot testing, for
    # REST/GraphQL request validation, chained-workflow testing, and JMeter-backed load testing.
    Tool(name="list_api_collections", description="List API test collections (Postman-style, for REST/GraphQL testing — separate from UI test scripts).", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_api_collection", description="Get full details of an API collection by ID.", inputSchema={"type": "object", "properties": {"collection_id": {"type": "string"}}, "required": ["collection_id"]}),
    Tool(name="create_api_collection", description="Create a new API collection to group related API requests.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "variables": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "initialValue": {"type": "string"}}}, "description": "Collection-scoped variables, e.g. base_url"}}, "required": ["name"]}),
    Tool(name="list_api_requests", description="List all saved API requests.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_api_request", description="Get full details of an API request by ID.", inputSchema={"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}),
    Tool(name="create_api_request", description="Create and save a new API request (REST/GraphQL).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]}, "url": {"type": "string", "description": "May contain {{varName:defaultValue}} placeholders"}, "collection_id": {"type": "string"}, "query_params": {"type": "array", "items": {"type": "object"}}, "header_params": {"type": "array", "items": {"type": "object"}}, "body_params": {"description": "Request body, shape depends on contentType"}}, "required": ["name", "method", "url"]}),
    Tool(name="test_api_request", description="Execute a saved (or ad-hoc) API request once and get the response, without needing a full workflow.", inputSchema={"type": "object", "properties": {"request": {"type": "object", "description": "Full ApiRequestV2 object: name, method, url, headerParams, queryParams, bodyParams, ..."}, "variables": {"type": "object", "description": "Pre-seeded {{var}} overrides, e.g. {\"token\": \"abc123\"}"}, "data_row": {"type": "object", "description": "Test-data row: column name -> value"}, "environment": {"type": "string", "description": "Name of the collection environment to activate"}}, "required": ["request"]}),
    Tool(name="import_curl", description="Import one or more curl commands as API requests (splits on lines starting with 'curl'). Set save=false to preview without persisting.", inputSchema={"type": "object", "properties": {"commands": {"type": "string"}, "save": {"type": "boolean", "default": True}, "collection_name": {"type": "string", "default": "cURL Import"}, "collection_id": {"type": "string", "description": "Add to an existing collection instead of creating a new one"}}, "required": ["commands"]}),
    Tool(name="import_postman_collection", description="Import a Postman Collection (v2.0/v2.1 JSON) as API requests. Set save=false to preview without persisting.", inputSchema={"type": "object", "properties": {"collection": {"type": "object", "description": "Raw Postman collection JSON"}, "save": {"type": "boolean", "default": True}}, "required": ["collection"]}),
    Tool(name="list_workflows", description="List chained API-request workflows.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_workflow", description="Get full details of a workflow by ID.", inputSchema={"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}),
    Tool(name="create_workflow", description="Create a chained-API-request workflow (multiple requests run in sequence, e.g. for a user journey).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "workflow_list": {"type": "array", "items": {"type": "object"}, "description": "Ordered list of chained requests — if the shape is unclear, use get_service_spec first"}}, "required": ["name"]}),
    Tool(name="test_workflow", description="Run a one-off chained API workflow test (does not need to be saved first).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "api_requests": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "method": {"type": "string"}, "name": {"type": "string"}, "url": {"type": "string"}, "headers": {"type": "object"}, "body": {}}}, "description": "Max 50 requests"}, "description": {"type": "string"}, "load_ratio": {"type": "number"}}, "required": ["name", "api_requests"]}),
    Tool(name="list_performance_bots", description="List JMeter-backed load/performance bots only. NOT the answer to 'show me my bots' — that is list_bots; these are a separate mtaf-core product surface and this list is empty in most projects.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_performance_bot", description="Get full details of a performance bot by ID.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}}, "required": ["bot_id"]}),
    Tool(name="run_performance_bot", description="Run an existing performance bot by ID. Runs async in the background (can take hours) — returns a metrics ID immediately for polling via get_performance_results.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}}, "required": ["bot_id"]}),
    Tool(name="stop_performance_bot", description="Stop a running performance bot.", inputSchema={"type": "object", "properties": {"bot_id": {"type": "string"}}, "required": ["bot_id"]}),
    Tool(name="get_performance_results", description="Poll a running JMeter LOAD test (throughput, response times, error rate) by the metrics ID from run_performance_bot. Not for UI executions — those are get_execution_report / get_performance_report, which take an execution_id.", inputSchema={"type": "object", "properties": {"metrics_id": {"type": "string"}, "polling": {"type": "boolean", "default": True}}, "required": ["metrics_id"]}),
    Tool(name="list_vault_secrets", description="List secrets in the API/performance-testing vault (mtaf-core), for API requests and workflows. Metadata only — never decrypted values. The vault for UI test-script credentials is a DIFFERENT store: list_config_vault_secrets.", inputSchema={"type": "object", "properties": {}}),

    # Local execution agent — for TestBots configured to run on the user's own machine
    # (gridUrlForExecution contains "localhost"), not the cloud grid.
    Tool(name="check_local_agent_status", description="Check whether the local execution agent is running on this machine. Call this before execute_bot if the bot's environment targets localhost.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_local_agents", description="List machines in this project that have the local execution agent installed/registered.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_fake_data_types", description="List available fake/synthetic test-data generator types (e.g. Email, SIN, Full Name) usable with generate_fake_data.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="generate_fake_data", description="Generate one synthetic value of a given fake-data type, for populating test data.", inputSchema={"type": "object", "properties": {"display_name": {"type": "string", "description": "Must be one of the names returned by list_fake_data_types"}}, "required": ["display_name"]}),

    # Email
    Tool(name="send_email", description="Send a transactional email through TestBots (e.g. a test run summary).", inputSchema={"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "message": {"type": "string"}, "multiple_tos": {"type": "array", "items": {"type": "string"}}, "from_address": {"type": "string"}}, "required": ["to", "subject", "message"]}),

    # Consumer-Driven Contract Testing (Pact) — niche capability for API contract verification
    Tool(name="list_consumers", description="List Pact contract-testing consumers (API clients).", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_consumer", description="Create a Pact consumer.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    Tool(name="list_providers", description="List Pact contract-testing providers (API servers).", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_provider", description="Create a Pact provider.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    Tool(name="list_contracts", description="List Pact contracts (consumer-provider agreements).", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_contract", description="Create a Pact contract between a consumer and provider for one API interaction.", inputSchema={"type": "object", "properties": {"consumer_id": {"type": "string"}, "provider_id": {"type": "string"}, "method": {"type": "string"}, "contract_description": {"type": "string"}, "request_body": {"type": "string"}, "response_body": {"type": "string"}}, "required": ["consumer_id", "provider_id", "method"]}),
    Tool(name="run_pact_tests", description="Run both consumer and provider Pact verification tests for a contract.", inputSchema={"type": "object", "properties": {"contract_id": {"type": "string"}}, "required": ["contract_id"]}),

    # Service Virtualization (WireMock-backed API mocking)
    Tool(name="list_mock_mappings", description="List service-virtualization mock API mappings, optionally filtered.", inputSchema={"type": "object", "properties": {"method": {"type": "string"}, "search": {"type": "string"}}}),
    Tool(name="get_mock_mapping", description="Get a mock mapping by ID.", inputSchema={"type": "object", "properties": {"mapping_id": {"type": "string"}}, "required": ["mapping_id"]}),
    Tool(name="get_mock_mapping_template", description="Get an example mock-mapping JSON template to use as a starting point for create_mock_mapping.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="create_mock_mapping", description="Create a mock API mapping (request matcher + canned response) for service virtualization. Call get_mock_mapping_template first if the shape is unclear.", inputSchema={"type": "object", "properties": {"mapping": {"type": "object", "description": "WireMock-style mapping: {request: {method, url, headers}, response: {status, body, headers}}"}}, "required": ["mapping"]}),
    Tool(name="delete_mock_mapping", description="Delete a mock API mapping by ID.", inputSchema={"type": "object", "properties": {"mapping_id": {"type": "string"}}, "required": ["mapping_id"]}),

    # Auto-discovery — future-proof API access
    Tool(
        name="get_service_spec",
        description=(
            "Fetch the full OpenAPI spec (all endpoints, schemas, params) for any TestBots service. "
            "Use this to discover new or unknown endpoints when the hand-written tools don't cover a feature. "
            f"Available services: {', '.join(sorted(set(SERVICE_MAP.keys())))}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "Service name e.g. 'ahq-asset-services' or short alias 'asset'",
                }
            },
            "required": ["service_name"],
        },
    ),
    Tool(
        name="call_api",
        description=(
            "Call ANY TestBots REST endpoint directly. Use after get_service_spec to invoke a discovered endpoint "
            "that is not covered by a hand-written tool. Supports GET, POST, PUT, DELETE."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name or alias"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                "path": {"type": "string", "description": "Endpoint path e.g. '/rest/api/websites'"},
                "body": {"type": "object", "description": "Request body for POST/PUT"},
                "params": {"type": "object", "description": "Query parameters"},
                "extra_headers": {"type": "object", "description": "Additional headers if required"},
            },
            "required": ["service", "method", "path"],
        },
    ),
]


def _requested_tool_profile() -> str | None:
    """
    Hosted: `?profile=core` on the MCP URL, or an X-AHQ-Tool-Profile header. The query parameter
    is the one to document — connector UIs (Claude, ChatGPT, Copilot Studio) take a URL and
    nothing else, so it is the only lever some clients have.

    Stdio: AHQ_MCP_TOOL_PROFILE, since there is no URL to hang a parameter off.

    Same request-context probe _resolve_clients uses; LookupError is the stdio case, where no
    Starlette request exists.
    """
    try:
        req = server.request_context.request
    except LookupError:
        req = None
    if req is None:
        return settings.ahq_mcp_tool_profile or None
    return req.query_params.get("profile") or req.headers.get("x-ahq-tool-profile")


@server.list_tools()
async def list_tools() -> list[Tool]:
    # Presentation, not authorization: _dispatch stays permissive, so a hidden tool still runs
    # if a client calls it anyway. The security boundary is the AHQ token, which the gateway
    # re-validates on every call — filtering here is about what the model has to read, and a
    # stale client that cached the full list must not start getting confusing failures.
    allowed = resolve_tool_names(_requested_tool_profile())
    if allowed is None:
        return TOOLS
    return [t for t in TOOLS if t.name in allowed]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _require_stdio_config() -> None:
    """
    Fail fast (per tool call) when stdio mode is missing its .env config. Without this, an empty
    token + empty/wrong base URL makes every tool "succeed" against the web frontend's HTML —
    a 10-minute misdiagnosis instead of a 10-second fix (hit live, first teammate install).

    base_url is checked on the RESOLVED credentials (DEFAULT_BUNDLE's, already built via
    AhqCredentials.from_settings) rather than the raw AHQ_BASE_URL env var directly — the token's
    own urlDetails.baseUrl claim now resolves base_url the same way hosted mode already did, so
    AHQ_BASE_URL in .env is only needed as a fallback for a token without that claim.
    """
    from src.config.ahq_services import REPO_ROOT

    resolved_base_url = DEFAULT_BUNDLE.asset._credentials.base_url
    missing = [k for k, v in (
        ("AHQ_BASE_URL", resolved_base_url),
        ("AHQ_API_TOKEN", settings.ahq_api_token),
        ("AHQ_PROJECT_ID", settings.ahq_project_id),
    ) if not v]
    if missing:
        from pathlib import Path

        stable = Path.home() / ".ahq" / ".env"
        raise RuntimeError(
            f"testbots-mcp-server is not configured: {', '.join(missing)} is empty. "
            f"Create {stable} (recommended — survives plugin upgrades) or {REPO_ROOT / '.env'} "
            f"(copy .env.example and fill in the values — see INSTALL.md), then run "
            f"/reload-plugins (or restart the session)."
        )


def _resolve_clients() -> tuple[ClientBundle, bool]:
    """
    stdio mode: no Starlette request in context -> DEFAULT_BUNDLE (credentials from .env, one
    per process, identical to today's behavior).
    HTTP mode (src/http_server.py): a Starlette Request is available per tool-call via
    server.request_context.request -> build a fresh per-request ClientBundle from that
    request's X-API-AUTH-KEY/org-id/projectId headers, sharing the one pooled httpx.AsyncClient
    set up by the HTTP entrypoint's lifespan.
    """
    try:
        req = server.request_context.request
    except LookupError:
        req = None
    if req is None:
        _require_stdio_config()
        return DEFAULT_BUNDLE, False
    # OAuth requests carry their credentials sealed inside the Bearer token; DualAuthMiddleware
    # verifies it and stashes the result in the ASGI scope. Legacy header clients fall through
    # to the original X-API-AUTH-KEY/projectId path.
    creds = req.scope.get("ahq_credentials")
    if creds is None:
        creds = AhqCredentials.from_headers(
            req.headers, base_url=settings.ahq_base_url, allowed_extra_base_urls=settings.extra_base_urls()
        )
        # BaseAhqClient sends projectId as a header on EVERY AHQ request, so an empty one is not
        # an org-wide fallback — the API answers 200 with an empty result set, which reads as
        # "you have no websites/scripts/bots" instead of "you are misconfigured". Clients that
        # can only send a single auth header (Microsoft Copilot Studio's API-key auth is one)
        # land here by construction, so fail loudly rather than silently answering nothing.
        # The OAuth path can't hit this: /consent always resolves a project before issuing.
        if not creds.project_id:
            raise RuntimeError(
                "testbots-mcp-server is not configured for this request: the 'projectId' header is "
                "missing. Header auth requires 'projectId' alongside either 'X-API-AUTH-KEY' or a "
                "user's 'Authorization: Bearer' JWT (with 'org-id'). If your client can only send "
                "one header, connect via OAuth instead — its consent page picks the project for "
                "you (see CONNECT.md)."
            )
    return ClientBundle.build(credentials=creds, http_client=app_http_client.client), True


# The login-shaped fields of ResponseObj (ahq-data-commons ResponseObj.java) that carry nothing
# on a mutation response. Everything NOT named here survives — see _slim_response_obj.
_RESPONSE_OBJ_NOISE = frozenset({
    "timestamp", "status", "firstName", "lastName", "userId", "email",
    "organizationId", "partnerId", "projectId", "userRole", "firstTimeLogin",
    "invited", "active", "ssoEnabled", "token", "story", "success",
})


def _slim_response_obj(resp):
    """
    AHQ mutation endpoints answer with a ~25-field ResponseObj/login-shaped envelope that is
    almost entirely nulls (firstName, ssoEnabled, token, story, ...) — pure token waste on every
    write. Detect that envelope (message set, user fields empty) and drop the dead half. Also
    fix the untrustworthy success flag: ResponseObj.success defaults to false and several
    handlers never set it, so a "Test script added successfully" arrives with success:false —
    derive it from the message/status instead.

    This subtracts the known-dead fields rather than keeping a fixed whitelist. A whitelist
    silently ate every key a client method had ADDED to the envelope on the way out: execute_bot
    attaches jobId and a next_step explaining that `id` is a job id and not an executionId, and
    both were stripped here, leaving exactly {id, message, success} and a caller polling the
    wrong id. Anything a client deliberately adds is by definition not envelope noise, so the
    default has to be to keep it.
    """
    if (
        isinstance(resp, dict)
        and "ssoEnabled" in resp
        and resp.get("message") is not None
        and resp.get("firstName") is None
    ):
        slim = {
            k: v for k, v in resp.items()
            if k not in _RESPONSE_OBJ_NOISE and v not in (None, "", [])
        }
        msg = str(resp.get("message", "")).lower()
        slim["success"] = bool(
            resp.get("success")
            or "success" in msg
            or "enqueued" in msg
            or resp.get("status") in (0, 200)
        )
        return slim
    return resp


async def _dispatch_hosted(name: str, arguments: dict, clients: ClientBundle):
    """
    Hosted-only wrapper: per-org rate limiting plus one audit line per tool call. Tool
    ARGUMENTS are deliberately never logged — they routinely contain credentials (script
    steps, vault payloads).
    """
    creds = clients.user._credentials
    org = creds.org_id or "unknown"
    if not _rate_limiter.allow(org):
        return {
            "error": f"Rate limit exceeded ({settings.ahq_mcp_rate_limit_per_min} calls/min "
                     f"per organization). Retry shortly."
        }
    started = time.monotonic()
    try:
        result = await _dispatch(name, arguments, clients, is_hosted=True)
    except Exception as e:
        audit_log("tool_call", org=org, project=creds.project_id, tool=name,
                  duration_ms=int((time.monotonic() - started) * 1000),
                  ok=False, error=_describe(e)[:200])
        raise
    error = result.get("error") if isinstance(result, dict) else None
    audit_log("tool_call", org=org, project=creds.project_id, tool=name,
              duration_ms=int((time.monotonic() - started) * 1000),
              ok=error is None, **({"error": str(error)[:200]} if error is not None else {}))
    return result


def _describe(exc: Exception) -> str:
    """Never lose the exception type. `str(httpx.ReadTimeout())` is the empty string."""
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        clients, is_hosted = _resolve_clients()
        if is_hosted:
            result = await _dispatch_hosted(name, arguments, clients)
        else:
            result = await _dispatch(name, arguments, clients, is_hosted)
        return [TextContent(type="text", text=json.dumps(_slim_response_obj(result), indent=2))]
    except Exception as e:
        # Type first: httpx's timeout exceptions carry no message at all, so a bare str(e) here
        # reported every timeout in the server as {"error": ""} -- no tool, no cause, nothing.
        return [TextContent(type="text", text=json.dumps({"error": _describe(e)}))]


@server.list_prompts()
async def list_prompts():
    return list_skill_prompts()


@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None):
    try:
        hosted = server.request_context.request is not None
    except LookupError:
        hosted = False
    return get_skill_prompt(name, hosted=hosted)


async def _default_run_name(clients: ClientBundle, bot_id: str) -> str:
    """A readable execution name, so a Claude-triggered run isn't a blank row in Test Reports."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        bot = await clients.test_mgmt.get_bot(bot_id)
        label = (bot or {}).get("name") if isinstance(bot, dict) else None
    except Exception:
        label = None
    return f"{label or 'TestBot'} - {stamp}"


async def _execution_id_for_job(clients: ClientBundle, job_id: str) -> str | None:
    """The executionId for a background job id, or None if it can't be resolved.

    Deliberately best-effort: this only runs after a direct lookup already failed, so a failure
    here must re-raise the original error rather than replace it with a confusing one about
    report listings.
    """
    try:
        runs = await clients.test_mgmt.list_recent_reports(limit=50)
    except Exception:
        return None
    for run in runs if isinstance(runs, list) else []:
        if isinstance(run, dict) and run.get("backgroundJobId") == job_id:
            return run.get("executionId") or run.get("_id") or run.get("id")
    return None


def _require_deletion_confirmation(entity_type: str, entity_id: str, entity_name: str, confirmed: bool) -> Optional[dict]:
    """
    Confirmation gate for destructive operations. Prevents accidental deletions of shared/permanent assets.

    Returns an error dict if confirmation is missing/wrong; None if confirmed.
    """
    if not confirmed:
        return {
            "status": "NEEDS_CONFIRMATION",
            "error": f"Deleting {entity_type} '{entity_name}' ({entity_id}) is permanent and cannot be undone.",
            "instructions": (
                f"This deletion affects everyone with access to this project. Pass confirmed=true "
                f"to proceed with deletion. Resend the same request with confirmed=true only after "
                f"confirming with the user that they actually want to delete this."
            ),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name,
        }
    return None


async def _dispatch(name: str, args: dict, clients: ClientBundle, is_hosted: bool = False):
    if is_hosted and name in _HOSTED_UNSUPPORTED:
        return {"error": f"{name} is not available over the hosted MCP server yet — run testbots-mcp-server locally via stdio for this tool."}

    if name in VALIDATORS:
        try:
            VALIDATORS[name](**args)
        except ValidationError as e:
            return {"error": format_validation_error(name, e)}

    # Context
    if name == "list_my_projects":
        # /users/me 500s for an ORGANIZATION token rather than returning an empty user, so the
        # "could not identify" guard below was unreachable in exactly the case it was written
        # for -- callers got a raw 500 instead. That is not an error worth surfacing: the tool
        # exists to disambiguate between several projects, and a token that names no user has
        # nothing to disambiguate.
        try:
            me = await clients.user.get_current_user() or {}
        except AhqApiError:
            me = {}
        user_id = me.get("userId") or me.get("id") or ""
        if not user_id:
            return {"projects": [], "note": (
                "This credential names no user, so there are no per-user projects to choose "
                "between — expected for an ORGANIZATION API token. The project in use is the "
                "one configured for this session; call get_context to see it.")}
        return {"projects": await clients.user.list_projects_for_user(user_id)}
    if name == "get_context":
        return await _get_context(clients)

    # Asset
    if name == "list_websites":
        return await clients.asset.list_websites()
    if name == "search_websites":
        return await clients.asset.search_websites(args["name"])
    if name == "create_website":
        return await clients.asset.create_website(args["name"], args["url"])
    if name == "list_pages":
        return await clients.asset.list_pages(args["website_id"])
    if name == "create_page":
        return await clients.asset.create_page(args["website_id"], args["name"], args["url"])
    if name == "get_page_by_url":
        return await clients.asset.get_page_by_url(args["website_id"], args["url"])
    if name == "add_locators":
        return await clients.asset.add_locators(
            args["page_id"], args["website_id"], args["page_url"], args.get("page_name", ""), args["locators"]
        )
    if name == "update_locator":
        return await clients.asset.update_locator(
            args["website_id"], args["page_id"], args["locator_id"],
            args["locator_name"], args["locator_type"], args["locate_by"], args["locator_value"],
        )
    if name == "scan_broken_locators":
        return await clients.asset.list_broken_locators()
    if name == "find_locator_usage":
        return await _find_locator_usage(
            clients, args["locator_id"],
            include_common_functions=args.get("include_common_functions", True))
    if name == "heal_locator":
        return await _heal_locator(
            clients.asset, args["locator_id"], args["website_id"],
            credentials=args.get("credentials"), hosted=is_hosted,
        )
    if name == "apply_locator_fix":
        return await clients.asset.apply_locator_strategy(
            args["website_id"], args["locator_id"], args["chosen_strategy"],
        )

    # Test scripts
    if name == "list_test_scripts":
        return await clients.test_mgmt.list_test_scripts(args.get("name"))
    if name == "get_test_script":
        script = await clients.test_mgmt.get_test_script(args["script_id"])
        return _paginate_script(
            script,
            steps_from=args.get("steps_from"),
            steps_to=args.get("steps_to"),
            summary=args.get("summary", False),
        )
    if name == "delete_test_script":
        return await clients.test_mgmt.delete_test_script(
            args["script_id"], args.get("confirmed", False)
        )
    if name in ("add_test_steps", "update_test_script", "delete_test_steps",
                "reorder_test_steps") and not args.get("branch_name"):
        # Omitting it lands the edit on whatever branch the token is ambiently pointed at: the PUT
        # is a full-document write and the GET's currentBranchName is the request's branch, not the
        # script's (see TestMgmtClient._put_script). The edit still reports success, so the damage
        # only shows at commit time -- commitAll snapshots the TARGET branch's stale head and pins
        # a version older than the work just done, under a 200.
        try:
            current = await clients.test_mgmt.get_test_script(args["script_id"])
            seen = current.get("currentBranchName") or "unknown"
        except Exception:
            seen = "could not read it"
        return {"error": (
            "branch_name is required — pass the branch this script actually lives on. Confirm it "
            f"with get_scripts_for_branch rather than trusting currentBranchName (currently: {seen}), "
            "which reflects this request's ambient branch. Omitting it silently moves the edit to "
            "another branch and a later commit_branch will appear to revert your work."
        )}

    if name == "add_test_steps":
        return await clients.test_mgmt.add_test_steps(
            args["script_id"], args["steps"], args.get("position"),
            branch_name=args.get("branch_name"),
            expected_version=args.get("expected_version"))
    if name == "delete_test_steps":
        return await clients.test_mgmt.delete_test_steps(
            args["script_id"],
            sequences=args.get("sequences"),
            step_ids=args.get("step_ids"),
            branch_name=args.get("branch_name"),
            expected_version=args.get("expected_version"))
    if name == "reorder_test_steps":
        return await clients.test_mgmt.reorder_test_steps(
            args["script_id"], args["order"],
            branch_name=args.get("branch_name"),
            expected_version=args.get("expected_version"))
    if name == "update_test_script":
        # branch_name is a sibling of `changes`, not one of the entity fields inside it — it
        # selects WHERE the edit lands rather than what the document says.
        return await clients.test_mgmt.update_test_script(
            args["script_id"], branch_name=args.get("branch_name"),
            expected_version=args.get("expected_version"), **args["changes"])
    if name == "create_test_script":
        # Schema `required` is advisory — not every MCP client enforces it — and this is the one
        # argument whose wrong value fails silently much later: a script created on protected
        # `main` cannot be committed (403), so edits stay uncommitted and execute_bot keeps
        # running the last committed version. Refusing here turns that into a question the user
        # actually gets asked, which no amount of description text reliably achieved.
        if not args.get("branch_name"):
            try:
                branches = [b.get("branchName") for b in await clients.test_mgmt.list_branches()]
                known = ", ".join(b for b in branches if b) or "none found"
            except Exception:
                known = "could not list them"
            return {"error": (
                "branch_name is required — ask the user which branch this script should live on "
                "before creating it, and offer creating a new one alongside the existing ones. "
                f"Existing branches: {known}. Do not assume 'main': it is protected, so a later "
                "commit_branch returns 403 and the script never executes what you edited."
            )}

        kwargs = {}
        if "status" in args:
            kwargs["status"] = args["status"]
        if "script_type" in args:
            kwargs["script_type"] = args["script_type"]
        if "branch_name" in args:
            kwargs["branch_name"] = args["branch_name"]
        if "repair_comment" in args:
            kwargs["repair_comment"] = args["repair_comment"]
        return await clients.test_mgmt.create_test_script(
            args["name"], args["steps"], args.get("page_id"), args.get("website_id"), args.get("story_id"), **kwargs
        )

    if name == "list_step_templates":
        return await clients.test_mgmt.list_templates(args.get("offset", 0))
    if name == "search_step_templates":
        return await clients.test_mgmt.search_templates(args["title"])
    if name == "get_step_template":
        return await clients.test_mgmt.get_template(args["template_id"])

    # Recorded scripts
    if name == "list_recorded_scripts":
        return await clients.test_mgmt.list_recorded_scripts(args.get("name"), args.get("branch_name"))
    if name == "get_recorded_script":
        return await clients.test_mgmt.get_recorded_script(args["recorded_script_id"])
    if name == "promote_recorded_script":
        return await clients.test_mgmt.promote_recorded_script(
            args["recorded_script_id"],
            args["story_id"],
            name=args.get("name"),
            website_id=args.get("website_id"),
            status=args.get("status"),
            description=args.get("description"),
            branch_name=args.get("branch_name", "main"),
        )

    # Common Functions (User Test Steps)
    if name == "list_common_functions":
        return await clients.asset.list_common_functions(args.get("name"))
    if name == "get_common_function":
        return await clients.asset.get_common_function(args["common_function_id"])
    if name == "create_common_function":
        return await clients.asset.create_common_function(
            args["name"], args["website_id"], args["status"], args["return_type"],
            steps=args.get("steps"), parameters=args.get("parameters"),
            description=args.get("description"),
        )
    if name == "update_common_function":
        # Tool args are snake_case; the CommonFunction document is camelCase. Only fields the
        # caller actually sent become part of the merge — everything else survives via the
        # client's GET-merge-PUT.
        field_map = {
            "name": "name", "description": "description", "status": "status",
            "website_id": "websiteId", "steps": "testSteps",
            "parameters": "parameters", "return_type": "returnType",
        }
        changes = {doc_key: args[arg_key] for arg_key, doc_key in field_map.items() if arg_key in args}
        if not changes:
            return {"error": "update_common_function needs at least one field to change (name, description, status, website_id, steps, parameters, return_type)"}
        return await clients.asset.update_common_function(args["common_function_id"], **changes)

    # Organization
    if name == "list_epics":
        return await clients.test_mgmt.list_epics()
    if name == "create_epic":
        clash = await _existing_match(clients.test_mgmt.list_epics, args["name"], args.get("confirmed"))
        if clash:
            return clash
        return await clients.test_mgmt.create_epic(args["name"])
    if name == "list_stories":
        return await clients.test_mgmt.list_stories(args["epic_id"])
    if name == "create_story":
        clash = await _existing_match(
            lambda: clients.test_mgmt.list_stories(args["epic_id"]), args["name"], args.get("confirmed"))
        if clash:
            return clash
        return await clients.test_mgmt.create_story(args["epic_id"], args["name"])
    if name == "list_bots":
        return await clients.test_mgmt.list_bots(args.get("name"))
    if name == "list_suites":
        return await clients.test_mgmt.list_suites()
    if name == "list_environments":
        return await clients.config.list_environments()
    if name == "create_suite":
        scripts = None
        if args.get("script_ids"):
            scripts = []
            for i, sid in enumerate(args["script_ids"], start=1):
                script = await clients.test_mgmt.get_test_script(sid)
                scripts.append({"testScriptId": sid, "name": script.get("name", ""),
                                "status": script.get("status"), "selected": True, "sequence": i})
        return await clients.test_mgmt.create_suite(args["name"], scripts)
    if name == "add_scripts_to_suite":
        return await clients.test_mgmt.add_scripts_to_suite(args["suite_id"], args["script_ids"])
    if name == "remove_scripts_from_suite":
        return await clients.test_mgmt.remove_scripts_from_suite(args["suite_id"], args["script_ids"])

    # Version Control
    if name == "list_branches":
        return await clients.test_mgmt.list_branches(args.get("query"))
    if name == "get_scripts_for_branch":
        return await clients.test_mgmt.get_scripts_for_branch(args["branch_name"])
    if name == "delete_branch":
        # Confirmation gate: deleting a branch is permanent and affects all scripts on it
        confirmation_error = _require_deletion_confirmation(
            "branch", args["branch_name"], args["branch_name"], args.get("confirmed", False)
        )
        if confirmation_error:
            return confirmation_error
        return await clients.test_mgmt.delete_branch(args["branch_name"])
    if name == "create_branch":
        return await clients.test_mgmt.create_branch(
            args["branch_name"],
            from_branch=args.get("from_branch", "main"),
            strategy=args.get("strategy"),
            confirmed=args.get("confirmed", False),
            script_ids=args.get("script_ids"),
            is_protected=args.get("is_protected", False),
        )
    if name == "commit_branch":
        return await clients.test_mgmt.commit_branch(args["branch_name"], args["message"], args.get("tag"))
    if name == "list_commits":
        return await clients.test_mgmt.list_commits(args["branch_name"], args.get("page", 0), args.get("size", 20))
    if name == "create_pull_request":
        return await clients.test_mgmt.create_pull_request(
            args["source_branch"], args["target_branch"], args["title"],
            description=args.get("description"),
            reviewer_ids=args.get("reviewer_ids"),
            script_ids=args.get("script_ids"),
            delete_source_branch_after_merge=args.get("delete_source_branch_after_merge", False),
        )
    if name == "list_pull_requests":
        return await clients.test_mgmt.list_pull_requests(
            args.get("status"), args.get("query"), args.get("page", 0), args.get("size", 20)
        )
    if name == "get_pull_request":
        return await clients.test_mgmt.get_pull_request(args["pr_id"])
    if name == "get_pull_request_diff":
        return await clients.test_mgmt.get_pull_request_diff(args["pr_id"], args.get("script_id"))
    if name == "approve_pull_request":
        return await clients.test_mgmt.approve_pull_request(args["pr_id"])
    if name == "request_pr_changes":
        return await clients.test_mgmt.request_pr_changes(args["pr_id"], args.get("comment"))
    if name == "merge_pull_request":
        return await clients.test_mgmt.merge_pull_request(args["pr_id"])
    if name == "close_pull_request":
        return await clients.test_mgmt.close_pull_request(args["pr_id"])

    # Project Roles
    if name == "list_project_roles":
        return await clients.test_mgmt.list_project_roles()
    if name == "create_project_role":
        return await clients.test_mgmt.create_project_role(
            args["role_name"], args["permissions"], args.get("is_default", False)
        )
    if name == "update_project_role_permissions":
        return await clients.test_mgmt.update_project_role_permissions(args["role_id"], args["permissions"])
    if name == "delete_project_role":
        return await clients.test_mgmt.delete_project_role(args["role_id"])
    if name == "assign_project_role":
        return await clients.test_mgmt.assign_project_role(args["role_id"], args["user_id"])
    if name == "list_project_members":
        return await clients.test_mgmt.list_project_members()

    # Archive Manager
    if name == "list_archived_assets":
        if args["entity_type"] == "recorded_script":
            return await clients.test_mgmt.list_archived_recorded_scripts(
                args.get("search"), args.get("page", 0), args.get("size", 50)
            )
        return await clients.user.list_archived(
            args["entity_type"], args.get("search"), args.get("page", 0), args.get("size", 50)
        )
    if name == "restore_asset":
        if args["entity_type"] == "recorded_script":
            return await clients.test_mgmt.restore_recorded_script(args["asset_id"])
        return await clients.user.restore_archived(args["entity_type"], args["asset_id"])
    if name == "permanently_delete_asset":
        # Confirmation gate: permanent deletion is irreversible
        confirmation_error = _require_deletion_confirmation(
            args["entity_type"], args["asset_id"], args.get("asset_name", args["asset_id"]), args.get("confirmed", False)
        )
        if confirmation_error:
            return confirmation_error
        if args["entity_type"] == "recorded_script":
            return await clients.test_mgmt.permanently_delete_recorded_script(args["asset_id"])
        return await clients.user.permanently_delete_archived(args["entity_type"], args["asset_id"])

    # Tunnel
    if name == "get_tunnel_status":
        return await clients.tunnel.get_tunnel_status()
    if name == "start_tunnel":
        return await clients.tunnel.start_tunnel()
    if name == "stop_tunnel":
        return await clients.tunnel.stop_tunnel()
    if name == "execute_tunnel_command":
        return await clients.tunnel.execute_tunnel_command(args["command"])

    # Global Parameters
    if name == "list_global_parameters":
        return await clients.config.list_global_parameters()
    if name == "search_global_parameters":
        return await clients.config.search_global_parameters(args.get("name"))
    if name == "add_global_parameter":
        return await clients.config.add_global_parameter(args["name"], args["value"], args.get("description"))
    if name == "check_global_parameter_usage":
        return await clients.config.check_global_parameter_usage(args["custom_property_id"])
    if name == "flatten_and_delete_global_parameter":
        return await clients.config.flatten_and_delete_global_parameter(args["custom_property_id"])

    # Vault (config-services)
    if name == "list_config_vault_secrets":
        return await clients.config.list_config_vault_secrets()
    if name == "get_config_vault_secret":
        return await clients.config.get_config_vault_secret(args["secret_id"])
    if name == "create_config_vault_secret":
        return await clients.config.create_config_vault_secret(args["name"], args["value"], args.get("description"))
    if name == "update_config_vault_secret":
        return await clients.config.update_config_vault_secret(args["secret_id"], args.get("value"), args.get("description"))
    if name == "delete_config_vault_secret":
        return await clients.config.delete_config_vault_secret(args["secret_id"])

    # Execution
    if name == "get_bot_notifications":
        return await clients.test_mgmt.get_bot_report_config(args["bot_id"])
    if name == "configure_bot_notifications":
        return await clients.test_mgmt.save_bot_report_config(
            args["bot_id"],
            recipients=args.get("recipients"),
            notify_on_pass=args.get("notify_on_pass"),
            notify_on_fail=args.get("notify_on_fail"),
            attach_pdf=args.get("attach_pdf"),
            template_type=args.get("template_type"),
            schedule_on_completion=args.get("schedule_on_completion"),
        )
    if name == "create_test_bot":
        return await clients.test_mgmt.create_test_bot(
            args["name"], args["test_suites"],
            description=args.get("description", ""),
            bot_type=args.get("bot_type"),
            folder_id=args.get("folder_id"),
            profile_id=args.get("profile_id"),
            number_of_retries=args.get("number_of_retries", 0),
        )
    if name == "list_bot_types":
        return await clients.test_mgmt.list_bot_types()
    if name == "list_grids":
        return await clients.config.list_grids()
    if name == "create_environment":
        return await clients.config.create_environment(
            args["name"], args["url"], args.get("env_type", "Web"), args.get("description", ""))
    if name == "get_grid_capabilities":
        return await clients.config.get_grid_capabilities(
            args["grid_id"], args.get("testing_type", "Web"), args.get("browser"))
    if name == "list_browsers":
        return await clients.config.list_browsers()
    if name == "list_execution_types":
        return await clients.config.list_execution_types()
    if name == "execute_bot":
        # Correct entry point: executor-services validates bot, fan-outs, then calls background-v2 internally.
        # Rebuild execution_configuration from the validated model (not the raw args dict) so its
        # declared defaults (timeout=60, waitForElementTimeout=30, closeBrowserAfterEachExecution=True,
        # type="Web", targetBranchName="main") actually reach the API. The VALIDATORS check above
        # only used this model to raise on bad input and threw it away — any field the caller
        # omitted stayed absent from the outgoing JSON, and the backend fills an absent field with
        # 0/false/blank rather than a working value (confirmed live: an omitted timeout/
        # waitForElementTimeout produced a 0-second timeout, dooming the run).
        execution_configuration = RunExecutionConfiguration(**args["execution_configuration"]).model_dump(exclude_none=True)
        # Every id below is accepted by the API and only fails once a browser session is already
        # being set up, so a doomed run costs the same 2-6 minutes a real one does. Check first.
        preflight = await _preflight_execution_configuration(clients, execution_configuration)
        if preflight:
            return preflight
        # The UI's run dialog always sends a display name; this tool left it optional and the
        # caller rarely passes one, so Claude-triggered runs showed up unnamed in Test
        # Reports and could not be told apart. The bot name plus a timestamp is what the
        # dialog effectively produces, and it beats a blank row.
        if not args.get("name"):
            args["name"] = await _default_run_name(clients, args["bot_id"])
        execution_configuration = await _fill_custom_properties(clients, execution_configuration)
        is_local = await _is_local_grid(clients, execution_configuration["gridId"])
        execution_configuration = _fill_resolution_default(execution_configuration, is_local)
        if is_local:
            if is_hosted:
                # The hosted server runs as a shared cloud pod — "localhost:9202" from there is
                # the pod's own loopback, not the caller's laptop. There is no reverse channel
                # (confirmed: ahq-standalone-local-v2-services' agent registry is a one-way
                # heartbeat the agent pushes up, not a command channel the cloud can push down),
                # so this can never work over a hosted connector by design, not just for now.
                return {
                    "error": (
                        "This TestBot's grid runs on your own machine's local agent "
                        "(localhost:9202), which the hosted MCP server can't reach — only a "
                        "stdio connection running on that same machine can. Use the testbots-skills "
                        "Claude Code plugin (stdio) for local-grid execution, or point this bot "
                        "at a cloud grid instead."
                    )
                }
            # Local grid: POST directly to this machine's own agent (localhost:9202), bypassing
            # the cloud entirely — see _is_local_grid's docstring. profile_id/partial_execution
            # are cloud-executor-only concepts (never seen in the browser's direct-to-agent
            # request) and are not supported on this path.
            return await clients.local_exec.execute_bot_locally(
                args["bot_id"], execution_configuration, name=args.get("name"),
            )
        return await clients.executor.execute_bot(
            args["bot_id"], execution_configuration,
            name=args.get("name"), profile_id=args.get("profile_id"),
            partial_execution=args.get("partial_execution", False),
        )
    if name == "get_execution_status":
        status = await clients.executor.get_bot_execution_status(args["execution_id"])
        # The lightweight status endpoint reports UNKNOWN once the run leaves the queue (and for
        # finished runs) — confirmed live. Resolve the real overall status from the detailed
        # report so callers aren't forced to know about the second endpoint.
        if isinstance(status, dict) and status.get("status") in (None, "UNKNOWN"):
            try:
                detailed = await clients.executor.get_execution_results(args["execution_id"])
                if isinstance(detailed, dict) and detailed.get("status"):
                    status["status"] = detailed["status"]
                    status["statusSource"] = "detailed-results"
            except Exception:
                pass  # keep the lightweight answer — a poll must not fail because the report isn't ready
        return status

    # Script inspection & scoped execution (from retrospective improvements)
    if name == "inspect_script":
        return await _inspect_script(clients, args["script_id"])
    if name == "validate_script_locators":
        return await _validate_script_locators(clients, args["script_id"])
    if name == "check_script_for_credential_exposure":
        return await _check_script_for_credential_exposure(clients, args["script_id"])
    if name == "execute_script_only":
        return await _execute_script_only(clients, args["script_id"], args["execution_configuration"], args.get("confirm_script_id"))

    if name == "schedule_bot_recurring":
        # Real scheduler (test-management-services' /rest/api/schedulers) — NOT
        # background-v2-services' schedule-recurring endpoint this tool used before, which
        # writes to a different, UI-invisible mechanism (confirmed live 2026-07-15: the
        # equivalent one-time endpoint reports success and shows a PENDING job that never
        # actually runs, and disappears from status lookup). Same defaulting fix as execute_bot
        # for the nested execution_configuration (see its comment).
        execution_configuration = RunExecutionConfiguration(**args["execution_configuration"]).model_dump(exclude_none=True)
        # Worth even more here than on execute_bot: a schedule with a dead grid/environment id
        # fails on its own, unattended, every time it fires.
        preflight = await _preflight_execution_configuration(clients, execution_configuration)
        if preflight:
            return preflight
        execution_configuration = await _fill_custom_properties(clients, execution_configuration)
        execution_configuration = _fill_resolution_default(
            execution_configuration, await _is_local_grid(clients, execution_configuration["gridId"]))
        return await clients.test_mgmt.create_scheduler(
            args["bot_id"], args["name"], args.get("emails") or [], args["cron"], execution_configuration,
        )
    if name == "cancel_schedule":
        return await clients.test_mgmt.delete_scheduler(args["schedule_id"])
    if name == "update_schedule":
        execution_configuration = None
        if args.get("execution_configuration"):
            execution_configuration = RunExecutionConfiguration(**args["execution_configuration"]).model_dump(exclude_none=True)
            preflight = await _preflight_execution_configuration(clients, execution_configuration)
            if preflight:
                return preflight
            execution_configuration = await _fill_custom_properties(clients, execution_configuration)
            execution_configuration = _fill_resolution_default(
                execution_configuration, await _is_local_grid(clients, execution_configuration["gridId"]))
        return await clients.test_mgmt.update_scheduler(
            args["schedule_id"], args.get("bot_id"), args.get("name"), args.get("emails"),
            args.get("cron"), execution_configuration,
        )
    if name == "toggle_schedule":
        return await clients.test_mgmt.toggle_scheduler(args["schedule_id"])
    if name == "list_schedulers":
        return await clients.test_mgmt.list_schedulers(args.get("bot_id"), args.get("offset", 0), args.get("size", 100))
    if name == "list_scheduler_recipient_emails":
        return await clients.test_mgmt.list_scheduler_recipient_emails()
    if name == "convert_text_to_cron":
        return await clients.test_mgmt.convert_text_to_cron(args["text"])
    if name == "list_recent_runs":
        return await clients.test_mgmt.list_recent_reports(args.get("bot_id"), args.get("limit", 10))

    # Reporting
    if name == "get_job_status":
        status = await clients.background.get_job_status(args["job_id"])
        # This endpoint keeps saying PROCESSING well after a run has finished — observed
        # reporting it ~11 minutes past a 45-second run, with chromedriver already exited. The
        # docs correctly tell callers to poll it, so the staleness converts directly into
        # waiting for nothing. An execution record carrying this job id is proof the run
        # reached the executor, and its own status is the one that moved.
        if isinstance(status, dict) and status.get("status") not in {
                "SUCCEEDED", "FAILED", "DELETED"}:
            execution_id = await _execution_id_for_job(clients, args["job_id"])
            if execution_id:
                status = {**status, "executionId": execution_id,
                          "note": ("An execution record exists for this job, so the run has "
                                   "started and this dispatch status may be stale. Call "
                                   "get_execution_report(executionId) for the real state.")}
        return status
    if name == "get_execution_report":
        # NOTE: this used to call clients.background.get_execution_report, a method that never
        # existed on BackgroundClient (only get_job_status/get_queue_status/list_recent_runs/
        # schedule_*/cancel_schedule do) — every call threw AttributeError. get_execution_results
        # on ExecutorClient (previously unwired to any Tool) is the real pass/fail report endpoint.
        # execute_bot hands back a JOB id and nothing else, while this endpoint wants an
        # EXECUTION id. Callers were told to run list_recent_runs and match on name and
        # timestamp, which is guesswork that picks the wrong run as soon as two are in flight.
        # Executions carry backgroundJobId, so the mapping is a lookup, not a guess.
        execution_id = args["execution_id"]
        try:
            report = await clients.executor.get_execution_results(execution_id)
        except AhqApiError:
            resolved = await _execution_id_for_job(clients, execution_id)
            if not resolved:
                raise
            report = await clients.executor.get_execution_results(resolved)
            report = {**report, "resolvedFromJobId": execution_id,
                      "executionId": resolved} if isinstance(report, dict) else report
        if args.get("summary") or args.get("failed_only"):
            return _summarize_report(report, failed_only=args.get("failed_only", False))
        return report
    if name == "get_performance_report":
        return await clients.executor.get_performance_report(args["execution_id"])

    # Application context
    if name == "crawl_url":
        return await _crawl_url(
            url=args["url"],
            credentials=args.get("credentials"),
            max_pages=args.get("max_pages", 20),
            hosted=is_hosted,
        )
    if name == "extract_requirements":
        return _extract_requirements(args["file_path"])

    # API / Performance Testing (mtaf-core)
    if name == "list_api_collections":
        return await clients.managed_testing.list_api_collections()
    if name == "get_api_collection":
        return await clients.managed_testing.get_api_collection(args["collection_id"])
    if name == "create_api_collection":
        return await clients.managed_testing.create_api_collection(args["name"], args.get("description"), args.get("variables"))
    if name == "list_api_requests":
        return await clients.managed_testing.list_api_requests()
    if name == "get_api_request":
        return await clients.managed_testing.get_api_request(args["request_id"])
    if name == "create_api_request":
        return await clients.managed_testing.create_api_request(
            args["name"], args["method"], args["url"], args.get("collection_id"),
            args.get("query_params"), args.get("header_params"), args.get("body_params"),
        )
    if name == "test_api_request":
        return await clients.managed_testing.test_api_request(
            args["request"], args.get("variables"), args.get("data_row"), args.get("environment")
        )
    if name == "import_curl":
        return await clients.managed_testing.import_curl(
            args["commands"], args.get("save", True), args.get("collection_name", "cURL Import"), args.get("collection_id")
        )
    if name == "import_postman_collection":
        return await clients.managed_testing.import_postman(args["collection"], args.get("save", True))
    if name == "list_workflows":
        return await clients.managed_testing.list_workflows()
    if name == "get_workflow":
        return await clients.managed_testing.get_workflow(args["workflow_id"])
    if name == "create_workflow":
        return await clients.managed_testing.create_workflow(args["name"], args.get("description"), args.get("workflow_list"))
    if name == "test_workflow":
        return await clients.managed_testing.test_workflow(
            args["name"], args["api_requests"], args.get("description"), args.get("load_ratio")
        )
    if name == "list_performance_bots":
        return await clients.managed_testing.list_performance_bots()
    if name == "get_performance_bot":
        return await clients.managed_testing.get_performance_bot(args["bot_id"])
    if name == "run_performance_bot":
        return await clients.managed_testing.run_performance_bot(args["bot_id"])
    if name == "stop_performance_bot":
        return await clients.managed_testing.stop_performance_bot(args["bot_id"])
    if name == "get_performance_results":
        return await clients.managed_testing.get_performance_results(args["metrics_id"], args.get("polling", True))
    if name == "list_vault_secrets":
        return await clients.managed_testing.list_vault_secrets()

    # Local execution agent
    if name == "check_local_agent_status":
        return await clients.local_exec.get_agent_status()
    if name == "list_local_agents":
        return await clients.local_exec.list_registered_agents()
    if name == "list_fake_data_types":
        return await clients.local_exec.list_fake_data_types()
    if name == "generate_fake_data":
        return await clients.local_exec.generate_fake_data(args["display_name"])

    # Email
    if name == "send_email":
        return await clients.email.send_email(
            args["to"], args["subject"], args["message"], args.get("multiple_tos"), args.get("from_address")
        )

    # Pact contract testing
    if name == "list_consumers":
        return await clients.cdct.list_consumers()
    if name == "create_consumer":
        return await clients.cdct.create_consumer(args["name"])
    if name == "list_providers":
        return await clients.cdct.list_providers()
    if name == "create_provider":
        return await clients.cdct.create_provider(args["name"])
    if name == "list_contracts":
        return await clients.cdct.list_contracts()
    if name == "create_contract":
        return await clients.cdct.create_contract(
            args["consumer_id"], args["provider_id"], args["method"],
            args.get("contract_description"), args.get("request_body"), args.get("response_body"),
        )
    if name == "run_pact_tests":
        return await clients.cdct.run_both_tests(args["contract_id"])

    # Service Virtualization
    if name == "list_mock_mappings":
        return await clients.virtualization.list_mock_mappings(args.get("method"), args.get("search"))
    if name == "get_mock_mapping":
        return await clients.virtualization.get_mock_mapping(args["mapping_id"])
    if name == "get_mock_mapping_template":
        return await clients.virtualization.get_mock_mapping_template()
    if name == "create_mock_mapping":
        return await clients.virtualization.create_mock_mapping(args["mapping"])
    if name == "delete_mock_mapping":
        return await clients.virtualization.delete_mock_mapping(args["mapping_id"])

    # Auto-discovery
    if name == "get_service_spec":
        return await clients.generic.get_service_spec(args["service_name"])
    if name == "call_api":
        return await clients.generic.call_api(
            service=args["service"],
            method=args["method"],
            path=args["path"],
            body=args.get("body"),
            params=args.get("params"),
            extra_headers=args.get("extra_headers"),
        )

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _check_project_in_org() -> str | None:
    """
    Slice 9k: the org is derived from the token (can't drift), but AHQ_PROJECT_ID is configured
    independently — a token from org A plus a project id from org B passes every local check and
    then silently reads/writes empty or wrong-org data on every call (hit live 2026-07-13).
    Returns a loud message when the configured project is not among the token org's projects,
    None when it is. Network/API failure downgrades to a warning message prefixed WARNING —
    never blocks serving.
    """
    try:
        projects = await DEFAULT_BUNDLE.user.list_projects()
    except Exception as e:
        return f"WARNING: project↔org check skipped ({e})"

    # Real documents use `_id` + `projectName` (confirmed live 2026-07-14); other spellings
    # kept defensively.
    def _pid(p):
        return p.get("projectId") or p.get("id") or p.get("_id")

    ids = {str(_pid(p)) for p in projects if isinstance(p, dict) and _pid(p)}
    if settings.ahq_project_id in ids:
        return None
    listing = ", ".join(
        f"{p.get('name') or p.get('projectName', '?')}={_pid(p)}"
        for p in projects[:10] if isinstance(p, dict)
    ) or "none visible to this token"
    return (
        f"ERROR: AHQ_PROJECT_ID '{settings.ahq_project_id}' does not belong to this token's "
        f"organization — every tool would read/write the wrong place. Update ~/.ahq/.env with "
        f"one of this org's projects: {listing}"
    )


async def main():
    try:
        _require_stdio_config()
    except RuntimeError as e:
        # Keep serving (so tool calls return the same actionable error instead of the client
        # showing an opaque "server failed to start"), but say it loudly once up front.
        print(f"[testbots-mcp-server] NOT CONFIGURED: {e}", file=sys.stderr)
    else:
        print(f"[testbots-mcp-server] base_url={DEFAULT_BUNDLE.asset._credentials.base_url} "
              f"project={settings.ahq_project_id}", file=sys.stderr)
        try:
            me = await DEFAULT_BUNDLE.asset.validate_token()
            name = me.get("name") or me.get("userId", "unknown")
            print(f"[testbots-mcp-server] Connected as: {name}", file=sys.stderr)
        except Exception as e:
            print(f"[testbots-mcp-server] WARNING: Token validation failed: {e}", file=sys.stderr)
        mismatch = await _check_project_in_org()
        if mismatch:
            print(f"[testbots-mcp-server] {mismatch}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
